"""Shared helpers for the intraday (10-minute) NIFTY / BANKNIFTY futures work: Upstox v3 candles and quotes, contract resolution, storage.

Storage layout (INTRADAY_DATA, default /opt/kronos_bot/data/intraday -- outside the git repo):
  candles/<label>_<N>m.csv.gz   one file per instrument and interval, columns ts,o,h,l,c,v,oi (ts = IST wall time of the bar START)
                                label = NIFTY_IDX / BANKNIFTY_IDX or the futures trading symbol (e.g. NIFTY26NOVFUT)
  depth/<YYYY-MM-DD>/<label>.jsonl.gz   quote snapshots with 5-level depth (see depth_recorder.py)
  clean/index_10m_<UNDERLYING>.csv.gz   validated, flagged index 10-minute bars (see build_dataset.py)
  contracts.csv                 daily log of which contract was near / next / far
  master/fut_<date>.json        cached NIFTY / BANKNIFTY futures from the Upstox instrument master
  logs/                         recorder logs
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DATA = Path(os.environ.get("INTRADAY_DATA", "/opt/kronos_bot/data/intraday"))
IST = timezone(timedelta(hours=5, minutes=30))
UNDERLYINGS = ("NIFTY", "BANKNIFTY")
INDEX_KEYS = {"NIFTY": "NSE_INDEX|Nifty 50", "BANKNIFTY": "NSE_INDEX|Nifty Bank"}
LOT_FALLBACK = {"NIFTY": 65, "BANKNIFTY": 30}
_BASE = "https://api.upstox.com"
COLS = ["ts", "o", "h", "l", "c", "v", "oi"]
SESSION_OPEN_MIN = 9 * 60 + 15            # 09:15


def now_ist() -> datetime:
    return datetime.now(IST)


def _headers() -> dict:
    from brokers.upstox_data import _headers as h
    return h()


def _get(url: str, retries: int = 3, timeout: int = 30) -> requests.Response:
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=_headers(), timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(1.5 * (i + 1)); last = r; continue
            return r
        except requests.RequestException as e:
            last = e; time.sleep(1.5 * (i + 1))
    if isinstance(last, requests.Response):
        return last
    raise RuntimeError(f"request failed after {retries} tries: {last}")


# ── contracts ────────────────────────────────────────────────────────────────

def load_contracts(today: date | None = None) -> list[dict]:
    """Every unexpired NIFTY / BANKNIFTY future in the Upstox master, sorted by expiry. Cached per day."""
    today = today or now_ist().date()
    cache = DATA / "master" / f"fut_{today.isoformat()}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    raw = requests.get("https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz", timeout=90).content
    rows = []
    for x in json.loads(gzip.decompress(raw)):
        if x.get("segment") != "NSE_FO" or x.get("instrument_type") != "FUT" or x.get("underlying_symbol") not in UNDERLYINGS:
            continue
        exp = datetime.fromtimestamp(x["expiry"] / 1000, IST).date()
        if exp < today:
            continue
        rows.append({"underlying": x["underlying_symbol"], "symbol": x["trading_symbol"], "key": x["instrument_key"],
                     "expiry": exp.isoformat(), "lot": int(x.get("lot_size") or LOT_FALLBACK[x["underlying_symbol"]])})
    rows.sort(key=lambda r: (r["underlying"], r["expiry"]))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(rows, indent=1))
    return rows


def ranked_contracts(today: date | None = None) -> dict[str, list[dict]]:
    """{underlying: [contract with rank 1 (near), 2 (next), 3 (far)]}"""
    out: dict[str, list[dict]] = {u: [] for u in UNDERLYINGS}
    for r in load_contracts(today):
        out[r["underlying"]].append(dict(r, rank=len(out[r["underlying"]]) + 1))
    return out


def targets(today: date | None = None, max_rank: int = 3) -> list[dict]:
    """Everything the candle recorder captures: each index plus its first `max_rank` futures."""
    t = []
    for u, cs in ranked_contracts(today).items():
        t.append({"label": f"{u}_IDX", "key": INDEX_KEYS[u], "kind": "index", "underlying": u, "rank": 0, "lot": LOT_FALLBACK[u]})
        for c in cs[:max_rank]:
            t.append({"label": c["symbol"], "key": c["key"], "kind": "future", "underlying": u, "rank": c["rank"], "lot": c["lot"],
                      "expiry": c["expiry"]})
    return t


# ── candles ──────────────────────────────────────────────────────────────────

def _frame(candles: list) -> pd.DataFrame:
    d = pd.DataFrame(candles, columns=COLS)
    if d.empty:
        return d
    d["ts"] = pd.to_datetime(d["ts"]).dt.tz_localize(None)        # keep the exchange wall time (+05:30 stripped)
    for c in ("o", "h", "l", "c"):
        d[c] = d[c].astype(float)
    d["v"] = d["v"].fillna(0).astype("int64"); d["oi"] = d["oi"].fillna(0).astype("int64")
    return d.sort_values("ts").reset_index(drop=True)


def candles_range(key: str, unit: str, interval: int, frm: date, to: date) -> pd.DataFrame:
    """One request of historical candles (inclusive dates). Minute candles: at most ~1 month per request."""
    url = f"{_BASE}/v3/historical-candle/{urllib.parse.quote(key, safe='')}/{unit}/{interval}/{to.isoformat()}/{frm.isoformat()}"
    r = _get(url)
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code} {r.text[:200]}")
    return _frame(r.json().get("data", {}).get("candles", []))


def candles_intraday(key: str, unit: str, interval: int) -> pd.DataFrame:
    """Today's candles so far (or the full session once it is over)."""
    url = f"{_BASE}/v3/historical-candle/intraday/{urllib.parse.quote(key, safe='')}/{unit}/{interval}"
    r = _get(url)
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code} {r.text[:200]}")
    return _frame(r.json().get("data", {}).get("candles", []))


def candles_chunked(key: str, unit: str, interval: int, frm: date, to: date, chunk_days: int = 27) -> pd.DataFrame:
    parts, hi = [], to
    while hi >= frm:
        lo = max(frm, hi - timedelta(days=chunk_days))
        d = candles_range(key, unit, interval, lo, hi)
        if len(d):
            parts.append(d)
        hi = lo - timedelta(days=1)
        time.sleep(0.15)
    return pd.concat(parts).drop_duplicates("ts").sort_values("ts").reset_index(drop=True) if parts else pd.DataFrame(columns=COLS)


# ── quotes ───────────────────────────────────────────────────────────────────

def quotes(keys: list[str]) -> dict[str, dict]:
    """{instrument_token: quote} -- the response is keyed by symbol, so re-key by the token inside it."""
    url = f"{_BASE}/v2/market-quote/quotes?instrument_key=" + ",".join(urllib.parse.quote(k, safe="") for k in keys)
    r = _get(url, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code} {r.text[:200]}")
    j = r.json()
    if j.get("status") != "success":
        raise RuntimeError(str(j)[:200])
    return {v["instrument_token"]: v for v in j["data"].values()}


# ── storage ──────────────────────────────────────────────────────────────────

def candle_path(label: str, interval: int) -> Path:
    return DATA / "candles" / f"{label}_{interval}m.csv.gz"


def merge_write(path: Path, df: pd.DataFrame) -> int:
    """Merge new bars into the file (dedupe on ts, newest wins), atomic rewrite. Returns total rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        old = pd.read_csv(path, parse_dates=["ts"])
        df = pd.concat([old, df])
    df = df.drop_duplicates("ts", keep="last").sort_values("ts").reset_index(drop=True)
    tmp = path.with_suffix(".tmp.gz")
    df.to_csv(tmp, index=False, compression="gzip")
    os.replace(tmp, path)
    return len(df)


def load_clean(underlying: str, usable_only: bool = True) -> pd.DataFrame:
    """The validated 10-minute index bars written by build_dataset.py (ts index; ret_bp = within-session close-to-close return)."""
    d = pd.read_csv(DATA / "clean" / f"index_10m_{underlying}.csv.gz", parse_dates=["ts"])
    return d[d["usable"]].reset_index(drop=True) if usable_only else d
