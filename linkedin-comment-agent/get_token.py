#!/usr/bin/env python3
"""LinkedIn OAuth Token Generator
Generates a new token with r_member_social scope for reading posts/comments.

Usage:
1. Run this script: python3 get_token.py
2. Open the URL in your browser
3. Authorize the app
4. Paste the redirect URL back here
5. Script outputs the new token
"""

import os
import sys
import json
import requests
from urllib.parse import urlencode, urlparse, parse_qs
from dotenv import load_dotenv
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import webbrowser

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

CLIENT_ID = os.getenv("LI_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("LI_CLIENT_SECRET", "")
REDIRECT_URI = "http://localhost:9876/callback"

if not CLIENT_ID or not CLIENT_SECRET:
    print("Error: LI_CLIENT_ID and LI_CLIENT_SECRET must be set in .env")
    sys.exit(1)

# Request these scopes
SCOPES = "openid profile w_member_social r_member_social"

auth_code_result = {}

class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        if "code" in query:
            auth_code_result["code"] = query["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Authorization successful!</h1><p>You can close this window.</p>")
        else:
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            error = query.get("error", ["unknown"])[0]
            desc = query.get("error_description", [""])[0]
            self.wfile.write(f"<h1>Error: {error}</h1><p>{desc}</p>".encode())
            auth_code_result["error"] = f"{error}: {desc}"
    
    def log_message(self, format, *args):
        pass  # Suppress logs


def main():
    # Step 1: Generate auth URL
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": "linkedin_comment_agent",
    }
    auth_url = f"https://www.linkedin.com/oauth/v2/authorization?{urlencode(params)}"
    
    print("\n=== LinkedIn OAuth Token Generator ===")
    print(f"\nOpen this URL in your browser:\n")
    print(auth_url)
    print(f"\nWaiting for callback on {REDIRECT_URI}...")
    
    # Start local server to catch the callback
    server = HTTPServer(("localhost", 9876), CallbackHandler)
    server.timeout = 300  # 5 min timeout
    
    # Wait for one request
    while "code" not in auth_code_result and "error" not in auth_code_result:
        server.handle_request()
    
    server.server_close()
    
    if "error" in auth_code_result:
        print(f"\nError: {auth_code_result['error']}")
        sys.exit(1)
    
    code = auth_code_result["code"]
    print(f"\nGot authorization code: {code[:20]}...")
    
    # Step 2: Exchange code for token
    token_resp = requests.post(
        "https://www.linkedin.com/oauth/v2/accessToken",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=30
    )
    
    if token_resp.status_code != 200:
        print(f"\nToken exchange failed: {token_resp.status_code}")
        print(token_resp.text)
        sys.exit(1)
    
    data = token_resp.json()
    access_token = data.get("access_token")
    expires_in = data.get("expires_in", 0)
    scope = data.get("scope", "")
    
    from datetime import datetime, timedelta
    expires_date = (datetime.now() + timedelta(seconds=expires_in)).strftime("%Y-%m-%d")
    
    print(f"\n=== New Token ===")
    print(f"Access Token: {access_token[:30]}...")
    print(f"Scope: {scope}")
    print(f"Expires: {expires_date}")
    
    # Step 3: Update .env
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    with open(env_path, "r") as f:
        env_content = f.read()
    
    # Replace token and expiry
    import re
    env_content = re.sub(r"LI_ACCESS_TOKEN=.*", f"LI_ACCESS_TOKEN={access_token}", env_content)
    env_content = re.sub(r"LI_TOKEN_EXPIRES=.*", f"LI_TOKEN_EXPIRES={expires_date}", env_content)
    
    with open(env_path, "w") as f:
        f.write(env_content)
    
    print(f"\n.env updated with new token (expires {expires_date})")
    
    # Also update Content Planner's token
    cp_env = "/opt/ai-projekte/Content Creation/li_publish/.env"
    if os.path.exists(cp_env):
        with open(cp_env, "r") as f:
            cp_content = f.read()
        cp_content = re.sub(r"LI_ACCESS_TOKEN=.*", f"LI_ACCESS_TOKEN={access_token}", cp_content)
        cp_content = re.sub(r"LI_TOKEN_EXPIRES=.*", f"LI_TOKEN_EXPIRES={expires_date}", cp_content)
        with open(cp_env, "w") as f:
            f.write(cp_content)
        print(f"Content Planner .env also updated")
    
    # Verify scopes
    introspect = requests.post(
        "https://www.linkedin.com/oauth/v2/introspectToken",
        data={
            "token": access_token,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=30
    )
    if introspect.status_code == 200:
        print(f"\nVerified scopes: {introspect.json().get('scope')}")


if __name__ == "__main__":
    main()
