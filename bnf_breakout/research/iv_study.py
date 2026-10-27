# -*- coding: utf-8 -*-
"""What do the OTHER inputs to an option's price do? Implied volatility, mostly.

    python iv_study.py OI_INTRADAY.jsonl [BNF_5MIN_DIR]

Every strategy tested so far treated an option as a leveraged bet on the index.
Its price also depends on implied volatility (vega), on time (theta), and on how the
two move with spot. This measures what the data we hold can measure, with real chain
ATM IV (not Black-Scholes), non-overlapping windows, and effect sizes translated
into % of premium so they can be compared with a 25% stop.

Rule of thumb used throughout: for an ATM option, price is roughly proportional to
IV, so a change in IV of x% (relative) is a change in premium of about x%.
A 1.0 vol-point rise on IV 14 is therefore about +7% of premium. By comparison a
0.40% index move on a ~12-day BANKNIFTY option is about +28% (delta .5, premium
~0.7% of spot).

DATA LIMITS, stated rather than hidden: 236 sessions (Apr 28 - Oct), NIFTY and
BANKNIFTY only, snapshots about 5 minutes apart, ATM IV as published by the chain
(it is sometimes unchanged for several snapshots), so small IV moves are noisy.
"""
import json
import math
import sys
from collections import defaultdict

import numpy as np


def load(path):
    rows = defaultdict(list)
    for ln in open(path, encoding='utf-8', errors='ignore'):
        try:
            d = json.loads(ln)
        except Exception:
            continue
        iv, sp, ts = d.get('atm_iv'), d.get('spot'), d.get('ts_str')
        if not iv or not sp or not ts or float(iv) <= 0:
            continue
        rows[(d['instrument'], ts[:10])].append((ts[11:16], float(sp), float(iv), d.get('iv_skew')))
    for k in rows:
        rows[k].sort()
    return rows


def at(series, hhmm):
    """Last snapshot at or before hhmm."""
    last = None
    for t, sp, iv, sk in series:
        if t <= hhmm:
            last = (sp, iv, sk)
        else:
            break
    return last


def rank(v):
    o = np.argsort(v)
    r = np.empty(len(v))
    r[o] = np.arange(len(v))
    return r


def spearman(x, y):
    if len(x) < 8:
        return float('nan'), float('nan')
    rx, ry = rank(np.asarray(x)), rank(np.asarray(y))
    rho = float(np.corrcoef(rx, ry)[0, 1])
    t = rho * math.sqrt((len(x) - 2) / max(1 - rho * rho, 1e-12))
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2))))
    return rho, p


WINDOWS = [('10:00', '11:00'), ('11:00', '12:00'), ('12:00', '13:00'), ('13:00', '14:00')]


def main(oi_path):
    rows = load(oi_path)
    print('=' * 100)
    print('IMPLIED VOLATILITY STUDY — the inputs to option price that no strategy has used')
    print('=' * 100)
    for inst in ('BANKNIFTY', 'NIFTY'):
        obs = []                                    # (dS%, dIV pts, IV0, day, window)
        for (i, day), ser in sorted(rows.items()):
            if i != inst or len(ser) < 40:
                continue
            for a, b in WINDOWS:
                x, y = at(ser, a), at(ser, b)
                if x and y:
                    obs.append(((y[0] / x[0] - 1) * 100, y[1] - x[1], x[1], day, a))
        if not obs:
            continue
        dS = np.array([o[0] for o in obs])
        dI = np.array([o[1] for o in obs])
        iv0 = np.array([o[2] for o in obs])
        rel = dI / iv0 * 100
        print(f'\n--- {inst}: {len(obs)} one-hour windows over {len({o[3] for o in obs})} sessions, '
              f'median IV {np.median(iv0):.1f} ---')

        print('\n A. HOW IV REACTS TO THE INDEX (the leverage effect): mean change in IV by spot move')
        print(f'   {"index move over the hour":28} {"n":>5} {"mean dIV":>10} {"= % of premium":>16}')
        edges = [(-9, -0.4, 'down more than 0.40%'), (-0.4, -0.15, 'down 0.15-0.40%'),
                 (-0.15, 0.15, 'flat (within 0.15%)'), (0.15, 0.4, 'up 0.15-0.40%'), (0.4, 9, 'up more than 0.40%')]
        for lo, hi, lab in edges:
            m = (dS >= lo) & (dS < hi)
            if m.sum() >= 5:
                se = rel[m].std(ddof=1) / math.sqrt(m.sum())
                print(f'   {lab:28} {m.sum():>5} {dI[m].mean():>+9.2f}p {rel[m].mean():>+13.1f}%  (+/-{1.96 * se:.1f})')
        sl_all = float(np.polyfit(dS, dI, 1)[0])
        dn, up = dS < 0, dS > 0
        sl_dn = float(np.polyfit(dS[dn], dI[dn], 1)[0])
        sl_up = float(np.polyfit(dS[up], dI[up], 1)[0])
        rho, p = spearman(dS, dI)
        print(f'   slope: {sl_all:+.2f} vol pts per +1% of index (rho {rho:+.2f}, p={p:.2g});'
              f'  on DOWN moves {sl_dn:+.2f}, on UP moves {sl_up:+.2f}')
        print('   (negative = IV rises when the index falls. A PUT then earns delta AND vega on a fall;')
        print('    a CALL earns delta but gives vega back on a rise.)')

        print('\n B. THE COST OF HOLDING WHILE NOTHING HAPPENS: IV drift by time of day, flat hours only')
        print(f'   {"hour starting":14} {"n":>5} {"mean dIV":>10} {"= % of premium":>16}')
        for a, b in WINDOWS:
            m = np.array([(o[4] == a) for o in obs]) & (np.abs(dS) < 0.15)
            if m.sum() >= 5:
                se = rel[m].std(ddof=1) / math.sqrt(m.sum())
                print(f'   {a:14} {m.sum():>5} {dI[m].mean():>+9.2f}p {rel[m].mean():>+13.1f}%  (+/-{1.96 * se:.1f})')

        # C. IV level vs next move in IV (mean reversion) using only PRIOR sessions
        print('\n C. DOES A LOW IV PREDICT IV RISING? (IV at 10:00 vs the prior 20 sessions, then 10:00 -> 12:00)')
        days = sorted({d for (i, d) in rows if i == inst})
        iv10 = {}
        for d in days:
            x = at(rows[(inst, d)], '10:00') if (inst, d) in rows else None
            if x:
                iv10[d] = x[1]
        xs, ys = [], []
        for k, d in enumerate(days):
            if d not in iv10 or k < 21:
                continue
            hist = [iv10[e] for e in days[k - 20:k] if e in iv10]
            if len(hist) < 15:
                continue
            z = (iv10[d] - float(np.mean(hist))) / (float(np.std(hist)) + 1e-9)
            y = at(rows[(inst, d)], '12:00')
            if y:
                xs.append(z)
                ys.append(y[1] - iv10[d])
        if len(xs) >= 20:
            rho, p = spearman(xs, ys)
            xs_, ys_ = np.array(xs), np.array(ys)
            lo, hi = ys_[xs_ <= np.quantile(xs_, 0.25)], ys_[xs_ >= np.quantile(xs_, 0.75)]
            print(f'   n={len(xs)} sessions: rho = {rho:+.2f} (p={p:.2g});  '
                  f'cheapest quartile -> dIV {lo.mean():+.2f}p,  dearest quartile -> dIV {hi.mean():+.2f}p')

    print('\n' + '=' * 100)


if __name__ == '__main__':
    main(sys.argv[1])
