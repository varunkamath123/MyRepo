# -*- coding: utf-8 -*-
"""
BSE OI Fetcher — SENSEX Option Chain via Fyers API
====================================================
Fetches SENSEX option chain data using the authenticated Fyers session
(same session the main bot uses for price data). No separate credentials
or scraping required.

BSE's public website API is IP-blocked for cloud/EC2 requests, so we
route through Fyers which already has BSE F&O data in its feed.

Fyers symbol for SENSEX optionchain: 'BSE:SENSEX-INDEX'
(Note: 'BSE:SENSEX' is rejected by Fyers optionchain endpoint)

The BSE Fyers response uses different field names from the NSE response:
  optionsChain  (with 's') instead of optionChain
  strike_price  (snake_case) instead of strikePrice
  oich          instead of oiChange
  No 'iv' field — atm_iv and iv_skew are not available from BSE feed.
  Top-level callOi / putOi provide total OI (used for PCR directly).

Public interface
----------------
  get_oc_context(fyers, underlying_price, force=False)
      Returns paper_bot-compatible OC context dict:
        pcr, max_pain, atm_iv, iv_skew, oi_bias, strikes
      Same format as nse_oi.get_oc_context() — drop-in replacement.
      atm_iv and iv_skew will be None (not in BSE Fyers feed).

Caching
-------
  Module-level dict. TTL = 5 minutes. Bypassed when force=True.
"""

from __future__ import annotations

import time

_CACHE_TTL = 300   # seconds — same as nse_oi

_cache: dict = {'ts': 0, 'ctx': None}

_SYMBOL = 'BSE:SENSEX-INDEX'   # correct Fyers optionchain symbol for SENSEX


# ── Public interface ───────────────────────────────────────────────────────────

def get_oc_context(fyers,
                   underlying_price: float,
                   force: bool = False) -> dict:
    """
    Return option-chain context dict for SENSEX (BSE-listed).

    Compatible with nse_oi.get_oc_context() return format:
      pcr      : float | None   (put_oi / call_oi; >1.2 bullish, <0.7 bearish)
      max_pain : float | None
      atm_iv   : None           (IV not available in BSE Fyers feed)
      iv_skew  : None           (IV not available in BSE Fyers feed)
      oi_bias  : 'bullish' | 'bearish' | 'neutral'
      strikes  : {strike_px: {'call_oi', 'call_iv', 'put_oi', 'put_iv'}}

    Parameters
    ----------
    fyers           : authenticated fyers_apiv3.FyersModel instance
    underlying_price: current SENSEX spot (used as fallback for spot)
    force           : bypass cache
    """
    empty = {'pcr': None, 'max_pain': None, 'atm_iv': None,
             'iv_skew': None, 'oi_bias': 'neutral', 'strikes': {}}

    if fyers is None:
        return empty

    # Cache check
    if (not force
            and _cache.get('ctx')
            and (time.time() - _cache.get('ts', 0)) < _CACHE_TTL):
        return _cache['ctx']

    try:
        resp = fyers.optionchain({
            'symbol'     : _SYMBOL,
            'strikecount': 20,
            'timestamp'  : '',
        })

        if resp.get('s') != 'ok':
            print(f"  [bse_oi] Fyers optionchain error: {resp.get('message', resp)}")
            return empty

        data    = resp.get('data', {})
        oc_rows = data.get('optionsChain', [])

        # Separate underlying row from option rows
        options     = [r for r in oc_rows if r.get('option_type') in ('CE', 'PE')]
        spot_rows   = [r for r in oc_rows if not r.get('option_type')]

        if not options:
            print(f"  [bse_oi] Empty optionsChain for SENSEX")
            return empty

        # ── PCR from top-level totals (most accurate; Fyers provides this) ──
        call_oi_total = float(data.get('callOi', 0) or 0)
        put_oi_total  = float(data.get('putOi',  0) or 0)
        pcr = (round(put_oi_total / call_oi_total, 3)
               if call_oi_total else None)

        # ── Spot price ───────────────────────────────────────────────────────
        spot = float(spot_rows[0].get('ltp', underlying_price)) if spot_rows else underlying_price

        # ── Per-strike OI map ────────────────────────────────────────────────
        strikes_map: dict = {}
        for r in options:
            k   = int(r.get('strike_price', 0))
            oi  = int(float(r.get('oi', 0) or 0))
            strikes_map.setdefault(
                k, {'call_oi': 0, 'call_iv': 0.0, 'put_oi': 0, 'put_iv': 0.0}
            )
            if r['option_type'] == 'CE':
                strikes_map[k]['call_oi'] = oi
            else:
                strikes_map[k]['put_oi']  = oi

        # ── Max pain ─────────────────────────────────────────────────────────
        max_pain = _compute_max_pain(strikes_map)

        # ── OI bias from PCR ─────────────────────────────────────────────────
        if pcr is not None:
            oi_bias = ('bullish' if pcr > 1.2 else
                       'bearish' if pcr < 0.7 else 'neutral')
        else:
            oi_bias = 'neutral'

        ctx = {
            'pcr'      : pcr,
            'max_pain' : max_pain,
            'atm_iv'   : None,   # BSE Fyers feed does not provide IV
            'iv_skew'  : None,   # BSE Fyers feed does not provide IV
            'oi_bias'  : oi_bias,
            'strikes'  : strikes_map,
        }

        _cache['ts']  = time.time()
        _cache['ctx'] = ctx
        return ctx

    except Exception as e:
        print(f"  [bse_oi] Exception: {e}")
        return empty


# ── Helpers ───────────────────────────────────────────────────────────────────

def _compute_max_pain(strikes_map: dict) -> float | None:
    """
    Max pain = strike where total notional loss to option buyers is minimum.
    Option writers are most incentivised to pin spot toward this level near expiry.
    """
    if not strikes_map:
        return None
    min_pain    = None
    pain_strike = None
    for k in strikes_map:
        call_loss = sum(
            v['call_oi'] * (s - k)
            for s, v in strikes_map.items()
            if s > k
        )
        put_loss = sum(
            v['put_oi'] * (k - s)
            for s, v in strikes_map.items()
            if s < k
        )
        total = call_loss + put_loss
        if min_pain is None or total < min_pain:
            min_pain    = total
            pain_strike = float(k)
    return pain_strike


# ── Convenience ───────────────────────────────────────────────────────────────

def cache_age() -> str:
    """Return human-readable age of cached SENSEX OI data."""
    ts = _cache.get('ts', 0)
    if not ts:
        return 'no cache'
    age = int(time.time() - ts)
    return f"{age}s ago"
