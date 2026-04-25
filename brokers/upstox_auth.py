"""
Upstox authentication — direct auth flow (no browser needed on EC2).
Stores access token to logs/upstox_token.txt.
"""
from __future__ import annotations
import os
import json
import logging
import requests
from datetime import datetime
from pathlib import Path

from config import (
    UPSTOX_API_KEY, UPSTOX_API_SECRET, UPSTOX_REDIRECT_URI,
    UPSTOX_ACCESS_TOKEN_PATH,
)

log = logging.getLogger(__name__)

BASE_URL = "https://api.upstox.com/v2"


def get_access_token() -> str:
    """Load token from file if valid today, otherwise raise (requires manual auth step)."""
    token_path = Path(UPSTOX_ACCESS_TOKEN_PATH)
    if token_path.exists():
        data = json.loads(token_path.read_text())
        if data.get("date") == str(datetime.now().date()):
            return data["access_token"]
    raise RuntimeError(
        "Upstox token missing or stale. Run: python brokers/upstox_auth.py --auth"
    )


def save_token(access_token: str) -> None:
    path = Path(UPSTOX_ACCESS_TOKEN_PATH)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"access_token": access_token, "date": str(datetime.now().date())}))
    log.info("[AUTH] Token saved to %s", path)


def exchange_auth_code(auth_code: str) -> str:
    """Exchange authorization code for access token."""
    api_key = os.environ.get("UPSTOX_API_KEY", UPSTOX_API_KEY)
    api_secret = os.environ.get("UPSTOX_API_SECRET", UPSTOX_API_SECRET)

    resp = requests.post(
        f"{BASE_URL}/login/authorization/token",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        data={
            "code": auth_code,
            "client_id": api_key,
            "client_secret": api_secret,
            "redirect_uri": UPSTOX_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
    )
    resp.raise_for_status()
    token = resp.json()["access_token"]
    save_token(token)
    return token


if __name__ == "__main__":
    import sys
    if "--auth" in sys.argv:
        api_key = os.environ.get("UPSTOX_API_KEY", UPSTOX_API_KEY)
        auth_url = (
            f"https://api.upstox.com/v2/login/authorization/dialog"
            f"?client_id={api_key}&redirect_uri={UPSTOX_REDIRECT_URI}&response_type=code"
        )
        print(f"\nOpen this URL in browser:\n{auth_url}\n")
        code = input("Paste the 'code' param from redirect URL: ").strip()
        token = exchange_auth_code(code)
        print(f"Token saved. Auth complete.")
