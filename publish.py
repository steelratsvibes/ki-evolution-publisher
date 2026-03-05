#!/usr/bin/env python3
"""Standalone Instagram/Facebook publisher for GitHub Actions.

Reads a staged JSON from queue/, publishes to IG + FB, moves to done/.
Requires only `requests`. All tokens via environment variables.
"""

import json
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests

# ── Config from environment ──
IG_USER_ID = os.environ.get("IG_USER_ID")
IG_TOKEN = os.environ.get("IG_ACCESS_TOKEN")
FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "1033633629830009")
FB_TOKEN = os.environ.get("FB_ACCESS_TOKEN") or IG_TOKEN
API_VERSION = "v25.0"
TIMEOUT = 60

QUEUE_DIR = Path(__file__).parent / "queue"
DONE_DIR = Path(__file__).parent / "done"


def ig_post(path: str, payload: dict, retries: int = 3) -> dict:
    url = f"https://graph.facebook.com/{API_VERSION}/{path}"
    for attempt in range(retries):
        resp = requests.post(url, data=payload, timeout=TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 500 and attempt < retries - 1:
            wait = (attempt + 1) * 10
            print(f"  API 500 — retry in {wait}s...")
            time.sleep(wait)
            continue
        raise RuntimeError(f"API {resp.status_code}: {resp.text[:300]}")


def check_token() -> bool:
    resp = requests.get(
        f"https://graph.facebook.com/{API_VERSION}/me",
        params={"access_token": IG_TOKEN, "fields": "id,name"},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"TOKEN UNGUELTIG! {resp.text[:200]}")
        return False
    print(f"Token OK: {resp.json().get('name', '?')}")
    return True


def publish_carousel(data: dict) -> str | None:
    urls = data["slide_urls"]
    caption = data["caption"]
    n = len(urls)
    print(f"\nCarousel: {n} Slides")

    # Item containers
    cids = []
    for i, url in enumerate(urls):
        print(f"  Container {i+1}/{n}...", end=" ", flush=True)
        r = ig_post(f"{IG_USER_ID}/media", {
            "image_url": url, "is_carousel_item": "true",
            "access_token": IG_TOKEN,
        })
        cids.append(r["id"])
        print("OK")
        if i < n - 1:
            time.sleep(3)

    # Carousel container
    print("  Carousel erstellen...")
    r = ig_post(f"{IG_USER_ID}/media", {
        "media_type": "CAROUSEL", "children": ",".join(cids),
        "caption": caption, "access_token": IG_TOKEN,
    })
    creation_id = r["id"]

    # Publish
    print("  15s warten + publishen...")
    time.sleep(15)
    r = ig_post(f"{IG_USER_ID}/media_publish", {
        "creation_id": creation_id, "access_token": IG_TOKEN,
    })
    mid = r["id"]
    print(f"  CAROUSEL LIVE: {mid}")
    return mid


def publish_stories(data: dict) -> list[str]:
    story_urls = data.get("story_urls", [])
    if not story_urls:
        return []

    n = len(story_urls)
    print(f"\nStories: {n}")
    mids = []
    for i, url in enumerate(story_urls):
        print(f"  Story {i+1}/{n}...", end=" ", flush=True)
        r = ig_post(f"{IG_USER_ID}/media", {
            "image_url": url, "media_type": "STORIES",
            "access_token": IG_TOKEN,
        })
        time.sleep(5)
        r = ig_post(f"{IG_USER_ID}/media_publish", {
            "creation_id": r["id"], "access_token": IG_TOKEN,
        })
        mids.append(r["id"])
        print(f"OK ({r['id']})")
        if i < n - 1:
            time.sleep(5)
    return mids


def publish_facebook(data: dict) -> str | None:
    urls = data["slide_urls"]
    caption = data.get("caption_fb") or data["caption"].split("\n\n#")[0]
    n = len(urls)

    # Get page token
    r = requests.get(
        f"https://graph.facebook.com/{API_VERSION}/me/accounts",
        params={"access_token": FB_TOKEN},
        timeout=30,
    )
    if r.status_code != 200:
        print(f"  FB Page Token ERROR: {r.text[:200]}")
        return None
    page_token = None
    for page in r.json().get("data", []):
        if page["id"] == FB_PAGE_ID:
            page_token = page["access_token"]
            break
    if not page_token:
        print(f"  FB Page {FB_PAGE_ID} nicht gefunden")
        return None

    print(f"\nFacebook: {n} Fotos")

    # Upload as unpublished
    photo_ids = []
    for i, url in enumerate(urls):
        print(f"  Foto {i+1}/{n}...", end=" ", flush=True)
        r = ig_post(f"{FB_PAGE_ID}/photos", {
            "url": url, "published": "false", "access_token": page_token,
        })
        photo_ids.append(r["id"])
        print("OK")
        if i < n - 1:
            time.sleep(2)

    # Create feed post
    payload = {"message": caption, "access_token": page_token}
    for i, pid in enumerate(photo_ids):
        payload[f"attached_media[{i}]"] = json.dumps({"media_fbid": pid})

    r = ig_post(f"{FB_PAGE_ID}/feed", payload)
    post_id = r["id"]
    print(f"  FB POST LIVE: {post_id}")
    return post_id


def find_todays_job() -> Path | None:
    today = date.today().isoformat()
    candidates = sorted(QUEUE_DIR.glob("*.json"))
    # First try exact date match
    for f in candidates:
        data = json.loads(f.read_text())
        if data.get("planned_date") == today:
            return f
    # Then take the oldest overdue job
    for f in candidates:
        data = json.loads(f.read_text())
        if data.get("planned_date") <= today:
            return f
    return None


def main():
    if not IG_TOKEN or not IG_USER_ID:
        print("ERROR: IG_ACCESS_TOKEN and IG_USER_ID must be set")
        sys.exit(1)

    if not check_token():
        sys.exit(1)

    job_file = find_todays_job()
    if not job_file:
        print("Kein Post fuer heute in der Queue.")
        sys.exit(0)

    data = json.loads(job_file.read_text())
    print(f"\nPost: {data['title']}")
    print(f"Datum: {data['planned_date']}")
    print(f"Slides: {len(data['slide_urls'])}")

    result = {"content_id": data["content_id"], "planned_date": data["planned_date"]}

    # Carousel
    try:
        carousel_id = publish_carousel(data)
        result["carousel_id"] = carousel_id
    except Exception as e:
        print(f"CAROUSEL FEHLER: {e}")
        result["carousel_error"] = str(e)
        sys.exit(1)

    # Stories
    try:
        story_ids = publish_stories(data)
        result["story_ids"] = story_ids
    except Exception as e:
        print(f"STORIES FEHLER: {e}")
        result["stories_error"] = str(e)

    # Facebook
    if not data.get("skip_fb"):
        try:
            fb_id = publish_facebook(data)
            result["fb_id"] = fb_id
        except Exception as e:
            print(f"FB FEHLER: {e}")
            result["fb_error"] = str(e)

    # Move to done
    result["published_at"] = datetime.now(timezone.utc).isoformat()
    data["result"] = result
    DONE_DIR.mkdir(exist_ok=True)
    done_path = DONE_DIR / job_file.name
    done_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    job_file.unlink()

    print(f"\nFERTIG — verschoben nach done/{job_file.name}")


if __name__ == "__main__":
    main()
