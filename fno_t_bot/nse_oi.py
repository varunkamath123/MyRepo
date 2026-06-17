# -*- coding: utf-8 -*-
"""
NSE OI Fetcher — Shared Module
================================
Fetches option chain data from NSE public website via jugaad-data.
No authentication required. Works for NIFTY and BANKNIFTY.

SENSEX is BSE-listed (not NSE) — returns None for SENSEX; callers
should fall back to Fyers optionchain or skip OI context.

Requirements
------------
  pip install jugaad-data

Two public interfaces
---------------------
  fetch_chain(instrument, expiry_index=0)
      Returns the same dict structure as oi_levels.fetch_chain():
        spot, expiry, all_expiries, pcr, max_pain, atm_iv, iv_skew,
        total_call_oi, total_put_oi, df (per-strike DataFrame)
      Used by: oi_levels.py (display), oi_zones_eod.py (EOD save)

  get_oc_context(instrument, underlying_price)
      Returns paper_bot-compatible OC context dict:
        pcr, max_pain, atm_iv, iv_skew, oi_bias, strikes
      Results are cached per instrument for _CACHE_TTL seconds so the
      1-minute paper_bot loop does not hammer NSE on every bar.
      Used by: paper_bot.py (live per-bar context)

Caching
-------
  Module-level dict keyed by instrument. TTL = 5 minutes.
  Cache is bypassed when force=True is passed.
"""

from __future__ import annotations   # dict | None syntax on Python 3.9

import json
import logging
import time
import datetime as dt
import warnings
import pandas as pd

warnings.filterwarnings('ignore')

_log = logging.getLogger(__name__)

# ── Instrument mapping ────────────────────────────────────────────────────────
# jugaad-data uses the same symbol names as NSE website
_NSE_SUPPORTED = {'NIFTY', 'BANKNIFTY'}   # SENSEX is BSE — not supported

_CACHE_TTL = 300   # seconds — refresh NSE data at most once per 5 min

# Cache: {instrument: {'ts': float, 'chain': dict, 'ctx': dict}}
_cache: dict = {}

# ── Intraday OI snapshot buffer ────────────────────────────────────────────────
# Stores PCR/MaxPain/spot snapshots captured each time fetch_chain() makes a
# fresh NSE call (i.e. when cache expires, ~every 5 min during market hours).
# Used by get_pcr_drift() to compute directional momentum in the options market.
# Ring-buffered: keeps last _MAX_SNAPSHOTS records (≈ 25 hours at 5-min cadence).

_snapshots: list[dict] = []   # [{ts, instrument, pcr, max_pain, spot, atm_iv, iv_skew}]
_MAX_SNAPSHOTS  = 300         # 300 × 5 min = 25 h

_OI_HISTORY_FILE: str | None = None   # set by set_history_path() at bot startup


# ── Snapshot buffer helpers ───────────────────────────────────────────────────

def set_history_path(path: str) -> None:
    """Configure the JSONL file where intraday OI snapshots are appended.

    Call once at bot startup:
        nse_oi.set_history_path('/opt/trading_bot/live_bot/logs/oi_intraday.jsonl')

    Each fresh NSE fetch appends one JSON line with timestamp, instrument,
    PCR, MaxPain, spot, ATM-IV, IV-skew.  Used for post-session analysis and
    for building a multi-day PCR drift database.
    """
    global _OI_HISTORY_FILE
    _OI_HISTORY_FILE = path


def get_pcr_drift(instrument: str, lookback_mins: int = 30) -> float | None:
    """Return PCR change over the last *lookback_mins* minutes.

    Positive = PCR rising (PUT buyers increasing → bearish momentum).
    Negative = PCR falling (CALL buyers increasing → bullish momentum).

    Returns None when fewer than 2 snapshots exist in the window
    (e.g. first 30 min of the day, or NSE timeouts).
    """
    now     = time.time()
    cutoff  = now - lookback_mins * 60
    recent  = [s for s in _snapshots
               if s.get('instrument') == instrument.upper()
               and s.get('ts', 0) >= cutoff
               and s.get('pcr') is not None]
    if len(recent) < 2:
        return None
    return round(recent[-1]['pcr'] - recent[0]['pcr'], 4)


def _log_snapshot(chain: dict) -> None:
    """Append a fresh-fetch snapshot to the in-memory buffer and JSONL file."""
    global _snapshots
    record = {
        'ts'        : time.time(),
        'ts_str'    : dt.datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        'instrument': chain.get('instrument', '?'),
        'pcr'       : chain.get('pcr'),
        'max_pain'  : chain.get('max_pain'),
        'spot'      : chain.get('spot'),
        'atm_iv'    : chain.get('atm_iv'),
        'iv_skew'   : chain.get('iv_skew'),
    }
    _snapshots.append(record)
    if len(_snapshots) > _MAX_SNAPSHOTS:
        _snapshots = _snapshots[-_MAX_SNAPSHOTS:]
    if _OI_HISTORY_FILE:
        try:
            with open(_OI_HISTORY_FILE, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record) + '\n')
        except Exception as _e:
            _log.debug(f"[nse_oi] history write failed: {_e}")


# ── jugaad-data singleton ─────────────────────────────────────────────────────

_nse_live = None

def _get_nse():
    """Return a cached NSELive instance (import is slow first time)."""
    global _nse_live
    if _nse_live is None:
        from jugaad_data.nse import NSELive   # type: ignore
        _nse_live = NSELive()
    return _nse_live


# ── Core fetch ────────────────────────────────────────────────────────────────

def fetch_chain(instrument: str,
                expiry_index: int = 0,
                force: bool = False) -> dict | None:
    """
    Fetch and parse NSE option chain for NIFTY or BANKNIFTY.

    Parameters
    ----------
    instrument   : 'NIFTY' or 'BANKNIFTY'
    expiry_index : 0 = nearest expiry, 1 = next, etc.
    force        : bypass cache even if fresh

    Returns
    -------
    dict with keys:
      instrument, spot, expiry, all_expiries,
      pcr, max_pain, atm_iv, iv_skew,
      total_call_oi, total_put_oi, df
    or None on error / unsupported instrument.
    """
    instrument = instrument.upper()
    if instrument not in _NSE_SUPPORTED:
        _log.warning(f"[nse_oi] {instrument} is not on NSE — skipping NSE fetch.")
        return None

    # Cache check
    cached = _cache.get(instrument, {})
    if not force and cached.get('chain') and (time.time() - cached.get('ts', 0)) < _CACHE_TTL:
        return cached['chain']

    try:
        nse  = _get_nse()
        raw  = nse.equities_option_chain(instrument)
    except Exception as e:
        _log.warning(f"[nse_oi] NSELive fetch failed for {instrument}: {e}")
        return None

    records = raw.get('records', {})
    spot    = records.get('underlyingValue')
    expiry_dates = records.get('expiryDates', [])
    chain_data   = records.get('data', [])

    if not spot or not chain_data:
        _log.warning(f"[nse_oi] Empty chain for {instrument} (market closed or NSE blocked)")
        return None

    spot  = float(spot)
    all_expiries = expiry_dates

    # Select expiry
    chosen_expiry = expiry_dates[min(expiry_index, len(expiry_dates) - 1)]

    # jugaad-data returns only the nearest expiry's rows in records.data
    # (no per-row 'expiryDate' field — the chosen_expiry is cosmetic only)
    rows: dict = {}
    for row in chain_data:
        k = float(row.get('strikePrice', 0))
        rows.setdefault(k, {
            'strike'      : k,
            'call_oi'     : 0.0, 'call_oi_chg': 0.0,
            'call_vol'    : 0.0, 'call_iv'    : 0.0, 'call_ltp': 0.0,
            'put_oi'      : 0.0, 'put_oi_chg' : 0.0,
            'put_vol'     : 0.0, 'put_iv'     : 0.0, 'put_ltp' : 0.0,
        })

        ce = row.get('CE', {})
        pe = row.get('PE', {})

        if ce:
            rows[k]['call_oi']     = float(ce.get('openInterest',       0) or 0)
            rows[k]['call_oi_chg'] = float(ce.get('changeinOpenInterest', 0) or 0)
            rows[k]['call_vol']    = float(ce.get('totalTradedVolume',   0) or 0)
            rows[k]['call_iv']     = float(ce.get('impliedVolatility',   0) or 0)
            rows[k]['call_ltp']    = float(ce.get('lastPrice',           0) or 0)
        if pe:
            rows[k]['put_oi']      = float(pe.get('openInterest',        0) or 0)
            rows[k]['put_oi_chg']  = float(pe.get('changeinOpenInterest', 0) or 0)
            rows[k]['put_vol']     = float(pe.get('totalTradedVolume',   0) or 0)
            rows[k]['put_iv']      = float(pe.get('impliedVolatility',   0) or 0)
            rows[k]['put_ltp']     = float(pe.get('lastPrice',           0) or 0)

    if not rows:
        _log.warning(f"[nse_oi] No strikes parsed for {instrument} expiry {chosen_expiry}")
        return None

    df = (pd.DataFrame(list(rows.values()))
            .sort_values('strike')
            .reset_index(drop=True))

    total_call_oi = df['call_oi'].sum()
    total_put_oi  = df['put_oi'].sum()
    pcr = round(total_put_oi / total_call_oi, 3) if total_call_oi else float('nan')

    # Max pain: strike minimising total option buyer notional loss
    max_pain = _compute_max_pain(df, spot)

    # ATM IV and IV skew (put_iv − call_iv at ATM: positive = fear premium)
    atm_iv, iv_skew = _compute_atm_iv(df, spot)

    result = {
        'instrument'    : instrument,
        'spot'          : spot,
        'expiry'        : chosen_expiry,
        'all_expiries'  : all_expiries,
        'pcr'           : pcr,
        'max_pain'      : max_pain,
        'atm_iv'        : atm_iv,
        'iv_skew'       : iv_skew,
        'total_call_oi' : total_call_oi,
        'total_put_oi'  : total_put_oi,
        'df'            : df,
    }

    _cache[instrument] = {'ts': time.time(), 'chain': result, 'ctx': None}
    _log_snapshot(result)   # append to intraday buffer + JSONL (PCR drift analysis)
    return result


# ── paper_bot context interface ───────────────────────────────────────────────

def get_oc_context(instrument: str,
                   underlying_price: float,
                   force: bool = False) -> dict:
    """
    Return option-chain context dict for paper_bot.py.

    Compatible with the existing get_option_chain_context() return format:
      pcr      : float | None
      max_pain : float | None
      atm_iv   : float | None   (%, e.g. 12.5)
      iv_skew  : float | None   (put_atm_iv - call_atm_iv; positive = fear)
      oi_bias  : 'bullish' | 'bearish' | 'neutral'
      strikes  : {strike_px: {'call_oi', 'call_iv', 'put_oi', 'put_iv'}}

    Results cached for _CACHE_TTL seconds to avoid hitting NSE every minute.
    """
    empty = {'pcr': None, 'max_pain': None, 'atm_iv': None,
             'iv_skew': None, 'oi_bias': 'neutral', 'strikes': {}}

    # Cache check — reuse ctx if chain is still fresh
    cached = _cache.get(instrument, {})
    if (not force
            and cached.get('ctx')
            and (time.time() - cached.get('ts', 0)) < _CACHE_TTL):
        return cached['ctx']

    chain = fetch_chain(instrument, expiry_index=0, force=force)
    if chain is None:
        return empty

    df       = chain['df']
    pcr      = chain['pcr']
    max_pain = chain['max_pain']
    atm_iv   = chain['atm_iv']
    iv_skew  = chain['iv_skew']

    # OI bias from PCR
    if pcr is not None and not pd.isna(pcr):
        if pcr > 1.2:
            oi_bias = 'bullish'
        elif pcr < 0.7:
            oi_bias = 'bearish'
        else:
            oi_bias = 'neutral'
    else:
        oi_bias = 'neutral'

    # Per-strike dict for challenger strike selection
    strikes: dict = {}
    for _, row in df.iterrows():
        k = int(row['strike'])
        strikes[k] = {
            'call_oi' : int(row['call_oi']),
            'call_iv' : float(row['call_iv']),
            'put_oi'  : int(row['put_oi']),
            'put_iv'  : float(row['put_iv']),
        }

    ctx = {
        'pcr'      : round(pcr, 2) if pcr and not pd.isna(pcr) else None,
        'max_pain' : max_pain,
        'atm_iv'   : atm_iv,
        'iv_skew'  : iv_skew,
        'oi_bias'  : oi_bias,
        'strikes'  : strikes,
    }

    _cache[instrument]['ctx'] = ctx
    return ctx


# ── Helpers ───────────────────────────────────────────────────────────────────

def _compute_max_pain(df: pd.DataFrame, spot: float) -> float:
    """
    Strike where total notional loss to option BUYERS is maximised.
    = strike option WRITERS are most incentivised to pin price toward.
    """
    strikes = df['strike'].values
    call_oi = df['call_oi'].values
    put_oi  = df['put_oi'].values
    pain    = {}

    for k in strikes:
        call_loss = sum((k - s) * oi for s, oi in zip(strikes, call_oi) if s < k)
        put_loss  = sum((s - k) * oi for s, oi in zip(strikes, put_oi)  if s > k)
        pain[k]   = call_loss + put_loss

    return float(min(pain, key=pain.get)) if pain else spot


def _compute_atm_iv(df: pd.DataFrame, spot: float) -> tuple[float | None, float | None]:
    """
    Return (atm_iv, iv_skew) where:
      atm_iv  = average of ATM call_iv and put_iv (nearest-to-spot strike)
      iv_skew = put_iv − call_iv at ATM (positive = fear / put premium)
    """
    if df.empty:
        return None, None

    # Nearest strike to spot
    atm_row = df.iloc[(df['strike'] - spot).abs().argsort()[:1]]
    if atm_row.empty:
        return None, None

    call_iv = float(atm_row.iloc[0]['call_iv'])
    put_iv  = float(atm_row.iloc[0]['put_iv'])

    if call_iv == 0 and put_iv == 0:
        return None, None

    atm_iv  = round((call_iv + put_iv) / 2, 2) if (call_iv and put_iv) else round(call_iv or put_iv, 2)
    iv_skew = round(put_iv - call_iv, 2) if (call_iv and put_iv) else None

    return atm_iv, iv_skew


# ── Convenience ───────────────────────────────────────────────────────────────

def cache_age(instrument: str) -> str:
    """Return human-readable age of cached data for an instrument."""
    cached = _cache.get(instrument, {})
    ts = cached.get('ts')
    if not ts:
        return 'no cache'
    age = int(time.time() - ts)
    return f"{age}s ago"
