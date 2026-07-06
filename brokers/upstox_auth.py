"""
Upstox OAuth2 auth for Kronos Futures Bot.
Run once per day before market open (via systemd timer at 08:45 IST or manually).

Flow:
  1. Prints a login URL
  2. User opens it in browser, logs in
  3. Browser redirects to http://127.0.0.1:8080/?code=XXXX (connection refused is fine)
  4. User copies the 'code' value from the URL bar and pastes it here
  5. Token exchanged and saved to logs/upstox_token.txt
"""
from __future__ import annotations
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

TOKEN_PATH = Path(__file__).parent.parent / "logs" / "upstox_token.txt"
_TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
_LOGIN_URL = "https://api.upstox.com/v2/login/authorization/dialog"


def token_is_valid() -> bool:
    if not TOKEN_PATH.exists():
        return False
    lines = TOKEN_PATH.read_text().strip().split("\n")
    if len(lines) < 2 or not lines[0].strip():
        return False
    try:
        from zoneinfo import ZoneInfo
        IST = ZoneInfo("Asia/Kolkata")
        token_date = datetime.fromisoformat(lines[1]).date()
        return token_date == datetime.now(IST).date()
    except Exception as e:
        log.warning("token_is_valid() error: %s", e)
        return False


def get_access_token() -> str:
    if not TOKEN_PATH.exists():
        raise RuntimeError("Upstox token missing. Run: python brokers/upstox_auth.py")
    lines = TOKEN_PATH.read_text().strip().split("\n")
    if not lines[0].strip():
        raise RuntimeError("Upstox token empty. Run: python brokers/upstox_auth.py")
    return lines[0].strip()


def exchange_auth_code(auth_code: str) -> str:
    api_key    = os.environ["UPSTOX_API_KEY"]
    api_secret = os.environ["UPSTOX_API_SECRET"]
    redirect   = os.environ.get("UPSTOX_REDIRECT_URI", "http://127.0.0.1:8080/")

    resp = requests.post(
        _TOKEN_URL,
        data={
            "code":          auth_code,
            "client_id":     api_key,
            "client_secret": api_secret,
            "redirect_uri":  redirect,
            "grant_type":    "authorization_code",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        timeout=15,
    )
    data = resp.json()
    if resp.status_code != 200 or "access_token" not in data:
        raise RuntimeError(f"Token exchange failed: {data}")
    token = data["access_token"]
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token + "\n" + datetime.now().isoformat())
    log.info("Upstox token saved → %s", TOKEN_PATH)
    return token


if __name__ == "__main__":
    if token_is_valid():
        log.info("Token still valid — skipping re-auth.")
        sys.exit(0)

    api_key  = os.environ["UPSTOX_API_KEY"]
    redirect = os.environ.get("UPSTOX_REDIRECT_URI", "http://127.0.0.1:8080/")
    params   = {"client_id": api_key, "redirect_uri": redirect, "response_type": "code", "state": "kronos"}
    url      = f"{_LOGIN_URL}?{urlencode(params)}"

    print("\n" + "=" * 70)
    print("UPSTOX AUTH — open this URL in your browser:")
    print(url)
    print("=" * 70)
    print(f"After login, browser redirects to:  {redirect}?code=XXXX&...")
    print("(Connection refused is expected — copy the 'code' value from the URL)")
    print("=" * 70 + "\n")

    code = input("Paste auth_code here: ").strip()
    if not code:
        log.error("No auth code provided.")
        sys.exit(1)

    try:
        exchange_auth_code(code)
        log.info("Authentication successful.")
        sys.exit(0)
    except Exception as e:
        log.error("Auth failed: %s", e)
        sys.exit(1)
