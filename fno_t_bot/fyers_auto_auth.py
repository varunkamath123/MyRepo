"""
Fyers Automated Daily Authentication
Performs the full Fyers login flow programmatically (no browser needed).
Requires FYERS_PIN and FYERS_TOTP_KEY to be set in .env

Schedule this to run at 08:45 IST every trading day (before market open).

Usage:
  python fyers_auto_auth.py           # Authenticate and save token
  python fyers_auto_auth.py --check   # Check if today's token exists

How to get your TOTP key:
  1. In Fyers app → Profile → Security → 2FA → "Show secret key"
  2. Copy the base32 secret (NOT the QR code) → set as FYERS_TOTP_KEY in .env
"""

import os
import sys
import json
import logging
from datetime import datetime

import pytz
import pyotp
import requests
from fyers_apiv3 import fyersModel

import config

IST    = pytz.timezone('Asia/Kolkata')
logger = logging.getLogger('fyers_auto_auth')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)

# Fyers v3 API endpoints for headless login
# Step 1 endpoint changed from login_step1 → send_login_otp (Fyers API update 2025)
_BASE  = "https://api-t2.fyers.in/vagator/v2"
_TOKEN = "https://api-t1.fyers.in/api/v3"


def _post(url: str, payload: dict) -> dict:
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


def auto_authenticate() -> str | None:
    """
    Full automated Fyers auth flow:
      Step 1: Login with client_id + password  → request_key
      Step 2: Verify TOTP                       → request_key2
      Step 3: Verify PIN                        → access_token (temp)
      Step 4: Exchange for API access token     → final token

    Returns the access token string, or None on failure.
    """
    pin      = config.FYERS_PIN
    totp_key = config.FYERS_TOTP_KEY

    if not pin or not totp_key:
        logger.error(
            "FYERS_PIN and FYERS_TOTP_KEY must be set in .env for auto-auth.\n"
            "  Falling back to manual auth — run fyers_auth.py instead."
        )
        return None

    # ── Step 1: Initiate login (get request_key for TOTP) ─────────────────────
    # Fyers updated endpoint: send_login_otp (was login_step1, now returns
    # a request_key that is used to verify TOTP in step 2)
    logger.info("Step 1: Initiating login with client_id...")
    try:
        r1 = _post(f"{_BASE}/send_login_otp", {
            "fy_id" : config.FYERS_CLIENT_ID,
            "app_id": "2",   # 2 = TOTP method (not SMS OTP)
        })
    except Exception as e:
        logger.error(f"Login step 1 failed: {e}")
        return None

    if r1.get('code') != 200 and r1.get('s') != 'ok':
        logger.error(f"Login step 1 rejected: {r1}")
        return None

    request_key = r1.get('request_key') or r1.get('data', {}).get('request_key')
    if not request_key:
        logger.error(f"No request_key in step 1 response: {r1}")
        return None

    # ── Step 2: TOTP verification ─────────────────────────────────────────────
    logger.info("Step 2: Verifying TOTP...")
    totp_code = pyotp.TOTP(totp_key).now()
    try:
        r2 = _post(f"{_BASE}/verify_otp", {
            "request_key": request_key,
            "otp"        : totp_code,
        })
    except Exception as e:
        logger.error(f"TOTP step failed: {e}")
        return None

    if r2.get('code') != 200 and r2.get('s') != 'ok':
        logger.error(f"TOTP rejected: {r2}")
        return None

    request_key2 = r2.get('request_key') or r2.get('data', {}).get('request_key')
    if not request_key2:
        logger.error(f"No request_key in step 2 response: {r2}")
        return None

    # ── Step 3: PIN verification ──────────────────────────────────────────────
    logger.info("Step 3: Verifying PIN...")
    try:
        r3 = _post(f"{_BASE}/verify_pin", {
            "request_key"  : request_key2,
            "identity_type": "pin",
            "identifier"   : pin,
        })
    except Exception as e:
        logger.error(f"PIN step failed: {e}")
        return None

    if r3.get('code') != 200 and r3.get('s') != 'ok':
        logger.error(f"PIN rejected: {r3}")
        return None

    temp_token = r3.get('data', {}).get('access_token') or r3.get('access_token')
    if not temp_token:
        logger.error(f"No temp access_token in step 3 response: {r3}")
        return None

    # ── Step 4: Exchange for API access token ─────────────────────────────────
    logger.info("Step 4: Generating API access token...")
    try:
        session = fyersModel.SessionModel(
            client_id   = config.FYERS_APP_ID,
            secret_key  = config.FYERS_SECRET_KEY,
            redirect_uri= config.REDIRECT_URI,
            response_type="code",
            grant_type  = "authorization_code",
        )

        # Generate auth URL and extract auth_code using temp_token
        auth_url = session.generate_authcode()

        # Hit the auth URL with the temp token to get the auth code
        r4 = requests.get(
            auth_url,
            headers={"Authorization": f"Bearer {temp_token}"},
            allow_redirects=False,
            timeout=15,
        )
        # Extract auth_code from redirect location
        location = r4.headers.get('Location', '')
        if 'auth_code=' not in location:
            # Try POST approach
            r4b = requests.post(
                f"{_TOKEN}/token",
                json={
                    "grant_type"  : "authorization_code",
                    "appIdHash"   : config.FYERS_APP_ID,
                    "code"        : temp_token,
                    "redirect_uri": config.REDIRECT_URI,
                },
                timeout=15,
            )
            data = r4b.json()
        else:
            auth_code = location.split('auth_code=')[1].split('&')[0]
            session.set_token(auth_code)
            data = session.generate_token()

        if data.get('code') == 200 or data.get('s') == 'ok':
            access_token = data['access_token']
            _save_token(access_token)
            logger.info("Auto-auth successful. Token saved.")
            return access_token
        else:
            logger.error(f"Token exchange failed: {data}")
            return None

    except Exception as e:
        logger.error(f"Token exchange error: {e}")
        return None


def _save_token(token: str) -> None:
    """Save token + timestamp to logs/token.txt (same format as fyers_auth.py)."""
    os.makedirs(config.LOG_DIRECTORY, exist_ok=True)
    path = os.path.join(config.LOG_DIRECTORY, 'token.txt')
    with open(path, 'w') as f:
        f.write(token + '\n')
        f.write(datetime.now().isoformat())
    logger.info(f"Token saved to {path}")


def token_is_valid() -> bool:
    """Return True if a same-day token already exists."""
    path = os.path.join(config.LOG_DIRECTORY, 'token.txt')
    if not os.path.exists(path):
        return False
    with open(path) as f:
        lines = f.read().strip().split('\n')
    if len(lines) < 2:
        return False
    try:
        token_date = datetime.fromisoformat(lines[1]).date()
        return token_date == datetime.now(IST).date()
    except ValueError:
        return False


if __name__ == "__main__":
    if '--check' in sys.argv:
        valid = token_is_valid()
        print(f"Token valid: {valid}")
        sys.exit(0 if valid else 1)

    if token_is_valid():
        print("Valid token already exists for today. Skipping auth.")
        sys.exit(0)

    print("Starting automated Fyers authentication...")
    token = auto_authenticate()
    if token:
        print("Authentication successful.")
        sys.exit(0)
    else:
        print("Auto-auth failed. Run fyers_auth.py for manual auth.")
        sys.exit(1)
