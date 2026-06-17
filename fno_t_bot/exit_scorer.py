"""
exit_scorer.py — Continuous scoring-based exit system (0–100).

Replaces the fixed 12:00 checkpoint and 14:30 force-close with a rolling
score evaluated every bot cycle. Score bands:

  HOLD     >= 55  — do nothing, let the trade run
  CAUTION  35-54  — tighten trailing distance (10% → 6%)
  EXIT     <  35  — market exit

The exchange SL-M (hard stop-loss order already placed) and the 14:55
absolute safety exit are RETAINED as outer safety nets — the scorer only
decides whether to EXIT voluntarily, not whether to hold past safety levels.

Six components (see compute_exit_score() docstring):
  A. Directional integrity   0–35  (DI+/-, ADX)
  B. Structure alignment    -20–20 (EMA, ST5)
  C. Price vs levels        -25–20 (VWAP, OR_L reclaim, MaxPain)
  D. Option health            0–10 (P&L tier)
  E. Time decay penalty     -20–0  (replaces hard time exits)
  F. Reversal warnings      -20–0  (rev_guard, PCR drift)

Maximum positive: 35+20+20+10 = 85
Minimum (floor): -15 (band behaviour matters more than raw value)
"""
from __future__ import annotations

from typing import Optional


# ── Score band thresholds ──────────────────────────────────────────────────────
SCORE_HOLD    = 55
SCORE_CAUTION = 35


def compute_exit_score(
    trade_type:   str,           # 'CALL' or 'PUT'
    bar:          object,        # pandas Series — one 5-min bar (named attributes)
    or_high:      float,
    or_low:       float,
    entry_price:  float,         # option entry price (₹)
    current_opt:  float,         # current option LTP (₹, BS or live)
    entry_time:   object,        # pd.Timestamp (timezone-aware)
    now:          object,        # pd.Timestamp current bar time
    oi_context:   Optional[dict] = None,  # {'pcr': float, 'max_pain': float,
                                          #  'pcr_drift': float}  — may be None
    rev_score:    float = 0.0,   # 0–100 from compute_reversal_risk()
    dte:          float = 2.0,   # days to expiry
) -> dict:
    """
    Compute the exit score for an open position.

    Returns a dict:
        {
            'score'     : int,        # 0–85+ (capped at display for logging)
            'band'      : str,        # 'HOLD' | 'CAUTION' | 'EXIT'
            'trail_dist': float,      # 0.10 (HOLD) or 0.06 (CAUTION)
            'components': dict,       # per-component breakdown for logging
            'reason'    : str,        # human-readable summary
        }
    """
    is_put  = trade_type.upper() == 'PUT'
    is_call = not is_put

    def _get(name, default=0.0):
        """Safely read a value from a pandas Series / dict / object."""
        try:
            v = getattr(bar, name, None)
            if v is None and hasattr(bar, '__getitem__'):
                v = bar[name]
            return float(v) if v is not None else default
        except Exception:
            return default

    close    = _get('Close',    _get('close', 0.0))
    adx      = _get('ADX',      _get('adx', 0.0))
    di_plus  = _get('DI_plus',  _get('dip', 0.0))
    di_minus = _get('DI_minus', _get('dim', 0.0))
    ema_fast = _get('EMA_fast', _get('ema9',  close))
    ema_slow = _get('EMA_slow', _get('ema21', close))
    vwap     = _get('VWAP',     _get('vwap',  close))
    st5      = _get('ST_5m',    0.0)   # +1 bullish, -1 bearish, 0 unknown

    components = {}
    reasons    = []

    # ── A. Directional integrity (0–35 pts) ───────────────────────────────────
    #
    # For a PUT trade we want DI- dominant; for CALL we want DI+ dominant.
    # Score the favouring DI, spread direction, and ADX level.
    a_score = 0

    if is_put:
        fav_di  = di_minus    # we want DI- > DI+
        opp_di  = di_plus
    else:
        fav_di  = di_plus     # we want DI+ > DI-
        opp_di  = di_minus

    spread = fav_di - opp_di  # positive = thesis intact

    if spread > 5:
        a_score += 20
        reasons.append(f'DI spread +{spread:.1f} (thesis intact)')
    elif spread > 0:
        a_score += 10
        reasons.append(f'DI spread +{spread:.1f} (narrow)')
    elif spread >= -5:
        a_score += 5
        reasons.append(f'DI spread {spread:.1f} (near crossover)')
    else:
        # Opposing DI crossed — directional case is broken
        a_score += 0
        reasons.append(f'DI crossover against trade ({spread:.1f})')

    if adx >= 40:
        a_score += 15
        reasons.append(f'ADX={adx:.0f} (strong)')
    elif adx >= 30:
        a_score += 10
        reasons.append(f'ADX={adx:.0f} (moderate)')
    elif adx >= 20:
        a_score += 5
        reasons.append(f'ADX={adx:.0f} (weak)')
    else:
        a_score += 0
        reasons.append(f'ADX={adx:.0f} (flat/noise)')

    components['A_directional'] = a_score

    # ── B. Structure alignment (-20–20 pts) ───────────────────────────────────
    b_score = 0

    ema_bull = ema_fast > ema_slow
    ema_aligned = (is_put and not ema_bull) or (is_call and ema_bull)
    ema_flipped = (is_put and ema_bull) or (is_call and not ema_bull)

    if ema_aligned:
        b_score += 10
        reasons.append('EMA aligned')
    elif ema_flipped:
        b_score -= 10
        reasons.append('EMA flipped against trade ⚠')

    # SuperTrend 5m: +1=bullish, -1=bearish
    if st5 != 0:
        st5_favours = (is_put and st5 < 0) or (is_call and st5 > 0)
        if st5_favours:
            b_score += 5
            reasons.append('ST5 aligned')
        else:
            b_score -= 5
            reasons.append('ST5 flipped ⚠')

    components['B_structure'] = b_score

    # ── C. Price vs levels (-25–20 pts) ──────────────────────────────────────
    c_score = 0

    # VWAP: PUT wants price below VWAP (bearish), CALL wants above
    if is_put and close < vwap:
        c_score += 10
        reasons.append(f'Below VWAP ({vwap:.0f})')
    elif is_call and close > vwap:
        c_score += 10
        reasons.append(f'Above VWAP ({vwap:.0f})')
    else:
        # Price moved back through VWAP against trade
        c_score -= 5
        reasons.append(f'VWAP crossover against trade ({vwap:.0f})')

    # OR_L reclaim (PUT only) — if price reclaims OR_L from below = thesis break
    if is_put and or_low > 0 and close > or_low:
        c_score -= 15
        reasons.append(f'OR_L reclaimed ({or_low:.0f}) — thesis break ⚠')
    # OR_H breach (CALL only) — if price falls back below OR_H after breakout = thesis break
    elif is_call and or_high > 0 and close < or_high:
        c_score -= 15
        reasons.append(f'OR_H given back ({or_high:.0f}) — thesis break ⚠')

    # MaxPain gravity (DTE ≤ 2 only)
    if oi_context:
        mp = oi_context.get('max_pain')
        if mp and dte <= 2 and close > 0:
            mp_dist_pct = abs(close - mp) / close
            if mp_dist_pct <= 0.003:   # within 0.3% of MaxPain
                c_score -= 10
                reasons.append(f'MaxPain pin zone ({mp:.0f}, {mp_dist_pct*100:.1f}%)')

    components['C_levels'] = c_score

    # ── D. Option health (0–10 pts) ───────────────────────────────────────────
    d_score = 0
    if entry_price > 0:
        pnl_pct = (current_opt - entry_price) / entry_price
        if pnl_pct > 0.15:
            d_score += 10
            reasons.append(f'Option +{pnl_pct*100:.0f}% (healthy)')
        elif pnl_pct >= 0:
            d_score += 5
            reasons.append(f'Option +{pnl_pct*100:.0f}% (slight gain)')
        elif pnl_pct >= -0.10:
            d_score += 0
            reasons.append(f'Option {pnl_pct*100:.0f}% (small loss, SL guards)')
        else:
            d_score += 0
            reasons.append(f'Option {pnl_pct*100:.0f}% (deep loss — SL imminent)')
    components['D_option_health'] = d_score

    # ── E. Time decay penalty (−20 to 0) ─────────────────────────────────────
    # Replaces hard time-based exits (12:00 close, 14:30 force-close).
    # Gradually penalises staying open as expiry approaches intraday.
    e_score = 0
    try:
        now_hhmm = now.strftime('%H:%M')
    except Exception:
        now_hhmm = '09:15'

    if now_hhmm >= '14:15':
        e_score = -20
        reasons.append('Time penalty: after 14:15')
    elif now_hhmm >= '13:30':
        e_score = -10
        reasons.append('Time penalty: 13:30–14:15')
    else:
        e_score = 0   # before 13:30 — no penalty

    components['E_time_decay'] = e_score

    # ── F. Reversal warnings (−20 to 0) ──────────────────────────────────────
    f_score = 0

    if rev_score >= 60:
        f_score -= 10
        reasons.append(f'REV-GUARD elevated ({rev_score:.0f}/100)')

    if oi_context:
        pcr_drift = oi_context.get('pcr_drift', 0.0)
        # pcr_drift > 0 = PCR rising = more puts = bearish
        # For PUT: drift going negative (PCR falling) = CALL buying = against us
        if is_put and pcr_drift < -0.05:
            f_score -= 10
            reasons.append(f'PCR drift against PUT ({pcr_drift:+.2f})')
        elif is_call and pcr_drift > 0.05:
            f_score -= 10
            reasons.append(f'PCR drift against CALL ({pcr_drift:+.2f})')

    components['F_reversal'] = f_score

    # ── Final score ───────────────────────────────────────────────────────────
    total = (a_score + b_score + c_score + d_score + e_score + f_score)
    total = max(total, -30)   # floor — prevent extreme negative from dominating log

    if total >= SCORE_HOLD:
        band       = 'HOLD'
        trail_dist = None   # keep configured trail_dist unchanged
    elif total >= SCORE_CAUTION:
        band       = 'CAUTION'
        trail_dist = None   # CAUTION is informational in v1 — don't change trail
    else:
        band       = 'EXIT'
        trail_dist = None   # EXIT → force-close, trail irrelevant

    return {
        'score'     : total,
        'band'      : band,
        'trail_dist': trail_dist,
        'components': components,
        'reasons'   : reasons,
        'pnl_pct'   : (current_opt - entry_price) / entry_price if entry_price > 0 else 0.0,
    }


def band_label(score: int) -> str:
    if score >= SCORE_HOLD:
        return 'HOLD'
    elif score >= SCORE_CAUTION:
        return 'CAUTION'
    return 'EXIT'
