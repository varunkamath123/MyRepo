# -*- coding: utf-8 -*-
"""
Weighted Composite Signal Scorer
=================================
Combines lagging technicals (EMA, ADX, VWAP) with structural/leading
indicators (15m HTF alignment, PCR, OI zones) into a single 0–100 score.

Literature basis
----------------
  HTF alignment   : 2-timeframe confirmation raises WR 39% → 58%
                    (Garg et al. 2019; multiple retail algo backtests)
  Equal weighting : Most robust with < 100 trades; IC-weighting overfits
                    with small samples (Lo 2002; multi-factor momentum studies)
  PCR directional : OI-based PCR more predictive than volume PCR (Jena 2019)
                    Directional in moderate range (NIFTY 0.8–1.3), contrarian
                    only at extremes. Bullish when put writing dominates (PCR>1.2)
  ADX weighting   : IC ~15–20% for momentum regime filters; graduated scoring
                    more informative than binary pass/fail (Aronson 2006)
  OI zones        : Acts as supply/demand context — not a momentum filter.
                    Lower weight (10%) because EOD data has 1-day lag.

Component weights (must sum to 100)
------------------------------------
  htf_align     20   Strongest predictor — 15m direction adds ~19% WR lift
  adx_mag       15   Trend strength — graduated (not binary) for more signal
  pcr_align     20   Leading/structural — partially known before market opens
  ema_fresh     15   Core signal — freshness decays (old cross = weaker)
  vwap_dist     10   Directional conviction — near-VWAP = ambiguous signal
  oi_zone        5   Structural S/R context — lower weight (EOD data)
  max_pain       5   MM gravitational force — live MaxPain proximity (already computed)
  consolidation 10   ATR compression quality — coiled spring = higher follow-through
  ─────────────
  Total        100

Phase control
-------------
  PHASE 1 (active now)  : Pure observation — log score, no lot changes
  PHASE 2 (after 30 trades) : Soft gate — low score → cap to 1 lot
  PHASE 3 (after validation): Hard gate — very low score → skip trade

Public interface
----------------
  score(signal_type, df, htf, oc, oz, lookback_used=5) → dict
  format_score(result) → str
  PHASE — module-level constant to promote between phases
"""

from __future__ import annotations

import logging
import math

import pandas as pd

_log = logging.getLogger(__name__)

# ── Component weights (must sum to 100) ──────────────────────────────────────

WEIGHTS: dict[str, int] = {
    'htf_align'    : 20,   # 15m higher-timeframe direction alignment
    'adx_mag'      : 15,   # ADX magnitude (graduated trend strength)
    'pcr_align'    : 20,   # PCR directional alignment (leading/structural)
    'ema_fresh'    : 15,   # EMA crossover freshness (bars since cross)
    'vwap_dist'    : 10,   # VWAP distance (directional conviction proxy)
    'oi_zone'      :  5,   # OI zone position (structural S/R — lower: EOD data lag)
    'max_pain'     :  5,   # MaxPain proximity — MM gravitational force (live data)
    'consolidation': 10,   # ATR compression quality — coiled spring before signal
}

assert sum(WEIGHTS.values()) == 100, \
    f"WEIGHTS must sum to 100, got {sum(WEIGHTS.values())}"

# ── Phase control ─────────────────────────────────────────────────────────────
# Promote to Phase 2 after ~30 paper trades by changing PHASE = 2 here.
# Promote to Phase 3 after validating that low-score trades underperform.

PHASE         = 2   # 1=observe only | 2=soft gate (lots) | 3=hard gate (skip)
SOFT_GATE_MIN = 40  # Phase 2: below this → force 1 lot regardless of strength
HARD_GATE_MIN = 30  # Phase 3: below this → skip trade
LOT_BOOST_MIN = 65  # Phase 2+: score ≥ this → suggest 2 lots


# ── Public API ────────────────────────────────────────────────────────────────

def score(
    signal_type  : str,
    df,                         # pandas DataFrame — last row is current bar
    htf          : dict,        # get_htf_context() → {supertrend_15m, ema_15m_trend}
    oc           : dict,        # get_option_chain_context() → {pcr, oi_bias, ...}
    oz           : dict,        # get_zone_signal() result → {action, score, reason}
    lookback_used: int = 5,     # config.EMA_CROSSOVER_LOOKBACK
    path         : str = 'vX',  # signal path — OR_BREAK skips ema_fresh penalty
) -> dict:
    """
    Compute composite signal score (0–100) from 6 weighted components.

    Parameters
    ----------
    signal_type   : 'CALL' or 'PUT'
    df            : DataFrame with EMA_fast, EMA_slow, ADX, VWAP, Close columns
    htf           : Higher-timeframe context dict
    oc            : Option chain context dict
    oz            : OI zone signal dict (may be {} if unavailable)
    lookback_used : Bars searched for EMA cross (EMA_CROSSOVER_LOOKBACK)

    Returns
    -------
    dict with keys:
      total          : int 0-100  composite score
      components     : {name: {score, max, pct, reason}}
      lot_suggestion : 1 or 2 (based on score — active in Phase 2+)
      gate           : 'TRADE' | 'REDUCE' | 'SKIP' (active in Phase 2/3)
      quality        : 'STRONG'|'GOOD'|'MODERATE'|'WEAK'|'POOR'
      summary        : human-readable one-liner
      phase          : current PHASE value
    """
    components: dict[str, dict] = {}

    # ── 1. HTF Alignment (25 pts) ─────────────────────────────────────────────
    # Both 15m SuperTrend AND 15m EMA agree → full credit
    # Only one aligns                        → half credit
    # Both oppose                            → zero
    #
    # supertrend_15m is int: +1=BULL, -1=BEAR (None if unavailable)
    # ema_15m_trend  is str: 'BULL'/'BEAR'    (None if unavailable)

    st_val  = htf.get('supertrend_15m')           # +1, -1, or None
    ema_15m = htf.get('ema_15m_trend')             # 'BULL', 'BEAR', or None

    expected = 'BULL' if signal_type == 'CALL' else 'BEAR'

    st_agree  = (st_val is not None) and ({1: 'BULL', -1: 'BEAR'}.get(st_val) == expected)
    ema_agree = (ema_15m is not None) and (ema_15m == expected)
    st_label  = {1: 'BULL', -1: 'BEAR'}.get(st_val, '?')

    if st_val is None and ema_15m is None:
        # No 15m data → give neutral (half)
        htf_pts    = round(WEIGHTS['htf_align'] * 0.50)   # 12
        htf_reason = "15m data unavailable — neutral"
    elif st_agree and ema_agree:
        htf_pts    = WEIGHTS['htf_align']                  # 25
        htf_reason = f"15m ST={st_label} + EMA={ema_15m} both aligned ✓✓"
    elif st_agree or ema_agree:
        htf_pts    = round(WEIGHTS['htf_align'] * 0.50)   # 12 (rounded)
        aligned    = f"ST={st_label}" if st_agree else f"EMA={ema_15m}"
        opposed    = f"EMA={ema_15m}" if st_agree else f"ST={st_label}"
        htf_reason = f"15m {aligned} aligned ✓ | {opposed} opposed ✗ (partial)"
    else:
        htf_pts    = 0
        htf_reason = f"15m ST={st_label} + EMA={ema_15m} both oppose {signal_type} ✗✗"

    components['htf_align'] = _comp(htf_pts, 'htf_align', htf_reason)

    # ── 2. ADX Magnitude (20 pts, graduated) ─────────────────────────────────
    # Graduated scoring captures that ADX=40 is meaningfully stronger than ADX=26.
    # Thresholds from our backtest + standard TA literature:
    #   ≥40 = extreme trend (full)
    #   35–40 = strong      (80%)
    #   30–35 = moderate    (60%)
    #   25–30 = minimum OK  (40%)
    #   <25  = weak         (0% — below entry threshold)

    row = df.iloc[-1]
    adx = float(row.get('ADX', 0) or 0)

    if adx >= 40:
        adx_pts    = WEIGHTS['adx_mag']                      # 20
        adx_reason = f"ADX={adx:.1f} (extreme ≥40) ✓✓"
    elif adx >= 35:
        adx_pts    = round(WEIGHTS['adx_mag'] * 0.80)       # 16
        adx_reason = f"ADX={adx:.1f} (strong 35–40) ✓"
    elif adx >= 30:
        adx_pts    = round(WEIGHTS['adx_mag'] * 0.60)       # 12
        adx_reason = f"ADX={adx:.1f} (moderate 30–35)"
    elif adx >= 25:
        adx_pts    = round(WEIGHTS['adx_mag'] * 0.40)       # 8
        adx_reason = f"ADX={adx:.1f} (minimum 25–30)"
    else:
        adx_pts    = 0
        adx_reason = f"ADX={adx:.1f} (<25 — below threshold)"

    components['adx_mag'] = _comp(adx_pts, 'adx_mag', adx_reason)

    # ── 3. PCR Alignment (20 pts) ─────────────────────────────────────────────
    # PCR interpretation (OI-based, NIFTY/BANKNIFTY empirical range 0.8–1.3):
    #   PCR > 1.2 → heavy put writing → market makers expect upside → CALL ✓✓
    #   PCR 1.0–1.2 → mild put dominance → neutral-bullish → CALL ✓ (partial)
    #   PCR 0.7–1.0 → balanced to slight call dominance → neutral
    #   PCR < 0.7 → heavy call writing → market makers expect downside → PUT ✓✓
    # (Source: Jena et al. 2019 — OI PCR more predictive than volume PCR)
    # PCR thresholds for conflict (opposing signal) remove credit entirely.

    pcr = oc.get('pcr')

    if pcr is None:
        pcr_pts    = round(WEIGHTS['pcr_align'] * 0.50)     # 10 — unknown, neutral
        pcr_reason = "PCR=None (unavailable) — neutral assumed"
    elif signal_type == 'CALL':
        if pcr > 1.2:
            pcr_pts    = WEIGHTS['pcr_align']                # 20
            pcr_reason = f"PCR={pcr:.2f} > 1.2 (heavy put writing → bullish ✓✓)"
        elif pcr >= 1.0:
            pcr_pts    = round(WEIGHTS['pcr_align'] * 0.60) # 12
            pcr_reason = f"PCR={pcr:.2f} 1.0–1.2 (mild bullish, partial ✓)"
        elif pcr >= 0.7:
            pcr_pts    = round(WEIGHTS['pcr_align'] * 0.25) # 5
            pcr_reason = f"PCR={pcr:.2f} 0.7–1.0 (neutral-bearish, caution)"
        else:
            pcr_pts    = 0
            pcr_reason = f"PCR={pcr:.2f} < 0.7 (call writing → opposes CALL ✗)"
    else:  # PUT
        if pcr < 0.7:
            pcr_pts    = WEIGHTS['pcr_align']                # 20
            pcr_reason = f"PCR={pcr:.2f} < 0.7 (heavy call writing → bearish ✓✓)"
        elif pcr <= 1.0:
            pcr_pts    = round(WEIGHTS['pcr_align'] * 0.60) # 12
            pcr_reason = f"PCR={pcr:.2f} 0.7–1.0 (mild bearish, partial ✓)"
        elif pcr <= 1.2:
            pcr_pts    = round(WEIGHTS['pcr_align'] * 0.25) # 5
            pcr_reason = f"PCR={pcr:.2f} 1.0–1.2 (neutral-bullish, caution)"
        else:
            pcr_pts    = 0
            pcr_reason = f"PCR={pcr:.2f} > 1.2 (put writing → opposes PUT ✗)"

    components['pcr_align'] = _comp(pcr_pts, 'pcr_align', pcr_reason)

    # ── 4. EMA Crossover Freshness (15 pts) ───────────────────────────────────
    # A very recent cross (1–2 bars ago) is the clearest momentum signal.
    # An old cross that has been running for 8+ bars is stale — price may be
    # extended. Freshness decays: 1-2 bars=100%, 3-4=75%, 5-7=50%, 8-10=25%, >10=0%
    # We detect the cross by scanning backwards for the last EMA direction flip.

    try:
        n      = min(lookback_used + 8, len(df))
        window = df.iloc[-n:]
        ema_f  = window['EMA_fast'].values
        ema_s  = window['EMA_slow'].values
        is_bull = ema_f > ema_s

        bars_since_cross = len(is_bull)   # default: no cross found = old
        for j in range(len(is_bull) - 1, 0, -1):
            if is_bull[j] != is_bull[j - 1]:
                bars_since_cross = len(is_bull) - 1 - j
                break

        if bars_since_cross <= 2:
            fresh_pts    = WEIGHTS['ema_fresh']                    # 15
            fresh_reason = f"EMA cross {bars_since_cross}b ago (very fresh ✓✓)"
        elif bars_since_cross <= 4:
            fresh_pts    = round(WEIGHTS['ema_fresh'] * 0.75)     # 11
            fresh_reason = f"EMA cross {bars_since_cross}b ago (recent ✓)"
        elif bars_since_cross <= 7:
            fresh_pts    = round(WEIGHTS['ema_fresh'] * 0.50)     # 7
            fresh_reason = f"EMA cross {bars_since_cross}b ago (moderate)"
        elif bars_since_cross <= 10:
            fresh_pts    = round(WEIGHTS['ema_fresh'] * 0.25)     # 4
            fresh_reason = f"EMA cross {bars_since_cross}b ago (stale)"
        else:
            fresh_pts    = 0
            fresh_reason = f"EMA cross {bars_since_cross}+ bars ago (very stale)"

    except Exception as exc:
        _log.warning(f"[signal_scorer] EMA freshness error: {exc}")
        fresh_pts    = 0                                           # 0 on error — don't inflate score
        fresh_reason = f"EMA freshness error ({exc})"

    # OR_BREAK / CONT paths: the trigger itself IS the fresh signal.
    # EMA cross freshness is irrelevant and unfairly penalises these valid entries.
    #   OR_BREAK — price breaking the Opening Range replaces EMA cross freshness.
    #   CONT     — EMA spread widening on a pre-window trend; cross is intentionally old.
    if path in ('OR_BREAK', 'CONT'):
        fresh_pts    = WEIGHTS['ema_fresh']   # full 15 pts
        fresh_reason = f"{path} path — ema_fresh N/A (continuation trigger ✓)"
    components['ema_fresh'] = _comp(fresh_pts, 'ema_fresh', fresh_reason)

    # ── 5. VWAP Distance (10 pts) ─────────────────────────────────────────────
    # Clear separation from VWAP = strong directional commitment.
    # Near VWAP = price still in equilibrium zone = ambiguous signal.
    # Thresholds (% of VWAP): <0.05%=marginal, 0.05-0.10%=weak, 0.10-0.20%=moderate, ≥0.20%=strong
    # (These align with our bot.py signal_strength 0.10% threshold but use
    #  graduated scoring instead of binary.)

    try:
        vwap  = float(row.get('VWAP', 0) or 0)
        close = float(row['Close'])
        if vwap > 0:
            dist_pct = abs(close - vwap) / vwap * 100   # in percent
            if dist_pct >= 0.20:
                vwap_pts    = WEIGHTS['vwap_dist']                   # 10
                vwap_reason = f"VWAP dist={dist_pct:.3f}% (strong ≥0.20% ✓✓)"
            elif dist_pct >= 0.10:
                vwap_pts    = round(WEIGHTS['vwap_dist'] * 0.70)    # 7
                vwap_reason = f"VWAP dist={dist_pct:.3f}% (moderate 0.10–0.20% ✓)"
            elif dist_pct >= 0.05:
                vwap_pts    = round(WEIGHTS['vwap_dist'] * 0.40)    # 4
                vwap_reason = f"VWAP dist={dist_pct:.3f}% (weak 0.05–0.10%)"
            else:
                vwap_pts    = round(WEIGHTS['vwap_dist'] * 0.10)    # 1
                vwap_reason = f"VWAP dist={dist_pct:.3f}% (marginal <0.05%)"
        else:
            vwap_pts    = round(WEIGHTS['vwap_dist'] * 0.50)        # 5 — neutral
            vwap_reason = "VWAP unavailable — neutral"
    except Exception as exc:
        vwap_pts    = round(WEIGHTS['vwap_dist'] * 0.50)            # 5
        vwap_reason = f"VWAP error ({exc})"

    components['vwap_dist'] = _comp(vwap_pts, 'vwap_dist', vwap_reason)

    # ── 6. OI Zone Position (5 pts, reduced from 10) ────────────────────────
    # Reuses the already-computed get_zone_signal() result.
    # Weight reduced from 10→5 to make room for live MaxPain scoring.
    # BOOST (broke through OI wall = gamma squeeze likely) →  5
    # TAKE  (clear OI space, neutral context)             →  3-4
    # REDUCE (approaching OI wall, may cap the move)      →  1
    # SKIP  (price hugging adverse OI wall)               →  0
    # Unknown/missing                                     →  2-3

    oz_action   = oz.get('action',  'TAKE') if oz else 'TAKE'
    oz_subscore = oz.get('score',   50)     if oz else 50
    oz_detail   = oz.get('reason',  'n/a')  if oz else 'unavailable'

    if oz_action == 'BOOST':
        oi_pts    = WEIGHTS['oi_zone']                     # 10
        oi_reason = f"OI-ZONE=BOOST (z={oz_subscore}) — {oz_detail}"
    elif oz_action == 'TAKE':
        oi_pts    = round(WEIGHTS['oi_zone'] * 0.70)      # 7
        oi_reason = f"OI-ZONE=TAKE (z={oz_subscore}) — {oz_detail}"
    elif oz_action == 'REDUCE':
        oi_pts    = round(WEIGHTS['oi_zone'] * 0.20)      # 2
        oi_reason = f"OI-ZONE=REDUCE (z={oz_subscore}) — {oz_detail}"
    elif oz_action == 'SKIP':
        oi_pts    = 0
        oi_reason = f"OI-ZONE=SKIP (z={oz_subscore}) — {oz_detail}"
    else:
        oi_pts    = round(WEIGHTS['oi_zone'] * 0.50)      # 5
        oi_reason = f"OI-ZONE={oz_action} (unknown) — neutral"

    components['oi_zone'] = _comp(oi_pts, 'oi_zone', oi_reason)

    # ── 7. Max Pain Proximity (5 pts) ─────────────────────────────────────────
    # MaxPain = strike where OI is most balanced = where MM want price to expire.
    # Price BELOW MaxPain → MM gravitational pull upward → CALL tailwind.
    # Price ABOVE MaxPain → MM gravitational pull downward → PUT tailwind.
    # |dist| > 1.0% = strong force; |dist| < 0.3% = price near MaxPain (neutral).
    # MaxPain is already computed live by nse_oi.py (no new data needed).

    try:
        max_pain = oc.get('max_pain')
        close    = float(row['Close'])

        if max_pain is None or max_pain == 0:
            mp_pts    = round(WEIGHTS['max_pain'] * 0.50)   # 2-3 — neutral
            mp_reason = "MaxPain=None (unavailable) — neutral"
        else:
            mp_dist_pct = (float(max_pain) - close) / close * 100
            # mp_dist_pct > 0 → MaxPain above price → CALL tailwind / PUT headwind
            # mp_dist_pct < 0 → MaxPain below price → PUT tailwind / CALL headwind
            if signal_type == 'CALL':
                if mp_dist_pct > 1.0:
                    mp_pts    = WEIGHTS['max_pain']                    # 5
                    mp_reason = (f"MaxPain={max_pain:,.0f} ({mp_dist_pct:+.1f}%) — "
                                 f"strong MM tailwind for CALL ✓✓")
                elif mp_dist_pct > 0.3:
                    mp_pts    = round(WEIGHTS['max_pain'] * 0.60)      # 3
                    mp_reason = (f"MaxPain={max_pain:,.0f} ({mp_dist_pct:+.1f}%) — "
                                 f"moderate MM tailwind for CALL ✓")
                elif mp_dist_pct > -0.3:
                    mp_pts    = round(WEIGHTS['max_pain'] * 0.40)      # 2
                    mp_reason = (f"MaxPain={max_pain:,.0f} ({mp_dist_pct:+.1f}%) — "
                                 f"near MaxPain (neutral zone)")
                else:
                    mp_pts    = 0
                    mp_reason = (f"MaxPain={max_pain:,.0f} ({mp_dist_pct:+.1f}%) — "
                                 f"price above MaxPain (MM headwind for CALL ✗)")
            else:  # PUT
                if mp_dist_pct < -1.0:
                    mp_pts    = WEIGHTS['max_pain']                    # 5
                    mp_reason = (f"MaxPain={max_pain:,.0f} ({mp_dist_pct:+.1f}%) — "
                                 f"strong MM tailwind for PUT ✓✓")
                elif mp_dist_pct < -0.3:
                    mp_pts    = round(WEIGHTS['max_pain'] * 0.60)      # 3
                    mp_reason = (f"MaxPain={max_pain:,.0f} ({mp_dist_pct:+.1f}%) — "
                                 f"moderate MM tailwind for PUT ✓")
                elif mp_dist_pct < 0.3:
                    mp_pts    = round(WEIGHTS['max_pain'] * 0.40)      # 2
                    mp_reason = (f"MaxPain={max_pain:,.0f} ({mp_dist_pct:+.1f}%) — "
                                 f"near MaxPain (neutral zone)")
                else:
                    mp_pts    = 0
                    mp_reason = (f"MaxPain={max_pain:,.0f} ({mp_dist_pct:+.1f}%) — "
                                 f"price below MaxPain (MM headwind for PUT ✗)")
    except Exception as exc:
        _log.warning(f"[signal_scorer] MaxPain error: {exc}")
        mp_pts    = round(WEIGHTS['max_pain'] * 0.50)
        mp_reason = f"MaxPain error ({exc})"

    components['max_pain'] = _comp(mp_pts, 'max_pain', mp_reason)

    # ── 8. Consolidation Quality (10 pts) ─────────────────────────────────────
    # An EMA cross emerging from ATR compression (coiled spring) has far higher
    # follow-through than a cross in a choppy, expanding-ATR environment.
    # ATR ratio = ATR_now / ATR_20bar_mean:
    #   < 0.70 → compressed (coiled spring) → full credit
    #   0.70–0.90 → mild compression → partial credit
    #   0.90–1.10 → normal → neutral credit
    #   1.10–1.30 → expanding → caution (chasing)
    #   > 1.30 → strongly expanding → 0 (worst time to enter)
    # (Aronson 2006; breakout-from-compression literature)

    try:
        atr_now = float(row.get('ATR14', float('nan')))
        if pd.isna(atr_now) or len(df) < 22:
            # Not enough bars for meaningful ATR comparison
            cons_pts    = round(WEIGHTS['consolidation'] * 0.50)    # 5 — neutral
            cons_reason = "ATR14 insufficient data — neutral"
        else:
            atr_mean_20 = df['ATR14'].iloc[-22:-2].mean()   # 20 bars before current
            if pd.isna(atr_mean_20) or atr_mean_20 <= 0:
                cons_pts    = round(WEIGHTS['consolidation'] * 0.50)
                cons_reason = "ATR mean unavailable — neutral"
            else:
                atr_ratio = atr_now / atr_mean_20
                if atr_ratio < 0.70:
                    cons_pts    = WEIGHTS['consolidation']              # 10
                    cons_reason = (f"ATR ratio={atr_ratio:.2f} (<0.70 compressed — "
                                   f"coiled spring ✓✓)")
                elif atr_ratio < 0.90:
                    cons_pts    = round(WEIGHTS['consolidation'] * 0.70)   # 7
                    cons_reason = (f"ATR ratio={atr_ratio:.2f} "
                                   f"(mild compression 0.70–0.90 ✓)")
                elif atr_ratio < 1.10:
                    cons_pts    = round(WEIGHTS['consolidation'] * 0.50)   # 5
                    cons_reason = (f"ATR ratio={atr_ratio:.2f} (normal ATR range)")
                elif atr_ratio < 1.30:
                    cons_pts    = round(WEIGHTS['consolidation'] * 0.30)   # 3
                    cons_reason = (f"ATR ratio={atr_ratio:.2f} "
                                   f"(ATR expanding — caution)")
                else:
                    cons_pts    = 0
                    cons_reason = (f"ATR ratio={atr_ratio:.2f} "
                                   f"(>1.30 chasing expansion — poor quality ✗)")
    except Exception as exc:
        _log.warning(f"[signal_scorer] Consolidation quality error: {exc}")
        cons_pts    = round(WEIGHTS['consolidation'] * 0.50)
        cons_reason = f"Consolidation error ({exc})"

    components['consolidation'] = _comp(cons_pts, 'consolidation', cons_reason)

    # ── Aggregate ─────────────────────────────────────────────────────────────
    total = sum(c['score'] for c in components.values())

    # Quality label
    if total >= 80:
        quality = 'STRONG'
    elif total >= 65:
        quality = 'GOOD'
    elif total >= 50:
        quality = 'MODERATE'
    elif total >= 35:
        quality = 'WEAK'
    else:
        quality = 'POOR'

    # Lot suggestion + gate (Phase 2+ active; Phase 1 = logging only)
    if total >= LOT_BOOST_MIN:
        lot_suggestion = 2
        gate = 'TRADE'
    elif total >= SOFT_GATE_MIN:
        lot_suggestion = 1
        gate = 'TRADE'
    elif total >= HARD_GATE_MIN:
        lot_suggestion = 1
        gate = 'REDUCE'     # Phase 3: consider reducing or skipping
    else:
        lot_suggestion = 1
        gate = 'SKIP'       # Phase 3: skip trade

    summary = (
        f"{quality} ({total}/100) | gate={gate} | lots={lot_suggestion}x "
        f"[Phase {PHASE} — {'observe only' if PHASE == 1 else 'active'}]"
    )

    return {
        'total'         : total,
        'components'    : components,
        'lot_suggestion': lot_suggestion,
        'gate'          : gate,
        'quality'       : quality,
        'summary'       : summary,
        'phase'         : PHASE,
    }


def format_score(result: dict) -> str:
    """
    Return a multi-line human-readable log string for the composite score.

    Example output:
      [SCORER] GOOD (68/100) | gate=TRADE | lots=2x [Phase 1 — observe only]
      [SCORER]   htf_align   ████████░░  20/25 — 15m ST=BULL + EMA=BULL aligned ✓✓
      [SCORER]   adx_mag     ████████░░  16/20 — ADX=36.2 (strong 35-40) ✓
      [SCORER]   pcr_align   ████████████ 20/20 — PCR=1.32 > 1.2 (bullish ✓✓)
      [SCORER]   ema_fresh   ████████░░░  11/15 — EMA cross 4b ago (recent ✓)
      [SCORER]   vwap_dist   ████░░░░░░    4/10 — VWAP dist=0.07% (weak)
      [SCORER]   oi_zone     ███████░░░    7/10 — OI-ZONE=TAKE (z=60)
    """
    lines = [f"  [SCORER] {result['summary']}"]
    for name, c in result['components'].items():
        pct   = c['pct']
        filled = '█' * (pct // 10)
        empty  = '░' * (10 - pct // 10)
        lines.append(
            f"  [SCORER]   {name:12s} {filled}{empty} {c['score']:2d}/{c['max']:2d} — {c['reason']}"
        )
    return '\n'.join(lines)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _comp(pts: int, key: str, reason: str) -> dict:
    """Build a component dict with score, max, pct, reason."""
    max_pts = WEIGHTS[key]
    return {
        'score' : pts,
        'max'   : max_pts,
        'pct'   : round(pts / max_pts * 100) if max_pts else 0,
        'reason': reason,
    }
