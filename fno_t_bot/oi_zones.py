# -*- coding: utf-8 -*-
"""
OI Zones — Shared Signal Module
=================================
Loads the EOD-saved OI zone JSON (from oi_zones_eod.py) and converts
current spot price + intended signal direction into an actionable trading
decision for use by paper_bot.py (and the eventual live bot).

Decision logic
--------------
  BOOST  (+1 lot) — price just broke THROUGH a major wall with momentum
                    OR PCR strongly aligned with signal direction
  TAKE             — standard entry, no strong OI context
  REDUCE (−1 lot) — price approaching a wall that may cap the move
                    OR PCR moderately opposing signal direction
  SKIP             — price hugging a wall from the losing side
                    OR effective range is too tight (<0.8%) — box chop
                    OR max pain strongly opposing signal (near expiry)

Designed to be imported and called from paper_bot.py; never runs as main.

Usage
-----
  from oi_zones import load_zones, get_zone_signal

  # At bot startup:
  oi = load_zones('NIFTY')

  # Before enter_trade():
  decision = get_zone_signal(spot, 'CALL', oi)
  # decision = {'action': 'TAKE', 'reason': '...', 'lots_adj': 0, 'score': 0}
"""

from __future__ import annotations

import json, os, datetime as dt, math
from pathlib import Path
from typing import Optional

_HERE       = Path(__file__).parent
_ZONES_DIR  = _HERE.parent / 'data' / 'oi_zones'

# ── Tunable thresholds ────────────────────────────────────────────────────────

# Distance from wall (as % of price) that governs each zone:
BREAK_PCT   = 0.0008   # 0.08% — "just broke through" (BOOST zone)
SKIP_PCT    = 0.0020   # 0.20% — approaching wall from wrong side (SKIP zone)
REDUCE_PCT  = 0.0050   # 0.50% — nearby wall, momentum may be capped (REDUCE)

# PCR thresholds
PCR_BULL_STRONG  = 1.35   # heavy put writing → bullish bias (CALL BOOST)
PCR_BULL_MILD    = 1.10   # mild put writing  → CALL TAKE
PCR_BEAR_MILD    = 0.90   # mild call writing → PUT TAKE
PCR_BEAR_STRONG  = 0.70   # heavy call writing → PUT BOOST

# Max pain thresholds (only relevant close to expiry, DTE ≤ 2)
MAX_PAIN_GRAVITY_PCT = 0.005   # within 0.5% of max pain = "in gravity zone"
# If price is > 1% ABOVE max pain on a CALL entry near expiry → REDUCE
MAX_PAIN_ADVERSE_PCT = 0.010


# ── Loader ────────────────────────────────────────────────────────────────────

def load_zones(instrument: str,
               max_age_days: int = 3) -> Optional[dict]:
    """
    Load the most recent OI zones JSON for `instrument`.

    Parameters
    ----------
    instrument   : 'NIFTY', 'BANKNIFTY', or 'SENSEX'
    max_age_days : reject files older than this many calendar days
                   (default 3 = covers weekends + Monday morning load)

    Returns None if file missing, too old, or malformed.
    """
    path = _ZONES_DIR / f"latest_{instrument}.json"
    if not path.exists():
        return None

    try:
        with open(path, encoding='utf-8') as f:
            z = json.load(f)
    except Exception:
        return None

    # Age check: zones from Friday are valid through Monday
    try:
        zone_date  = dt.date.fromisoformat(z['date'])
        today      = dt.date.today()
        age        = (today - zone_date).days
        if age > max_age_days:
            return None   # too stale — bot should warn and run without OI context
    except Exception:
        pass   # no date field or malformed → still usable

    return z


def zones_age_description(zones: dict) -> str:
    """Return human-readable age string for log output."""
    try:
        zone_date   = dt.date.fromisoformat(zones['date'])
        fetched_at  = zones.get('fetched_at', '?')
        today       = dt.date.today()
        age         = (today - zone_date).days
        if age == 0:
            return f"today at {fetched_at}"
        elif age == 1:
            return f"yesterday ({zone_date}) at {fetched_at}"
        else:
            return f"{age} days ago ({zone_date}) at {fetched_at}"
    except Exception:
        return "unknown age"


# ── Core signal logic ─────────────────────────────────────────────────────────

def get_zone_signal(spot: float,
                    signal_type: str,
                    zones: Optional[dict],
                    dte: int = 3) -> dict:
    """
    Evaluate current price against OI zones and return a trading decision.

    Parameters
    ----------
    spot        : current underlying price
    signal_type : 'CALL' or 'PUT'
    zones       : dict from load_zones(), or None (graceful no-op)
    dte         : days to expiry of the option we intend to buy

    Returns
    -------
    dict with:
      action   : 'BOOST' | 'TAKE' | 'REDUCE' | 'SKIP'
      reason   : human-readable explanation
      lots_adj : +1 (BOOST), 0 (TAKE), -1 (REDUCE), -99 (SKIP sentinel)
      score    : integer sentiment score (-2 to +2) for logging
      context  : short context string for the log line
    """
    # ── No zones available — no-op ────────────────────────────────────────
    if not zones:
        return _result('TAKE', 'No OI zones loaded — proceeding without context', 0, 0)

    resistance  = zones.get('resistance', [])
    support     = zones.get('support', [])
    pcr         = zones.get('pcr', 1.0)
    pcr_bias    = zones.get('pcr_bias', 'NEUTRAL')
    max_pain    = zones.get('max_pain', spot)
    eff_range   = zones.get('effective_range', {})
    range_type  = zones.get('range_type', 'NORMAL')

    reasons = []
    score   = 0   # running sentiment (-2 = strong skip, +2 = strong boost)

    is_call = (signal_type == 'CALL')

    # ── 1. Tight range — score penalty only, never a hard block ──────────
    # ORB trades carry a time-bound hard stop (checkpoint + 14:30 force-close)
    # and a 25% stop-loss that handle boxed-price risk organically.
    # range_type == 'TIGHT' contributes -1 to score (reduces to 1 lot) but
    # does not block the trade.
    if range_type == 'TIGHT':
        eff_pct = eff_range.get('pct', 0.0)
        lo      = eff_range.get('lower', spot)
        hi      = eff_range.get('upper', spot)
        reasons.append(
            f"Tight OI range {lo:,.0f}–{hi:,.0f} ({eff_pct:.2f}%) — "
            f"trading at reduced lots"
        )
        score -= 1

    # ── 2. Wall proximity check ───────────────────────────────────────────
    # For CALL: care about resistance levels ABOVE spot
    # For PUT:  care about support levels BELOW spot
    if is_call:
        walls_ahead = sorted(
            [r for r in resistance if r['strike'] > spot],
            key=lambda x: x['strike']
        )
        walls_behind = sorted(
            [r for r in resistance if r['strike'] <= spot],
            key=lambda x: x['strike'], reverse=True
        )
    else:  # PUT
        walls_ahead = sorted(
            [s for s in support if s['strike'] < spot],
            key=lambda x: x['strike'], reverse=True
        )
        walls_behind = sorted(
            [s for s in support if s['strike'] >= spot],
            key=lambda x: x['strike']
        )

    nearest_ahead  = walls_ahead[0]  if walls_ahead  else None
    just_broke     = walls_behind[0] if walls_behind else None

    # Distance to nearest wall ahead
    if nearest_ahead:
        dist_ahead = abs(nearest_ahead['strike'] - spot) / spot
        strength   = nearest_ahead.get('strength', 'MINOR')

        # Very close wall — strong absorption risk; score -2 but trade still fires
        # at reduced lots. Stop-loss handles the downside, not a pre-block.
        if dist_ahead < SKIP_PCT and strength in ('MAJOR', 'WALL'):
            direction = 'below resistance' if is_call else 'above support'
            reasons.append(
                f"Price {direction} {nearest_ahead['strike']:,.0f} ({strength}) "
                f"by only {dist_ahead*100:.2f}% — wall may absorb momentum"
            )
            score -= 2
        # REDUCE: approaching a wall that may cap the move
        # Also applies to fresh OI building at any wall ahead (writers reinforcing)
        elif dist_ahead < REDUCE_PCT and (strength in ('MAJOR', 'WALL')
                                           or nearest_ahead.get('fresh_build', False)):
            reasons.append(
                f"Approaching {signal_type} wall {nearest_ahead['strike']:,.0f} "
                f"({dist_ahead*100:.2f}% away — {strength}"
                + (" fresh-build" if nearest_ahead.get('fresh_build') else "") + ")"
            )
            score -= 1

    # BOOST: price just broke through a wall (wall is now behind us)
    # For CALL: just broke resistance (now below us). For PUT: just broke support (now above us).
    walls_behind_pool = resistance if is_call else support
    if just_broke and walls_behind_pool:
        dist_behind = abs(just_broke['strike'] - spot) / spot
        strength    = just_broke.get('strength', 'MINOR')
        if dist_behind < BREAK_PCT and strength in ('MAJOR', 'WALL'):
            side = 'resistance' if is_call else 'support'
            reasons.append(
                f"Just broke through {side} {just_broke['strike']:,.0f} ({strength}) "
                f"— gamma squeeze likely"
            )
            score += 2

        # Note: fresh_build at an already-broken wall is NOT a penalty — we've cleared it.

    # ── 3. PCR bias ───────────────────────────────────────────────────────
    if is_call:
        if pcr > PCR_BULL_STRONG:
            reasons.append(f"PCR={pcr:.2f} (heavy put writing) → bullish OI bias supports CALL")
            score += 1
        elif pcr < PCR_BEAR_STRONG:
            reasons.append(f"PCR={pcr:.2f} (heavy call writing) → bearish OI bias opposes CALL")
            score -= 1
    else:  # PUT
        if pcr < PCR_BEAR_STRONG:
            reasons.append(f"PCR={pcr:.2f} (heavy call writing) → bearish OI bias supports PUT")
            score += 1
        elif pcr > PCR_BULL_STRONG:
            reasons.append(f"PCR={pcr:.2f} (heavy put writing) → bullish OI bias opposes PUT")
            score -= 1

    # ── 4. Max pain gravity (relevant only near expiry) ────────────────────
    if dte <= 2:
        mp_dist      = (spot - max_pain) / spot
        pain_pct_str = f"{abs(mp_dist)*100:.2f}%"
        if is_call and mp_dist > MAX_PAIN_ADVERSE_PCT:
            # Price well above max pain → gravity pulls DOWN → opposes CALL
            reasons.append(
                f"Max pain {max_pain:,.0f} is {pain_pct_str} below spot "
                f"(DTE={dte}) — gravity opposes CALL entry"
            )
            score -= 1
        elif not is_call and mp_dist < -MAX_PAIN_ADVERSE_PCT:
            # Price well below max pain → gravity pulls UP → opposes PUT
            reasons.append(
                f"Max pain {max_pain:,.0f} is {pain_pct_str} above spot "
                f"(DTE={dte}) — gravity opposes PUT entry"
            )
            score -= 1
        elif abs(mp_dist) < MAX_PAIN_GRAVITY_PCT:
            reasons.append(f"Spot near max pain {max_pain:,.0f} (DTE={dte}) — expect pinning")
            score -= 1  # choppy near pin zone

    # ── 5. Convert score to action ─────────────────────────────────────────
    if not reasons:
        reasons = ['Price in clear OI space']

    reason_str = '; '.join(reasons)

    if score >= 2:
        return _result('BOOST',  reason_str, score, +1)
    elif score >= 0:
        return _result('TAKE',   reason_str, score,  0)
    else:   # any negative score → reduce lots; ORB always fires
        return _result('REDUCE', reason_str, score, -1)


def _result(action: str, reason: str, score: int, lots_adj: int) -> dict:
    emoji = {'BOOST': '🚀', 'TAKE': '✅', 'REDUCE': '⚠️', 'SKIP': '🚫'}.get(action, '')
    return {
        'action'  : action,
        'reason'  : reason,
        'lots_adj': lots_adj,
        'score'   : score,
        'context' : f"{emoji} OI:{action}",
    }


# ── Convenience summary ───────────────────────────────────────────────────────

def describe_zones(zones: dict) -> str:
    """One-line summary of loaded zones for the bot's startup log."""
    if not zones:
        return "No OI zones"
    er     = zones.get('effective_range', {})
    lo     = er.get('lower', 0)
    hi     = er.get('upper', 0)
    pcr    = zones.get('pcr', '?')
    rtype  = zones.get('range_type', '?')
    mp     = zones.get('max_pain', 0)
    age    = zones_age_description(zones)
    return (f"OI zones from {age}: "
            f"range {lo:,.0f}–{hi:,.0f} ({rtype}) | "
            f"PCR={pcr} ({zones.get('pcr_bias','?')}) | "
            f"MaxPain={mp:,.0f}")
