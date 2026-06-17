"""
sgx_nifty.py — Pre-market GIFT/SGX Nifty gap fetcher  (Phase 1: logging only)

Fetches live GIFT Nifty price from sgxnifty.org before market open.
The page reports change% vs previous close, so we don't need a separate
prev-close lookup — the exchange already computes it.

Phase 1 (current): fetch at bot startup, log alongside every signal.
Phase 2 (after 30 days): use gap metrics to adjust lot sizing and ORB gating.

Usage (from other modules):
    import sgx_nifty
    ctx = sgx_nifty.fetch_and_analyze(logger)   # returns dict or None
    shared_state.set_sgx_context(ctx)
"""
from __future__ import annotations

import re
import logging
from datetime import datetime

import requests
import pytz

IST = pytz.timezone('Asia/Kolkata')

# ─── Constants ────────────────────────────────────────────────────────────────

WIDE_GAP_THRESHOLD   = 1.5   # % absolute — ORB reliability drops sharply above this
STRONG_GAP_THRESHOLD = 1.0   # % absolute — noteworthy directional bias

_SOURCES = [
    'https://sgxnifty.org/',
    'https://giftnifty.org/',
]
_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/122.0.0.0 Safari/537.36'
    )
}

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    """Try multiple patterns to extract GIFT Nifty price from page HTML."""
    patterns = [
        r'[Ll]ast\s*[Tt]rade[^0-9]{0,20}([2-3][0-9]{4}(?:,[0-9]{3})*(?:\.[0-9]+)?)',
        r'"lastPrice"\s*:\s*"?([2-3][0-9]{4}(?:,[0-9]{3})*(?:\.[0-9]+)?)',
        r'class="[^"]*price[^"]*"[^>]*>\s*([2-3][0-9]{4}(?:,[0-9]{3})*(?:\.[0-9]+)?)',
        r'>([2-3][0-9]{4}(?:,[0-9]{3})*\.[0-9]+)<',   # 5-digit with decimal in NIFTY range
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            try:
                val = float(m.group(1).replace(',', ''))
                if 15_000 <= val <= 40_000:   # NIFTY plausible range sanity check
                    return val
            except ValueError:
                continue
    return None


def _parse_change_pct(text: str) -> float | None:
    """Extract change % from page HTML.  Looks for patterns like '+3.07%' or '-1.2%'."""
    patterns = [
        r'([+-][0-9]{1,2}\.[0-9]{1,2})%',
        r'change[^0-9-+]{0,30}([+-][0-9]{1,2}\.[0-9]{1,2})',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None

# ─── Public API ───────────────────────────────────────────────────────────────

def fetch_and_analyze(logger: logging.Logger | None = None) -> dict | None:
    """
    Fetch live GIFT/SGX Nifty data and return a gap-analysis dict.

    Returns:
        {
          'price'      : float,
          'change_pct' : float,       # % vs previous close (from exchange)
          'direction'  : 'UP'|'DOWN'|'FLAT',
          'wide_gap'   : bool,        # abs(change_pct) >= 1.5%
          'strong_gap' : bool,        # abs(change_pct) >= 1.0%
          'fetched_at' : str,         # 'HH:MM IST'
          'source'     : str,
        }
    or None on complete failure.
    """
    for url in _SOURCES:
        try:
            resp = requests.get(url, timeout=8, headers=_HEADERS)
            resp.raise_for_status()
            text = resp.text

            price      = _parse_price(text)
            change_pct = _parse_change_pct(text)

            if price is None and change_pct is None:
                if logger:
                    logger.warning(f'[SGX] Could not parse data from {url}')
                continue

            # Derive direction from change_pct (or assume FLAT if both missing)
            pct = change_pct or 0.0
            direction  = 'UP'   if pct >  0.10 else \
                         'DOWN' if pct < -0.10 else 'FLAT'
            wide_gap   = abs(pct) >= WIDE_GAP_THRESHOLD
            strong_gap = abs(pct) >= STRONG_GAP_THRESHOLD

            ctx = {
                'price'      : price,
                'change_pct' : change_pct,
                'direction'  : direction,
                'wide_gap'   : wide_gap,
                'strong_gap' : strong_gap,
                'fetched_at' : datetime.now(IST).strftime('%H:%M IST'),
                'source'     : url,
            }

            if logger:
                flag = ' ⚡ WIDE GAP' if wide_gap else (' ↑↓ STRONG' if strong_gap else '')
                logger.info(
                    f'[SGX] GIFT Nifty: '
                    f'{price:,.1f} | {pct:+.2f}% | {direction}{flag} '
                    f'@ {ctx["fetched_at"]}'
                )
                if wide_gap:
                    logger.info(
                        '[SGX] Wide gap day (>1.5%) — ORB reliability reduced; '
                        'OI Level Breakout is primary early signal.'
                    )
            return ctx

        except Exception as exc:
            if logger:
                logger.warning(f'[SGX] {url} — fetch error: {exc}')
            continue

    if logger:
        logger.warning('[SGX] All sources failed — no pre-market gap context.')
    return None


def log_signal_alignment(ctx: dict | None, signal_type: str,
                         logger: logging.Logger | None = None) -> None:
    """
    Log whether a fired signal aligns with or counters the SGX gap.
    Call this at every signal entry for 30-day correlation capture.

    signal_type: 'CALL' (bullish) or 'PUT' (bearish)
    """
    if ctx is None or logger is None:
        return
    pct       = ctx.get('change_pct', 0.0) or 0.0
    direction = ctx.get('direction', 'UNKNOWN')
    gap_str   = f'{pct:+.2f}% ({direction})'
    wide_flag = ' [WIDE]' if ctx.get('wide_gap') else ''

    sgx_bull = direction == 'UP'
    sgx_bear = direction == 'DOWN'
    trade_bull = signal_type == 'CALL'

    aligned = (sgx_bull and trade_bull) or (sgx_bear and not trade_bull)
    tag = 'ALIGNED' if aligned else 'COUNTER-TREND'

    logger.info(
        f'  [SGX] Pre-market gap: {gap_str}{wide_flag} | '
        f'Signal: {signal_type} | Gap alignment: {tag}'
    )
