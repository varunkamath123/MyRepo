"""
adaptive_params.py — Strategy Parameter Optimizer

Reads logs/market_learnings.jsonl (built by daily_debrief.py) and simulates
what P&L would have been at different parameter values. Provides data-driven
evidence for whether current thresholds are correctly calibrated.

Key optimizations:
  1. PATH_A_MIN_PROFIT_TO_HOLD  (currently 0.15 = 15%)
     → Simulate 8%–25% to find where "hold past 11:30" becomes net-positive
  2. PATH_A_REENTRY_ADX_MIN     (currently 35)
     → Simulate 28–40 to assess re-entry conviction threshold
  3. PATH_A_DAY_ADX_MIN         (per-day)
     → Identify if any day's ADX floor is mis-calibrated
  4. Optimal lot-sizing by regime
     → When regime=CHOPPY, does 1-lot outperform 2-lot?

Output: printed table + appended to logs/adaptive_params_history.jsonl

Requires: market_learnings.jsonl with fields:
  option_pct_at_1130, option_pct_peak, option_pct_final (from enhanced daily_debrief.py)
  path_a_fired, total_pnl_net, day_of_week, market_regime, adx_at_entry, lots

Usage:
  python adaptive_params.py              # full optimization run
  python adaptive_params.py --weeks 8   # limit lookback to 8 weeks
"""

from __future__ import annotations

import json
import os
import sys
import logging
from datetime import datetime, timedelta

import pytz

_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(_DIR)

IST = pytz.timezone('Asia/Kolkata')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [ADAPTIVE] %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('adaptive_params')

JSONL_PATH   = os.path.join(_DIR, 'logs', 'market_learnings.jsonl')
OUTPUT_PATH  = os.path.join(_DIR, 'logs', 'adaptive_params_history.jsonl')
MIN_RECORDS  = 15   # minimum records before optimization is meaningful


# ── Data loading ───────────────────────────────────────────────────────────────

def load_records(weeks_back: int | None = None) -> list[dict]:
    if not os.path.exists(JSONL_PATH):
        return []

    cutoff = None
    if weeks_back:
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
                if rec.get('path_a_fired'):   # only PATH-A trades
                    records.append(rec)
            except json.JSONDecodeError:
                pass
    return records


# ── Hold threshold simulation ──────────────────────────────────────────────────

def simulate_hold_threshold(records: list[dict], threshold: float) -> dict:
    """
    Simulate P&L if PATH_A_MIN_PROFIT_TO_HOLD were set to `threshold`.

    Requires records with `option_pct_at_1130` and `option_pct_final`.
    Records missing these fields are counted but excluded from simulation.

    Logic:
      - If option_pct_at_1130 >= threshold AND adx_at_1130 >= hold_adx_min:
          simulate HOLD → outcome = option_pct_final (actual exit %)
      - Else:
          simulate CLOSE_AT_1130 → outcome = option_pct_at_1130

    Note: this is a LOWER BOUND simulation. Actual hold outcomes depend on
    trail/target exit, which we approximate via option_pct_final.
    """
    HOLD_ADX_MIN  = 30   # mirrors config.PATH_A_HOLD_ADX_MIN (held constant here)
    simulated_pnl = 0.0
    actual_pnl    = 0.0
    n_hold        = 0
    n_close       = 0
    n_missing     = 0

    for r in records:
        actual_pnl += r.get('total_pnl_net', 0)
        pct_1130 = r.get('option_pct_at_1130')
        pct_fin  = r.get('option_pct_final',  r.get('total_pnl_pct'))
        lot_size = r.get('lots', 1)
        entry_px = r.get('entry_price_used', 200)   # approximate if not recorded
        adx_1130 = r.get('adx_at_1130', 99)        # default high = assume hold possible

        if pct_1130 is None or pct_fin is None:
            n_missing += 1
            simulated_pnl += r.get('total_pnl_net', 0)  # can't simulate → use actual
            continue

        adx_ok = (adx_1130 or 0) >= HOLD_ADX_MIN

        if pct_1130 >= threshold * 100 and adx_ok:
            # Simulate: hold past 11:30 → use actual final P&L as proxy
            simulated_pnl += r.get('total_pnl_net', 0)
            n_hold += 1
        else:
            # Simulate: close at 11:30
            # Estimate P&L at 11:30: use the option_pct_at_1130
            sim_pnl_at_1130 = (pct_1130 / 100) * entry_px * lot_size
            simulated_pnl += sim_pnl_at_1130
            n_close += 1

    n_total = len(records)
    delta   = simulated_pnl - actual_pnl

    return {
        'threshold'    : threshold,
        'n_total'      : n_total,
        'n_hold'       : n_hold,
        'n_close'      : n_close,
        'n_missing'    : n_missing,
        'actual_pnl'   : round(actual_pnl, 0),
        'simulated_pnl': round(simulated_pnl, 0),
        'delta'        : round(delta, 0),
    }


def optimize_hold_threshold(records: list[dict]) -> list[dict]:
    """Try hold thresholds from 8% to 25% in 1% steps."""
    if not records:
        return []
    thresholds = [x / 100 for x in range(8, 26, 1)]
    return [simulate_hold_threshold(records, t) for t in thresholds]


# ── Re-entry ADX simulation ────────────────────────────────────────────────────

def simulate_reentry_adx(records: list[dict], adx_min: float) -> dict:
    """
    Simulate P&L if PATH_A_REENTRY_ADX_MIN were set to `adx_min`.

    Records with path_a_reentry=True and adx_at_entry>=adx_min → count as valid.
    Records where reentry was skipped (would-have-fired) can't easily be computed
    without intraday data — so this is a simple "coverage" simulation.
    """
    reentry_records = [r for r in records if r.get('path_a_reentry')]
    if not reentry_records:
        return {'adx_min': adx_min, 'n': 0, 'wr': 0, 'avg_pnl': 0,
                'note': 'No re-entry events yet'}

    qualified = [r for r in reentry_records
                 if (r.get('adx_at_entry') or 0) >= adx_min]
    n = len(qualified)
    if n == 0:
        return {'adx_min': adx_min, 'n': 0, 'wr': 0, 'avg_pnl': 0}

    wins    = sum(1 for r in qualified if (r.get('total_pnl_net') or 0) > 0)
    avg_pnl = sum(r.get('total_pnl_net', 0) for r in qualified) / n
    return {
        'adx_min': adx_min,
        'n'      : n,
        'wr'     : round(wins / n * 100, 1),
        'avg_pnl': round(avg_pnl, 0),
    }


def optimize_reentry_adx(records: list[dict]) -> list[dict]:
    """Try re-entry ADX thresholds 28–42 in steps of 2."""
    return [simulate_reentry_adx(records, adx) for adx in range(28, 44, 2)]


# ── DOW ADX analysis ───────────────────────────────────────────────────────────

def analyze_dow_adx(records: list[dict]) -> dict:
    """
    Win-rate by day-of-week AND by ADX band at entry.
    Helps calibrate PATH_A_DAY_ADX_MIN per day.
    """
    from collections import defaultdict

    dow_adx: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in records:
        dow = r.get('day_of_week', '?')
        adx = r.get('adx_at_entry')
        if adx is None:
            continue
        if adx < 25:
            band = '<25'
        elif adx < 30:
            band = '25-29'
        elif adx < 35:
            band = '30-34'
        else:
            band = '≥35'
        dow_adx[dow][band].append(r.get('total_pnl_net', 0))

    result = {}
    for dow, bands in sorted(dow_adx.items()):
        result[dow] = {}
        for band, pnls in sorted(bands.items()):
            n    = len(pnls)
            wins = sum(1 for p in pnls if p > 0)
            result[dow][band] = {
                'n': n, 'wr': round(wins/n*100, 1),
                'avg_pnl': round(sum(pnls)/n, 0),
            }
    return result


# ── Regime lot-size analysis ───────────────────────────────────────────────────

def analyze_regime_lot_size(records: list[dict]) -> dict:
    """
    Compare 1-lot vs 2-lot outcomes by regime.
    When regime=CHOPPY, does 2-lot amplify losses?
    """
    from collections import defaultdict

    regime_lots: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in records:
        regime = r.get('regime_at_open', r.get('market_regime', 'UNKNOWN'))
        lots   = r.get('lots', 1)
        regime_lots[regime][str(lots)].append(r.get('total_pnl_net', 0))

    result = {}
    for regime, lot_groups in regime_lots.items():
        result[regime] = {}
        for lot, pnls in sorted(lot_groups.items()):
            n    = len(pnls)
            wins = sum(1 for p in pnls if p > 0)
            result[regime][f'{lot}L'] = {
                'n': n, 'wr': round(wins/n*100, 1),
                'avg_pnl': round(sum(pnls)/n, 0),
            }
    return result


# ── Report formatter ───────────────────────────────────────────────────────────

def fmt_hold_table(results: list[dict]) -> str:
    current_thr = 0.15  # config.PATH_A_MIN_PROFIT_TO_HOLD
    lines = ['\n  Hold Threshold Simulation (PATH_A_MIN_PROFIT_TO_HOLD):']
    lines.append(f'  {"Thresh":>7}  {"Hold":>5}  {"Close":>5}  '
                 f'{"Actual ₹":>10}  {"Simulated ₹":>12}  {"Delta ₹":>10}')
    lines.append('  ' + '-' * 62)
    for r in results:
        marker = '◄ current' if abs(r['threshold'] - current_thr) < 0.005 else ''
        lines.append(
            f'  {r["threshold"]*100:>6.0f}%  {r["n_hold"]:>5}  {r["n_close"]:>5}  '
            f'₹{r["actual_pnl"]:>+9,.0f}  ₹{r["simulated_pnl"]:>+11,.0f}  '
            f'₹{r["delta"]:>+9,.0f}  {marker}'
        )
    return '\n'.join(lines)


def fmt_reentry_table(results: list[dict]) -> str:
    current_adx = 35  # config.PATH_A_REENTRY_ADX_MIN
    lines = ['\n  Re-entry ADX Simulation (PATH_A_REENTRY_ADX_MIN):']
    lines.append(f'  {"ADX min":>7}  {"n":>4}  {"WR":>6}  {"avg_pnl":>10}')
    lines.append('  ' + '-' * 35)
    for r in results:
        if r['n'] == 0:
            continue
        marker = ' ◄ current' if r['adx_min'] == current_adx else ''
        lines.append(
            f'  {r["adx_min"]:>7}  {r["n"]:>4}  {r["wr"]:>5.1f}%  '
            f'₹{r["avg_pnl"]:>+9,.0f}{marker}'
        )
    return '\n'.join(lines)


def fmt_dow_table(results: dict) -> str:
    lines = ['\n  Day-of-Week × ADX Band (win rate):']
    lines.append(f'  {"DOW":>4}  {"Band":>6}  {"n":>4}  {"WR":>6}  {"avg_pnl":>10}')
    lines.append('  ' + '-' * 40)
    for dow, bands in results.items():
        for band, v in bands.items():
            lines.append(f'  {dow:>4}  {band:>6}  {v["n"]:>4}  '
                         f'{v["wr"]:>5.1f}%  ₹{v["avg_pnl"]:>+9,.0f}')
    return '\n'.join(lines)


def fmt_regime_lot_table(results: dict) -> str:
    lines = ['\n  Lot Size × Regime (win rate):']
    lines.append(f'  {"Regime":>20}  {"Lot":>4}  {"n":>4}  {"WR":>6}  {"avg_pnl":>10}')
    lines.append('  ' + '-' * 52)
    for regime, lots in results.items():
        for lot, v in lots.items():
            lines.append(f'  {regime:>20}  {lot:>4}  {v["n"]:>4}  '
                         f'{v["wr"]:>5.1f}%  ₹{v["avg_pnl"]:>+9,.0f}')
    return '\n'.join(lines)


# ── Recommendations ────────────────────────────────────────────────────────────

def generate_recommendations(hold_results: list[dict], records: list[dict]) -> list[str]:
    """Generate actionable recommendations from simulation results."""
    recs = []
    n = len(records)

    if n < MIN_RECORDS:
        recs.append(f'Insufficient data ({n} records, need ≥{MIN_RECORDS}). '
                    f'Continue accumulating — check back in '
                    f'{MIN_RECORDS - n} more trading days.')
        return recs

    # Hold threshold: find the threshold that maximizes simulated P&L
    if hold_results:
        missing_count = hold_results[0].get('n_missing', n)
        if missing_count < n * 0.5:   # enough records have option_pct_at_1130
            best = max(hold_results, key=lambda r: r['simulated_pnl'])
            current = next((r for r in hold_results if abs(r['threshold'] - 0.15) < 0.005), None)
            if best and current and best['threshold'] != 0.15:
                delta = best['simulated_pnl'] - current['simulated_pnl']
                if abs(delta) > 1000:
                    recs.append(
                        f'HOLD THRESHOLD: Optimal = {best["threshold"]*100:.0f}% '
                        f'(current: 15%). Estimated P&L delta: ₹{delta:+,.0f} '
                        f'over {n} trades. Consider updating PATH_A_MIN_PROFIT_TO_HOLD.'
                    )
                else:
                    recs.append(
                        f'HOLD THRESHOLD: Current 15% appears well-calibrated '
                        f'(best delta vs optimal: ₹{delta:+,.0f} — within noise).'
                    )
        else:
            recs.append(
                f'HOLD THRESHOLD: {missing_count}/{n} records missing '
                f'option_pct_at_1130. Run enhanced daily_debrief.py for more data.'
            )

    # Regime lot size: flag if 2-lot in CHOPPY regime is losing money
    regime_lots = analyze_regime_lot_size(records)
    for regime, lots in regime_lots.items():
        if 'CHOPPY' in regime and '2L' in lots and '1L' in lots:
            two_lot = lots['2L']
            one_lot = lots['1L']
            if two_lot['avg_pnl'] < one_lot['avg_pnl'] - 1000 and two_lot['n'] >= 3:
                recs.append(
                    f'LOT SIZE in {regime}: 2-lot avg ₹{two_lot["avg_pnl"]:+,.0f} '
                    f'vs 1-lot ₹{one_lot["avg_pnl"]:+,.0f}. '
                    f'Consider capping at 1 lot when regime=CHOPPY.'
                )

    return recs or ['All parameters appear well-calibrated given current data.']


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
    n = len(records)
    run_date = datetime.now(IST).date().isoformat()

    print(f'\n{"="*60}')
    print(f'  FnO_T_Bot Adaptive Parameter Analysis — {run_date}')
    print(f'  PATH-A records: {n}  (need ≥{MIN_RECORDS} for optimization)')
    print(f'{"="*60}')

    if n == 0:
        print('\n  No records yet. Run daily_debrief.py after trading hours.')
        return

    hold_results    = optimize_hold_threshold(records)
    reentry_results = optimize_reentry_adx(records)
    dow_results     = analyze_dow_adx(records)
    regime_lot      = analyze_regime_lot_size(records)
    recs            = generate_recommendations(hold_results, records)

    print(fmt_hold_table(hold_results))
    print(fmt_reentry_table(reentry_results))
    print(fmt_dow_table(dow_results))
    print(fmt_regime_lot_table(regime_lot))

    print('\n  ── Recommendations ──')
    for r in recs:
        print(f'  → {r}')

    # Save snapshot
    snapshot = {
        'run_date'        : run_date,
        'n_records'       : n,
        'hold_simulation' : hold_results,
        'reentry_sim'     : reentry_results,
        'dow_adx'         : dow_results,
        'regime_lot'      : regime_lot,
        'recommendations' : recs,
    }
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, 'a', encoding='utf-8') as f:
        f.write(json.dumps(snapshot) + '\n')

    print(f'\n  Snapshot saved to {OUTPUT_PATH}')
    print(f'{"="*60}\n')


if __name__ == '__main__':
    main()
