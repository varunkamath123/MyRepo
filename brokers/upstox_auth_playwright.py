"""
Headless Upstox token refresh using Playwright (Chromium).
Automates the full browser login flow — no browser window needed.

Requires: pip install playwright && playwright install chromium --with-deps
Credentials in .env: UPSTOX_MOBILE, UPSTOX_PIN, UPSTOX_TOTP_KEY
"""
from __future__ import annotations
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pyotp
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

TOKEN_PATH  = Path(__file__).parent.parent / "logs" / "upstox_token.txt"
_AUTH_URL   = "https://api.upstox.com/v2/login/authorization/dialog"
_TOKEN_URL  = "https://api.upstox.com/v2/login/authorization/token"


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


def _headless_get_auth_code(api_key: str, redirect_uri: str,
                             mobile: str, pin: str, totp_key: str) -> str:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    auth_code: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = browser.new_context()
        page = context.new_page()

        # Intercept the redirect-to-localhost and capture the auth code.
        # Check the actual host — not just a substring — so the initial auth URL
        # (which contains 127.0.0.1 as a query param) is not accidentally aborted.
        from urllib.parse import urlparse as _urlparse

        def handle_route(route):
            url = route.request.url
            host = _urlparse(url).netloc
            if host.startswith("127.0.0.1"):
                m = re.search(r"[?&]code=([^&]+)", url)
                if m:
                    auth_code.append(m.group(1))
                route.abort()
            else:
                route.continue_()

        page.route("**", handle_route)

        full_url = (
            f"{_AUTH_URL}?client_id={api_key}"
            f"&redirect_uri={redirect_uri}&response_type=code&state=kronos"
        )
        log.info("[PW] Loading Upstox login page…")
        page.goto(full_url, wait_until="networkidle", timeout=30000)

        # ── Step 1: Mobile number → click "Get OTP" ──────────────────────────
        log.info("[PW] Entering mobile number…")
        page.wait_for_selector("input[type='text']", timeout=15000)
        page.fill("input[type='text']", mobile)
        page.wait_for_timeout(500)
        page.get_by_text("Get OTP").click()
        page.wait_for_load_state("networkidle", timeout=15000)
        page.wait_for_timeout(1000)

        # ── Step 2: TOTP code → click "Continue" ────────────────────────────
        log.info("[PW] Entering TOTP…")
        try:
            page.wait_for_selector("input[type='text']", timeout=10000)
            totp_code = pyotp.TOTP(totp_key).now()
            page.fill("input[type='text']", totp_code)
            page.wait_for_timeout(500)
            page.get_by_text("Continue").click()
            page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            log.warning("[PW] No OTP field after Get OTP — check TOTP setup")

        # ── Step 3: PIN (if shown as second factor) ──────────────────────────
        try:
            page.wait_for_selector("input[type='password']", timeout=5000)
            log.info("[PW] Entering PIN…")
            page.fill("input[type='password']", pin)
            page.wait_for_timeout(500)
            page.get_by_text("Continue").click()
            page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            log.info("[PW] No PIN field — skipping")

        # Allow a moment for the final redirect
        page.wait_for_timeout(2000)
        browser.close()

    if not auth_code:
        raise RuntimeError(
            "[PW] Auth code not captured — login may have failed. "
            "Check EC2 logs or run brokers/upstox_auth.py manually."
        )
    return auth_code[0]


def refresh_token() -> str:
    import requests

    api_key    = os.environ["UPSTOX_API_KEY"]
    api_secret = os.environ["UPSTOX_API_SECRET"]
    redirect   = os.environ.get("UPSTOX_REDIRECT_URI", "http://127.0.0.1:8080/")
    mobile     = os.environ["UPSTOX_MOBILE"]
    pin        = os.environ["UPSTOX_PIN"]
    totp_key   = os.environ["UPSTOX_TOTP_KEY"]

    code = _headless_get_auth_code(api_key, redirect, mobile, pin, totp_key)
    log.info("[PW] Auth code obtained — exchanging for token…")

    resp = requests.post(
        _TOKEN_URL,
        data={
            "code":          code,
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
    log.info("[PW] Token saved → %s", TOKEN_PATH)
    return token


if __name__ == "__main__":
    if token_is_valid():
        log.info("Token still valid for today — nothing to do.")
        sys.exit(0)

    log.info("[PW] Starting headless token refresh…")
    try:
        refresh_token()
        log.info("[PW] Done.")
        sys.exit(0)
    except Exception as e:
        log.error("[PW] Headless refresh failed: %s", e)
        log.error("[PW] Fallback: run brokers/upstox_auth.py manually")
        sys.exit(1)
