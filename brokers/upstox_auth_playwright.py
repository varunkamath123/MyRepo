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


def _start_local_server(port: int = 8080):
    """Start a tiny HTTP server on localhost to receive the OAuth redirect."""
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from urllib.parse import urlparse as _up, parse_qs as _pqs

    captured: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            code = _pqs(_up(self.path).query).get("code", [None])[0]
            if code:
                captured.append(code)
                log.info("[SRV] Auth code captured from redirect!")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h2>Auth complete. You can close this window.</h2>")

        def log_message(self, *_):
            pass  # silence default request logging

    srv = HTTPServer(("127.0.0.1", port), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, captured


def _headless_get_auth_code(api_key: str, redirect_uri: str,
                             mobile: str, pin: str, totp_key: str) -> str:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    # Start a local HTTP server that will receive the OAuth redirect from Upstox.
    # Much more reliable than route interception for capturing the auth code.
    srv, auth_code = _start_local_server(port=8080)
    log.info("[PW] Local redirect server started on port 8080")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            context = browser.new_context()
            page = context.new_page()

            full_url = (
                f"{_AUTH_URL}?client_id={api_key}"
                f"&redirect_uri={redirect_uri}&response_type=code&state=kronos"
            )
            log.info("[PW] Loading Upstox login page…")
            page.goto(full_url, wait_until="networkidle", timeout=30000)

            # ── Step 1: Mobile number → click "Get OTP" ──────────────────────
            log.info("[PW] Entering mobile number…")
            page.wait_for_selector("input[type='text']", timeout=15000)
            page.locator("input[type='text']").first.fill(mobile)
            page.wait_for_timeout(500)
            log.info("[PW] Clicking Get OTP…")
            page.get_by_text("Get OTP").click()

            # Wait for the OTP screen to appear (mobile input transitions away)
            log.info("[PW] Waiting for OTP screen…")
            page.wait_for_timeout(2500)

            # ── Step 2: TOTP → Continue ───────────────────────────────────────
            log.info("[PW] Entering TOTP…")
            try:
                otp_input = page.locator("input[type='text']").first
                otp_input.wait_for(state="visible", timeout=10000)
                totp_code = pyotp.TOTP(totp_key).now()
                log.info("[PW] TOTP code: %s", totp_code)
                otp_input.fill(totp_code)
                page.wait_for_timeout(500)
                page.get_by_text("Continue").click()
                page.wait_for_timeout(2500)
            except PWTimeout:
                log.warning("[PW] No OTP field after Get OTP — check TOTP setup")

            # ── Step 3: PIN ───────────────────────────────────────────────────
            log.info("[PW] Looking for PIN field…")
            pin_found = False
            for pin_selector in ("input[type='password']", "input[type='text']"):
                try:
                    pin_loc = page.locator(pin_selector).first
                    pin_loc.wait_for(state="visible", timeout=4000)
                    current_val = pin_loc.input_value()
                    if current_val and len(current_val) == 6 and current_val.isdigit():
                        log.info("[PW] Field still has TOTP value — not PIN, skipping")
                        continue
                    log.info("[PW] Entering PIN via %s…", pin_selector)
                    pin_loc.fill(pin)
                    page.wait_for_timeout(500)
                    page.get_by_text("Continue").click()
                    pin_found = True
                    break
                except PWTimeout:
                    continue
            if not pin_found:
                log.info("[PW] No separate PIN field — skipping")

            # Wait for the browser to follow the redirect to our local server
            log.info("[PW] Waiting for OAuth redirect…")
            for _ in range(30):   # up to 15 s
                if auth_code:
                    break
                page.wait_for_timeout(500)

            log.info("[PW] Final URL: %s", page.url)
            browser.close()
    finally:
        srv.shutdown()

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
