"""
unified_scorer.py — Single time-weighted scoring model.

Score(direction, t) = sum(weight_i(band) * ratio_i(direction)), range 0-100.
Threshold (default 55) gates entry. Weights shift across 5 time bands to reflect
which signals matter most at each phase of the day.

Time bands: '09:30', '10:00', '11:00', '12:00', '13:00'
Each band has 11 components summing to 100.

Scoring evolution (auto-upgrading via data/scorer_state.json):
  Phase 1 (live May 2026, ~11 trades):
    ADX: rolling 60-bar percentile rank within today's session — partial credit
         for moderate readings, full credit only when truly exceptional.
    OR Thrust: width-normalised extension — how many OR-widths did price move
         beyond the boundary?  Replaces flat 0.10% binary gate.
    All other components remain binary (aligned = full weight, else 0).
  Phase 2 (auto at 30 completed trades):
    Momentum: rolling percentile of 3-bar ROC magnitude replaces binary
         trending/not-trending check.  Gives partial credit for weak momentum.
  Phase 3 (auto at 80 completed trades):
    Weights replaced by logistic regression coefficients fitted on actual
    trade outcomes (written to unified_weights.json by weekly_analyzer.py).
    Component evaluation unchanged; only the weights shift to reflect what
    has empirically predicted wins in live trading.

Columns used from df (5m bars):
  Close, ADX, DI_plus, DI_minus, VWAP, EMA_fast, EMA_slow, ST_5m

st15_val : +1 bull / -1 bear  (15m SuperTrend)
oi_bias  : 'CONFIRM' / 'NEUTRAL' / 'REJECT'
"""
from __future__ import annotations

import json
import os
import time

import pandas as pd

# scipy optional — graceful fallback to binary scoring if unavailable
try:
    from scipy.stats import percentileofscore as _pctrank
    _SCIPY_OK = True
except ImportError:
    _SCIPY_OK = False

_DIR          = os.path.dirname(os.path.abspath(__file__))
_WEIGHTS_PATH = os.path.join(_DIR, '..', 'data', 'unified_weights.json')
_STATE_PATH   = os.path.join(_DIR, '..', 'data', 'scorer_state.json')

# ── Default weights by time band (each band must sum to 100) ─────────────────
DEFAULT_WEIGHTS: dict[str, dict[str, int]] = {
    '09:30': {
        'or_breakout': 25, 'or_thrust': 10, 'adx': 15, 'di_align': 10,
        'st5':  5, 'st15': 10, 'ema':  5, 'vwap':  5,
        'oi':   5, 'exhaustion':  0, 'momentum': 10,
    },
    '10:00': {
        'or_breakout': 20, 'or_thrust': 10, 'adx': 15, 'di_align': 10,
        'st5':  5, 'st15': 15, 'ema': 10, 'vwap':  5,
        'oi':   5, 'exhaustion':  0, 'momentum':  5,
    },
    '11:00': {
        'or_breakout': 10, 'or_thrust':  5, 'adx': 15, 'di_align': 10,
        'st5': 10, 'st15': 15, 'ema': 10, 'vwap':  5,
        'oi':  10, 'exhaustion':  5, 'momentum':  5,
    },
    '12:00': {
        'or_breakout':  5, 'or_thrust':  0, 'adx': 15, 'di_align': 10,
        'st5': 10, 'st15': 20, 'ema': 10, 'vwap':  5,
        'oi':  10, 'exhaustion': 10, 'momentum':  5,
    },
    '13:00': {
        'or_breakout':  0, 'or_thrust':  0, 'adx': 15, 'di_align':  5,
        'st5': 10, 'st15': 25, 'ema': 10, 'vwap':  5,
        'oi':  10, 'exhaustion': 15, 'momentum':  5,
    },
}

_BANDS = ['09:30', '10:00', '11:00', '12:00', '13:00']


# ── Scorer phase (reads scorer_state.json, cached 60 s) ──────────────────────
_phase_cache: dict = {'phase': 1, '_ts': 0.0}


def _get_phase() -> int:
    """Return active scorer phase (1/2/3). Cached 60 s to avoid per-call disk reads."""
    now = time.monotonic()
    if now - _phase_cache['_ts'] > 60.0:
        try:
            if os.path.exists(_STATE_PATH):
                with open(_STATE_PATH, encoding='utf-8') as f:
                    _phase_cache.update(json.load(f))
        except Exception:
            pass
        _phase_cache['_ts'] = now
    return int(_phase_cache.get('phase', 1))


# ── Percentile helper ─────────────────────────────────────────────────────────
def _percentile_score(value: float, history: list, fallback_threshold: float) -> float:
    """
    Where does `value` sit in `history`?  Returns 0.0–1.0.

    Falls back to a binary pass/fail at `fallback_threshold` when:
      - scipy is not installed, OR
      - history has fewer than 20 valid data points (too thin to rank reliably).
    """
    clean = [float(v) for v in history
             if v is not None and not (isinstance(v, float) and pd.isna(v))]
    if not _SCIPY_OK or len(clean) < 20:
        return 1.0 if value >= fallback_threshold else 0.0
    return _pctrank(clean, value, kind='rank') / 100.0


# ── Weight I/O ────────────────────────────────────────────────────────────────
def get_time_band(now_str: str) -> str:
    for band in reversed(_BANDS):
        if now_str >= band:
            return band
    return _BANDS[0]


def load_weights() -> dict[str, dict[str, int]]:
    try:
        if os.path.exists(_WEIGHTS_PATH):
            with open(_WEIGHTS_PATH, encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return DEFAULT_WEIGHTS


def save_weights(weights: dict[str, dict[str, int]]) -> None:
    os.makedirs(os.path.dirname(_WEIGHTS_PATH), exist_ok=True)
    with open(_WEIGHTS_PATH, 'w', encoding='utf-8') as f:
        json.dump(weights, f, indent=2)


# ── Main scoring function ─────────────────────────────────────────────────────
def compute_score(
    direction: str,
    df: 'pd.DataFrame',
    or_hi: 'float | None',
    or_lo: 'float | None',
    st15_val: 'int | None',
    oi_bias: str,
    now_str: str,
    morning_adx_peak: float = 0.0,
    morning_dir: 'str | None' = None,
    weights: 'dict | None' = None,
) -> dict:
    """
    Compute unified entry score for a direction signal at the current time.

    Args:
        direction        : 'CALL' or 'PUT'
        df               : 5m OHLCV + indicators DataFrame (uses last row)
        or_hi / or_lo    : Opening Range boundaries (None if not established)
        st15_val         : 15m SuperTrend (+1 bull / -1 bear / None)
        oi_bias          : 'CONFIRM' / 'NEUTRAL' / 'REJECT'
        now_str          : current time as 'HH:MM'
        morning_adx_peak : peak ADX seen during OR window (for exhaustion check)
        morning_dir      : dominant morning direction 'CALL'/'PUT' (for exhaustion)
        weights          : override weight dict (None = load from disk / defaults)

    Returns dict:
        score       : int 0-100
        band        : str (active time band key)
        phase       : int (1/2/3 — active scoring phase)
        components  : dict name → (weight, ratio_0_to_1, pts_awarded)
    """
    if weights is None:
        weights = load_weights()

    band  = get_time_band(now_str)
    w     = weights.get(band, DEFAULT_WEIGHTS[band])
    phase = _get_phase()

    row   = df.iloc[-1]
    px    = float(row.get('Close',     float('nan')))
    adx   = float(row.get('ADX',      0.0) or 0.0)
    dip   = float(row.get('DI_plus',  0.0) or 0.0)
    dim   = float(row.get('DI_minus', 0.0) or 0.0)
    vwap  = float(row.get('VWAP',     float('nan')))
    ema_f = float(row.get('EMA_fast', float('nan')))
    ema_s = float(row.get('EMA_slow', float('nan')))
    st5r  = row.get('ST_5m', None)

    is_call = direction == 'CALL'
    components: dict[str, tuple] = {}
    score = 0

    # ── Binary award (aligned = full weight, else 0) ─────────────────────────
    def award(name: str, aligned: 'bool | None') -> int:
        wt = w.get(name, 0)
        if wt == 0 or aligned is None:
            components[name] = (wt, 0.0, 0)
            return 0
        pts = wt if aligned else 0
        components[name] = (wt, 1.0 if aligned else 0.0, pts)
        return pts

    # ── Continuous award (ratio 0.0–1.0 → proportional pts) ─────────────────
    def award_pct(name: str, ratio: float) -> int:
        wt  = w.get(name, 0)
        pts = round(wt * max(0.0, min(1.0, ratio)))
        components[name] = (wt, round(ratio, 3), pts)
        return pts

    # ── 1. OR Breakout — price closed beyond OR boundary (binary) ────────────
    if or_hi is not None and or_lo is not None and not pd.isna(px):
        score += award('or_breakout',
                       (px > or_hi) if is_call else (px < or_lo))
    else:
        components['or_breakout'] = (w.get('or_breakout', 0), 0.0, 0)

    # ── 2. OR Thrust — width-normalised extension (continuous, Phase 1+) ─────
    #
    #  Old behaviour: binary pass/fail at 0.10% absolute extension.
    #  New behaviour: score scales with (extension / 0.5 × OR-width).
    #    0.5× OR-width extension = full weight (this is a strong, clean break).
    #    0.10%–0.5× OR-width    = partial credit, proportional.
    #    < 0.10% (thin fakeout) = zero (hard floor preserved).
    #
    if or_hi is not None and or_lo is not None and not pd.isna(px):
        if is_call:
            ext = (px - or_hi) / or_hi if or_hi > 0 else 0.0
        else:
            ext = (or_lo - px) / or_lo if or_lo > 0 else 0.0

        if ext < 0.001:
            # Below thin-fakeout floor: no credit regardless
            score += award_pct('or_thrust', 0.0)
        else:
            mid = (or_hi + or_lo) / 2
            or_width_pct = (or_hi - or_lo) / mid if mid > 0 else 0.01
            # Full score at 0.5× OR-width extension; linear below
            half_width = or_width_pct * 0.5
            thrust_ratio = min(1.0, ext / half_width) if half_width > 0 else 1.0
            score += award_pct('or_thrust', thrust_ratio)
    else:
        components['or_thrust'] = (w.get('or_thrust', 0), 0.0, 0)

    # ── 3. ADX — rolling 60-bar session percentile (continuous, Phase 1+) ────
    #
    #  Old behaviour: binary — ADX ≥ 25 → full weight.
    #  New behaviour: percentile of ADX within today's session DF.
    #    ADX at 90th pctile of session → nearly full weight.
    #    ADX just above 25 on a high-ADX day → moderate credit.
    #    Falls back to binary when session has fewer than 20 bars.
    #
    adx_history = df['ADX'].dropna().tolist()[-60:]
    adx_ratio   = _percentile_score(adx, adx_history, fallback_threshold=25.0)
    score       += award_pct('adx', adx_ratio)

    # ── 4. DI alignment (binary) ─────────────────────────────────────────────
    if dip > 0 or dim > 0:
        score += award('di_align', (dip > dim) if is_call else (dim > dip))
    else:
        components['di_align'] = (w.get('di_align', 0), 0.0, 0)

    # ── 5. 5m SuperTrend (binary) ────────────────────────────────────────────
    if st5r is not None:
        try:
            score += award('st5', (float(st5r) > 0) if is_call else (float(st5r) < 0))
        except (TypeError, ValueError):
            components['st5'] = (w.get('st5', 0), 0.0, 0)
    else:
        components['st5'] = (w.get('st5', 0), 0.0, 0)

    # ── 6. 15m SuperTrend (binary) ───────────────────────────────────────────
    if st15_val is not None:
        try:
            score += award('st15', (float(st15_val) > 0) if is_call else (float(st15_val) < 0))
        except (TypeError, ValueError):
            components['st15'] = (w.get('st15', 0), 0.0, 0)
    else:
        components['st15'] = (w.get('st15', 0), 0.0, 0)

    # ── 7. EMA 9/21 position (binary) ────────────────────────────────────────
    if not (pd.isna(ema_f) or pd.isna(ema_s)):
        score += award('ema', (ema_f > ema_s) if is_call else (ema_f < ema_s))
    else:
        components['ema'] = (w.get('ema', 0), 0.0, 0)

    # ── 8. VWAP side (binary) ────────────────────────────────────────────────
    if not pd.isna(vwap) and vwap > 0 and not pd.isna(px):
        score += award('vwap', (px > vwap) if is_call else (px < vwap))
    else:
        components['vwap'] = (w.get('vwap', 0), 0.0, 0)

    # ── 9. OI Bias (binary) ──────────────────────────────────────────────────
    score += award('oi', oi_bias == 'CONFIRM')

    # ── 10. Exhaustion (binary) ──────────────────────────────────────────────
    exh_wt = w.get('exhaustion', 0)
    if exh_wt > 0 and morning_dir is not None:
        reversal_dir = (morning_dir != direction)
        adx_waning   = adx < 25 or (morning_adx_peak > 0 and adx < morning_adx_peak * 0.65)
        di_converged = (abs(dip - dim) < morning_adx_peak * 0.5) if morning_adx_peak > 0 else False
        score += award('exhaustion', reversal_dir and (adx_waning or di_converged))
    else:
        components['exhaustion'] = (exh_wt, 0.0, 0)

    # ── 11. Momentum ─────────────────────────────────────────────────────────
    #
    #  Phase 1 (binary): last 3 bars trending in direction.
    #  Phase 2+ (continuous): percentile of |3-bar ROC| in today's session.
    #    Only directionally aligned ROC gets credit; opposing ROC = 0.
    #
    mom_wt = w.get('momentum', 0)
    if mom_wt > 0 and len(df) >= 4 and not pd.isna(px):
        try:
            older = float(df['Close'].iloc[-4])
            if phase >= 2:
                # Rolling 3-bar ROC history from today's session
                closes = df['Close'].dropna().tolist()
                roc_history = [
                    (closes[i] - closes[i - 3]) / closes[i - 3]
                    for i in range(3, len(closes))
                    if closes[i - 3] > 0
                ][-60:]
                raw_roc = (px - older) / older if older > 0 else 0.0
                # Directional filter: wrong-way momentum scores 0
                if (is_call and raw_roc > 0) or (not is_call and raw_roc < 0):
                    abs_history = [abs(r) for r in roc_history]
                    mom_ratio = _percentile_score(abs(raw_roc), abs_history,
                                                  fallback_threshold=0.0)
                else:
                    mom_ratio = 0.0
                score += award_pct('momentum', mom_ratio)
            else:
                # Phase 1: binary 3-bar trend
                prev     = float(df['Close'].iloc[-2])
                trending = (
                    (px > older and px >= prev) if is_call
                    else (px < older and px <= prev)
                )
                score += award('momentum', trending)
        except Exception:
            components['momentum'] = (mom_wt, 0.0, 0)
    else:
        components['momentum'] = (mom_wt, 0.0, 0)

    return {
        'score'     : min(100, score),
        'band'      : band,
        'phase'     : phase,
        'components': components,
    }
