"""
trade_probability.py — Entry probability + expected outcome predictor.

Called at trade entry to estimate:
  - win_prob    : probability this trade ends in profit (0-1)
  - exp_profit  : expected Rs gain if win (not EV — conditional on win)
  - exp_time_min: expected minutes until exit

Called at trade exit to record actual outcome and close the prediction loop.

All predictions written to logs/trade_predictions.jsonl.
End-of-day: compare predicted vs actual to calibrate factors over time.
"""

from __future__ import annotations
import json
import os
from datetime import datetime

import pytz
IST = pytz.timezone('Asia/Kolkata')

LOG_PATH = os.path.join(os.path.dirname(__file__), 'logs', 'trade_predictions.jsonl')

# ── Base win rates by path (from backtest) ────────────────────────────────────
_BASE_RATE = {
    'A'   : 0.68,   # PATH-A ORB — vX backtest 68.3% WR
    'REV' : 0.50,   # PATH-REV — reversal, less established
    'REENTRY': 0.60,  # re-entry anchored to ORB structure
}
_DEFAULT_BASE = 0.60

# ── Average win / loss sizes by instrument (Rs, rough from backtest) ──────────
_AVG_WIN = {
    'NIFTY'    : 5000,
    'BANKNIFTY': 4000,
    'SENSEX'   : 3500,
}
_AVG_LOSS = {
    'NIFTY'    : -2500,
    'BANKNIFTY': -2000,
    'SENSEX'   : -1800,
}

# ── Expected time to exit by path + phase (minutes after entry) ───────────────
_EXP_TIME = {
    ('A',    'PHASE-1'): 70,
    ('A',    'PHASE-2'): 45,
    ('A',    'PHASE-3'): 20,
    ('REV',  'PHASE-1'): 35,
    ('REV',  'PHASE-2'): 25,
    ('REV',  'PHASE-3'): 15,
    ('REENTRY', 'PHASE-2'): 40,
}
_DEFAULT_TIME = 40


def compute_trade_probability(
    signal: dict,
    position: dict,
    instrument: str,
    trade_no: int,
) -> dict:
    """
    Compute entry-time prediction for a trade.
    Returns prediction dict (also written to JSONL).

    signal keys used: type, adx, path, atm_iv, oi_score, oi_bias_label,
                      htf_align_score, rev_guard_score, rev_guard_label
    position keys used: regime, posture, entry_time, entry_price,
                        entry_underlying, strike, lot_size
    """
    path      = signal.get('path', '')
    direction = signal.get('type', '')
    adx       = float(signal.get('adx', 0))
    regime    = position.get('regime', 'UNKNOWN')
    entry_dt  = position.get('entry_time', datetime.now(IST))
    now_str   = entry_dt.strftime('%H:%M') if hasattr(entry_dt, 'strftime') else ''

    # ── Phase ────────────────────────────────────────────────────────────────
    if   now_str < '11:00': phase = 'PHASE-1'
    elif now_str < '13:00': phase = 'PHASE-2'
    else:                   phase = 'PHASE-3'

    # ── Base rate ─────────────────────────────────────────────────────────────
    base = _BASE_RATE.get(path, _DEFAULT_BASE)
    factors: dict[str, float] = {'base_rate': base}

    # ── Adjustment 1: HTF SuperTrend alignment ───────────────────────────────
    # signal['htf_st'] = 'BULL' or 'BEAR' or None
    htf_st = signal.get('htf_st')
    if htf_st:
        aligned = (
            (direction == 'CALL' and htf_st == 'BULL') or
            (direction == 'PUT'  and htf_st == 'BEAR')
        )
        adj = +0.08 if aligned else -0.08
        factors['htf_st_aligned'] = adj

    # ── Adjustment 2: OI bias ─────────────────────────────────────────────────
    oi_label = signal.get('oi_bias_label', 'NEUTRAL')
    oi_adj = {'CONFIRM': +0.10, 'NEUTRAL': 0.0, 'REJECT': -0.15}.get(oi_label, 0.0)
    factors['oi_bias'] = oi_adj

    # ── Adjustment 3: ADX strength ────────────────────────────────────────────
    if   adx >= 40: adx_adj = +0.06
    elif adx >= 30: adx_adj = +0.03
    elif adx >= 25: adx_adj =  0.00
    else:           adx_adj = -0.06
    factors['adx'] = adx_adj

    # ── Adjustment 4: Regime ─────────────────────────────────────────────────
    reg_adj = {'TRENDING': +0.05, 'CHOPPY': -0.05, 'CAUTIOUS': -0.03,
               'HIGH_VOL_CHOPPY': -0.07}.get(regime, 0.0)
    factors['regime'] = reg_adj

    # ── Adjustment 5: Phase (time of day) ────────────────────────────────────
    phase_adj = {'PHASE-1': +0.06, 'PHASE-2': 0.00, 'PHASE-3': -0.06}.get(phase, 0.0)
    factors['phase'] = phase_adj

    # ── Adjustment 6: HTF align score (0-100 score from options_bot) ─────────
    htf_align = signal.get('htf_align_score')   # None if not available
    if htf_align is not None:
        if   htf_align >= 50: factors['htf_align'] = +0.05
        elif htf_align <= 20: factors['htf_align'] = -0.05
        else:                 factors['htf_align'] =  0.00

    # ── Adjustment 7: REV-GUARD (only for PATH-REV) ───────────────────────────
    rev_label = signal.get('rev_guard_label', '')
    if path == 'REV' and rev_label:
        rev_adj = {'LOW': 0.0, 'MODERATE': -0.05, 'HIGH': -0.08}.get(rev_label, 0.0)
        factors['rev_guard'] = rev_adj

    # ── Final probability ─────────────────────────────────────────────────────
    total_adj = sum(v for k, v in factors.items() if k != 'base_rate')
    win_prob  = max(0.20, min(0.90, base + total_adj))

    # ── Expected profit (conditional on win) ─────────────────────────────────
    avg_win  = _AVG_WIN.get(instrument, 4000)
    exp_profit = round(avg_win * (1.0 + (win_prob - 0.60) * 2), 0)   # scales with confidence

    # ── Expected time to exit ─────────────────────────────────────────────────
    exp_time = _EXP_TIME.get((path, phase), _DEFAULT_TIME)

    record = {
        'date'              : entry_dt.strftime('%Y-%m-%d') if hasattr(entry_dt,'strftime') else '',
        'instrument'        : instrument,
        'trade_no'          : trade_no,
        'path'              : path,
        'direction'         : direction,
        'strike'            : position.get('strike'),
        'entry_time'        : entry_dt.strftime('%H:%M:%S') if hasattr(entry_dt,'strftime') else '',
        'entry_price'       : round(position.get('entry_price', 0), 2),
        'entry_underlying'  : round(position.get('entry_underlying', 0), 2),
        'lot_size'          : position.get('lot_size', 0),
        'adx_at_entry'      : round(adx, 1),
        'regime'            : regime,
        'phase'             : phase,
        'htf_st'            : signal.get('htf_st', ''),
        'oi_bias'           : oi_label,
        'rev_guard'         : rev_label,
        'prob_win'          : round(win_prob, 3),
        'exp_profit_rs'     : int(exp_profit),
        'exp_time_min'      : exp_time,
        'factors'           : {k: round(v, 3) for k, v in factors.items()},
        # unified scorer fields (populated when UNIFIED_SCORER_ENABLED=True)
        'unified_score'     : signal.get('unified_score'),
        'unified_band'      : signal.get('unified_band'),
        'unified_components': signal.get('unified_components', {}),
        # filled at exit
        'actual_outcome'    : None,
        'actual_pnl_rs'     : None,
        'actual_exit_time'  : None,
        'actual_exit_reason': None,
        'actual_time_min'   : None,
    }

    _append_or_update(record)
    return record


def update_trade_outcome(
    instrument: str,
    trade_no: int,
    date_str: str,
    outcome: str,        # 'WIN' / 'LOSS' / 'EOD'
    pnl_rs: float,
    exit_time: datetime,
    exit_reason: str,
    entry_time: datetime,
) -> None:
    """Update the prediction record with actual exit data."""
    elapsed = int((exit_time - entry_time).total_seconds() / 60) if entry_time else None

    records = _load_all()
    for r in records:
        if (r.get('instrument') == instrument
                and r.get('trade_no') == trade_no
                and r.get('date') == date_str
                and r.get('actual_outcome') is None):
            r['actual_outcome']    = outcome
            r['actual_pnl_rs']     = round(pnl_rs, 2)
            r['actual_exit_time']  = exit_time.strftime('%H:%M:%S') if exit_time else None
            r['actual_exit_reason']= exit_reason
            r['actual_time_min']   = elapsed
            break

    _rewrite_all(records)


# ── JSONL helpers ─────────────────────────────────────────────────────────────

def _append_or_update(record: dict) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, 'a', encoding='utf-8') as f:
        f.write(json.dumps(record) + '\n')


def _load_all() -> list[dict]:
    if not os.path.exists(LOG_PATH):
        return []
    records = []
    with open(LOG_PATH, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def _rewrite_all(records: list[dict]) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, 'w', encoding='utf-8') as f:
        for r in records:
            f.write(json.dumps(r) + '\n')


def print_todays_predictions(date_str: str | None = None) -> None:
    """Print today's predictions vs actuals — called from capital_status or manual run."""
    if date_str is None:
        date_str = datetime.now(IST).strftime('%Y-%m-%d')
    records = [r for r in _load_all() if r.get('date') == date_str]
    if not records:
        print(f"No predictions logged for {date_str}.")
        return
    print(f"\n{'='*65}")
    print(f"  Trade Probability Log — {date_str}")
    print(f"{'='*65}")
    for r in records:
        status = r.get('actual_outcome') or 'OPEN'
        pnl    = r.get('actual_pnl_rs')
        pnl_s  = f"Rs{pnl:+,.0f}" if pnl is not None else "pending"
        print(
            f"  #{r['trade_no']} {r['instrument']:10s} {r['path']:6s} {r['direction']:4s} "
            f"| prob={r['prob_win']*100:.0f}%  exp=Rs{r['exp_profit_rs']:,}  "
            f"~{r['exp_time_min']}min"
        )
        print(
            f"       actual={status:<4}  {pnl_s}  "
            f"time={r.get('actual_time_min','?')}min  "
            f"reason={r.get('actual_exit_reason','?')}"
        )
        factors = r.get('factors', {})
        adj_str = '  '.join(
            f"{k}:{v:+.2f}" for k, v in factors.items() if k != 'base_rate'
        )
        print(f"       factors: base={factors.get('base_rate',0):.2f}  {adj_str}")
    print()


if __name__ == '__main__':
    import sys
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    print_todays_predictions(date_arg)
