# -*- coding: utf-8 -*-
"""
Exit rule comparison on REAL option premiums
============================================

Why this is worth doing even with no entry edge
-----------------------------------------------
The random-entry baseline showed the entry logic is indistinguishable from a
coin flip. That makes entry work unjustifiable -- but it does NOT make exit
work pointless. "Given you are holding this option, what is the best way out?"
is a separate question, and a real one: if two exit rules produce different
outcomes on identical premium paths, that difference is attributable to the
rule alone, not to entry selection.

Data
----
data_breeze/options/*.csv -- actual 5-min OHLCV per contract, fetched from
Breeze. NOT Black-Scholes. This matters: the Aug 19 comparison showed BS with
rolling HV under-priced real premiums by 41-123%, and BS with real IV still
missed by 15.7% with the sign varying. Every earlier exit conclusion in this
project was drawn on modelled prices. These are traded prices.

Method
------
For each contract, every bar with sufficient forward runway is treated as an
entry, and each candidate rule is walked forward on the SAME path. That
removes entry-time selection and gives a large sample from few contracts.

Caveat stated up front: entries within one contract overlap heavily, so these
are not independent observations. Differences between rules on the same paths
are meaningful; absolute win rates are not a forecast.
"""
from __future__ import annotations
import glob, os, re
import statistics as st

import pandas as pd


def load_contracts(folder: str) -> list[dict]:
    out = []
    for f in sorted(glob.glob(os.path.join(folder, '*.csv'))):
        m = re.match(r'opt_(\w+?)_(\d{8})_(\d+)(CE|PE)\.csv', os.path.basename(f))
        if not m:
            continue
        try:
            df = pd.read_csv(f, parse_dates=['ts'], index_col='ts')
        except Exception:
            continue
        if len(df) < 12 or 'Close' not in df:
            continue
        out.append(dict(inst=m.group(1), day=m.group(2), strike=int(m.group(3)),
                        kind=m.group(4), df=df, file=os.path.basename(f)))
    return out


# ── Exit rules ───────────────────────────────────────────────────────────────
# Each returns (pnl_pct, bars_held, reason). `path` is the forward premium
# series as a list of (high, low, close) after entry.

def _walk(path, entry, stop=None, target=None, trail_act=None, trail_dist=None,
          np_bars=None, np_peak=None, max_bars=None):
    peak = 0.0
    for k, (hi, lo, cl) in enumerate(path):
        if max_bars is not None and k >= max_bars:
            return (cl - entry) / entry, k + 1, 'time'
        # intrabar worst case first — a stop that would have been hit is hit
        if stop is not None and (lo - entry) / entry <= -stop:
            return -stop, k + 1, 'stop'
        if target is not None and (hi - entry) / entry >= target:
            return target, k + 1, 'target'
        cur = (cl - entry) / entry
        peak = max(peak, (hi - entry) / entry)
        if (trail_act is not None and trail_dist is not None
                and peak > trail_act and cur < peak - trail_dist):
            return cur, k + 1, 'trail'
        if (np_bars is not None and np_peak is not None
                and k + 1 >= np_bars and peak < np_peak and cur < 0):
            return cur, k + 1, 'never-progressed'
    hi, lo, cl = path[-1]
    return (cl - entry) / entry, len(path), 'close'


RULES = {
    # the live stack (NEVER_PROGRESS 45min = 9 five-minute bars)
    'CURRENT  stop25/tgt50/trail12-10/NP45':
        dict(stop=.25, target=.50, trail_act=.12, trail_dist=.10, np_bars=9, np_peak=.05),
    'no Never-Progressed':
        dict(stop=.25, target=.50, trail_act=.12, trail_dist=.10),
    'wider trail 18/12':
        dict(stop=.25, target=.50, trail_act=.18, trail_dist=.12, np_bars=9, np_peak=.05),
    'tighter trail 8/6':
        dict(stop=.25, target=.50, trail_act=.08, trail_dist=.06, np_bars=9, np_peak=.05),
    'pure trail, no target':
        dict(stop=.25, trail_act=.12, trail_dist=.10, np_bars=9, np_peak=.05),
    'stop+target only':
        dict(stop=.25, target=.50),
    'stop only, ride to close':
        dict(stop=.25),
    'hold to close (no rules)':
        dict(),
    'tight stop 15%, trail 12/10':
        dict(stop=.15, target=.50, trail_act=.12, trail_dist=.10, np_bars=9, np_peak=.05),
}


def run(folder: str, min_forward: int = 12, step: int = 3) -> dict:
    contracts = load_contracts(folder)
    results = {k: [] for k in RULES}
    reasons = {k: {} for k in RULES}
    n_entries = 0
    for c in contracts:
        df = c['df']
        closes = df['Close'].astype(float).tolist()
        highs  = df['High'].astype(float).tolist()
        lows   = df['Low'].astype(float).tolist()
        for i in range(0, len(df) - min_forward, step):
            entry = closes[i]
            if entry <= 1.0:
                continue
            path = list(zip(highs[i+1:], lows[i+1:], closes[i+1:]))
            if len(path) < min_forward:
                continue
            n_entries += 1
            for name, kw in RULES.items():
                pnl, bars, why = _walk(path, entry, **kw)
                results[name].append(pnl)
                reasons[name][why] = reasons[name].get(why, 0) + 1
    return dict(results=results, reasons=reasons,
                n_contracts=len(contracts), n_entries=n_entries)


def report(res: dict) -> None:
    R, RE = res['results'], res['reasons']
    print("=" * 92)
    print(f"EXIT RULES ON REAL PREMIUMS — {res['n_contracts']} contracts, "
          f"{res['n_entries']:,} simulated entries")
    print("=" * 92)
    print(f"{'rule':40s} {'mean%':>8s} {'median%':>8s} {'win%':>7s} "
          f"{'p10%':>8s} {'p90%':>8s}")
    print("-" * 92)
    rows = []
    for name, vals in R.items():
        if not vals:
            continue
        v = sorted(vals)
        q = lambda p: v[int(p * (len(v) - 1))]
        rows.append((st.mean(vals), name, st.median(vals),
                     100.0 * sum(1 for x in vals if x > 0) / len(vals), q(.10), q(.90)))
    for mean, name, med, win, p10, p90 in sorted(rows, reverse=True):
        print(f"{name:40s} {mean*100:+8.2f} {med*100:+8.2f} {win:6.1f}% "
              f"{p10*100:+8.1f} {p90*100:+8.1f}")
    print()
    best = max(rows)[1]; cur = 'CURRENT  stop25/tgt50/trail12-10/NP45'
    cm = next(r[0] for r in rows if r[1] == cur)
    bm = max(rows)[0]
    print(f"  current stack mean : {cm*100:+.2f}%")
    print(f"  best rule mean     : {bm*100:+.2f}%   ({best})")
    print(f"  difference         : {(bm-cm)*100:+.2f} pct-points per trade")
    print()
    print("  exit-reason mix, current stack:")
    tot = sum(RE[cur].values())
    for why, n in sorted(RE[cur].items(), key=lambda x: -x[1]):
        print(f"    {why:18s} {n:6d}  {100*n/tot:5.1f}%")
