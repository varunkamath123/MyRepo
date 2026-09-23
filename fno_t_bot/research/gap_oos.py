# -*- coding: utf-8 -*-
"""
Out-of-sample validation of the gap-fade effect
===============================================

The in-sample result: BANKNIFTY gap-up days close -0.113% on average, gap-down
days +0.187%, permutation p=0.0032. NIFTY and SENSEX point the same way but
are not individually significant.

Before that becomes a strategy it has to survive the checks that 3,072
intraday variants never got:

  A. MULTIPLE TESTING -- 9 tests were run (3 instruments x 3 questions).
     Bonferroni threshold is 0.05/9 = 0.0056.
  B. SIGN CONSISTENCY -- do all three instruments lean the same way? Under
     the null that is a coin flip each, so 3/3 carries information.
  C. OUT OF SAMPLE -- split chronologically, fit nothing, and check the
     second half independently. This is the check that matters most.
  D. THRESHOLD ROBUSTNESS -- does it survive at gap cutoffs other than the
     0.15% chosen first? If it only works at one cutoff, it is noise.
"""
from __future__ import annotations
import statistics as st
import random

from gap_study import sessions, _perm_diff_median


def split_test(rows: list[dict], label: str, thresh: float = 0.15) -> dict:
    rows = sorted(rows, key=lambda r: r['day'])
    mid = len(rows) // 2
    halves = (('IN-SAMPLE  (1st half)', rows[:mid]),
              ('OUT-SAMPLE (2nd half)', rows[mid:]))
    print("=" * 84)
    print(f"{label}  —  chronological split, threshold ±{thresh}%")
    print("=" * 84)
    res = {}
    for name, half in halves:
        up = [r['day_pct'] for r in half if r['gap_pct'] >= thresh]
        dn = [r['day_pct'] for r in half if r['gap_pct'] <= -thresh]
        if len(up) < 5 or len(dn) < 5:
            print(f"  {name}: too few gapped sessions"); continue
        p = _perm_diff_median(up, dn)
        spread = st.mean(dn) - st.mean(up)
        print(f"  {name}  {half[0]['day']}..{half[-1]['day']}")
        print(f"     gap UP   n={len(up):3d}  mean {st.mean(up):+7.3f}%  "
              f"up-days {100*sum(1 for x in up if x>0)/len(up):5.1f}%")
        print(f"     gap DOWN n={len(dn):3d}  mean {st.mean(dn):+7.3f}%  "
              f"up-days {100*sum(1 for x in dn if x>0)/len(dn):5.1f}%")
        print(f"     fade spread (down − up): {spread:+.3f} pct-pts   p={p:.4f}")
        res[name] = dict(spread=spread, p=p, n_up=len(up), n_dn=len(dn))
    print()
    return res


def threshold_sweep(rows: list[dict], label: str) -> None:
    print(f"  {label} — threshold robustness (spread = mean gapDown − mean gapUp)")
    print(f"     {'thresh':>8s} {'nUp':>5s} {'nDn':>5s} {'spread':>9s} {'p':>8s}")
    for t in (0.10, 0.15, 0.20, 0.30, 0.40, 0.50):
        up = [r['day_pct'] for r in rows if r['gap_pct'] >= t]
        dn = [r['day_pct'] for r in rows if r['gap_pct'] <= -t]
        if len(up) < 5 or len(dn) < 5:
            print(f"     {t:8.2f} {len(up):5d} {len(dn):5d}      (too few)")
            continue
        p = _perm_diff_median(up, dn, n_iter=3000)
        print(f"     {t:8.2f} {len(up):5d} {len(dn):5d} "
              f"{st.mean(dn)-st.mean(up):+9.3f} {p:8.4f}")
    print()


def sign_consistency(all_rows: dict, thresh: float = 0.15) -> None:
    print("=" * 84)
    print("SIGN CONSISTENCY across instruments (independent of significance)")
    print("=" * 84)
    signs = []
    for inst, rows in all_rows.items():
        up = [r['day_pct'] for r in rows if r['gap_pct'] >= thresh]
        dn = [r['day_pct'] for r in rows if r['gap_pct'] <= -thresh]
        if len(up) < 5 or len(dn) < 5:
            continue
        s = st.mean(dn) - st.mean(up)
        signs.append(s)
        print(f"  {inst:10s} fade spread {s:+.3f} pct-pts  "
              f"({'FADE' if s > 0 else 'CONTINUATION'})")
    k = sum(1 for s in signs if s > 0)
    n = len(signs)
    # two-sided sign test
    from math import comb
    p = sum(comb(n, i) for i in range(k, n + 1)) / (2 ** n) * 2 if n else 1.0
    print(f"\n  {k}/{n} instruments show FADE.  sign-test p ≈ {min(p,1.0):.4f}")
    print("  (all leaning the same way is itself evidence, separate from any")
    print("   single instrument clearing its own significance bar)")
    print()
