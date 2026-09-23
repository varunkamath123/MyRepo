# -*- coding: utf-8 -*-
"""
Random-entry baseline
=====================

The question this answers
------------------------
Every result we have about entry quality (median MFE 104 pts, MFE/MAE 1.33,
chase-band gradients) is stated in absolute terms. None of it has been compared
against chance. Any entry in a moving index shows positive favourable
excursion -- so 1.33 might be exactly what randomness produces here.

This runs RANDOM entries through the identical measurement: same instruments,
same sessions, same time-of-day distribution, same forward window, same
index-point accounting. If PATH_REV / PATH_TREND cannot beat that, the entry
logic contributes nothing and no amount of downstream work matters.

Design choices that keep the comparison fair
--------------------------------------------
* Random entries are drawn on the SAME instrument-days the real engines fired,
  at the SAME clock times. Otherwise we would be comparing across different
  market conditions rather than across entry logic.
* Direction is a coin flip, which is the actual null hypothesis: "the engines
  add nothing over picking a side at random".
* Many random draws per real entry, so the baseline has tight error bars while
  the strategy sample stays at its true (small) size.
* Measured in index points -- no option pricing, so the 15.7% BS/IV error
  cannot contaminate the comparison.
"""
from __future__ import annotations
import os, glob, random
import statistics as st

import pandas as pd


def forward_excursion(day_df, i: int, direction: str, force_close: str):
    """MFE / MAE / close in index points from bar i to force-close."""
    fwd = day_df.iloc[i + 1:]
    fwd = fwd[[t.strftime('%H:%M') <= force_close for t in fwd.index]]
    if len(fwd) == 0:
        return None
    e = float(day_df['Close'].iloc[i])
    if direction == 'PUT':
        mfe = e - float(fwd['Low'].min())
        mae = float(fwd['High'].max()) - e
        cl  = e - float(fwd['Close'].iloc[-1])
    else:
        mfe = float(fwd['High'].max()) - e
        mae = e - float(fwd['Low'].min())
        cl  = float(fwd['Close'].iloc[-1]) - e
    return dict(mfe=mfe, mae=mae, close=cl)


def run(real_entries: list[dict], data_dir: str, dirs: dict,
        force_close: str = '14:30', draws_per_entry: int = 40,
        seed: int = 11) -> dict:
    """
    real_entries: [{'day':'20260819','inst':'NIFTY','hm':'10:15',
                    'dir':'PUT','mfe':..,'mae':..,'close':..}, ...]

    Returns the strategy's stats, the random baseline's stats, and a
    permutation p-value for the difference in median close.
    """
    random.seed(seed)
    cache: dict = {}

    def day_bars(inst: str, day: str):
        k = (inst, day)
        if k not in cache:
            f = sorted(glob.glob(f"{data_dir}/{dirs[inst]}/*{day}*.csv"))
            cache[k] = (pd.read_csv(f[0], parse_dates=['ts'], index_col='ts')
                        if f else None)
        return cache[k]

    rnd = []
    for e in real_entries:
        df = day_bars(e['inst'], e['day'])
        if df is None or len(df) < 12:
            continue
        # bar index matching the real entry's clock time
        idxs = [j for j, t in enumerate(df.index)
                if t.strftime('%H:%M') <= e['hm']]
        if not idxs:
            continue
        i = idxs[-1]
        for _ in range(draws_per_entry):
            d = random.choice(('CALL', 'PUT'))
            r = forward_excursion(df, i, d, force_close)
            if r:
                rnd.append(r)

    def stats(rows):
        if not rows:
            return {}
        mfe = [r['mfe'] for r in rows]; mae = [r['mae'] for r in rows]
        cl  = [r['close'] for r in rows]
        return dict(n=len(rows),
                    right=100.0 * sum(1 for c in cl if c > 0) / len(cl),
                    med_mfe=st.median(mfe), med_mae=st.median(mae),
                    med_close=st.median(cl), mean_close=st.mean(cl),
                    ratio=st.median(mfe) / max(st.median(mae), 1e-9))

    s_real = stats(real_entries)
    s_rand = stats(rnd)

    # Permutation test: is the strategy's median close better than random?
    p = None
    if real_entries and rnd:
        obs = st.median([r['close'] for r in real_entries]) - \
              st.median([r['close'] for r in rnd])
        pool = [r['close'] for r in real_entries] + [r['close'] for r in rnd]
        nA = len(real_entries)
        hits = 0; N = 2000
        for _ in range(N):
            random.shuffle(pool)
            d = st.median(pool[:nA]) - st.median(pool[nA:])
            if d >= obs:
                hits += 1
        p = hits / N

    return {'strategy': s_real, 'random': s_rand, 'p_value': p,
            'draws_per_entry': draws_per_entry}


def report(res: dict) -> None:
    s, r, p = res['strategy'], res['random'], res['p_value']
    print("=" * 78)
    print("STRATEGY vs RANDOM ENTRY — identical sessions, times, exits, accounting")
    print("=" * 78)
    if not s or not r:
        print("  insufficient data"); return
    print(f"{'':10s} {'n':>6s} {'right%':>8s} {'medMFE':>8s} {'medMAE':>8s} "
          f"{'medClose':>9s} {'MFE/MAE':>8s}")
    print("-" * 78)
    for name, d in (('strategy', s), ('random', r)):
        print(f"{name:10s} {d['n']:6d} {d['right']:7.1f}% {d['med_mfe']:8.1f} "
              f"{d['med_mae']:8.1f} {d['med_close']:9.1f} {d['ratio']:8.2f}")
    print()
    print(f"  random baseline = {res['draws_per_entry']} coin-flip draws per real entry")
    print(f"  edge in right-side%   : {s['right'] - r['right']:+.1f} pts")
    print(f"  edge in median close  : {s['med_close'] - r['med_close']:+.1f} index pts")
    print(f"  edge in MFE/MAE ratio : {s['ratio'] - r['ratio']:+.2f}")
    if p is not None:
        print(f"  permutation p-value   : {p:.3f}  "
              f"({'significant' if p < 0.05 else 'NOT significant'} at 5%)")
    print()
    if p is not None and p < 0.05 and s['med_close'] > r['med_close']:
        print("  -> entry logic beats chance. Downstream work is justified.")
    else:
        print("  -> entry logic does NOT beat chance on this sample. Any apparent")
        print("     'edge' in MFE/MAE or chase bands is consistent with randomness,")
        print("     and further entry tuning cannot be justified from this data.")
