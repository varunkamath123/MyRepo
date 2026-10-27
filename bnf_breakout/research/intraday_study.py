# -*- coding: utf-8 -*-
"""Does the morning predict the rest of the day? In basis points, against costs.

    python intraday_study.py [BNF_5MIN_DIR]

Documented in the literature for index products: the return over the first half-hour
tends to predict the return over the last half-hour; and an overnight gap either
fills or runs. Neither has been tested on this data. Each is reported as the average
SIGNED forward return (in bps of spot) from trading in the direction the morning
suggests, because a correlation can be statistically real and still be far smaller
than the ~8 bps a round trip costs, which is the number that decides whether it is
tradeable.

Windows (5-minute bars, 09:15 open):
    first  = bars 0-5    09:15-09:45
    mid    = bars 6-68   09:45-14:55
    last   = bars 69-74  14:55-15:25
    gap    = open vs previous close
"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bnf_data  # noqa: E402

COST_BPS = 8.0


def spearman(x, y):
    x, y = np.asarray(x), np.asarray(y)
    rx, ry = np.argsort(np.argsort(x)), np.argsort(np.argsort(y))
    rho = float(np.corrcoef(rx, ry)[0, 1])
    t = rho * math.sqrt((len(x) - 2) / max(1 - rho * rho, 1e-12))
    return rho, 2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2))))


def report(label, sig, fwd):
    """sig: the morning signal; fwd: the forward return in bps. Trade sign(sig)."""
    sig, fwd = np.asarray(sig), np.asarray(fwd)
    m = sig != 0
    sig, fwd = sig[m], fwd[m]
    n = len(sig)
    rho, p = spearman(sig, fwd)
    pnl = np.sign(sig) * fwd                       # signed outcome of following the signal
    hit = float((pnl > 0).mean() * 100)
    se = pnl.std(ddof=1) / math.sqrt(n)
    h = n // 2
    big = np.abs(sig) >= np.quantile(np.abs(sig), 2 / 3)
    print(f'  {label:44} n={n:3} rho {rho:+.3f} (p={p:.2g})  hit {hit:4.1f}%  '
          f'mean {pnl.mean():+6.2f} bps (+/-{1.96 * se:.2f})  halves {pnl[:h].mean():+.2f}/{pnl[h:].mean():+.2f}'
          f'  | biggest-third signals {pnl[big].mean():+.2f} bps')
    return pnl.mean()


def main(d):
    sess = bnf_data.load_history(d)
    print('=' * 112)
    print(f'INTRADAY STRUCTURE — {len(sess)} BANKNIFTY sessions {sess[0]["day"]} .. {sess[-1]["day"]}')
    print(f'Forward returns in bps of spot. A round trip costs about {COST_BPS:.0f} bps, so a mean must clear that')
    print('=' * 112)
    first, mid, last, gap, rest = [], [], [], [], []
    prevC = None
    for s in sess:
        C, O = s['C'], s['O']
        if len(C) < 75:
            prevC = C[-1]
            continue
        first.append((C[5] / O[0] - 1) * 1e4)
        mid.append((C[68] / C[5] - 1) * 1e4)
        last.append((C[74] / C[68] - 1) * 1e4)
        rest.append((C[74] / C[5] - 1) * 1e4)
        gap.append(0.0 if prevC is None else (O[0] / prevC - 1) * 1e4)
        prevC = C[-1]
    first, mid, last, gap, rest = (np.array(a) for a in (first, mid, last, gap, rest))
    print('\n  Following the morning, forward return in the direction of the signal:')
    a = report('first 30 min -> LAST 30 min', first, last)
    b = report('first 30 min -> rest of day (09:45-15:25)', first, rest)
    c = report('first 30 min -> mid-day (09:45-14:55)', first, mid)
    d_ = report('overnight GAP -> rest of day (09:45-15:25)', gap, rest)
    e = report('overnight GAP -> first 30 min', gap, first)
    print('\n  Fading instead of following is the same number with the sign flipped.')
    print(f'  Best absolute mean above is {max(abs(x) for x in (a, b, c, d_, e)):.2f} bps against a {COST_BPS:.0f} bps cost.')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else r'C:\Users\Varun Kamath\AppData\Local\Temp\claude\C--Program-Files-Git\cb28b681-71c2-46aa-822a-29e400e15f68\scratchpad\bnf')
