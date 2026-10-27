# -*- coding: utf-8 -*-
"""Can ANY set of features known at decision time predict BANKNIFTY's direction?

Run:  python direction_study.py <history_dir>      (banknifty_5min_YYYYMMDD.csv files)

Pre-registered before looking at results:
  question   Is the sign of the index's forward return predictable from information available
             at the decision time? (direction only: no option pricing, no stops.)
  decisions  10:30 and 12:00 (bar closes). Horizons: +60 min and the 15:00 close. 4 tests, so
             treat a single p < 0.0125 as the bar, not 0.05.
  features   gap, first-period return, last-30-min return, position in yesterday's range,
             yesterday's return, 5-day return, distance to PDH / PDL, range so far vs
             yesterday's range, price vs the day's mean close.
  model      L2-regularised logistic regression, standardised features, fitted on ALL PRIOR
             sessions only (expanding window, refit every 20 sessions, first 120 sessions
             never scored). No tuning on the scored sessions.
  baselines  always long, always short, follow the first-period move (momentum), fade it,
             and coin flips. A model that cannot beat the better of those has learnt nothing.
  success    mean signed return CI excludes 0 AND both chronological halves are positive AND
             the gross edge exceeds the round-trip cost (REPLAY_COST_INDEX_PCT).
  the unknown  'abstain' is the honest use of uncertainty: also report trading only the most
             confident third of predictions.
"""
import glob
import os
import sys

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

COST_BPS = 8.0                                    # REPLAY_COST_INDEX_PCT = 0.08%
rng = np.random.default_rng(7)


def load(d):
    S = []
    for f in sorted(glob.glob(os.path.join(d, 'banknifty_5min_*.csv'))):
        x = pd.read_csv(f)
        x['ts'] = pd.to_datetime(x['ts'])
        x = x.reset_index(drop=True)
        if len(x) >= 70:
            S.append(x)
    return S


def features(S, k, kd):
    d, p = S[k], S[k - 1]
    pp = S[k - 2]['Close'].iloc[-1]
    c = d['Close'].values
    pdh, pdl, pc = p['High'].max(), p['Low'].min(), p['Close'].iloc[-1]
    c5 = S[k - 5]['Close'].iloc[-1] if k >= 5 else pp
    prange = (pdh - pdl) / pc
    so_far = (d['High'][:kd + 1].max() - d['Low'][:kd + 1].min()) / pc
    return dict(
        gap=d['Open'].iloc[0] / pc - 1,
        r_open=c[kd] / d['Open'].iloc[0] - 1,
        r_30=c[kd] / c[max(kd - 6, 0)] - 1,
        pos_prev=(pc - pdl) / max(pdh - pdl, 1e-9),
        prev_ret=pc / pp - 1,
        r5=pc / c5 - 1,
        d_pdh=c[kd] / pdh - 1,
        d_pdl=c[kd] / pdl - 1,
        rng_ratio=so_far / max(prange, 1e-9),
        vs_mean=c[kd] / float(np.mean(c[:kd + 1])) - 1,
    )


def forward(d, kd, horizon):
    c = d['Close'].values
    end = len(d) - 1
    for j, t in enumerate(d['ts']):
        if t.strftime('%H:%M') >= '14:55':          # the bar closing at 15:00
            end = j
            break
    j = min(kd + horizon, end) if horizon else end
    return (c[j] / c[kd] - 1) * 100                 # percent


def ci_mean(x, n=4000):
    x = np.asarray(x, float)
    b = [rng.choice(x, len(x)).mean() for _ in range(n)]
    return np.percentile(b, 2.5), np.percentile(b, 97.5)


def report(name, sig, ret):
    """sig in {-1, 0, +1}; ret in percent. Signed return in bps over taken trades."""
    m = sig != 0
    if m.sum() < 20:
        print(f'  {name:<34} n={m.sum()} (too few)')
        return
    s = sig[m] * ret[m] * 100                     # bps
    hit = (s > 0).mean()
    lo, hi = ci_mean(s)
    h = len(s) // 2
    print(f'  {name:<34} n={m.sum():3d} hit {hit*100:4.1f}%  mean {s.mean():+6.1f} bps  CI[{lo:+6.1f},{hi:+6.1f}]'
          f'  halves {s[:h].mean():+6.1f}/{s[h:].mean():+6.1f}  net of cost {s.mean() - COST_BPS:+6.1f}')


def main(d):
    S = load(d)
    print(f'{len(S)} sessions, {S[0]["ts"].iloc[0].date()} .. {S[-1]["ts"].iloc[0].date()}')
    for tname, kd in (('10:30', 14), ('12:00', 33)):
        for hname, hz in (('+60min', 12), ('to 15:00', 0)):
            X, R, ids = [], [], []
            for k in range(6, len(S)):
                X.append(features(S, k, kd))
                R.append(forward(S[k], kd, hz))
                ids.append(k)
            X = pd.DataFrame(X)
            R = np.array(R)
            n = len(X)
            P = np.full(n, np.nan)
            start = 120
            for a in range(start, n, 20):
                tr = slice(0, a)
                sc = StandardScaler().fit(X.iloc[tr])
                y = (R[tr] > 0).astype(int)
                m = LogisticRegression(C=0.1, max_iter=500).fit(sc.transform(X.iloc[tr]), y)
                b = min(a + 20, n)
                P[a:b] = m.predict_proba(sc.transform(X.iloc[a:b]))[:, 1]
            ok = ~np.isnan(P)
            Rs, Ps, Xs = R[ok], P[ok], X[ok]
            print(f'\n=== decide {tname}, horizon {hname}: {ok.sum()} scored sessions, '
                  f'base rate up {np.mean(Rs > 0)*100:.0f}%, mean move {Rs.mean()*100:+.1f} bps, '
                  f'sd {Rs.std()*100:.0f} bps')
            report('always long', np.ones(len(Rs)), Rs)
            report('always short', -np.ones(len(Rs)), Rs)
            mom = np.sign(Xs['r_open'].values)
            report('follow first-period move', mom, Rs)
            report('fade first-period move', -mom, Rs)
            report('coin flip (random side)', rng.choice([-1, 1], len(Rs)), Rs)
            sig = np.where(Ps > 0.5, 1, -1)
            report('MODEL, every session', sig, Rs)
            conf = np.abs(Ps - 0.5)
            top = conf >= np.quantile(conf, 2 / 3)
            report('MODEL, most confident third', np.where(top, sig, 0), Rs)
            acc = ((Ps > 0.5) == (Rs > 0)).mean()
            print(f'  directional accuracy {acc*100:.1f}%  (binomial 95% CI about +/-{1.96*np.sqrt(.25/len(Rs))*100:.1f} pts)')
    print('\nA result counts only if the model beats the best baseline above, both halves are positive,'
          f' and the mean exceeds the {COST_BPS:.0f} bps cost.')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '.')
