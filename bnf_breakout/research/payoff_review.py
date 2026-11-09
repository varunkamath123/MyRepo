# -*- coding: utf-8 -*-
"""Payoff-asymmetry review of the LIVE paper trades: is the edge in the win rate, or in a few big winners?

Run:  python payoff_review.py <logs_dir> [--since 2026-10-27]

For a strategy that cuts losers early and lets winners run, "win rate" is the wrong lens. What decides it:
  payoff ratio        average win / average loss
  break-even win rate 1 / (1 + payoff ratio): the win rate at which expectancy is exactly zero
  expectancy          mean net P&L per trade, with a bootstrap CI (the number that matters)
  profit factor       gross wins / gross losses
  coverage            how many average losers ONE average winner pays for
  concentration       share of the net P&L that comes from the top 1 / top 2 trades
and how often the big winners actually arrive relative to the losers they have to cover.
"""
import glob
import json
import math
import os
import sys

import numpy as np

rng = np.random.default_rng(11)


def load(logs, since):
    T = []
    for f in sorted(glob.glob(os.path.join(logs, 'bnf_trades_*.jsonl'))):
        for ln in open(f, encoding='utf-8', errors='ignore'):
            try:
                t = json.loads(ln)
            except Exception:
                continue
            if t.get('kind') in ('abandoned',) or 'pnl_net' not in t:
                continue
            if t['entry_time'][:10] >= since:
                T.append(t)
    return T


def main(logs, since):
    T = load(logs, since)
    if len(T) < 3:
        print('too few trades')
        return
    x = np.array([t['pnl_net'] for t in T], float)
    w, l = x[x > 0], x[x <= 0]
    pr = w.mean() / abs(l.mean()) if len(l) and len(w) else float('nan')
    print(f'{len(x)} trades since {since}: net Rs{x.sum():+,.0f}')
    print(f'  win rate {len(w) / len(x) * 100:.0f}%   avg win Rs{w.mean():+,.0f}   avg loss Rs{l.mean():+,.0f}')
    print(f'  payoff ratio {pr:.2f}   break-even win rate {100 / (1 + pr):.0f}%   profit factor {w.sum() / abs(l.sum()):.2f}')
    print(f'  one average winner covers {pr:.1f} average losers')
    bs = [rng.choice(x, len(x)).mean() for _ in range(5000)]
    t_stat = x.mean() / (x.std(ddof=1) / math.sqrt(len(x)))
    print(f'  expectancy Rs{x.mean():+,.0f} per trade   bootstrap 95% CI [{np.percentile(bs, 2.5):+,.0f}, {np.percentile(bs, 97.5):+,.0f}]   t = {t_stat:.2f}')
    srt = np.sort(x)[::-1]
    for k in (1, 2):
        print(f'  top {k} trade(s) = {srt[:k].sum() / x.sum() * 100:.0f}% of the net; without them Rs{x.sum() - srt[:k].sum():+,.0f} over {len(x) - k} trades')
    big = x >= 2 * abs(l.mean())
    print(f'  "big" winners (>= 2 average losses): {big.sum()} of {len(x)} trades; losers per big winner: {(x <= 0).sum() / max(big.sum(), 1):.1f}'
          f'  (needs <= {pr * 1.0:.1f} to break even on coverage alone)')
    h = len(x) // 2
    print(f'  halves: first {h} trades Rs{x[:h].sum():+,.0f} | last {len(x) - h} trades Rs{x[h:].sum():+,.0f}')
    print('\n  THE 30-TRADE VERDICT RULE: bootstrap CI of the total > 0 AND both halves > 0; '
          f'now n={len(x)}, CI of the mean {"excludes" if np.percentile(bs, 2.5) > 0 else "includes"} zero, '
          f'halves {"both positive" if x[:h].sum() > 0 and x[h:].sum() > 0 else "NOT both positive"}.')
    sh = []
    for f in sorted(glob.glob(os.path.join(logs, 'bnf_shadow_2*.jsonl'))):
        for ln in open(f, encoding='utf-8', errors='ignore'):
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if r.get('kind') == 'stale_shadow':
                sh.append(r)
    if sh:
        d = np.array([r['delta_vs_real_incl_overlap'] for r in sh], float)
        print(f'\n  STALE SHADOW: {len(sh)} stale-closed trades; sum of (shadow - real incl. overlap) = Rs{d.sum():+,.0f}; '
              f'shadow better in {int((d > 0).sum())} of {len(d)}')


if __name__ == '__main__':
    a = [v for v in sys.argv[1:] if not v.startswith('--')]
    since = '2026-10-27'
    if '--since' in sys.argv:
        since = sys.argv[sys.argv.index('--since') + 1]
        a = [v for v in a if v != since]
    main(a[0] if a else '.', since)
