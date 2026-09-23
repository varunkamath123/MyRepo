# -*- coding: utf-8 -*-
"""
Proportional vs absolute trailing stop, on REAL option premiums
===============================================================

ONE pre-registered hypothesis
-----------------------------
The live trail gives back a FIXED 10 percentage points from the peak
(TRAILING_DISTANCE = 0.10) once activated at +12%. That fixed give-back is
scale-blind, so it punishes small peaks hardest:

    live TREND wins, peak -> realised     capture
        25.60% -> 14.02%                    55%
        19.75% ->  9.29%                    47%
        12.73% ->  2.18%                    17%   <- +Rs113 on a 12.7% peak
        25.72% -> 15.72%                    61%

A peak barely above the 12% activation surrenders nearly everything it made.
Hypothesis: giving back a FRACTION of the peak instead of a fixed number of
points captures more, because the give-back then scales with what there is to
protect.

Honesty about multiple testing
------------------------------
exit_lab.py already tested 9 variants and found the live stack best. This adds
a further 8, so the running trial count for exit rules is 17 and any winner
here must be discounted accordingly. Reported alongside each variant is a
PAIRED test against the live stack on identical premium paths -- the paired
form is the right one here because every rule walks the same entries, so the
difference is attributable to the rule and nothing else. Entries within one
contract overlap heavily, so p-values are optimistic; treat them as a ranking
device, not a probability.
"""
from __future__ import annotations
import os, sys, statistics as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from exit_lab import load_contracts

FOLDER = os.environ.get('FNO_OPT_DATA', r'C:\quant_trading\data_breeze\options')


def walk(path, entry, stop=None, target=None, trail_act=None, trail_dist=None,
         trail_frac=None, np_bars=None, np_peak=None, red_bars=None):
    """As exit_lab._walk, plus:

    trail_frac -- give back this FRACTION of the peak (proportional trail).
                  When both trail_dist and trail_frac are given, the trigger is
                  whichever is tighter, i.e. the larger implied floor.
    red_bars   -- cut any position still negative after this many bars.
    """
    peak = 0.0
    for k, (hi, lo, cl) in enumerate(path):
        # intrabar worst case first -- a stop that would have been hit is hit
        if stop is not None and (lo - entry) / entry <= -stop:
            return -stop, k + 1, 'stop'
        if target is not None and (hi - entry) / entry >= target:
            return target, k + 1, 'target'
        cur = (cl - entry) / entry
        peak = max(peak, (hi - entry) / entry)
        if trail_act is not None and peak > trail_act:
            floors = []
            if trail_dist is not None:
                floors.append(peak - trail_dist)
            if trail_frac is not None:
                floors.append(peak * (1.0 - trail_frac))
            if floors and cur < max(floors):
                return cur, k + 1, 'trail'
        if (np_bars is not None and np_peak is not None
                and k + 1 >= np_bars and peak < np_peak and cur < 0):
            return cur, k + 1, 'never-progressed'
        if red_bars is not None and k + 1 >= red_bars and cur < 0:
            return cur, k + 1, 'red-cut'
    hi, lo, cl = path[-1]
    return (cl - entry) / entry, len(path), 'close'


LIVE = 'LIVE  trail 12/10 abs'
RULES = {
    LIVE:
        dict(stop=.25, target=.50, trail_act=.12, trail_dist=.10, np_bars=9, np_peak=.05),
    # --- the hypothesis: give back a fraction of peak, not fixed points ---
    'prop  act12 give 50% of peak':
        dict(stop=.25, target=.50, trail_act=.12, trail_frac=.50, np_bars=9, np_peak=.05),
    'prop  act12 give 40% of peak':
        dict(stop=.25, target=.50, trail_act=.12, trail_frac=.40, np_bars=9, np_peak=.05),
    'prop  act12 give 33% of peak':
        dict(stop=.25, target=.50, trail_act=.12, trail_frac=.33, np_bars=9, np_peak=.05),
    'prop  act08 give 40% of peak':
        dict(stop=.25, target=.50, trail_act=.08, trail_frac=.40, np_bars=9, np_peak=.05),
    # --- hybrid: whichever floor is tighter ---
    'hybrid abs10 + prop40':
        dict(stop=.25, target=.50, trail_act=.12, trail_dist=.10, trail_frac=.40,
             np_bars=9, np_peak=.05),
    # --- loss-reduction side ---
    'LIVE + cut red after 60m':
        dict(stop=.25, target=.50, trail_act=.12, trail_dist=.10, np_bars=9, np_peak=.05,
             red_bars=12),
    'prop40 + cut red after 60m':
        dict(stop=.25, target=.50, trail_act=.12, trail_frac=.40, np_bars=9, np_peak=.05,
             red_bars=12),
    'prop40 + stop 20%':
        dict(stop=.20, target=.50, trail_act=.12, trail_frac=.40, np_bars=9, np_peak=.05),
}


def run(min_forward=12, step=3):
    contracts = load_contracts(FOLDER)
    res = {k: [] for k in RULES}
    why = {k: {} for k in RULES}
    cap = {k: [] for k in RULES}          # realised / peak, for trail exits
    n = 0
    for c in contracts:
        df = c['df']
        cl = df['Close'].astype(float).tolist()
        hi = df['High'].astype(float).tolist()
        lo = df['Low'].astype(float).tolist()
        for i in range(0, len(df) - min_forward, step):
            e = cl[i]
            if e <= 1.0:
                continue
            path = list(zip(hi[i+1:], lo[i+1:], cl[i+1:]))
            if len(path) < min_forward:
                continue
            pk = max((h - e) / e for h, _, _ in path)
            n += 1
            for name, kw in RULES.items():
                p, bars, w = walk(path, e, **kw)
                res[name].append(p)
                why[name][w] = why[name].get(w, 0) + 1
                if pk > 0.12:
                    cap[name].append(p / pk)
    return dict(res=res, why=why, cap=cap, n=n, nc=len(contracts))


if __name__ == '__main__':
    print('loading real premium contracts ...', flush=True)
    out = run()
    res, cap = out['res'], out['cap']
    base = res[LIVE]
    print(f"\n{'='*100}")
    print(f"PROPORTIONAL vs ABSOLUTE TRAIL — {out['nc']} contracts, "
          f"{out['n']:,} entries, real traded premiums")
    print('='*100)
    print(f"{'rule':32s} {'mean':>8s} {'median':>8s} {'win%':>7s} "
          f"{'capture':>8s} {'vs live':>9s} {'paired p':>10s}")
    print('-'*100)
    rows = []
    for name, v in res.items():
        w = 100 * sum(1 for x in v if x > 0) / len(v)
        c = st.median(cap[name]) if cap[name] else float('nan')
        d = st.mean(v) - st.mean(base)
        p = float('nan')
        if name != LIVE:
            try:
                from scipy import stats as sps
                diff = [a - b for a, b in zip(v, base)]
                if any(abs(x) > 1e-12 for x in diff):
                    p = sps.wilcoxon(diff).pvalue
            except Exception:
                pass
        rows.append((st.mean(v), name, st.median(v), w, c, d, p))
    for mean, name, med, w, c, d, p in sorted(rows, reverse=True):
        star = ' *' if name == LIVE else ''
        print(f"{name+star:32s} {mean*100:+7.2f}% {med*100:+7.2f}% {w:6.1f}% "
              f"{c*100:7.1f}% {d*100:+8.2f}pp {p:10.4f}")
    print('\n  capture = realised / peak, median over entries whose peak cleared 12%')
    print('  paired p = Wilcoxon on per-entry differences vs the live stack')
    print('  (overlapping entries within a contract -> p-values optimistic)')
    print('\nexit-reason mix')
    for name in res:
        tot = sum(out['why'][name].values())
        mix = '  '.join(f"{k} {100*v/tot:.0f}%" for k, v in
                        sorted(out['why'][name].items(), key=lambda z: -z[1]))
        print(f"  {name:32s} {mix}")
