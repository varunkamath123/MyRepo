# -*- coding: utf-8 -*-
"""
Does the overnight gap carry tradeable information?
===================================================

Pre-registered, before any code is written around it. Exactly one hypothesis
family is being tested here -- unlike the 3,072 intraday variants that produced
DSR 0.0% -- so the multiple-testing penalty stays small and a positive result
would actually mean something.

The question
------------
If gap direction conditions what the session then does, two separate strategies
(gap-up / gap-down) are justified. If it does not, they are two ways of
overfitting the same absent edge.

Three sub-questions, each with a null:
  1. CONTINUATION: does a gap up predict an up day? (null: no relationship)
  2. FADE: do gaps get filled -- does price return to prior close?
  3. VOLATILITY: is the intraday RANGE different after a gap? (a real effect
     even if direction is not, and it would matter for sizing/stops)

Measured in index points and percentages. No option pricing, so nothing here
inherits the 15.7% BS/IV error.
"""
from __future__ import annotations
import glob, os
import statistics as st
import random

import pandas as pd


def sessions(data_dir: str, sub: str) -> list[dict]:
    """Build per-session records with the prior close attached."""
    files = sorted(glob.glob(os.path.join(data_dir, sub, '*.csv')))
    out, prev_close, prev_day = [], None, None
    for f in files:
        day = os.path.basename(f).split('_')[-1].replace('.csv', '')
        try:
            df = pd.read_csv(f, parse_dates=['ts'], index_col='ts')
        except Exception:
            continue
        if len(df) < 20:
            continue
        o = float(df['Open'].iloc[0]); c = float(df['Close'].iloc[-1])
        hi = float(df['High'].max());  lo = float(df['Low'].min())
        if prev_close is not None:
            gap_pct = (o - prev_close) / prev_close * 100.0
            # did price trade back to the prior close at any point?
            filled = (lo <= prev_close <= hi)
            out.append(dict(
                day=day, open=o, close=c, high=hi, low=lo,
                prev_close=prev_close, gap_pct=gap_pct,
                day_pct=(c - o) / o * 100.0,
                range_pct=(hi - lo) / o * 100.0,
                up_ext=(hi - o) / o * 100.0,
                dn_ext=(o - lo) / o * 100.0,
                gap_filled=filled,
            ))
        prev_close, prev_day = c, day
    return out


def _perm_diff_median(a: list[float], b: list[float], n_iter: int = 5000,
                      seed: int = 3) -> float:
    """Two-sided permutation p-value for difference in medians."""
    if len(a) < 3 or len(b) < 3:
        return 1.0
    random.seed(seed)
    obs = abs(st.median(a) - st.median(b))
    pool = list(a) + list(b); nA = len(a)
    hits = 0
    for _ in range(n_iter):
        random.shuffle(pool)
        if abs(st.median(pool[:nA]) - st.median(pool[nA:])) >= obs:
            hits += 1
    return hits / n_iter


def analyse(rows: list[dict], label: str, thresh: float = 0.15) -> None:
    up   = [r for r in rows if r['gap_pct'] >=  thresh]
    dn   = [r for r in rows if r['gap_pct'] <= -thresh]
    flat = [r for r in rows if abs(r['gap_pct']) < thresh]
    print("=" * 84)
    print(f"{label}   sessions={len(rows)}   gap threshold ±{thresh}%")
    print("=" * 84)
    print(f"  gap up {len(up)}   flat {len(flat)}   gap down {len(dn)}")
    if len(up) < 5 or len(dn) < 5:
        print("  too few gapped sessions\n"); return

    print()
    print("  1. CONTINUATION — does gap direction predict the day?")
    print(f"     {'cohort':10s} {'n':>4s} {'med day%':>9s} {'mean day%':>10s} {'up days%':>9s}")
    for nm, g in (('gap UP', up), ('flat', flat), ('gap DOWN', dn)):
        if not g: continue
        d = [r['day_pct'] for r in g]
        print(f"     {nm:10s} {len(g):4d} {st.median(d):+9.3f} {st.mean(d):+10.3f} "
              f"{100*sum(1 for x in d if x>0)/len(d):8.1f}%")
    p_cont = _perm_diff_median([r['day_pct'] for r in up],
                               [r['day_pct'] for r in dn])
    print(f"     permutation p (up vs down, median day move): {p_cont:.4f}"
          f"   -> {'SIGNIFICANT' if p_cont < 0.05 else 'not significant'}")

    print()
    print("  2. FADE — do gaps get filled?")
    for nm, g in (('gap UP', up), ('gap DOWN', dn)):
        f = 100.0 * sum(1 for r in g if r['gap_filled']) / len(g)
        print(f"     {nm:10s} filled same session: {f:5.1f}%  (n={len(g)})")
    print("     baseline: a flat session trivially 'fills', so compare UP vs DOWN")

    print()
    print("  3. VOLATILITY — is the day's range different after a gap?")
    print(f"     {'cohort':10s} {'n':>4s} {'med range%':>11s} {'med upExt%':>11s} {'med dnExt%':>11s}")
    for nm, g in (('gap UP', up), ('flat', flat), ('gap DOWN', dn)):
        if not g: continue
        print(f"     {nm:10s} {len(g):4d} {st.median([r['range_pct'] for r in g]):11.3f} "
              f"{st.median([r['up_ext'] for r in g]):11.3f} "
              f"{st.median([r['dn_ext'] for r in g]):11.3f}")
    p_vol = _perm_diff_median([r['range_pct'] for r in up + dn],
                              [r['range_pct'] for r in flat])
    print(f"     permutation p (gapped vs flat, median range): {p_vol:.4f}"
          f"   -> {'SIGNIFICANT' if p_vol < 0.05 else 'not significant'}")

    print()
    print("  4. DIRECTIONAL EDGE — best fixed rule conditional on gap")
    for nm, g in (('gap UP', up), ('gap DOWN', dn)):
        d = [r['day_pct'] for r in g]
        long_ = st.mean(d); short_ = -st.mean(d)
        best = 'LONG' if long_ > short_ else 'SHORT'
        print(f"     after {nm:9s}: always-LONG {long_:+.3f}%   always-SHORT {short_:+.3f}%"
              f"   -> best {best}")
    print()
