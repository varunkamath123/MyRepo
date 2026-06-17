# -*- coding: utf-8 -*-
"""
Post-11 Option Buy Scorer — v2 (May 2026)
==========================================
Pure weighted scoring system for option-buy signals at or after 11:00 IST.

Design principles
-----------------
  NO EMA         EMA is lagging by 8-16+ bars at 11am. DI spread (live directional
                 conviction) and OR extension (confirmed trend distance from the
                 Opening Range) replace it entirely as momentum signals.

  NO HARD BLOCK  No single component can reject a trade. The aggregate score
                 only determines lot sizing. Even a MARGINAL score (< 45) enters
                 at 1 lot — the stop-loss handles risk. The bot should never miss
                 a strong move because a secondary indicator is slightly offside.

  LOT SIZING     Scorer can only REDUCE lots vs what existing guards computed.
                 It never raises lots above the reversal-guard / OI-zone output.
                   STRONG (≥70) → lot_suggestion=2 (honours existing cap)
                   TRADE  (≥45) → lot_suggestion=1
                   MARGINAL(<45) → lot_suggestion=1, caution log

Components (weights sum to 100)
--------------------------------
  or_extension    25   Distance beyond OR high/low — confirms breakout is real
                       and still running. Small extension = potential fakeout.
  di_strength     20   DI+/DI- spread at this exact moment — fast, not lagging.
                       Replaces EMA freshness entirely.
  time_theta      15   Session time remaining + IV/DTE awareness.
                       11:00 is worth 2x more than 13:00 for option buyers.
  adx_strength    15   ADX magnitude. Graduated from 25 (minimum) to 45+
                       (extreme). Never blocks — even ADX<25 scores 1 pt.
  oi_context      15   PCR alignment (5) + MaxPain gravity (5) +
                       OI wall proximity via oz zone signal (5).
  vwap_structure  10   Price side + distance from VWAP. Wrong side = 0,
                       not a block — just scores 0.

Usage
-----
  import post11_scorer
  result = post11_scorer.score(
      signal_type = 'CALL',
      entry_time  = now.time(),
      df          = df,
      or_high     = self._or_high,
      or_low      = self._or_low,
      htf         = htf,
      oc          = oc,
      oz          = _oz_result,         # OI zone result (wall proximity)
      atm_iv      = oc.get('atm_iv'),
      dte         = config.DAYS_TO_EXPIRY,
  )
  self.logger.info(post11_scorer.format_score(result))
  _lots = min(_lots, result['lot_suggestion'])   # only reduce, never raise
"""
from __future__ import annotations

import datetime
import logging

_log = logging.getLogger(__name__)

# ── Weights (must sum to 100) ─────────────────────────────────────────────────
WEIGHTS: dict[str, int] = {
    'or_extension'  : 25,   # breakout distance — primary confirmation
    'di_strength'   : 20,   # DI spread NOW — live momentum, not lagging
    'time_theta'    : 15,   # session time remaining + IV/DTE awareness
    'adx_strength'  : 15,   # ADX magnitude (graduated)
    'oi_context'    : 15,   # PCR (5) + MaxPain (5) + OI wall distance (5)
    'vwap_structure': 10,   # price side + distance from VWAP
}
assert sum(WEIGHTS.values()) == 100, f"Weights must sum to 100, got {sum(WEIGHTS.values())}"

# ── Lot sizing thresholds ─────────────────────────────────────────────────────
STRONG_MIN = 70   # ≥70 → STRONG, lot_suggestion = 2
TRADE_MIN  = 45   # 45-69 → TRADE, lot_suggestion = 1
# 40-44 → MARGINAL, lot_suggestion = 1 (caution log, still enters)
# < POST11_SCORE_SKIP_MIN → SKIP (aggregate too weak — no single component blocks)
POST11_SCORE_SKIP_MIN = 40   # overridden by config.POST11_SCORE_SKIP_MIN if set

# ── IV / theta threshold ──────────────────────────────────────────────────────
IV_THETA_RISK = 12.0   # IV% below this on DTE ≤ 1 → theta penalty


def score(
    signal_type : str,              # 'CALL' or 'PUT'
    entry_time  : datetime.time,    # current IST time
    df,                             # pandas DataFrame — 5-min bars
    or_high     : float | None,     # OR high from _compute_or()
    or_low      : float | None,     # OR low
    htf         : dict,             # get_htf_context() — {supertrend_15m, ...}
    oc          : dict,             # get_oc_context() — {pcr, max_pain, atm_iv, ...}
    oz          : dict | None = None,  # get_zone_signal() result — OI wall proximity
    atm_iv      : float | None = None,
    dte         : int = 2,
) -> dict:
    """
    Compute 0-100 quality score for a post-11 option buy signal.

    Returns dict with:
      total          int  0-100
      quality        str  STRONG / GOOD / MODERATE / MARGINAL / WEAK
      gate           str  STRONG / TRADE / MARGINAL  (never SKIP)
      lot_suggestion int  1 or 2
      components     dict {name: {score, max, pct, reason}}
      summary        str  one-line log string
    """
    row   = df.iloc[-1]
    close = float(row.get('Close', row.get('C', 0)))
    oz    = oz or {}
    comps : dict[str, dict] = {}

    # ─── 1. OR Extension (25 pts) — primary confirmation ─────────────────────
    # How far beyond the OR high (CALL) or OR low (PUT) has price moved?
    # Large extension = the ORB is confirmed and still running.
    # Small extension = we're at the boundary, high fakeout risk.
    # Inside OR = not a valid ORB signal (should not reach scorer).
    try:
        if or_high and or_low and close > 0:
            if signal_type == 'CALL':
                boundary = or_high
                ext_pct  = (close - boundary) / boundary * 100
            else:
                boundary = or_low
                ext_pct  = (boundary - close) / boundary * 100

            if ext_pct >= 0.80:
                pts = 25
                reason = f"OR ext={ext_pct:.2f}% ≥0.80% — breakout running hard ✓✓✓"
            elif ext_pct >= 0.50:
                pts = 20
                reason = f"OR ext={ext_pct:.2f}% 0.50–0.80% — confirmed move ✓✓"
            elif ext_pct >= 0.25:
                pts = 13
                reason = f"OR ext={ext_pct:.2f}% 0.25–0.50% — moderate extension ✓"
            elif ext_pct >= 0.10:
                pts = 7
                reason = f"OR ext={ext_pct:.2f}% 0.10–0.25% — thin, possible fakeout"
            elif ext_pct >= 0:
                pts = 2
                reason = f"OR ext={ext_pct:.2f}% — marginal, near OR boundary"
            else:
                pts = 0
                reason = f"OR ext={ext_pct:.2f}% — price inside OR ✗"
        else:
            pts    = 10
            reason = "OR levels unavailable — neutral"
    except Exception as exc:
        pts    = 10
        reason = f"OR extension error: {exc}"
    comps['or_extension'] = _c(pts, 'or_extension', reason)

    # ─── 2. DI Strength (20 pts) — live momentum, replaces EMA ──────────────
    # By 11am the EMA cross is 8-16+ bars old — scoring its "freshness" is
    # meaningless.  DI+/DI- spread tells us if directional momentum is ALIVE
    # RIGHT NOW, not just whether it existed at 09:30.
    try:
        dip = float(row.get('DI_plus',  row.get('dip', 0)) or 0)
        dim = float(row.get('DI_minus', row.get('dim', 0)) or 0)

        spread = (dip - dim) if signal_type == 'CALL' else (dim - dip)
        # positive spread = momentum in trade direction; negative = opposing

        if spread >= 25:
            pts = 20
            reason = f"DI spread={spread:.1f} (DI+={dip:.1f} DI-={dim:.1f}) — strong conviction ✓✓✓"
        elif spread >= 15:
            pts = 15
            reason = f"DI spread={spread:.1f} — good conviction ✓✓"
        elif spread >= 8:
            pts = 9
            reason = f"DI spread={spread:.1f} — moderate conviction ✓"
        elif spread >= 3:
            pts = 4
            reason = f"DI spread={spread:.1f} — weak"
        elif spread >= 0:
            pts = 1
            reason = f"DI spread={spread:.1f} — marginal, barely directional"
        else:
            pts = 0
            reason = f"DI opposing: spread={spread:.1f} — momentum against {signal_type} ✗"
    except Exception as exc:
        pts    = 5
        reason = f"DI error: {exc}"
    comps['di_strength'] = _c(pts, 'di_strength', reason)

    # ─── 3. Time + Theta (15 pts) ────────────────────────────────────────────
    # Option buyers are fighting theta decay post-11. Earlier = more session
    # time = more chance for the option to move before 14:30 force-close.
    # High IV partially compensates (more premium movement per point).
    h, m = entry_time.hour, entry_time.minute
    mins  = (h - 11) * 60 + m   # minutes since 11:00

    if mins < 30:          # 11:00–11:30
        pts    = 15
        reason_t = f"{entry_time.strftime('%H:%M')} — early post-11 (full runway)"
    elif mins < 60:        # 11:30–12:00
        pts    = 11
        reason_t = f"{entry_time.strftime('%H:%M')} — 11:30-12:00 (good)"
    elif mins < 90:        # 12:00–12:30
        pts    = 7
        reason_t = f"{entry_time.strftime('%H:%M')} — 12:00-12:30 (theta building)"
    elif mins < 120:       # 12:30–13:00
        pts    = 4
        reason_t = f"{entry_time.strftime('%H:%M')} — 12:30-13:00 (late)"
    else:                  # 13:00+
        pts    = 2
        reason_t = f"{entry_time.strftime('%H:%M')} — after 13:00 (high theta drag)"

    _iv = atm_iv or oc.get('atm_iv') or 0.0
    bonus_note = ''
    if _iv > 0 and _iv < IV_THETA_RISK and dte <= 1:
        pts        = max(0, pts - 5)
        bonus_note = f" ⚠️ low IV={_iv:.1f}% on DTE={dte} (theta penalty -5)"
    elif _iv >= 20:
        pts        = min(WEIGHTS['time_theta'], pts + 2)
        bonus_note = f" | IV={_iv:.1f}% (high IV → option can move more +2)"

    comps['time_theta'] = _c(pts, 'time_theta', reason_t + bonus_note)

    # ─── 4. ADX Strength (15 pts) — trend magnitude ──────────────────────────
    # Graduated from <25 (choppy) to 45+ (extreme trend).
    # Even ADX < 25 scores 1 pt — the ORB signal itself already requires ADX ≥ 25
    # as a hard gate, so sub-25 entries here are rare (re-entry drift only).
    adx = float(row.get('ADX', row.get('adx', 0)) or 0)
    if adx >= 45:
        pts = 15; reason = f"ADX={adx:.1f} ≥45 — extreme trend ✓✓✓"
    elif adx >= 38:
        pts = 12; reason = f"ADX={adx:.1f} 38–45 — strong trend ✓✓"
    elif adx >= 30:
        pts = 9;  reason = f"ADX={adx:.1f} 30–38 — moderate trend ✓"
    elif adx >= 25:
        pts = 5;  reason = f"ADX={adx:.1f} 25–30 — minimum trending"
    else:
        pts = 1;  reason = f"ADX={adx:.1f} <25 — weak / choppy"
    comps['adx_strength'] = _c(pts, 'adx_strength', reason)

    # ─── 5. OI Context (15 pts): PCR (5) + MaxPain (5) + Wall (5) ───────────
    # Wall proximity sub-component (oz) added in v2 — OI wall 0.16% above
    # entry is a critical resistance that the existing OI-ZONE correctly flags
    # as REDUCE but wasn't feeding into scorer quality.
    oi_sub = 0
    oi_rsns: list[str] = []

    # PCR (5 pts)
    pcr = oc.get('pcr')
    if pcr is not None:
        if signal_type == 'CALL':
            if pcr >= 1.10:   oi_sub += 5; oi_rsns.append(f"PCR={pcr:.2f}↑ bullish ✓")
            elif pcr >= 0.85: oi_sub += 3; oi_rsns.append(f"PCR={pcr:.2f} neutral")
            else:             oi_sub += 0; oi_rsns.append(f"PCR={pcr:.2f}↓ opposes CALL ✗")
        else:
            if pcr <= 0.85:   oi_sub += 5; oi_rsns.append(f"PCR={pcr:.2f}↓ bearish ✓")
            elif pcr <= 1.10: oi_sub += 3; oi_rsns.append(f"PCR={pcr:.2f} neutral")
            else:             oi_sub += 0; oi_rsns.append(f"PCR={pcr:.2f}↑ opposes PUT ✗")
    else:
        oi_sub += 2; oi_rsns.append("PCR=? neutral")

    # MaxPain (5 pts)
    mp = oc.get('max_pain')
    if mp and close > 0:
        mp_dist = (float(mp) - close) / close * 100
        if   signal_type == 'CALL' and mp_dist >  0.5:
            oi_sub += 5; oi_rsns.append(f"MaxPain={mp:,.0f} above (+{mp_dist:.1f}%) tailwind ✓")
        elif signal_type == 'PUT'  and mp_dist < -0.5:
            oi_sub += 5; oi_rsns.append(f"MaxPain={mp:,.0f} below ({mp_dist:.1f}%) tailwind ✓")
        elif abs(mp_dist) <= 0.5:
            oi_sub += 2; oi_rsns.append(f"MaxPain={mp:,.0f} near ({mp_dist:+.1f}%) pin risk")
        else:
            oi_sub += 0; oi_rsns.append(f"MaxPain={mp:,.0f} ({mp_dist:+.1f}%) headwind ✗")
    else:
        oi_sub += 2   # neutral if unknown

    # OI Wall Proximity (5 pts) — from oz (get_zone_signal result)
    oz_action = oz.get('action', 'TAKE')
    if   oz_action == 'BOOST' : oi_sub += 5; oi_rsns.append("OI wall broken → gamma squeeze ✓✓")
    elif oz_action == 'TAKE'  : oi_sub += 3; oi_rsns.append("OI space clear ✓")
    elif oz_action == 'REDUCE': oi_sub += 1; oi_rsns.append("OI wall nearby — resistance ✗")
    else:                        oi_sub += 0; oi_rsns.append("OI zone adverse ✗✗")

    comps['oi_context'] = _c(
        min(oi_sub, WEIGHTS['oi_context']), 'oi_context', " | ".join(oi_rsns)
    )

    # ─── 6. VWAP Structure (10 pts) ──────────────────────────────────────────
    # Wrong side of VWAP scores 0 but does NOT block entry.
    # Price hugging VWAP scores low but is still valid (OR breakout is the
    # primary trigger; VWAP is structural context).
    try:
        vwap = float(row.get('VWAP', row.get('vwap', 0)) or 0)
        if vwap > 0:
            dist_pct  = abs(close - vwap) / vwap * 100
            right_side = (
                (signal_type == 'CALL' and close > vwap) or
                (signal_type == 'PUT'  and close < vwap)
            )
            if not right_side:
                pts = 0; reason = f"Wrong side of VWAP ({close:,.0f} vs {vwap:,.0f}) ✗✗"
            elif dist_pct >= 0.40:
                pts = 10; reason = f"VWAP dist={dist_pct:.3f}% ≥0.40% ✓✓"
            elif dist_pct >= 0.20:
                pts = 7;  reason = f"VWAP dist={dist_pct:.3f}% 0.20–0.40% ✓"
            elif dist_pct >= 0.08:
                pts = 4;  reason = f"VWAP dist={dist_pct:.3f}% 0.08–0.20%"
            else:
                pts = 1;  reason = f"VWAP dist={dist_pct:.3f}% — hugging VWAP"
        else:
            pts = 4; reason = "VWAP unavailable — neutral"
    except Exception as exc:
        pts    = 4
        reason = f"VWAP error: {exc}"
    comps['vwap_structure'] = _c(pts, 'vwap_structure', reason)

    # ─── ATR Expansion Adjustment (bonus/penalty, not a weighted component) ──
    # If the last bar's range is much larger than the recent average (ATR ratio > 1.3),
    # price has already "run" — entering now means chasing at a temporarily elevated
    # premium.  Conversely, a contracting ATR suggests consolidation before continuation.
    #
    # This is applied as an adjustment to the raw total rather than a separate scored
    # component (so existing weights stay unchanged and the effect is bounded).
    atr_adj  = 0
    atr_note = ''
    try:
        _highs  = df['High'].values  if 'High'  in df.columns else df['H'].values
        _lows   = df['Low'].values   if 'Low'   in df.columns else df['L'].values
        _closes = df['Close'].values if 'Close' in df.columns else df['C'].values
        if len(_highs) >= 6:
            # True Range of last 5 bars, compare to current bar
            _tr5 = [max(_highs[i] - _lows[i],
                        abs(_highs[i] - _closes[i-1]),
                        abs(_lows[i]  - _closes[i-1]))
                    for i in range(-5, 0)]
            _avg_tr = sum(_tr5[:-1]) / (len(_tr5) - 1)   # avg of 4 prior bars
            _cur_tr = _tr5[-1]
            if _avg_tr > 0:
                _atr_ratio = _cur_tr / _avg_tr
                if _atr_ratio < 0.80:
                    atr_adj  = +4
                    atr_note = f" | ATR ratio={_atr_ratio:.2f} (consolidating → +4 bonus)"
                elif _atr_ratio <= 1.20:
                    atr_adj  = +1
                    atr_note = f" | ATR ratio={_atr_ratio:.2f} (normal)"
                elif _atr_ratio <= 1.50:
                    atr_adj  = -3
                    atr_note = f" | ATR ratio={_atr_ratio:.2f} (expanding, caution -3)"
                else:
                    atr_adj  = -6
                    atr_note = f" | ATR ratio={_atr_ratio:.2f} (chasing expansion -6)"
    except Exception:
        pass

    # ─── Aggregate ────────────────────────────────────────────────────────────
    raw_total = sum(c['score'] for c in comps.values())
    total     = max(0, min(raw_total + atr_adj, 100))

    if   total >= 80:        quality = 'STRONG'
    elif total >= 65:        quality = 'GOOD'
    elif total >= 50:        quality = 'MODERATE'
    elif total >= TRADE_MIN: quality = 'MARGINAL'
    else:                    quality = 'WEAK'

    # Gate: aggregate score determines entry. No single component can block.
    # SKIP fires only when TOTAL is too low — the combination is bad, not one factor.
    _skip_min = POST11_SCORE_SKIP_MIN   # configurable via config.POST11_SCORE_SKIP_MIN
    if   total >= STRONG_MIN: gate = 'STRONG';   lot_suggestion = 2
    elif total >= TRADE_MIN:  gate = 'TRADE';    lot_suggestion = 1
    elif total >= _skip_min:  gate = 'MARGINAL'; lot_suggestion = 1
    else:                     gate = 'SKIP';     lot_suggestion = 0   # aggregate too weak

    summary = (
        f"POST11 {quality} ({total}/100) | "
        f"{entry_time.strftime('%H:%M')} | gate={gate} | lots={lot_suggestion}x"
        + atr_note
    )
    return {
        'total'         : total,
        'quality'       : quality,
        'gate'          : gate,
        'lot_suggestion': lot_suggestion,
        'components'    : comps,
        'atr_adj'       : atr_adj,
        'summary'       : summary,
    }


def format_score(result: dict) -> str:
    """Multi-line log string, same format as signal_scorer.format_score()."""
    lines = [f"  [POST11] {result['summary']}"]
    for name, c in result['components'].items():
        pct    = c['pct']
        filled = '█' * (pct // 10)
        empty  = '░' * (10 - pct // 10)
        lines.append(
            f"  [POST11]   {name:15s} {filled}{empty} {c['score']:2d}/{c['max']:2d}"
            f" — {c['reason']}"
        )
    return '\n'.join(lines)


# ── Internal ──────────────────────────────────────────────────────────────────

def _c(pts: int, key: str, reason: str) -> dict:
    m = WEIGHTS[key]
    return {'score': pts, 'max': m, 'pct': round(pts / m * 100) if m else 0, 'reason': reason}
