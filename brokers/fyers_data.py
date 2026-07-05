"""
Fyers data layer for Kronos Futures Bot.
Fetches 5-min OHLCV (index) and live LTP (futures contract) via fyers_apiv3.
Completely separate token from options bot — stored in logs/fyers_token.txt.
"""
from __future__ import annotations
import os
import time
import logging
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

# ── Fyers index symbols (for OHLCV / Kronos input) ───────────────────────────
INDEX_SYMBOLS = {
    "NIFTY":     "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
    "SENSEX":    "BSE:SENSEX-INDEX",
}

# ── Month codes for futures symbol construction ───────────────────────────────
_MONTH_CODES = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR",
    5: "MAY", 6: "JUN", 7: "JUL", 8: "AUG",
    9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
}

_TOKEN_PATH = Path(__file__).parent.parent / "logs" / "fyers_token.txt"
_fyers_client = None


def get_futures_symbol(instrument: str) -> str:
    """Return current-month futures symbol, e.g. NSE:NIFTY25JULFUT."""
    now = datetime.now()
    yy = str(now.year)[-2:]
    mon = _MONTH_CODES[now.month]
    if instrument == "SENSEX":
        return f"BSE:SENSEX{yy}{mon}FUT"
    return f"NSE:{instrument}{yy}{mon}FUT"


def get_fyers_client():
    """Return a cached FyersModel using the stored access token."""
    global _fyers_client
    if _fyers_client is not None:
        return _fyers_client

    token_file = _TOKEN_PATH
    if not token_file.exists():
        raise FileNotFoundError(
            f"Fyers token not found at {token_file}. "
            "Run brokers/fyers_auth_kronos.py --auth first."
        )

    access_token = token_file.read_text().strip()
    client_id = os.environ.get("FYERS_APP_ID", "")
    if not client_id:
        raise ValueError("FYERS_APP_ID not set in environment / .env")

    try:
        from fyers_apiv3 import fyersModel
        _fyers_client = fyersModel.FyersModel(
            client_id=client_id,
            token=access_token,
            log_path="",
            is_async=False,
        )
        log.info("[FYERS] Client initialised.")
    except ImportError:
        raise ImportError("fyers_apiv3 not installed. Run: pip install fyers-apiv3")

    return _fyers_client


def load_ohlcv(instrument: str, bars: int = 300) -> pd.DataFrame:
    """
    Fetch the last `bars` 5-min candles for the given instrument index.
    Returns DataFrame with columns: datetime, open, high, low, close, volume.
    """
    fyers = get_fyers_client()
    symbol = INDEX_SYMBOLS[instrument]

    now = datetime.now()
    # Fetch enough calendar days to cover `bars` trading bars (5 min each)
    # ~375 bars per trading day → 2 extra days buffer
    days_back = max(2, (bars // 370) + 2)
    start_dt = now - timedelta(days=days_back)

    params = {
        "symbol": symbol,
        "resolution": "5",
        "date_format": "1",   # human-readable YYYY-MM-DD
        "range_from": start_dt.strftime("%Y-%m-%d"),
        "range_to": now.strftime("%Y-%m-%d"),
        "cont_flag": "1",
    }

    resp = fyers.history(params)
    if resp.get("s") != "ok":
        raise RuntimeError(f"[FYERS] history error for {instrument}: {resp}")

    candles = resp["candles"]   # [[epoch, o, h, l, c, v], ...]
    df = pd.DataFrame(candles, columns=["epoch", "open", "high", "low", "close", "volume"])
    df["datetime"] = pd.to_datetime(df["epoch"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    df = df.drop(columns=["epoch"]).set_index("datetime").reset_index()
    df = df.tail(bars).reset_index(drop=True)

    log.debug("[FYERS] Loaded %d bars for %s", len(df), instrument)
    return df


def get_ltp(instrument: str) -> float:
    """Return latest traded price of the current-month futures contract."""
    fyers = get_fyers_client()
    symbol = get_futures_symbol(instrument)
    resp = fyers.quotes({"symbols": symbol})
    if resp.get("s") != "ok":
        raise RuntimeError(f"[FYERS] quotes error for {symbol}: {resp}")
    ltp = resp["d"][0]["v"]["lp"]
    log.debug("[FYERS] LTP %s = %.2f", symbol, ltp)
    return float(ltp)
