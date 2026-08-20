# -*- coding: utf-8 -*-
"""Apply the Deflated Sharpe Ratio to this project's real trade record.

Counts the configuration variants actually tested this week as the trial count,
because that is what selection bias is a function of.
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from deflated_sharpe import (deflated_sharpe, sharpe, min_track_record_length,
                             expected_max_sharpe)

LOG = '/opt/trading_bot/live_bot/logs'
skip = ("near_miss","challenger","test","predictions","weekly","day_types")

seen, tr = set(), []
for f in sorted(os.listdir(LOG)):
    if "trades" not in f or not f.endswith(".jsonl") or any(s in f for s in skip):
        continue
    for line in open(os.path.join(LOG, f)):
        line = line.strip()
        if not line: continue
        try: t = json.loads(line)
        except Exception: continue
        k = (t.get("entry_time","")[:19], t.get("instrument",""), round(t.get("pnl_net",0),0))
        if k in seen: continue
        seen.add(k); tr.append(t)
tr.sort(key=lambda x: x.get('entry_time',''))

# Configuration variants genuinely evaluated on live/backtest samples this week.
# Undercounting this makes DSR look BETTER, so this is deliberately generous
# to the strategy rather than to the argument.
TRIALS = {
    'chase_gate'   : 4,   # 0.75 / 0.93 / off / re-armed-no-exempt
    'rev_chase'    : 2,   # none / 0.40
    'trend_retrace': 2,   # 25-70% band / retired
    'trend_leg_atr': 2,   # none / 1.0
    'unified_thr'  : 3,   # 55 / 40 / observe-only
    'scorer_weights': 2,  # band profiles / per-path profiles
    'rev_components': 2,  # with IVSkew / without
    'oi_bias'      : 2,   # on / stripped
    'regime_gates' : 2,   # CHOPPY+TRENDING REV kill on / off
    'tuesday_gate' : 2,   # REV-only exempt / REV+TREND exempt
}
N_TRIALS = 1
for v in TRIALS.values():
    N_TRIALS *= v

def block(name, rows):
    pnl = [t.get('pnl_net', 0.0) for t in rows]
    print("=" * 78)
    print(f"{name}   n={len(pnl)}")
    print("=" * 78)
    if len(pnl) < 3:
        print("  sample too small for DSR\n"); return
    r = deflated_sharpe(pnl, n_trials=N_TRIALS)
    print(f"  observed Sharpe (per trade) : {r['sharpe']:+.4f}")
    print(f"  skew {r['skew']:+.2f}   kurtosis {r['kurtosis']:.2f}")
    print(f"  selection-bias threshold    : {r['sr0']:+.4f}   "
          f"(expected best of {N_TRIALS:,} random tries)")
    print(f"  DSR = P(true Sharpe > 0)    : {r['dsr']*100:5.1f}%")
    print(f"  -> {r['verdict']}")
    m = min_track_record_length(pnl)
    print(f"  trades needed for significance: "
          f"{'infinite (mean <= 0)' if m == float('inf') else f'{m:,.0f}'}   (have {len(pnl)})")
    print()

print()
print("#" * 78)
print("# DEFLATED SHARPE — how much of this week's work survives selection bias?")
print("#" * 78)
print(f"\n  configuration variants tested this week: {N_TRIALS:,}")
print("  (product of the independent choices listed in TRIALS)")
print(f"  bar a strategy must clear by luck alone: Sharpe "
      f"{expected_max_sharpe(N_TRIALS, 1/81):.4f} on n=82\n")

block("ALL TRADES (mixed pricing)", tr)
block("PATH_REV only", [t for t in tr if str(t.get('path')) == 'REV'])
block("PATH_TREND only", [t for t in tr if str(t.get('path')) == 'TREND'])
block("REAL-PRICED ERA (Aug 19+)",
      [t for t in tr if t.get('entry_time','')[:10] >= '2026-08-19'])

print("=" * 78)
print("READING THIS")
print("=" * 78)
print("  DSR asks: given how many variants we tried, could this result have")
print("  come from the luckiest of them by chance alone? >=95% means no.")
print()
print("  The trades-needed figure depends on sd/mean, so it is INVARIANT to a")
print("  uniform mis-pricing of P&L — it stays valid across the Black-Scholes era.")
