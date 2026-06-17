"""
Fyers Direct API Authentication (no browser, no Selenium, no Turnstile)
=======================================================================
Uses direct HTTP calls to Fyers login backend — bypasses Cloudflare entirely.

Flow:
  1. POST /send_login_otp  → get request_key
  2. POST /verify_otp      → verify TOTP, get new request_key
  3. POST /verify_pin      → verify PIN, get user access_token (Bearer)
  4. POST /api/v3/token    → get auth_code using Bearer token
  5. SDK generate_token()  → exchange auth_code for API access token

No browser, no Turnstile, works on EC2 cloud IPs.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from urllib import parse

import pyotp
import pytz
import requests

import config

IST = pytz.timezone('Asia/Kolkata')

logger = logging.getLogger('fyers_direct_auth')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)

# ── Fyers API endpoints ────────────────────────────────────────────────────────
_VAGATOR   = "https://api-t2.fyers.in/vagator/v2"
_API_V3    = "https://api-t1.fyers.in/api/v3"

URL_SEND_OTP   = _VAGATOR + "/send_login_otp"
URL_VERIFY_OTP = _VAGATOR + "/verify_otp"
URL_VERIFY_PIN = _VAGATOR + "/verify_pin"
URL_TOKEN      = _API_V3  + "/token"


# ── Token validity check (same as fyers_selenium_auth.py) ─────────────────────

def token_is_valid() -> bool:
    """
    Return True only if token.txt exists AND the JWT has not expired.

    Checks actual JWT exp field (not just file date) so post-holiday
    midnight tokens that age past their 6-hour window are correctly
    rejected by the 8:45 IST auth timer.
    Adds a 30-min early-refresh buffer so the timer always wins the race.
    """
    import base64
    import json as _json
    import time as _time

    path = os.path.join(config.LOG_DIRECTORY, 'token.txt')
    if not os.path.exists(path):
        return False
    with open(path) as f:
        lines = f.read().strip().split('\n')
    if not lines or not lines[0].strip():
        return False
    try:
        # Fast path: same-day file date guard
        if len(lines) >= 2:
            token_date = datetime.fromisoformat(lines[1]).date()
            if token_date != datetime.now(IST).date():
                return False
        # JWT expiry check — decode payload, check exp field
        jwt = lines[0].strip()
        parts = jwt.split('.')
        if len(parts) != 3:
            return False
        padding = '=' * (-len(parts[1]) % 4)
        payload = _json.loads(base64.urlsafe_b64decode(parts[1] + padding))
        exp = payload.get('exp', 0)
        BUFFER_SECS = 1800  # refresh 30 min before actual expiry
        if _time.time() + BUFFER_SECS > exp:
            logger.info(
                f"Token JWT expired or expiring within 30 min "
                f"(exp={exp}, now={int(_time.time())}) — treating as invalid"
            )
            return False
        return True
    except Exception as e:
        logger.warning(f"token_is_valid() check failed ({e}) — treating as invalid")
        return False


def _save_token(token: str) -> None:
    os.makedirs(config.LOG_DIRECTORY, exist_ok=True)
    path = os.path.join(config.LOG_DIRECTORY, 'token.txt')
    with open(path, 'w') as f:
        f.write(token + '\n')
        f.write(datetime.now().isoformat())
    logger.info(f"Token saved → {path}")


# ── Direct auth flow ───────────────────────────────────────────────────────────

def direct_authenticate() -> str | None:
    """
    Authenticate with Fyers API via direct HTTP calls (no browser).
    Returns the API access token string, or None on failure.
    """
    app_id    = config.FYERS_APP_ID       # e.g. "ZH59OA7HQ1-100"
    secret    = config.FYERS_SECRET_KEY
    fy_id     = config.FYERS_CLIENT_ID    # Fyers trading login ID e.g. "FAI64160"
    pin       = str(config.FYERS_PIN)
    totp_key  = config.FYERS_TOTP_KEY
    redir_uri = config.REDIRECT_URI

    if not all([app_id, secret, fy_id, pin, totp_key]):
        logger.error("Missing credentials — check .env (FYERS_APP_ID, FYERS_SECRET_KEY, "
                     "FYERS_CLIENT_ID, FYERS_PIN, FYERS_TOTP_KEY)")
        return None

    # Split "ZH59OA7HQ1-100" → app_id_part="ZH59OA7HQ1", app_type="100"
    if '-' in app_id:
        app_id_part, app_type = app_id.rsplit('-', 1)
    else:
        app_id_part, app_type = app_id, "100"

    sess = requests.Session()

    # ── Step 1: Send login OTP (triggers TOTP challenge) ──────────────────────
    logger.info(f"Step 1: Sending login OTP request for {fy_id}...")
    r = sess.post(URL_SEND_OTP, json={"fy_id": fy_id, "app_id": "2"})
    data = r.json()
    logger.info(f"  Response: {r.status_code} — {data}")
    if r.status_code != 200 or 'request_key' not in data:
        logger.error(f"Step 1 failed: {data}")
        return None
    request_key = data['request_key']
    logger.info(f"  request_key obtained.")

    # ── Step 2: Verify TOTP ───────────────────────────────────────────────────
    logger.info("Step 2: Verifying TOTP...")
    totp_code = pyotp.TOTP(totp_key).now()
    logger.info(f"  TOTP: {totp_code}")
    r = sess.post(URL_VERIFY_OTP, json={"request_key": request_key, "otp": totp_code})
    data = r.json()
    logger.info(f"  Response: {r.status_code} — {data}")
    if r.status_code != 200 or 'request_key' not in data:
        logger.error(f"Step 2 (TOTP) failed: {data}")
        return None
    request_key = data['request_key']
    logger.info("  TOTP verified.")

    # ── Step 3: Verify PIN ────────────────────────────────────────────────────
    logger.info("Step 3: Verifying PIN...")
    r = sess.post(URL_VERIFY_PIN, json={
        "request_key":   request_key,
        "identity_type": "pin",
        "identifier":    pin,
        "recaptcha_token": "",
    })
    data = r.json()
    logger.info(f"  Response: {r.status_code} — {str(data)[:200]}")
    if r.status_code != 200 or 'data' not in data:
        logger.error(f"Step 3 (PIN) failed: {data}")
        return None
    bearer_token = data['data']['access_token']
    logger.info("  PIN verified. Bearer token obtained.")

    # ── Step 4: Get auth_code ─────────────────────────────────────────────────
    logger.info("Step 4: Requesting auth_code...")
    r = sess.post(URL_TOKEN, json={
        "fyers_id":      fy_id,
        "app_id":        app_id_part,
        "redirect_uri":  redir_uri,
        "appType":       app_type,
        "code_challenge": "",
        "state":         "sample_state",
        "response_type": "code",
        "create_cookie": True,
    }, headers={"Authorization": f"Bearer {bearer_token}"})
    data = r.json()
    logger.info(f"  Response: {r.status_code} — {str(data)[:300]}")

    auth_code = None
    url_str = data.get("Url") or data.get("url") or ""
    if url_str:
        try:
            auth_code = parse.parse_qs(parse.urlparse(url_str).query)['auth_code'][0]
        except (KeyError, IndexError):
            pass

    if not auth_code:
        # Fallback: auth_code directly in response
        auth_code = (data.get("data", {}) or {}).get("auth_code") or data.get("auth_code")

    if not auth_code:
        logger.error(f"No auth_code found in response: {data}")
        return None
    logger.info(f"  auth_code obtained: {auth_code[:20]}...")

    # ── Step 5: Exchange auth_code for API access token ───────────────────────
    logger.info("Step 5: Exchanging auth_code for API access token...")
    from fyers_apiv3 import fyersModel
    token_session = fyersModel.SessionModel(
        client_id     = app_id,
        secret_key    = secret,
        redirect_uri  = redir_uri,
        response_type = "code",
        grant_type    = "authorization_code",
    )
    token_session.set_token(auth_code)
    token_data = token_session.generate_token()
    logger.info(f"  Token response: {str(token_data)[:200]}")

    if token_data.get('code') == 200 or token_data.get('s') == 'ok':
        access_token = token_data['access_token']
        _save_token(access_token)
        logger.info("Direct auth complete. Token saved.")
        return access_token

    logger.error(f"Token exchange failed: {token_data}")
    return None


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Fyers direct API auto-authentication')
    parser.add_argument('--force', action='store_true',
                        help='Re-authenticate even if valid token exists')
    parser.add_argument('--check', action='store_true',
                        help='Print token validity and exit')
    args = parser.parse_args()

    if args.check:
        valid = token_is_valid()
        print(f"Token valid today: {valid}")
        sys.exit(0 if valid else 1)

    if not args.force and token_is_valid():
        print("Valid token already exists for today. Skipping. (Use --force to override.)")
        sys.exit(0)

    token = direct_authenticate()
    if token:
        print("Authentication successful. Token saved to logs/token.txt")
        sys.exit(0)
    else:
        print("Direct auth failed. Check logs above for details.")
        sys.exit(1)
