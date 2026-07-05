"""
Fyers auth for Kronos Futures Bot — completely separate from the options bot.
Token stored at logs/fyers_token.txt.

Usage:
  python brokers/fyers_auth_kronos.py --auth        # first-time / manual auth
  python brokers/fyers_auth_kronos.py               # refresh (called by systemd timer)
"""
from __future__ import annotations
import os
import sys
import json
import logging
import hashlib
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_TOKEN_PATH = Path(__file__).parent.parent / "logs" / "fyers_token.txt"
_AUTH_CODE_PATH = Path(__file__).parent.parent / "logs" / "fyers_auth_code.txt"


def _load_env() -> tuple[str, str, str]:
    """Load FYERS_CLIENT_ID, FYERS_SECRET_KEY, FYERS_REDIRECT_URI from env."""
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
    client_id = os.environ["FYERS_CLIENT_ID"]
    secret_key = os.environ["FYERS_SECRET_KEY"]
    redirect_uri = os.environ.get("FYERS_REDIRECT_URI", "https://trade.fyers.in/api-login/redirect-uri/index.html")
    return client_id, secret_key, redirect_uri


def generate_auth_url() -> str:
    """Print the URL the user must open to authorise."""
    client_id, _, redirect_uri = _load_env()
    try:
        from fyers_apiv3 import fyersModel
        session = fyersModel.SessionModel(
            client_id=client_id,
            secret_key="",         # not needed for URL gen
            redirect_uri=redirect_uri,
            response_type="code",
            grant_type="authorization_code",
        )
        return session.generate_authcode()
    except Exception:
        # Manual fallback
        params = urllib.parse.urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": "kronos",
        })
        return f"https://api.fyers.in/api/v2/generate-authcode?{params}"


def exchange_code_for_token(auth_code: str) -> str:
    """Exchange auth code for access token and save to file."""
    client_id, secret_key, redirect_uri = _load_env()
    checksum = hashlib.sha256(f"{auth_code}:{secret_key}".encode()).hexdigest()

    from fyers_apiv3 import fyersModel
    session = fyersModel.SessionModel(
        client_id=client_id,
        secret_key=secret_key,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code",
    )
    session.set_token(auth_code)
    resp = session.generate_token()

    if resp.get("s") != "ok":
        raise RuntimeError(f"Token exchange failed: {resp}")

    access_token = resp["access_token"]
    _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_PATH.write_text(access_token)
    log.info("[FYERS-AUTH] Token saved to %s", _TOKEN_PATH)
    return access_token


def refresh_token_from_saved_code() -> None:
    """Called by systemd timer: reads saved auth_code and re-exchanges for fresh token."""
    if not _AUTH_CODE_PATH.exists():
        log.error("[FYERS-AUTH] No saved auth code at %s — run --auth first", _AUTH_CODE_PATH)
        sys.exit(1)
    auth_code = _AUTH_CODE_PATH.read_text().strip()
    exchange_code_for_token(auth_code)
    log.info("[FYERS-AUTH] Token refreshed at %s", datetime.now().isoformat())


if __name__ == "__main__":
    if "--auth" in sys.argv:
        url = generate_auth_url()
        print("\n" + "="*60)
        print("Open this URL in a browser and authorise:")
        print(url)
        print("="*60)
        auth_code = input("\nPaste the auth_code from the redirect URL: ").strip()
        _AUTH_CODE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _AUTH_CODE_PATH.write_text(auth_code)
        token = exchange_code_for_token(auth_code)
        print(f"\nToken saved. First 20 chars: {token[:20]}...")
    else:
        refresh_token_from_saved_code()
