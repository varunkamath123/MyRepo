# -*- coding: utf-8 -*-
"""Confidence score — how much should we trust that our P&L is an EDGE?

WHY THIS EXISTS
---------------
P&L is the only success metric worth chasing, but a P&L number on its own does
not say whether it came from skill or from one lucky Tuesday. This book is
currently +Rs1,911 over 30 real-priced trades -- and 543% of that profit came
from a single session. Quoting the +Rs1,911 without that context is the kind of
thing that gets capital deployed too early.

So: one number, 0-100, answering "if we went live tomorrow, how much of this
result should we expect to repeat?" It is deliberately hard to score well on.
A strategy with no demonstrated edge SHOULD read low, and ours does.

THE SIX COMPONENTS, and why each is here rather than something else

  1. SIGNIFICANCE (25)  Is mean per-trade P&L distinguishable from zero?
     The single most important question and the one most often skipped. A
     one-sample t-test on per-trade net P&L.

  2. SAMPLE POWER (20)  Do we have enough trades to have detected our own
     effect size? Computed as n vs the n required for 80% power at the OBSERVED
     mean and variance. Low scores here mean "we cannot yet know", which is a
     different failure from "it does not work".

  3. DATA INTEGRITY (15)  What fraction of the record is priced at real traded
     premiums? Earned its place the hard way: the Sep 24-28 symbol bug put 7 of
     17 counterfactual phantoms into the book as Black-Scholes fiction and
     changed three conclusions once filtered. A strategy measured on modelled
     prices has no confidence at all, however good the numbers look.

  4. CONCENTRATION (15)  How much of the profit survives removing the best day
     and the best trade? A result that evaporates without its single best
     session is one observation, not an edge.

  5. CONSISTENCY (15)  Green-day ratio, and whether BOTH chronological halves
     are positive. Catches a strategy that worked once and stopped.

  6. MULTIPLE-TESTING HONESTY (10)  A discount for how many variants have been
     tried. ~3,072 were swept before this configuration; the more you search,
     the better a random result looks. This component can only be earned back
     by out-of-sample survival, not by more searching.

READING IT
  0-20   no evidence of edge; paper only
  20-40  suggestive, badly underpowered
  40-60  real signal, not yet trustworthy for capital
  60-80  deployable at small size
  80+    deployable
"""
from __future__ import annotations
import glob, json, math, os, statistics as st
from collections import defaultdict

LOG_DIR = '/opt/trading_bot/live_bot/logs'
REAL_PREMIUM_FROM = '2026-08-18'     # the day paper switched to real LTPs
VARIANTS_TRIED = 3072                # the documented sweep count


def load_trades(log_dir=LOG_DIR):
    out = []
    for f in sorted(glob.glob(os.path.join(log_dir, 'FnO_T_Bot_*_trades_*.jsonl'))):
        if '.bak' in f or 'challenger' in f or 'EARLY' in f:
            continue
        day = os.path.basename(f).replace('.jsonl', '').split('_')[-1]
        for line in open(f, encoding='utf-8', errors='ignore'):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get('pnl_net') is None or day < REAL_PREMIUM_FROM:
                continue
            d['_day'] = day
            # Sep 24-28 weekly symbol bug: NIFTY/SENSEX weeklies were BS-priced.
            # BANKNIFTY is monthly-only and took the unaffected code path.
            d['_bs'] = (day >= '2026-09-24' and day <= '2026-09-28'
                        and d['instrument'] in ('NIFTY', 'SENSEX'))
            out.append(d)
    out.sort(key=lambda z: (z['_day'], z['entry_time']))
    return out


def score(trades):
    rel = [t for t in trades if not t['_bs']]
    p = [t['pnl_net'] for t in rel]
    parts, notes = {}, {}
    if len(p) < 3:
        return 0, {'insufficient data': 0}, {'insufficient data': f'n={len(p)}'}

    mu, sd = st.mean(p), (st.pstdev(p) or 1e-9)

    # 1. significance -------------------------------------------------------
    try:
        from scipy import stats as sps
        t_, pv = sps.ttest_1samp(p, 0)
    except Exception:
        t_ = mu / (sd / math.sqrt(len(p))); pv = 1.0
    if   pv < 0.01 and mu > 0: parts['significance'] = 25
    elif pv < 0.05 and mu > 0: parts['significance'] = 20
    elif pv < 0.10 and mu > 0: parts['significance'] = 12
    elif pv < 0.25 and mu > 0: parts['significance'] = 5
    else:                      parts['significance'] = 0
    notes['significance'] = f"mean Rs{mu:+,.0f}/trade, t={t_:+.2f}, p={pv:.3f}"

    # 2. sample power -------------------------------------------------------
    need = ((1.96 + 0.84) ** 2) * (sd ** 2) / (mu ** 2) if mu else 9e9
    frac = len(p) / need if need else 0
    parts['sample_power'] = int(max(0, min(20, round(20 * min(frac, 1.0)))))
    notes['sample_power'] = (f"n={len(p)}, need ~{need:,.0f} for 80% power "
                             f"at this effect size ({frac*100:.1f}% there)")

    # 3. data integrity -----------------------------------------------------
    share = len(rel) / len(trades) if trades else 0
    parts['data_integrity'] = int(round(15 * share))
    notes['data_integrity'] = (f"{len(rel)}/{len(trades)} trades real-priced "
                               f"({share*100:.0f}%)")

    # 4. concentration ------------------------------------------------------
    byday = defaultdict(float)
    for t in rel:
        byday[t['_day']] += t['pnl_net']
    tot = sum(p)
    if tot <= 0:
        parts['concentration'] = 0
        notes['concentration'] = f"book is not profitable (Rs{tot:+,.0f})"
    else:
        best_day = max(byday.values())
        wo_day = tot - best_day
        wo_trade = tot - max(p)
        keep = min(wo_day, wo_trade) / tot
        parts['concentration'] = int(max(0, min(15, round(15 * keep))))
        notes['concentration'] = (f"Rs{tot:+,.0f} total; without best day "
                                  f"Rs{wo_day:+,.0f}, without best trade "
                                  f"Rs{wo_trade:+,.0f}")

    # 5. consistency --------------------------------------------------------
    green = sum(1 for v in byday.values() if v > 0)
    gr = green / len(byday) if byday else 0
    h = len(p) // 2
    halves_ok = h >= 3 and sum(p[:h]) > 0 and sum(p[h:]) > 0
    parts['consistency'] = int(round(10 * gr)) + (5 if halves_ok else 0)
    notes['consistency'] = (f"{green}/{len(byday)} green days; halves "
                            f"Rs{sum(p[:h]):+,.0f} / Rs{sum(p[h:]):+,.0f}")

    # 6. multiple-testing honesty ------------------------------------------
    # Only out-of-sample survival earns this back. With thousands of variants
    # swept and no clean OOS period, it stays near zero by construction.
    oos = 1.0 if halves_ok and pv < 0.05 else (0.3 if halves_ok else 0.0)
    disc = max(0.0, 1.0 - math.log10(max(VARIANTS_TRIED, 1)) / 4.0)
    parts['multiple_testing'] = int(round(10 * disc * oos))
    notes['multiple_testing'] = (f"{VARIANTS_TRIED:,} variants swept; "
                                 f"OOS credit {oos:.1f}")

    return sum(parts.values()), parts, notes


BANDS = [(80, 'DEPLOYABLE'), (60, 'deployable at small size'),
         (40, 'real signal, not yet trustworthy for capital'),
         (20, 'suggestive, badly underpowered'),
         (0,  'no evidence of edge — paper only')]


def main():
    tr = load_trades()
    total, parts, notes = score(tr)
    print('=' * 78)
    print('STRATEGY CONFIDENCE SCORE')
    print('=' * 78)
    for k in ('significance', 'sample_power', 'data_integrity',
              'concentration', 'consistency', 'multiple_testing'):
        if k not in parts:
            continue
        mx = {'significance': 25, 'sample_power': 20, 'data_integrity': 15,
              'concentration': 15, 'consistency': 15, 'multiple_testing': 10}[k]
        bar = '#' * int(12 * parts[k] / mx) + '.' * (12 - int(12 * parts[k] / mx))
        print(f"  {k:18s} {parts[k]:3d}/{mx:<3d} [{bar}]  {notes.get(k,'')}")
    band = next(b for t, b in BANDS if total >= t)
    print('-' * 78)
    print(f"  TOTAL {total}/100   →  {band}")
    print('=' * 78)
    print("\n  WHAT WOULD RAISE IT MOST (cheapest first):")
    gaps = sorted(((({'significance':25,'sample_power':20,'data_integrity':15,
                      'concentration':15,'consistency':15,'multiple_testing':10}[k]
                     - v), k) for k, v in parts.items()), reverse=True)
    for gap, k in gaps[:4]:
        if gap <= 0:
            continue
        how = {
            'significance': 'more trades, or a larger per-trade edge — not more tuning',
            'sample_power': 'more trades at the CURRENT effect size, or a bigger effect',
            'data_integrity': 'keep every trade real-priced (symbol fix + strike walk now do this)',
            'concentration': 'profit spread across more sessions, not one outlier day',
            'consistency': 'both chronological halves positive, more green days',
            'multiple_testing': 'survive a clean out-of-sample period without re-tuning',
        }[k]
        print(f"    +{gap:2d} pts  {k:18s} {how}")


if __name__ == '__main__':
    main()
