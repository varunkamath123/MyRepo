"""
Upstox data layer for Kronos Futures Bot.
Fetches 5-min OHLCV (index) and live LTP via Upstox REST API v2.
Token read from logs/upstox_token.txt — run brokers/upstox_auth.py first.
"""
from __future__ import annotations
import logging
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)

_BASE = "https://api.upstox.com/v2"
_TOKEN_PATH = Path(__file__).parent.parent / "logs" / "upstox_token.txt"

INDEX_KEYS = {
    "NIFTY":     "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX":    "BSE_INDEX|SENSEX",
}


def _get_token() -> str:
    if not _TOKEN_PATH.exists():
        raise FileNotFoundError(
            f"Upstox token not found at {_TOKEN_PATH}. "
            "Run brokers/upstox_auth.py first."
        )
    return _TOKEN_PATH.read_text().strip().split("\n")[0]


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_token()}",
        "Accept": "application/json",
    }


def load_ohlcv(instrument: str, bars: int = 300) -> pd.DataFrame:
    """Fetch last `bars` 5-min candles for the index via Upstox historical API."""
    key = INDEX_KEYS[instrument]
    encoded_key = quote(key, safe="")

    now = datetime.now()
    days_back = max(3, (bars // 370) + 2)
    from_date = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")
    to_date = now.strftime("%Y-%m-%d")

    url = f"{_BASE}/historical-candle/{encoded_key}/5minute/{to_date}/{from_date}"
    resp = requests.get(url, headers=_headers(), timeout=15)

    if resp.status_code != 200:
        raise RuntimeError(f"[UPSTOX] history error for {instrument}: {resp.text}")

    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"[UPSTOX] history failed for {instrument}: {data}")

    candles = data["data"]["candles"]  # [[ts, o, h, l, c, v, oi], ...]
    df = pd.DataFrame(candles, columns=["datetime", "open", "high", "low", "close", "volume", "oi"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.drop(columns=["oi"])
    df = df.sort_values("datetime").tail(bars).reset_index(drop=True)

    log.debug("[UPSTOX] Loaded %d bars for %s", len(df), instrument)
    return df


def get_ltp(instrument: str) -> float:
    """Return latest traded price of the index (used as futures proxy in paper mode)."""
    key = INDEX_KEYS[instrument]
    encoded_key = quote(key, safe="")

    url = f"{_BASE}/market-quote/quotes?instrument_key={encoded_key}"
    resp = requests.get(url, headers=_headers(), timeout=10)

    if resp.status_code != 200:
        raise RuntimeError(f"[UPSTOX] quotes error for {instrument}: {resp.text}")

    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"[UPSTOX] quotes failed for {instrument}: {data}")

    quote_data = list(data["data"].values())[0]
    ltp = float(quote_data["last_price"])
    log.debug("[UPSTOX] LTP %s = %.2f", instrument, ltp)
    return ltp
