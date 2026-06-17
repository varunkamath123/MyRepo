"""
weekly_analyzer.py — Pattern Discovery from Market Learnings
Runs every Friday at 15:45 IST via cron / systemd timer (after daily_debrief.py).
Also runnable manually at any time.

Reads logs/market_learnings.jsonl (built by daily_debrief.py) and surfaces
actionable patterns from accumulated PATH-A ORB trade data:

  1. Win-rate by ADX band at entry  (is ADX≥35 materially better than ≥25?)
  2. Win-rate by day of week        (are Thu/Mon still worse?)
  3. Win-rate by market regime      (TRENDING vs CHOPPY vs REVERSAL)
  4. Win-rate by gap type           (GAP_AND_GO better entry direction?)
  5. 11:30 hold vs close analysis   (when did HOLD beat hard close?)
  6. Re-entry success rate          (is the second-try pattern valid?)
  7. EMA alignment at 11:30 vs outcome correlation
  8. OR width vs win-rate           (tighter OR = better breakout?)

Output: printed summary + appended to logs/weekly_analysis.jsonl

Usage:
  python weekly_analyzer.py              # analyze all history
  python weekly_analyzer.py --weeks 4   # last 4 weeks only
"""

from __future__ import annotations

import json
import os
import sys
import logging
from datetime import datetime, date, timedelta
from collections import defaultdict
from pathlib import Path

import pytz

# ── Path setup ────────────────────────────────────────────────────────────────
_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(_DIR)

IST = pytz.timezone('Asia/Kolkata')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [ANALYZER] %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('weekly_analyzer')

JSONL_PATH      = os.path.join(_DIR, 'logs', 'market_learnings.jsonl')
ANALYSIS_PATH   = os.path.join(_DIR, 'logs', 'weekly_analysis.jsonl')
MP_TRAP_LOG     = os.path.join(_DIR, 'logs', 'mp_trap_learnings.jsonl')
PRED_PATH       = os.path.join(_DIR, 'logs', 'trade_predictions.jsonl')


# ── Data loading ───────────────────────────────────────────────────────────────

def load_records(weeks_back: int | None = None) -> list[dict]:
    if not os.path.exists(JSONL_PATH):
        logger.warning(f"No data yet at {JSONL_PATH}. Run daily_debrief.py first.")
        return []

    cutoff = None
    if weeks_back is not None:
        cutoff = (datetime.now(IST).date() - timedelta(weeks=weeks_back)).isoformat()

    records = []
    with open(JSONL_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if cutoff and rec.get('date', '') < cutoff:
                    continue
                records.append(rec)
            except json.JSONDecodeError:
                pass

    logger.info(f"Loaded {len(records)} records{' (last ' + str(weeks_back) + ' weeks)' if weeks_back else ''}")
    return records


# ── Analysis helpers ───────────────────────────────────────────────────────────

def win_rate(records: list[dict]) -> tuple[float, int]:
    """Return (win_rate, n_trades). A win is total_pnl_net > 0."""
    traded = [r for r in records if r.get('path_a_fired')]
    if not traded:
        return 0.0, 0
    wins = sum(1 for r in traded if (r.get('total_pnl_net') or 0) > 0)
    return wins / len(traded) * 100, len(traded)


def avg_pnl(records: list[dict]) -> float:
    traded = [r for r in records if r.get('path_a_fired')]
    if not traded:
        return 0.0
    return sum(r.get('total_pnl_net', 0) for r in traded) / len(traded)


def bucket_by(records: list[dict], key_fn, label: str) -> dict:
    buckets = defaultdict(list)
    for r in records:
        k = key_fn(r)
        if k is not None:
            buckets[k].append(r)
    result = {}
    for k, recs in sorted(buckets.items()):
        wr, n = win_rate(recs)
        result[k] = {'wr': round(wr, 1), 'n': n, 'avg_pnl': round(avg_pnl(recs), 0)}
    return result


# ── Individual analyses ────────────────────────────────────────────────────────

def analyze_adx_bands(records: list[dict]) -> dict:
    """Win-rate by ADX at entry: <25, 25-29, 30-34, 35+"""
    def band(r):
        adx = r.get('adx_at_entry')
        if adx is None:
            return None
        if adx < 25:
            return 'ADX<25'
        if adx < 30:
            return 'ADX 25-29'
        if adx < 35:
            return 'ADX 30-34'
        return 'ADX ≥35'
    return bucket_by(records, band, 'ADX band')


def analyze_day_of_week(records: list[dict]) -> dict:
    return bucket_by(records, lambda r: r.get('day_of_week'), 'day of week')


def analyze_regime(records: list[dict]) -> dict:
    return bucket_by(records, lambda r: r.get('market_regime'), 'regime')


def analyze_gap_type(records: list[dict]) -> dict:
    def gap_bucket(r):
        gt = r.get('gap_type')
        return gt if gt else 'No Gap (<0.3%)'
    return bucket_by(records, gap_bucket, 'gap type')


def analyze_or_width(records: list[dict]) -> dict:
    """Win-rate by OR width buckets."""
    def width_bucket(r):
        w = r.get('or_width_pct')
        if w is None:
            return None
        if w < 0.15:
            return 'OR<0.15%'
        if w < 0.25:
            return 'OR 0.15-0.25%'
        if w < 0.35:
            return 'OR 0.25-0.35%'
        return 'OR≥0.35%'
    return bucket_by(records, width_bucket, 'OR width')


def analyze_hold_vs_close(records: list[dict]) -> dict:
    """
    For PATH-A positions that reached 11:30 with data:
    When position was held (exit_reason includes 'trail/target' after 11:30)
    vs closed (force-close at 11:30), compare outcomes.
    """
    # Records where 11:30 decision was made
    held  = [r for r in records if r.get('exit_reason') and
             any(k in (r.get('exit_reason') or '') for k in ('Trail', 'Target', '14:30', 'EOD', 'Force-Close'))]
    held_past = [r for r in held if 'Force-Close' not in (r.get('exit_reason') or '') and
                 r.get('path_a_fired')]
    closed_at = [r for r in held if 'Force-Close' in (r.get('exit_reason') or '') and
                 r.get('path_a_fired')]

    result = {}
    if held_past:
        wr, n = win_rate(held_past)
        result['HELD_PAST_1130'] = {'wr': round(wr, 1), 'n': n, 'avg_pnl': round(avg_pnl(held_past), 0)}
    if closed_at:
        wr, n = win_rate(closed_at)
        result['CLOSED_AT_1130'] = {'wr': round(wr, 1), 'n': n, 'avg_pnl': round(avg_pnl(closed_at), 0)}
    return result


def analyze_reentry(records: list[dict]) -> dict:
    """Re-entry success rate."""
    reentry_days = [r for r in records if r.get('path_a_reentry')]
    if not reentry_days:
        return {'n': 0, 'note': 'No re-entry trades yet — accumulating data'}
    wins = sum(1 for r in reentry_days if (r.get('total_pnl_net') or 0) > 0)
    return {
        'n'      : len(reentry_days),
        'wr'     : round(wins / len(reentry_days) * 100, 1),
        'avg_pnl': round(avg_pnl(reentry_days), 0),
    }


def analyze_ema_alignment(records: list[dict]) -> dict:
    """Does EMA alignment at 11:30 predict better continuation?"""
    def ema_key(r):
        if not r.get('path_a_fired'):
            return None
        bd = r.get('breakout_direction')
        ea = r.get('ema_aligned_1130')
        if bd and ea:
            return 'EMA_ALIGNED' if bd == ea else 'EMA_OPPOSED'
        return 'EMA_UNKNOWN'
    return bucket_by(records, ema_key, 'EMA alignment at 11:30')


# ── Optimal trailing threshold suggestion ─────────────────────────────────────

def analyze_mp_trap() -> dict:
    """
    Per-instrument MaxPain Trap (Variant A) analysis.
    Reads logs/mp_trap_learnings.jsonl.

    Returns per-instrument stats + threshold calibration suggestions:
      - fire_rate : how often the gap+PCR condition fires on DTE≤2 days
      - wr        : win rate of fired trades
      - avg_pnl   : average P&L per trade
      - suggestion: whether to tighten/loosen gap_pct or PCR threshold

    Calibration logic (after ≥10 trades per instrument):
      WR < 40%  → OI conditions too loose  → raise gap_pct or tighten PCR
      WR > 65%  → well-calibrated, or room to lower gap_pct for more entries
      WR 40–65% → acceptable band, keep current thresholds
    """
    if not os.path.exists(MP_TRAP_LOG):
        return {'_note': 'No mp_trap_learnings.jsonl yet — accumulating data'}

    trades: list[dict] = []
    with open(MP_TRAP_LOG, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    trades.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    if not trades:
        return {'_note': 'mp_trap_learnings.jsonl exists but is empty'}

    result: dict = {}
    for inst in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
        inst_trades = [t for t in trades if t.get('instrument') == inst]
        n = len(inst_trades)
        if n == 0:
            result[inst] = {'n': 0, 'note': 'No trades yet'}
            continue

        wins    = sum(1 for t in inst_trades if t.get('win'))
        wr      = round(wins / n * 100, 1)
        avg_pnl = round(sum(t.get('pnl', 0) for t in inst_trades) / n, 0)
        avg_gap = round(sum(abs(t.get('gap_pct', 0)) for t in inst_trades) / n, 3)
        total_pnl = round(sum(t.get('pnl', 0) for t in inst_trades), 0)

        # Read current config threshold for this instrument
        try:
            import config as _cfg
            v = _cfg.MP_TRAP_GAP_PCT
            cur_gap = (v.get(inst, 0.005) if isinstance(v, dict) else float(v)) * 100
            v2 = _cfg.MP_TRAP_PCR_PUT_CONFIRM
            cur_pcr_put = v2.get(inst, 0.85) if isinstance(v2, dict) else float(v2)
        except Exception:
            cur_gap, cur_pcr_put = 0.50, 0.85

        # Exit reason breakdown
        exit_reasons = defaultdict(int)
        for t in inst_trades:
            exit_reasons[t.get('exit_reason', 'Unknown')] += 1

        # Calibration suggestion
        MIN_TRADES = 10
        if n < MIN_TRADES:
            suggestion = f'Accumulating data ({n}/{MIN_TRADES} trades needed)'
        elif wr < 40:
            suggestion = (
                f'WR={wr}% is below 40% — OI conditions may be too loose. '
                f'Consider raising gap_pct {cur_gap:.2f}%→{cur_gap+0.05:.2f}% '
                f'or tightening PCR_PUT_CONFIRM {cur_pcr_put:.2f}→{cur_pcr_put-0.02:.2f}'
            )
        elif wr > 65:
            suggestion = (
                f'WR={wr}% — well-calibrated. '
                f'Could lower gap_pct {cur_gap:.2f}%→{cur_gap-0.05:.2f}% '
                f'to increase entry frequency if avg_pnl stays positive'
            )
        else:
            suggestion = f'WR={wr}% — within 40–65% target band. Keep current thresholds.'

        result[inst] = {
            'n'           : n,
            'wins'        : wins,
            'wr'          : wr,
            'avg_pnl'     : avg_pnl,
            'total_pnl'   : total_pnl,
            'avg_gap_pct' : avg_gap,
            'cur_gap_pct' : round(cur_gap, 3),
            'exit_reasons': dict(exit_reasons),
            'suggestion'  : suggestion,
        }

    return result


def fmt_mp_trap_table(data: dict) -> str:
    """Format the MP trap analysis for the weekly report."""
    lines = ['\n  MaxPain Trap — Per-Instrument Calibration:']
    note = data.get('_note')
    if note:
        lines.append(f'    {note}')
        return '\n'.join(lines)

    for inst, v in data.items():
        if inst.startswith('_'):
            continue
        if v.get('n', 0) == 0:
            lines.append(f'    {inst:<12} — no trades yet')
            continue
        lines.append(
            f'    {inst:<12} n={v["n"]:>3}  WR={v["wr"]:>5.1f}%  '
            f'avg=₹{v["avg_pnl"]:>+7,.0f}  total=₹{v["total_pnl"]:>+8,.0f}  '
            f'gap_thr={v["cur_gap_pct"]:.2f}%  avg_gap={v["avg_gap_pct"]:.2f}%'
        )
        exits = v.get('exit_reasons', {})
        exit_str = '  |  '.join(f'{k}: {c}' for k, c in exits.items())
        lines.append(f'      Exits: {exit_str}')
        lines.append(f'      → {v["suggestion"]}')
    return '\n'.join(lines)


def suggest_hold_threshold(records: list[dict]) -> dict:
    """
    Analyze: at what profit % at 11:30 does holding tend to be better?
    Groups PATH-A trades by pnl_pct bucket at 11:30 (from daily_debrief data).
    Currently uses total_pnl_pct as a proxy for 11:30 pct (since we don't
    log intrabar pnl_pct_at_1130 yet — TODO).
    """
    # Use option_pct_at_1130 (now logged by enhanced daily_debrief.py)
    with_data = [r for r in records if r.get('option_pct_at_1130') is not None]
    if not with_data:
        return {'note': 'option_pct_at_1130 not yet in records — needs daily_debrief v2 data.'}

    buckets = {}
    for r in with_data:
        pct = r['option_pct_at_1130']
        if pct < 5:
            band = '<5%'
        elif pct < 10:
            band = '5-10%'
        elif pct < 15:
            band = '10-15%'
        elif pct < 20:
            band = '15-20%'
        else:
            band = '≥20%'

        if band not in buckets:
            buckets[band] = []
        buckets[band].append(r)

    result = {}
    for band, recs in sorted(buckets.items()):
        hold_recs  = [r for r in recs if r.get('hold_decision') == 'HOLD']
        close_recs = [r for r in recs if r.get('hold_decision') == 'CLOSE']
        hold_wins  = sum(1 for r in hold_recs  if (r.get('total_pnl_net') or 0) > 0)
        close_wins = sum(1 for r in close_recs if (r.get('total_pnl_net') or 0) > 0)
        result[band] = {
            'n_hold'  : len(hold_recs),
            'hold_wr' : round(hold_wins  / max(len(hold_recs),  1) * 100, 1),
            'n_close' : len(close_recs),
            'close_wr': round(close_wins / max(len(close_recs), 1) * 100, 1),
        }
    return result


# ── Summary formatter ──────────────────────────────────────────────────────────

def fmt_table(data: dict, title: str) -> str:
    lines = [f'\n  {title}:']
    if not data:
        lines.append('    (no data)')
        return '\n'.join(lines)
    for k, v in data.items():
        if isinstance(v, dict):
            n   = v.get('n', 0)
            wr  = v.get('wr', 0)
            pnl = v.get('avg_pnl', 0)
            lines.append(f'    {str(k):<22} n={n:>3}  WR={wr:>5.1f}%  avg_pnl=₹{pnl:>+7,.0f}')
        else:
            lines.append(f'    {k}: {v}')
    return '\n'.join(lines)


# ── Scorer Phase Auto-Upgrade ─────────────────────────────────────────────────

_STATE_PATH   = os.path.join(_DIR, '..', 'data', 'scorer_state.json')
_COMPONENT_NAMES = [
    'or_breakout', 'or_thrust', 'adx', 'di_align', 'st5',
    'st15', 'ema', 'vwap', 'oi', 'exhaustion', 'momentum',
]


def _count_completed_prediction_trades() -> int:
    """Count completed trades in trade_predictions.jsonl that have unified_components."""
    if not os.path.exists(PRED_PATH):
        return 0
    count = 0
    with open(PRED_PATH, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if r.get('actual_outcome') and r.get('unified_components'):
                    count += 1
            except json.JSONDecodeError:
                pass
    return count


def _load_scorer_state() -> dict:
    state: dict = {'phase': 1, 'phase2_applied': False, 'phase3_applied': False}
    if os.path.exists(_STATE_PATH):
        try:
            with open(_STATE_PATH, encoding='utf-8') as f:
                state.update(json.load(f))
        except Exception:
            pass
    return state


def _save_scorer_state(state: dict) -> None:
    os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
    with open(_STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)


def _apply_logistic_regression_weights() -> bool:
    """
    Phase 3: fit LogisticRegression on unified_components → WIN/LOSS.
    Converts positive coefficients to band weights, normalises to sum=100.
    Returns True if weights were updated, False if sklearn missing or data thin.
    """
    try:
        from sklearn.linear_model import LogisticRegression
        import numpy as np
    except ImportError:
        logger.warning(
            '  [PHASE-3] scikit-learn not installed — run: '
            'sudo /opt/trading_bot/venv/bin/pip install scikit-learn'
        )
        return False

    try:
        import unified_scorer as us
    except ImportError:
        return False

    # Load completed trades with unified_components
    records: list[dict] = []
    with open(PRED_PATH, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if r.get('actual_outcome') and r.get('unified_components'):
                    records.append(r)
            except json.JSONDecodeError:
                pass

    _phase_band = {'PHASE-1': '09:30', 'PHASE-2': '11:00', 'PHASE-3': '12:00'}
    from collections import defaultdict
    band_records: dict = defaultdict(list)
    for r in records:
        band = r.get('unified_band') or _phase_band.get(r.get('phase', ''), '11:00')
        band_records[band].append(r)

    current = us.load_weights()
    new_w   = {band: dict(vals) for band, vals in current.items()}
    any_updated = False

    for band, brecs in band_records.items():
        if len(brecs) < 20 or band not in new_w:
            logger.info(f'  [PHASE-3] {band}: only {len(brecs)} trades — skipping regression')
            continue

        X, y = [], []
        for r in brecs:
            comp = r['unified_components']
            # Feature: 1 if component contributed points, else 0
            feats = [1.0 if float(comp.get(c, 0)) > 0 else 0.0 for c in _COMPONENT_NAMES]
            X.append(feats)
            y.append(1 if r['actual_outcome'] == 'WIN' else 0)

        X_arr = np.array(X)
        y_arr = np.array(y)

        if len(set(y_arr)) < 2:
            logger.info(f'  [PHASE-3] {band}: all {y_arr[0]} outcomes — need variance to fit')
            continue

        try:
            model = LogisticRegression(C=1.0, max_iter=500, random_state=42)
            model.fit(X_arr, y_arr)
            coefs = model.coef_[0]  # (n_components,)

            # Shift so min is 0, then scale to sum=100 preserving structural zeros
            shifted = np.maximum(0.0, coefs - coefs.min())
            total   = shifted.sum()
            if total == 0:
                continue

            for i, comp in enumerate(_COMPONENT_NAMES):
                # Preserve hard zeros (e.g. or_breakout=0 in 13:00 band)
                if current.get(band, {}).get(comp, 0) == 0:
                    new_w[band][comp] = 0
                else:
                    new_w[band][comp] = max(0, round(float(shifted[i]) / total * 100))

            # Normalise to exactly 100
            total_w = sum(new_w[band].values())
            if total_w > 0 and total_w != 100:
                scale = 100.0 / total_w
                for c in new_w[band]:
                    new_w[band][c] = max(0, round(new_w[band][c] * scale))
                diff = 100 - sum(new_w[band].values())
                if diff != 0:
                    biggest = max(new_w[band], key=new_w[band].get)
                    new_w[band][biggest] = max(0, new_w[band][biggest] + diff)

            any_updated = True
            top = sorted(new_w[band].items(), key=lambda x: -x[1])
            logger.info(
                f'  [PHASE-3] {band} ({len(brecs)} trades): '
                + '  '.join(f'{c}={v}' for c, v in top if v > 0)
            )
        except Exception as exc:
            logger.warning(f'  [PHASE-3] {band}: regression failed — {exc}')

    if any_updated:
        us.save_weights(new_w)
        logger.info('  [PHASE-3] Logistic regression weights saved to unified_weights.json')

    return any_updated


def check_scorer_phase_upgrade() -> None:
    """
    Auto-upgrade unified scorer phase based on completed trade count.
    Called every Friday from main() after weight recalibration.

    Phase 2 at 30 trades  — momentum gets rolling percentile scoring
    Phase 3 at 80 trades  — weights replaced by logistic regression
    """
    state = _load_scorer_state()
    n     = _count_completed_prediction_trades()
    run_date = datetime.now(IST).isoformat()
    changed  = False

    logger.info(f'  [PHASE-CHECK] {n} completed prediction trades | current phase={state["phase"]}')

    # ── Phase 2: momentum percentile ─────────────────────────────────────────
    if n >= 30 and not state.get('phase2_applied'):
        state['phase']              = max(state.get('phase', 1), 2)
        state['phase2_applied']     = True
        state['phase2_applied_date']= run_date
        state['phase2_trade_count'] = n
        changed = True
        logger.info(
            f'  [SCORER-UPGRADE] → Phase 2 activated at {n} trades. '
            f'Momentum now uses rolling percentile of 3-bar ROC.'
        )

    # ── Phase 3: logistic regression weights ─────────────────────────────────
    if n >= 80 and not state.get('phase3_applied'):
        logger.info(f'  [SCORER-UPGRADE] Attempting Phase 3 at {n} trades ...')
        success = _apply_logistic_regression_weights()
        if success:
            state['phase']              = max(state.get('phase', 2), 3)
            state['phase3_applied']     = True
            state['phase3_applied_date']= run_date
            state['phase3_trade_count'] = n
            changed = True
            logger.info(
                f'  [SCORER-UPGRADE] → Phase 3 activated at {n} trades. '
                f'Weights now reflect empirical win-rate contribution.'
            )
        else:
            logger.warning(
                f'  [SCORER-UPGRADE] Phase 3 skipped — see warnings above. '
                f'Will retry next Friday.'
            )

    if n < 30:
        need = 30 - n
        logger.info(f'  [PHASE-CHECK] Need {need} more trades for Phase 2 (momentum percentile)')
    elif n < 80:
        need = 80 - n
        logger.info(f'  [PHASE-CHECK] Need {need} more trades for Phase 3 (logistic regression)')

    if changed:
        _save_scorer_state(state)


# ── Unified Scorer Weight Recalibration ───────────────────────────────────────

def recalibrate_unified_weights() -> dict:
    """
    Read trade_predictions.jsonl (written by trade_probability.py + unified_scorer).
    For each completed trade that has unified_components logged, compute per-band
    per-component win-rate impact.  Adjust weights ±3 max per component per week.
    Normalize each band to sum=100.  Save to data/unified_weights.json.

    Requires minimum 20 completed trades with unified_components recorded.
    Returns updated weights dict (or current weights unchanged if insufficient data).
    """
    try:
        import unified_scorer as us
    except ImportError:
        logger.warning('  [RECAL] unified_scorer not found — skipping weight recalibration')
        return {}

    if not os.path.exists(PRED_PATH):
        logger.info(f'  [RECAL] {PRED_PATH} not found — skipping')
        return us.load_weights()

    records: list[dict] = []
    with open(PRED_PATH, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if r.get('actual_outcome') and r.get('unified_components'):
                    records.append(r)
            except json.JSONDecodeError:
                pass

    MIN_TRADES = 20
    if len(records) < MIN_TRADES:
        logger.info(
            f'  [RECAL] {len(records)} unified trades logged '
            f'(need {MIN_TRADES}) — keeping current weights'
        )
        return us.load_weights()

    current = us.load_weights()
    new_w   = {band: dict(vals) for band, vals in current.items()}

    # Map trade phase → band key (fallback when unified_band not logged)
    _phase_band = {'PHASE-1': '09:30', 'PHASE-2': '11:00', 'PHASE-3': '12:00'}

    from collections import defaultdict
    band_data: dict[str, list[tuple[bool, dict]]] = defaultdict(list)
    for r in records:
        band   = r.get('unified_band') or _phase_band.get(r.get('phase', ''), '11:00')
        is_win = r['actual_outcome'] == 'WIN'
        band_data[band].append((is_win, r['unified_components']))

    MAX_ADJ = 3   # max weight change per component per run

    changed: list[str] = []
    for band, bdata in band_data.items():
        if len(bdata) < 5 or band not in new_w:
            continue

        for comp in list(new_w[band]):
            aligned   = [(w, d) for w, d in bdata if d.get(comp, 0) > 0]
            unaligned = [(w, d) for w, d in bdata if d.get(comp, 0) == 0]
            if len(aligned) < 3 or len(unaligned) < 3:
                continue

            wr_al  = sum(1 for w, _ in aligned)   / len(aligned)
            wr_un  = sum(1 for w, _ in unaligned)  / len(unaligned)
            delta  = wr_al - wr_un

            if delta > 0.10:
                adj = min(MAX_ADJ, max(1, round(delta * 15)))
            elif delta < -0.10:
                adj = -min(MAX_ADJ, max(1, round(abs(delta) * 15)))
            else:
                adj = 0

            if adj != 0:
                old = new_w[band][comp]
                new_w[band][comp] = max(0, old + adj)
                changed.append(f'{band}/{comp}: {old}→{new_w[band][comp]} (WR delta {delta:+.2f})')

        # Normalize to sum=100
        total = sum(new_w[band].values())
        if total > 0 and total != 100:
            scale = 100.0 / total
            for c in new_w[band]:
                new_w[band][c] = max(0, round(new_w[band][c] * scale))
            # Fix rounding residual on largest component
            diff = 100 - sum(new_w[band].values())
            if diff != 0:
                biggest = max(new_w[band], key=new_w[band].get)
                new_w[band][biggest] = max(0, new_w[band][biggest] + diff)

    us.save_weights(new_w)
    logger.info(
        f'  [RECAL] Unified weights recalibrated from {len(records)} trades '
        f'({len(changed)} adjustments)'
    )
    for line in changed:
        logger.info(f'    {line}')
    return new_w


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    weeks_back = None
    if '--weeks' in sys.argv:
        idx = sys.argv.index('--weeks')
        if idx + 1 < len(sys.argv):
            try:
                weeks_back = int(sys.argv[idx + 1])
            except ValueError:
                pass

    records = load_records(weeks_back)
    if not records:
        logger.info("No records to analyze. Run daily_debrief.py first.")
        return

    run_date = datetime.now(IST).date().isoformat()
    total_days = len(set(r['date'] for r in records))
    path_a_days = sum(1 for r in records if r.get('path_a_fired'))

    logger.info(f"\n{'='*60}")
    logger.info(f"  FnO_T_Bot Weekly Analysis — {run_date}")
    logger.info(f"  Total market days: {total_days} | PATH-A fired: {path_a_days}")
    logger.info(f"{'='*60}")

    analyses = {
        'ADX at Entry'          : analyze_adx_bands(records),
        'Day of Week'           : analyze_day_of_week(records),
        'Market Regime'         : analyze_regime(records),
        'Gap Type'              : analyze_gap_type(records),
        'OR Width'              : analyze_or_width(records),
        '11:30 Hold vs Close'   : analyze_hold_vs_close(records),
        'EMA Alignment at 11:30': analyze_ema_alignment(records),
    }

    for title, data in analyses.items():
        print(fmt_table(data, title))

    reentry = analyze_reentry(records)
    print(f"\n  Re-entry Success Rate:")
    print(f"    {reentry}")

    mp_trap_data = analyze_mp_trap()
    print(fmt_mp_trap_table(mp_trap_data))

    hold_tip = suggest_hold_threshold(records)
    print(f"\n  11:30 Hold Threshold Insight (by option_pct_at_1130 band):")
    if isinstance(hold_tip, dict) and 'note' not in hold_tip:
        for band, v in hold_tip.items():
            print(f"    {band:<8}  hold n={v['n_hold']} WR={v['hold_wr']}%"
                  f"  |  close n={v['n_close']} WR={v['close_wr']}%")
    else:
        print(f"    {hold_tip.get('note', hold_tip)}")

    # ── Adaptive parameter optimization ──────────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"  Adaptive Parameter Optimizer")
    print(f"{'─'*60}")
    try:
        import adaptive_params as ap
        ap_records = ap.load_records(weeks_back)
        if len(ap_records) >= ap.MIN_RECORDS:
            hold_sim = ap.optimize_hold_threshold(ap_records)
            print(ap.fmt_hold_table(hold_sim))
            recs_ap  = ap.generate_recommendations(hold_sim, ap_records)
            print('\n  ── Recommendations ──')
            for r in recs_ap:
                print(f'  → {r}')
        else:
            print(f"  {len(ap_records)} PATH-A records (need ≥{ap.MIN_RECORDS} for optimization)")
    except ImportError:
        print("  adaptive_params.py not found")

    # ── Append analysis snapshot to JSONL ─────────────────────────────────────
    snapshot = {
        'run_date'       : run_date,
        'total_days'     : total_days,
        'path_a_days'    : path_a_days,
        'adx_bands'      : analyze_adx_bands(records),
        'day_of_week'    : analyze_day_of_week(records),
        'regime'         : analyze_regime(records),
        'gap_type'       : analyze_gap_type(records),
        'or_width'       : analyze_or_width(records),
        'hold_vs_close'  : analyze_hold_vs_close(records),
        'ema_alignment'  : analyze_ema_alignment(records),
        're_entry'       : analyze_reentry(records),
        'mp_trap'        : mp_trap_data,
    }
    with open(ANALYSIS_PATH, 'a', encoding='utf-8') as f:
        f.write(json.dumps(snapshot) + '\n')

    logger.info(f"\nSnapshot saved to {ANALYSIS_PATH}")

    # ── Unified Scorer Weight Recalibration ──────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"  Unified Scorer Weight Recalibration")
    print(f"{'─'*60}")
    recalibrate_unified_weights()

    # ── Scorer Phase Auto-Upgrade ─────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"  Scorer Phase Check")
    print(f"{'─'*60}")
    check_scorer_phase_upgrade()

    logger.info("=== Analysis complete ===\n")


if __name__ == '__main__':
    main()
