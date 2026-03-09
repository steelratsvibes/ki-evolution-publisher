#!/usr/bin/env python3
"""LinkedIn Comment Agent (UC-029)

Architecture:
- Posts + Comments: Apify scrapers (no r_member_social needed)
- Reply posting: LinkedIn v2 API (w_member_social)
- Reply generation: Claude CLI
- Approval: Email to kandelhard@gmail.com

Apify Actors:
- harvestapi/linkedin-profile-posts (get recent posts)
- harvestapi/linkedin-post-comments (get comments per post)

Modes:
- python comment_agent.py              — fetch comments + generate drafts + send approval email
- python comment_agent.py --check-approvals  — check inbox for approval responses + post approved replies
"""

import os
import sys
import re
import json
import time
import sqlite3
import logging
import smtplib
import imaplib
import email as email_module
import subprocess
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from datetime import datetime, timedelta
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from apify_client import ApifyClient
from openai import OpenAI

# --- Config ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

DB_PATH = os.path.join(BASE_DIR, "comments.db")
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "agent.log")),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# LinkedIn API (for posting comments)
LI_TOKEN = os.getenv("LI_ACCESS_TOKEN")
LI_PERSON_URN = os.getenv("LI_PERSON_URN")
LI_HEADERS = {
    "Authorization": f"Bearer {LI_TOKEN}",
    "X-Restli-Protocol-Version": "2.0.0",
    "Content-Type": "application/json",
}

# Apify
APIFY_TOKEN = os.getenv("APIFY_TOKEN")
LINKEDIN_PROFILE_URL = os.getenv(
    "LINKEDIN_PROFILE_URL",
    "https://www.linkedin.com/in/ronaldkandelhard"
)

# SMTP
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_SENDER = os.getenv("SMTP_SENDER")
APPROVAL_TO = os.getenv("APPROVAL_TO")

# IMAP (same credentials as SMTP)
IMAP_HOST = os.getenv("IMAP_HOST", SMTP_HOST)
IMAP_PORT = int(os.getenv("IMAP_PORT", 993))
IMAP_USER = os.getenv("IMAP_USER", SMTP_USER)
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", SMTP_PASSWORD)

# OpenAI (for reply generation)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Limits
MAX_POSTS = 5
APIFY_TIMEOUT = 120


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# --- 1. Fetch Recent Posts via Apify ---
def fetch_recent_posts():
    """Fetch own LinkedIn posts via Apify scraper."""
    log.info("Fetching recent posts via Apify...")

    client = ApifyClient(APIFY_TOKEN)
    try:
        run = client.actor("harvestapi/linkedin-profile-posts").call(
            run_input={
                "profileUrls": [LINKEDIN_PROFILE_URL],
                "maxPosts": MAX_POSTS,
            },
            timeout_secs=APIFY_TIMEOUT,
        )
        items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
        log.info(f"Apify returned {len(items)} posts")
    except Exception as e:
        log.error(f"Apify posts scraper failed: {e}")
        return []

    db = get_db()
    posts_with_comments = []

    for post in items:
        post_id = post.get("id", "")
        activity_urn = f"urn:li:activity:{post_id}"
        content = post.get("content", "")
        engagement = post.get("engagement", {})
        comment_count = engagement.get("comments", 0)
        linked_url = post.get("linkedinUrl", "")
        created_ts = post.get("postedAt", {}).get("timestamp", 0)

        db.execute(
            "INSERT OR REPLACE INTO tracked_posts "
            "(post_urn, text, created_at, last_checked, active) "
            "VALUES (?, ?, ?, ?, 1)",
            (activity_urn, content[:2000] if content else "",
             created_ts, int(time.time()))
        )

        if comment_count > 0:
            posts_with_comments.append({
                "activity_urn": activity_urn,
                "post_id": post_id,
                "url": linked_url,
                "text": content,
                "comment_count": comment_count,
            })
            log.info(f"Post {post_id}: {comment_count} comments")

    db.commit()
    db.close()
    log.info(f"{len(posts_with_comments)} posts have comments")
    return posts_with_comments


# --- 2. Fetch Comments via Apify ---
def fetch_comments(post):
    """Fetch comments for a specific post via Apify."""
    post_url = post["url"]
    activity_urn = post["activity_urn"]
    log.info(f"Fetching comments for post {post['post_id']}...")

    client = ApifyClient(APIFY_TOKEN)
    try:
        run = client.actor("harvestapi/linkedin-post-comments").call(
            run_input={
                "posts": [post_url],
                "maxItems": 50,
                "scrapeReplies": False,
            },
            timeout_secs=APIFY_TIMEOUT,
        )
        comments = list(
            client.dataset(run["defaultDatasetId"]).iterate_items()
        )
        log.info(
            f"Apify returned {len(comments)} comments "
            f"for post {post['post_id']}"
        )
    except Exception as e:
        log.error(
            f"Apify comments scraper failed for {post['post_id']}: {e}"
        )
        return []

    db = get_db()
    new_comments = []

    for comment in comments:
        comment_id = comment.get("id", "")
        actor = comment.get("actor", {})
        author_name = actor.get("name", "Unknown")
        comment_text = comment.get("commentary", "")
        created_ts = comment.get("createdAtTimestamp", 0)
        is_author = actor.get("author", False)

        # Skip own comments
        if is_author:
            continue

        # Check if already seen
        existing = db.execute(
            "SELECT comment_id FROM seen_comments WHERE comment_id = ?",
            (comment_id,)
        ).fetchone()

        if existing:
            continue

        author_urn = actor.get("id", "")
        db.execute(
            "INSERT INTO seen_comments "
            "(comment_id, post_urn, author_urn, author_name, "
            "text, created_at, seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (comment_id, activity_urn, author_urn, author_name,
             comment_text, created_ts, int(time.time()))
        )

        new_comments.append({
            "comment_id": comment_id,
            "post_urn": activity_urn,
            "author_name": author_name,
            "text": comment_text,
        })

    db.commit()
    db.close()
    log.info(f"{len(new_comments)} new comments found")
    return new_comments


# --- 3. Generate Reply via OpenAI GPT-4o-mini ---
def generate_reply(comment, post_text):
    """Generate a reply draft using OpenAI API."""
    log.info(
        f"Generating reply for comment from {comment['author_name']}: "
        f"{comment['text'][:60]}..."
    )

    system_prompt = (
        "Du bist Ronald Kandelhard. Du antwortest auf LinkedIn-Kommentare "
        "zu deinen eigenen Posts. Deine Stimme ist warm, direkt, konkret "
        "und nie generisch. Du schreibst auf Deutsch mit echten Umlauten. "
        "Schreibe NUR die Antwort (1-3 Saetze), keine Anfuehrungszeichen, "
        "kein Meta-Kommentar."
    )

    user_prompt = (
        f"Kontext des Posts: {post_text[:500]}\n\n"
        f"Kommentar von {comment['author_name']}: {comment['text']}\n\n"
        "Antworte in 1-3 Saetzen."
    )

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=200,
            temperature=0.7,
        )

        draft = response.choices[0].message.content.strip()
        if not draft:
            log.error("OpenAI returned empty response")
            return None

        log.info(f"Draft: {draft[:100]}...")

        db = get_db()
        db.execute(
            "INSERT INTO draft_replies "
            "(comment_id, post_urn, original_comment, draft_text, "
            "status, created_at) "
            "VALUES (?, ?, ?, ?, 'draft', ?)",
            (comment["comment_id"], comment["post_urn"],
             comment["text"], draft, int(time.time()))
        )
        db.commit()
        db.close()

        return draft

    except Exception as e:
        log.error(f"OpenAI API error: {e}")
        return None


# --- 4. Send Approval Email ---
def send_approval_email(drafts):
    """Send email with all draft replies for approval."""
    if not drafts:
        log.info("No drafts to send")
        return

    log.info(f"Sending approval email with {len(drafts)} drafts...")

    html_parts = [
        "<html><body style='font-family:Inter,sans-serif;"
        "max-width:600px;margin:0 auto'>",
        "<h2 style='color:#0F172A'>"
        "LinkedIn Kommentare: Neue Antwort-Entwuerfe</h2>",
        f"<p>{len(drafts)} neue Kommentare brauchen deine Freigabe.</p>",
        "<div style='background:#f1f5f9;padding:12px;"
        "border-radius:8px;margin-bottom:16px'>",
        "<b>Antwort-Optionen:</b> Antworte auf diese Mail mit:<br>",
        "<code>OK</code> = Alle freigeben<br>",
        "<code>OK 1,3</code> = Nur bestimmte freigeben<br>",
        "<code>NEIN 2 Feedback-Text</code> = Ablehnen mit Feedback",
        "</div>",
    ]

    for i, draft in enumerate(drafts, 1):
        post_preview = draft['post_text'][:150] if draft['post_text'] else ''
        html_parts.extend([
            "<div style='border:1px solid #e2e8f0;"
            "border-radius:8px;padding:16px;margin-bottom:16px'>",
            f"<h3 style='color:#0D9488;margin-top:0'>#{i}</h3>",
            f"<p style='color:#64748b;font-size:13px'>"
            f"<b>Post:</b> {post_preview}...</p>",
            f"<p><b>Kommentar von {draft['author_name']}:</b></p>",
            f"<blockquote style='border-left:3px solid #0D9488;"
            f"padding-left:12px;color:#334155;margin:8px 0'>"
            f"{draft['comment_text']}</blockquote>",
            f"<p><b>Vorgeschlagene Antwort:</b></p>",
            f"<blockquote style='border-left:3px solid #F59E0B;"
            f"padding-left:12px;color:#334155;margin:8px 0'>"
            f"{draft['draft_text']}</blockquote>",
            "</div>",
        ])

    html_parts.append("</body></html>")
    html_body = "\n".join(html_parts)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = (
        f"LinkedIn Kommentare: {len(drafts)} neue Antwort-Entwuerfe"
    )
    msg["From"] = SMTP_SENDER
    msg["To"] = APPROVAL_TO
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_SENDER, APPROVAL_TO, msg.as_string())
        log.info(f"Approval email sent to {APPROVAL_TO}")
    except Exception as e:
        log.error(f"Failed to send email: {e}")


# --- 5. Check Approvals via IMAP ---
def check_approvals():
    """Check IMAP inbox for approval responses to LinkedIn comment emails."""
    log.info("Checking IMAP inbox for approval responses...")

    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        imap.login(IMAP_USER, IMAP_PASSWORD)
    except Exception as e:
        log.error(f"IMAP login failed: {e}")
        return []

    try:
        imap.select("INBOX")

        # Search for replies to our approval emails
        status, msg_ids = imap.search(
            None, 'SUBJECT "LinkedIn Kommentare"'
        )

        if status != "OK" or not msg_ids[0]:
            log.info("No approval emails found in inbox")
            imap.logout()
            return []

        ids = msg_ids[0].split()
        log.info(f"Found {len(ids)} emails matching 'LinkedIn Kommentare'")

        results = []

        for mid in ids:
            status, data = imap.fetch(mid, "(RFC822)")
            if status != "OK":
                continue

            msg = email_module.message_from_bytes(data[0][1])

            # Decode subject
            subject_parts = decode_header(msg.get("Subject", ""))
            subject = ""
            for part, charset in subject_parts:
                if isinstance(part, bytes):
                    subject += part.decode(charset or "utf-8", errors="replace")
                else:
                    subject += str(part)

            # Only process replies (Re: or Aw:)
            if not re.match(
                r"^(Re|Aw|AW|Fwd|FW):\s*LinkedIn Kommentare",
                subject, re.IGNORECASE
            ):
                continue

            # Get sender — only accept from APPROVAL_TO
            from_addr = msg.get("From", "")
            # Extract email from "Name <email>" format
            from_match = re.search(r"[\w.+-]+@[\w.-]+", from_addr)
            sender_email = from_match.group(0) if from_match else from_addr

            if sender_email.lower() != APPROVAL_TO.lower():
                log.info(
                    f"Ignoring email from {sender_email} "
                    f"(expected {APPROVAL_TO})"
                )
                continue

            # Get plain text body
            body = _extract_plain_body(msg)
            if not body:
                log.info("Could not extract body from email")
                continue

            log.info(f"Processing approval email: {subject}")
            log.info(f"Body: {body[:200]}")

            # Parse the response
            actions = _parse_approval_body(body)
            if actions:
                results.extend(actions)

                # Mark with SEEN flag
                imap.store(mid, "+FLAGS", "\\Seen")

                # Delete processed approval email to avoid re-processing
                imap.store(mid, "+FLAGS", "\\Deleted")

        imap.expunge()
        imap.logout()

        log.info(f"Parsed {len(results)} approval actions")
        return results

    except Exception as e:
        log.error(f"IMAP processing error: {e}")
        try:
            imap.logout()
        except Exception:
            pass
        return []


def _extract_plain_body(msg):
    """Extract plain text body from email message."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace").strip()
        # Fallback: try HTML
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html = payload.decode(charset, errors="replace")
                    # Strip HTML tags for basic parsing
                    text = re.sub(r"<[^>]+>", " ", html)
                    text = re.sub(r"\s+", " ", text).strip()
                    return text
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace").strip()
    return None


def _parse_approval_body(body):
    """Parse email body for approval/rejection commands.

    Supported formats:
    - "OK" or "JA" -> approve all drafts
    - "OK 1,3,5" -> approve specific numbers
    - "NEIN 2" or "NEIN 2 zu formell" or "NEIN 2: zu formell" -> reject #2 with feedback
    """
    actions = []

    # Clean body: take only lines before quoted content
    lines = []
    for line in body.split("\n"):
        # Stop at typical quote markers
        if line.strip().startswith(">"):
            break
        if re.match(r"^-{3,}\s*(Original|Weitergeleitete)", line):
            break
        if re.match(r"^Am\s+\d+\.\d+\.\d+.*schrieb", line):
            break
        if re.match(r"^On\s+\w+.*wrote:", line):
            break
        lines.append(line.strip())

    text = " ".join(lines).strip()
    if not text:
        return actions

    log.info(f"Parsed response text: {text}")

    # Check for NEIN patterns first (can have multiple)
    nein_matches = re.finditer(
        r"NEIN\s+(\d+)[\s:]*(.+?)(?=NEIN\s+\d|OK|JA|$)",
        text, re.IGNORECASE
    )
    rejected_nums = set()
    for m in nein_matches:
        num = int(m.group(1))
        feedback = m.group(2).strip().rstrip(".")
        actions.append({
            "type": "reject",
            "number": num,
            "feedback": feedback if feedback else None,
        })
        rejected_nums.add(num)

    # Check for OK/JA with specific numbers
    ok_match = re.search(
        r"(?:OK|JA)\s+([\d,\s]+)", text, re.IGNORECASE
    )
    if ok_match:
        nums_str = ok_match.group(1)
        nums = [int(n.strip()) for n in nums_str.split(",") if n.strip().isdigit()]
        for num in nums:
            if num not in rejected_nums:
                actions.append({"type": "approve", "number": num})
    elif re.match(r"^\s*(OK|JA)\s*$", text, re.IGNORECASE):
        # Approve all
        actions.append({"type": "approve_all"})

    return actions


def apply_approval_actions(actions):
    """Apply parsed approval actions to draft_replies in DB."""
    if not actions:
        log.info("No approval actions to apply")
        return

    db = get_db()

    # Get all pending drafts ordered by id
    drafts = db.execute(
        "SELECT id, comment_id, post_urn, draft_text "
        "FROM draft_replies WHERE status = 'draft' "
        "ORDER BY id ASC"
    ).fetchall()

    if not drafts:
        log.info("No pending drafts in DB")
        db.close()
        return

    log.info(f"Found {len(drafts)} pending drafts")

    now = int(time.time())

    for action in actions:
        if action["type"] == "approve_all":
            for draft in drafts:
                db.execute(
                    "UPDATE draft_replies SET status = 'approved', "
                    "approved_at = ? WHERE id = ? AND status = 'draft'",
                    (now, draft["id"])
                )
                log.info(f"Approved draft #{draft['id']}")

        elif action["type"] == "approve":
            num = action["number"]
            if 1 <= num <= len(drafts):
                draft = drafts[num - 1]
                db.execute(
                    "UPDATE draft_replies SET status = 'approved', "
                    "approved_at = ? WHERE id = ? AND status = 'draft'",
                    (now, draft["id"])
                )
                log.info(f"Approved draft #{num} (id={draft['id']})")
            else:
                log.warning(
                    f"Draft #{num} out of range (1-{len(drafts)})"
                )

        elif action["type"] == "reject":
            num = action["number"]
            feedback = action.get("feedback")
            if 1 <= num <= len(drafts):
                draft = drafts[num - 1]
                db.execute(
                    "UPDATE draft_replies SET status = 'rejected', "
                    "feedback = ? WHERE id = ? AND status = 'draft'",
                    (feedback, draft["id"])
                )
                log.info(
                    f"Rejected draft #{num} (id={draft['id']}), "
                    f"feedback: {feedback}"
                )

                # Regenerate with feedback if provided
                if feedback:
                    _regenerate_with_feedback(draft, feedback)
            else:
                log.warning(
                    f"Draft #{num} out of range (1-{len(drafts)})"
                )

    db.commit()
    db.close()


def _regenerate_with_feedback(draft, feedback):
    """Regenerate a reply incorporating rejection feedback."""
    log.info(
        f"Regenerating reply for comment {draft['comment_id']} "
        f"with feedback: {feedback}"
    )

    db = get_db()

    # Get post text for context
    post = db.execute(
        "SELECT text FROM tracked_posts WHERE post_urn = ?",
        (draft["post_urn"],)
    ).fetchone()
    post_text = post["text"] if post else ""

    # Get original comment
    comment = db.execute(
        "SELECT text, author_name FROM seen_comments WHERE comment_id = ?",
        (draft["comment_id"],)
    ).fetchone()

    if not comment:
        log.error(f"Comment {draft['comment_id']} not found in DB")
        db.close()
        return

    system_prompt = (
        "Du bist Ronald Kandelhard. Du antwortest auf LinkedIn-Kommentare "
        "zu deinen eigenen Posts. Deine Stimme ist warm, direkt, konkret "
        "und nie generisch. Du schreibst auf Deutsch mit echten Umlauten. "
        "Schreibe NUR die Antwort (1-3 Saetze), keine Anfuehrungszeichen, "
        "kein Meta-Kommentar."
    )

    user_prompt = (
        f"Kontext des Posts: {post_text[:500]}\n\n"
        f"Kommentar von {comment['author_name']}: {comment['text']}\n\n"
        f"Vorheriger Entwurf wurde abgelehnt: \"{draft['draft_text']}\"\n"
        f"Feedback: {feedback}\n\n"
        "Schreibe eine verbesserte Antwort (1-3 Saetze), "
        "die das Feedback beruecksichtigt."
    )

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=200,
            temperature=0.7,
        )

        new_draft = response.choices[0].message.content.strip()
        if not new_draft:
            log.error("OpenAI returned empty response for regeneration")
            db.close()
            return

        log.info(f"Regenerated draft: {new_draft[:100]}...")

        # Insert new draft
        db.execute(
            "INSERT INTO draft_replies "
            "(comment_id, post_urn, original_comment, draft_text, "
            "status, created_at) "
            "VALUES (?, ?, ?, ?, 'draft', ?)",
            (draft["comment_id"], draft["post_urn"],
             comment["text"], new_draft, int(time.time()))
        )
        db.commit()
        db.close()

        # Send new approval email for just this draft
        send_approval_email([{
            "post_text": post_text,
            "author_name": comment["author_name"],
            "comment_text": comment["text"],
            "draft_text": new_draft,
        }])

    except Exception as e:
        log.error(f"Regeneration failed: {e}")
        db.close()


# --- 6. Post Approved Reply via LinkedIn v2 API ---
def post_reply(activity_urn, reply_text, comment_id):
    """Post an approved reply to a LinkedIn comment via v2 API."""
    log.info(f"Posting reply to comment {comment_id}...")

    encoded = quote(activity_urn, safe="")
    url = (
        f"https://api.linkedin.com/v2/socialActions/"
        f"{encoded}/comments"
    )

    payload = {
        "actor": LI_PERSON_URN,
        "message": {
            "text": reply_text
        },
        "object": activity_urn,
        "parentComment": (
            f"urn:li:comment:({activity_urn},{comment_id})"
        )
    }

    try:
        resp = requests.post(
            url, headers=LI_HEADERS, json=payload, timeout=30
        )

        if resp.status_code in (200, 201):
            resp_id = resp.json().get("id", "N/A")
            log.info(f"Reply posted successfully (id: {resp_id})")
            db = get_db()
            db.execute(
                "UPDATE draft_replies SET status = 'posted', "
                "posted_at = ? WHERE comment_id = ?",
                (int(time.time()), comment_id)
            )
            db.commit()
            db.close()
            return True
        else:
            log.error(
                f"Post reply error: {resp.status_code} - "
                f"{resp.text[:300]}"
            )
            return False

    except requests.RequestException as e:
        log.error(f"Network error posting reply: {e}")
        return False


def check_and_post_approved():
    """Post all approved replies."""
    db = get_db()
    approved = db.execute(
        "SELECT * FROM draft_replies WHERE status = 'approved'"
    ).fetchall()
    db.close()

    if not approved:
        log.info("No approved replies to post")
        return 0

    posted = 0
    for reply in approved:
        if post_reply(
            reply["post_urn"], reply["draft_text"], reply["comment_id"]
        ):
            posted += 1

    log.info(f"Posted {posted}/{len(approved)} approved replies")
    return posted


# --- Main Flow ---
def main():
    check_mode = "--check-approvals" in sys.argv

    if check_mode:
        log.info("=== LinkedIn Comment Agent: Check Approvals ===")

        # 1. Check IMAP inbox for approval responses
        actions = check_approvals()

        # 2. Apply actions to DB
        if actions:
            apply_approval_actions(actions)

        # 3. Post all approved replies
        posted = check_and_post_approved()

        log.info(
            f"=== Check Approvals finished: "
            f"{len(actions)} actions, {posted} posted ==="
        )
    else:
        log.info("=== LinkedIn Comment Agent starting ===")

        # Validate config
        if not LI_TOKEN:
            log.error("LI_ACCESS_TOKEN not set!")
            sys.exit(1)
        if not APIFY_TOKEN:
            log.error("APIFY_TOKEN not set!")
            sys.exit(1)

        # 1. Fetch posts via Apify
        posts_with_comments = fetch_recent_posts()

        if not posts_with_comments:
            log.info("No posts with comments found")
            check_and_post_approved()
            log.info("=== LinkedIn Comment Agent finished ===")
            return

        # 2. Fetch comments and generate replies
        all_drafts = []
        for post in posts_with_comments:
            new_comments = fetch_comments(post)

            for comment in new_comments:
                draft = generate_reply(comment, post.get("text", ""))
                if draft:
                    all_drafts.append({
                        "post_text": post.get("text", "(kein Text)"),
                        "author_name": comment["author_name"],
                        "comment_text": comment["text"],
                        "draft_text": draft,
                    })

        # 3. Send approval email
        if all_drafts:
            send_approval_email(all_drafts)
            log.info(f"Sent {len(all_drafts)} draft replies for approval")
        else:
            log.info("No new comments to process")

        # 4. Check and post approved
        check_and_post_approved()

        log.info("=== LinkedIn Comment Agent finished ===")


if __name__ == "__main__":
    main()
