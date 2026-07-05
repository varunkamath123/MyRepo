"""
Fyers headless auth for Kronos Futures Bot.
Adapted from the options bot's fyers_direct_auth.py — same Fyers account,
separate token path (logs/fyers_token.txt).

Run:  python brokers/fyers_direct_auth_kronos.py
Called by systemd timer Mon–Fri at 08:45 IST.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib import parse

import pyotp
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

logger = logging.getLogger("fyers_direct_auth_kronos")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

TOKEN_PATH = Path(__file__).parent.parent / "logs" / "fyers_token.txt"

_VAGATOR = "https://api-t2.fyers.in/vagator/v2"
_API_V3  = "https://api-t1.fyers.in/api/v3"
URL_SEND_OTP   = _VAGATOR + "/send_login_otp"
URL_VERIFY_OTP = _VAGATOR + "/verify_otp"
URL_VERIFY_PIN = _VAGATOR + "/verify_pin"
URL_TOKEN      = _API_V3  + "/token"


def _creds() -> tuple[str, str, str, str, str, str]:
    app_id   = os.environ["FYERS_APP_ID"]
    secret   = os.environ["FYERS_SECRET_KEY"]
    fy_id    = os.environ["FYERS_LOGIN_ID"]
    pin      = os.environ["FYERS_PIN"]
    totp_key = os.environ["FYERS_TOTP_KEY"]
    redir    = os.environ.get("FYERS_REDIRECT_URI",
                              "https://trade.fyers.in/api-login/redirect-uri/index.html")
    return app_id, secret, fy_id, pin, totp_key, redir


def _save_token(token: str) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token + "\n" + datetime.now().isoformat())
    logger.info("Token saved → %s", TOKEN_PATH)


def token_is_valid() -> bool:
    import base64, json as _json, time as _time
    if not TOKEN_PATH.exists():
        return False
    lines = TOKEN_PATH.read_text().strip().split("\n")
    if not lines or not lines[0].strip():
        return False
    try:
        if len(lines) >= 2:
            from datetime import timezone
            from zoneinfo import ZoneInfo
            IST = ZoneInfo("Asia/Kolkata")
            token_date = datetime.fromisoformat(lines[1]).date()
            if token_date != datetime.now(IST).date():
                return False
        jwt = lines[0].strip()
        parts = jwt.split(".")
        if len(parts) != 3:
            return False
        payload = _json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        exp = payload.get("exp", 0)
        if _time.time() + 1800 > exp:
            return False
        return True
    except Exception as e:
        logger.warning("token_is_valid() error: %s", e)
        return False


def authenticate() -> bool:
    app_id, secret, fy_id, pin, totp_key, redir = _creds()
    app_id_part = app_id.rsplit("-", 1)[0] if "-" in app_id else app_id
    app_type    = app_id.rsplit("-", 1)[1] if "-" in app_id else "100"

    sess = requests.Session()

    logger.info("Step 1: Send login OTP for %s", fy_id)
    r = sess.post(URL_SEND_OTP, json={"fy_id": fy_id, "app_id": "2"})
    data = r.json()
    if r.status_code != 200 or "request_key" not in data:
        logger.error("Step 1 failed: %s", data)
        return False
    request_key = data["request_key"]

    logger.info("Step 2: Verify TOTP")
    totp_code = pyotp.TOTP(totp_key).now()
    r = sess.post(URL_VERIFY_OTP, json={"request_key": request_key, "otp": totp_code})
    data = r.json()
    if r.status_code != 200 or "request_key" not in data:
        logger.error("Step 2 failed: %s", data)
        return False
    request_key = data["request_key"]

    logger.info("Step 3: Verify PIN")
    r = sess.post(URL_VERIFY_PIN, json={
        "request_key": request_key, "identity_type": "pin",
        "identifier": pin, "recaptcha_token": "",
    })
    data = r.json()
    if r.status_code != 200 or "data" not in data:
        logger.error("Step 3 failed: %s", data)
        return False
    bearer = data["data"]["access_token"]

    logger.info("Step 4: Get auth_code")
    r = sess.post(URL_TOKEN, json={
        "fyers_id": fy_id, "app_id": app_id_part,
        "redirect_uri": redir, "appType": app_type,
        "code_challenge": "", "state": "kronos",
        "response_type": "code", "create_cookie": True,
    }, headers={"Authorization": f"Bearer {bearer}"})
    data = r.json()

    auth_code = None
    for key in ("Url", "url"):
        url_str = data.get(key, "")
        if url_str:
            try:
                auth_code = parse.parse_qs(parse.urlparse(url_str).query)["auth_code"][0]
            except (KeyError, IndexError):
                pass
    if not auth_code:
        auth_code = (data.get("data") or {}).get("auth_code") or data.get("auth_code")
    if not auth_code:
        logger.error("No auth_code in response: %s", data)
        return False

    logger.info("Step 5: Exchange auth_code for API token")
    from fyers_apiv3 import fyersModel
    session = fyersModel.SessionModel(
        client_id=app_id, secret_key=secret,
        redirect_uri=redir, response_type="code",
        grant_type="authorization_code",
    )
    session.set_token(auth_code)
    resp = session.generate_token()
    if resp.get("s") == "ok" or resp.get("code") == 200:
        _save_token(resp["access_token"])
        logger.info("Authentication successful.")
        return True
    logger.error("Token exchange failed: %s", resp)
    return False


if __name__ == "__main__":
    if token_is_valid():
        logger.info("Token still valid — skipping re-auth.")
        sys.exit(0)
    ok = authenticate()
    sys.exit(0 if ok else 1)
