"""
Automated (headless) Upstox token refresh for Kronos Futures Bot.
Uses requests + pyotp — no browser needed.

Requires in .env:
    UPSTOX_MOBILE        = 10-digit mobile number
    UPSTOX_PIN           = Upstox login PIN/password
    UPSTOX_TOTP_KEY      = base32 TOTP secret (from Upstox 2FA setup)
    UPSTOX_API_KEY       = app client_id
    UPSTOX_API_SECRET    = app client_secret
    UPSTOX_REDIRECT_URI  = http://127.0.0.1:8080/

Run directly:  /opt/kronos_bot/venv/bin/python brokers/upstox_auth_auto.py
Systemd timer runs this at 08:30 IST daily, then restarts kronos_futures_bot.
"""
from __future__ import annotations
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import requests
import pyotp
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

TOKEN_PATH   = Path(__file__).parent.parent / "logs" / "upstox_token.txt"
_BASE        = "https://api.upstox.com/v2"
_AUTH_URL    = f"{_BASE}/login/authorization/dialog"
_TOKEN_URL   = f"{_BASE}/login/authorization/token"

_SESSION_HDR = {
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type":    "application/json",
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Origin":          "https://api.upstox.com",
}


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
    except Exception:
        return False


def _get_auth_code(session: requests.Session, api_key: str, redirect_uri: str,
                   mobile: str, pin: str, totp_key: str) -> str:
    """Automates the Upstox login flow and returns the OAuth2 auth code."""

    # Step 1 — Login with mobile + PIN
    log.info("[AUTH] Step 1: submitting credentials…")
    r = session.post(
        _AUTH_URL,
        json={"mobile_number": mobile, "password": pin},
        headers=_SESSION_HDR,
        timeout=15,
    )
    data = r.json()
    if data.get("status") != "success":
        raise RuntimeError(f"Login failed: {data}")
    login_token = data["data"]["token"]
    log.info("[AUTH] Login OK — token received")

    # Step 2 — TOTP verification
    log.info("[AUTH] Step 2: submitting TOTP…")
    totp_code = pyotp.TOTP(totp_key).now()
    r = session.post(
        _AUTH_URL,
        json={"token": login_token, "totp": totp_code},
        headers=_SESSION_HDR,
        timeout=15,
    )
    data = r.json()
    if data.get("status") != "success":
        raise RuntimeError(f"TOTP failed: {data}")
    log.info("[AUTH] TOTP OK")

    # Step 3 — Get auth code via redirect
    log.info("[AUTH] Step 3: fetching auth code…")
    params = {
        "client_id":     api_key,
        "redirect_uri":  redirect_uri,
        "response_type": "code",
        "state":         "kronos",
    }
    r = session.get(_AUTH_URL, params=params, headers=_SESSION_HDR,
                    allow_redirects=False, timeout=15)
    location = r.headers.get("Location", "")
    code = parse_qs(urlparse(location).query).get("code", [None])[0]
    if not code:
        raise RuntimeError(
            f"No auth code in redirect. Status={r.status_code} Location={location!r}"
        )
    log.info("[AUTH] Auth code obtained")
    return code


def refresh_token() -> str:
    """Full automated token refresh. Returns the new access token."""
    api_key    = os.environ["UPSTOX_API_KEY"]
    api_secret = os.environ["UPSTOX_API_SECRET"]
    redirect   = os.environ.get("UPSTOX_REDIRECT_URI", "http://127.0.0.1:8080/")
    mobile     = os.environ["UPSTOX_MOBILE"]
    pin        = os.environ["UPSTOX_PIN"]
    totp_key   = os.environ["UPSTOX_TOTP_KEY"]

    session = requests.Session()

    # Seed the session cookie by visiting the auth dialog first
    session.get(_AUTH_URL, params={
        "client_id": api_key, "redirect_uri": redirect,
        "response_type": "code", "state": "kronos",
    }, timeout=10)

    auth_code = _get_auth_code(session, api_key, redirect, mobile, pin, totp_key)

    # Exchange code → access token
    r = session.post(
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
    data = r.json()
    if r.status_code != 200 or "access_token" not in data:
        raise RuntimeError(f"Token exchange failed: {data}")

    token = data["access_token"]
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token + "\n" + datetime.now().isoformat())
    log.info("[AUTH] Token saved → %s", TOKEN_PATH)
    return token


if __name__ == "__main__":
    if token_is_valid():
        log.info("Token still valid for today — nothing to do.")
        sys.exit(0)

    log.info("[AUTH] Starting automated token refresh…")
    try:
        refresh_token()
        log.info("[AUTH] Done.")
        sys.exit(0)
    except Exception as e:
        log.error("[AUTH] Automated refresh failed: %s", e)
        log.error("[AUTH] Fall back to manual: run brokers/upstox_auth.py")
        sys.exit(1)
