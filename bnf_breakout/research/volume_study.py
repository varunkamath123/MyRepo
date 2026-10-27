# -*- coding: utf-8 -*-
"""Does VOLUME predict anything usable for BANKNIFTY option buying?

Run:  python volume_study.py <history_dir>

Pre-registered before looking at results (round-number cut-offs, nothing tuned):
  data      sessions with volume (index-future volume is 0 in the file before 2025-07-01).
            Relative volume (RV) = volume / median volume for the SAME bar over the previous
            20 sessions. Two forms at each decision time: RV_day (cumulative since the open)
            and RV_now (last 3 bars). Buckets: low < 0.8, mid 0.8-1.25, high > 1.25.
  Q1 MAGNITUDE   Does RV predict how far price moves next (|return| and range over +60 min and
                 to 15:00), and the chance of a move big enough to pay for a bought option
                 (> 0.5%)? Spearman rank correlation + bucket table.
  Q2 DIRECTION   Does RV change what the first-period move predicts? Momentum vs fade, by bucket.
  Q3 EVENT       A single big-volume bar (RV > 2): does the direction of that bar continue over
                 the next 6 bars? One event per session (the first) so events do not overlap.
  bar        A magnitude result matters only if it survives BOTH halves. A direction result must
             also beat the 8 bps cost. Many cells are shown, so a lone cell near the edge is noise.
"""
import glob
import os
import sys

import numpy as np
import pandas as pd

COST_BPS = 8.0
rng = np.random.default_rng(11)


def load(d):
    S = []
    for f in sorted(glob.glob(os.path.join(d, 'banknifty_5min_*.csv'))):
        x = pd.read_csv(f)
        x['ts'] = pd.to_datetime(x['ts'])
        if len(x) >= 70 and x['Volume'].sum() > 0:
            S.append(x.reset_index(drop=True))
    return S


def end_index(d):
    for j, t in enumerate(d['ts']):
        if t.strftime('%H:%M') >= '14:55':
            return j
    return len(d) - 1


def spearman(a, b):
    return pd.Series(a).rank().corr(pd.Series(b).rank())


def boot(x, n=3000):
    x = np.asarray(x, float)
    b = [rng.choice(x, len(x)).mean() for _ in range(n)]
    return np.percentile(b, 2.5), np.percentile(b, 97.5)


def main(d):
    S = load(d)
    print(f'{len(S)} sessions with volume, {S[0]["ts"].iloc[0].date()} .. {S[-1]["ts"].iloc[0].date()}')
    V = np.array([s['Volume'].values[:75] for s in S], float)
    rows = {14: [], 33: []}
    for k in range(20, len(S)):
        prior = V[k - 20:k]
        med = np.median(prior, axis=0)
        cum_med = np.median(np.cumsum(prior, axis=1), axis=0)
        s = S[k]
        c = s['Close'].values
        for kd in (14, 33):
            rv_day = V[k, :kd + 1].sum() / cum_med[kd]
            rv_now = V[k, kd - 2:kd + 1].sum() / med[kd - 2:kd + 1].sum()
            e = end_index(s)
            j60 = min(kd + 12, e)
            hi60, lo60 = s['High'].values[kd + 1:j60 + 1].max(), s['Low'].values[kd + 1:j60 + 1].min()
            rows[kd].append(dict(
                k=k, rv_day=rv_day, rv_now=rv_now,
                abs60=abs(c[j60] / c[kd] - 1) * 100, rng60=(hi60 - lo60) / c[kd] * 100,
                absEOD=abs(c[e] / c[kd] - 1) * 100,
                r60=(c[j60] / c[kd] - 1) * 100, rEOD=(c[e] / c[kd] - 1) * 100,
                r_open=c[kd] / s['Open'].iloc[0] - 1))
    bucket = lambda x: pd.cut(x, [0, 0.8, 1.25, 1e9], labels=['low<0.8', 'mid', 'high>1.25'])

    for kd, name in ((14, '10:30'), (33, '12:00')):
        D = pd.DataFrame(rows[kd])
        print(f'\n################ decision {name}  (n={len(D)})')
        print('Q1 MAGNITUDE: rank correlation of RV with what happens next')
        for rv in ('rv_day', 'rv_now'):
            print(f'  {rv}: ' + '   '.join(f'{t} rho {spearman(D[rv], D[t]):+.2f}' for t in ('abs60', 'rng60', 'absEOD')))
        for rv in ('rv_day', 'rv_now'):
            D['b'] = bucket(D[rv])
            g = D.groupby('b', observed=True)
            t = pd.DataFrame({'n': g.size(), 'mean |ret60| %': g.abs60.mean(), 'mean range60 %': g.rng60.mean(),
                              'P(range60>0.5%)': g.apply(lambda x: (x.rng60 > 0.5).mean()),
                              'mean |toEOD| %': g.absEOD.mean(), 'P(|toEOD|>0.5%)': g.apply(lambda x: (x.absEOD > 0.5).mean())})
            print(f'\n  by {rv} bucket:\n' + t.round(3).to_string())
            h = len(D) // 2
            for half, X in (('1st half', D.iloc[:h]), ('2nd half', D.iloc[h:])):
                print(f'   {half}: rho(rv, range60) {spearman(X[rv], X.rng60):+.2f}')

        print('\nQ2 DIRECTION: first-period momentum vs fade, by RV_day bucket (mean signed return to 15:00, bps)')
        D['b'] = bucket(D['rv_day'])
        for b, X in D.groupby('b', observed=True):
            sg = np.sign(X.r_open.values)
            mom = sg * X.rEOD.values * 100
            lo, hi = boot(mom)
            print(f'  {b:<10} n={len(X):3d}  momentum {mom.mean():+6.1f} bps CI[{lo:+6.1f},{hi:+6.1f}]   fade {-mom.mean():+6.1f}')

    print('\nQ3 EVENT: first bar per session with RV > 2 (09:30-14:00): does its direction continue over 6 bars?')
    ev = []
    for k in range(20, len(S)):
        prior = V[k - 20:k]
        med = np.median(prior, axis=0)
        s = S[k]
        o, c = s['Open'].values, s['Close'].values
        for i in range(3, 57):
            if med[i] > 0 and V[k, i] / med[i] > 2.0 and o[i] != c[i]:
                dr = 1 if c[i] > o[i] else -1
                ev.append(dict(k=k, t=s['ts'].iloc[i].strftime('%H:%M'), dr=dr,
                               body=abs(c[i] / o[i] - 1) * 100, f6=dr * (c[min(i + 6, 74)] / c[i] - 1) * 10000))
                break
    E = pd.DataFrame(ev)
    print(f'  {len(E)} events in {len(S) - 20} sessions')
    if len(E) >= 20:
        lo, hi = boot(E.f6)
        h = len(E) // 2
        print(f'  continuation over next 6 bars: mean {E.f6.mean():+.1f} bps CI[{lo:+.1f},{hi:+.1f}] hit {(E.f6 > 0).mean() * 100:.0f}%'
              f'  halves {E.f6[:h].mean():+.1f}/{E.f6[h:].mean():+.1f}  (cost {COST_BPS:.0f} bps)')
        big = E.body > E.body.median()
        for nm, X in (('big-bodied events', E[big]), ('small-bodied events', E[~big])):
            print(f'    {nm}: n={len(X)} mean {X.f6.mean():+.1f} bps')
    print('\nReading it: Q1 tells you whether volume forecasts HOW MUCH, Q2/Q3 whether it forecasts WHICH WAY.')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '.')
