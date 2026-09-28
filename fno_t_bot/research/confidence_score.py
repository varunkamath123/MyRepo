# -*- coding: utf-8 -*-
"""CLI for the confidence score. The logic lives in fno_t_bot/confidence.py so
the live bot can publish it on every position close; this is just a readable
view of the same numbers.

    python fno_t_bot/research/confidence_score.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import confidence as C

trades = C.load_trades()
total, parts, notes = C.score(trades)
rel = [t for t in trades if not t.get('_bs')]
print('=' * 78)
print('STRATEGY CONFIDENCE SCORE')
print('=' * 78)
for k in ('significance', 'sample_power', 'data_integrity',
          'concentration', 'consistency', 'multiple_testing'):
    mx = C.MAX[k]; v = parts.get(k, 0)
    fill = int(12 * v / mx)
    print(f"  {k:18s} {v:3d}/{mx:<3d} [{'#'*fill}{'.'*(12-fill)}]  {notes.get(k,'')}")
print('-' * 78)
print(f"  TOTAL {total}/100   →  {C.band(total)}")
print(f"  book: Rs{sum(t['pnl_net'] for t in rel):+,.0f} over {len(rel)} real-priced trades")
print('=' * 78)
gaps = sorted(((C.MAX[k] - v, k) for k, v in parts.items()), reverse=True)
how = {'significance': 'more trades, or a larger per-trade edge — not more tuning',
       'sample_power': 'more trades at the CURRENT effect size, or a bigger effect',
       'data_integrity': 'keep every trade real-priced',
       'concentration': 'profit spread across sessions, not one outlier day',
       'consistency': 'both chronological halves positive, more green days',
       'multiple_testing': 'survive a clean out-of-sample period without re-tuning'}
print("\n  WHAT WOULD RAISE IT MOST:")
for gap, k in gaps[:4]:
    if gap > 0:
        print(f"    +{gap:2d} pts  {k:18s} {how[k]}")
