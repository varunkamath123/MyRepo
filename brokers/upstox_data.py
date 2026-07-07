"""
Upstox data layer for Kronos Futures Bot.
Fetches 5-min OHLCV (index) and live LTP via Upstox REST API v2.
Token read from logs/upstox_token.txt — run brokers/upstox_auth.py first.
"""
from __future__ import annotations
import logging
import pandas as pd
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)

# Indian Standard Time — the market's timezone. The server may run on UTC, so
# "today" for session-completeness must be computed in IST, not server-local.
IST = timezone(timedelta(hours=5, minutes=30))

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


def load_daily_ohlcv(instrument: str, bars: int = 200) -> pd.DataFrame:
    """
    Fetch last `bars` COMPLETE daily candles for the index via Upstox historical API.

    Excludes any partial current-day bar, so the returned frame is always
    complete bars through the last closed session — the correct context for a
    daily-candle Kronos signal generated during market hours.
    """
    key = INDEX_KEYS[instrument]
    encoded_key = quote(key, safe="")

    now = datetime.now()
    # Daily bars: fetch generously (weekends/holidays reduce count)
    days_back = int(bars * 1.6) + 10
    from_date = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")
    to_date = now.strftime("%Y-%m-%d")

    url = f"{_BASE}/historical-candle/{encoded_key}/day/{to_date}/{from_date}"
    resp = requests.get(url, headers=_headers(), timeout=15)

    if resp.status_code != 200:
        raise RuntimeError(f"[UPSTOX] daily history error for {instrument}: {resp.text}")

    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"[UPSTOX] daily history failed for {instrument}: {data}")

    candles = data["data"]["candles"]  # [[ts, o, h, l, c, v, oi], ...]
    if not candles:
        raise RuntimeError(f"[UPSTOX] no daily candles returned for {instrument}")

    df = pd.DataFrame(candles, columns=["datetime", "open", "high", "low", "close", "volume", "oi"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    # Upstox timestamps are tz-aware (UTC+05:30); strip tz for consistent comparison
    if getattr(df["datetime"].dt, "tz", None) is not None:
        df["datetime"] = df["datetime"].dt.tz_localize(None)
    df = df.drop(columns=["oi"])
    df = df.sort_values("datetime").reset_index(drop=True)

    # Drop today's partial bar if market is still open (only keep complete sessions).
    # "Today" must be evaluated in IST — the server may be on UTC, which would
    # otherwise discard the most recent complete NSE session.
    today = pd.Timestamp(datetime.now(IST).date())
    df = df[df["datetime"].dt.normalize() < today].reset_index(drop=True)

    df = df.tail(bars).reset_index(drop=True)
    log.debug("[UPSTOX] Loaded %d complete daily bars for %s", len(df), instrument)
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
