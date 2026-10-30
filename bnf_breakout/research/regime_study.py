# -*- coding: utf-8 -*-
"""Is there a more STABLE regime filter than the shipped one, and does it trade better?

Run:  python regime_study.py <history_dir>

PRE-REGISTERED before any result was seen.
  Background  The shipped 'hot regime' gate is a DAY-LEVEL binary: previous session's std of 5-minute
              returns > the median of the 60 sessions before it. On Oct 30 it read 0.00069 vs 0.00070
              ('cold') right after a 1.72% range trend day (92nd percentile of range), and skipped a
              setup. The gate is constant within a day, so the replay is run ONCE with the gate off
              (strategy exits, replay costs, volume gate off, OI gate off) and each definition is
              applied as a filter on the trade's day. That is exact, not an approximation.
  Variants    shipped  std of last session's 5-min returns          > median of the prior 60
              range1   last session's high-low range / open         > median of the prior 60
              std3     mean std over the last 3 sessions            > median of the prior 60 stds
              range3   mean range over the last 3 sessions          > median of the prior 60 ranges
              park1    Parkinson (high-low) vol of the last session > median of the prior 60
              none     no regime gate
  Stability   Reported for each: share of days within +/-3% of the threshold (knife-edge days) and
              the day-to-day flip rate.
  Bar         A variant replaces 'shipped' only if (a) its mean per trade is higher AND the paired,
              session-level bootstrap difference in total outcome vs 'shipped' excludes 0, AND
              (b) both chronological halves favour it. 5 alternatives are compared, so one lone
              marginal win means nothing. Failing the bar means 'keep what is shipped'.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import bnf_config as cfg            # noqa: E402
import bnf_data                     # noqa: E402
import bnf_replay as RP             # noqa: E402
import bnf_strategy as S            # noqa: E402

rng = np.random.default_rng(31)
W = cfg.HOT_REGIME_WINDOW


def measures(sess):
    std = np.array([S.session_rv(s['C']) for s in sess])
    rngp = np.array([(np.max(s['H']) - np.min(s['L'])) / s['O'][0] * 100 for s in sess])
    park = np.array([np.sqrt(np.mean(np.log(np.asarray(s['H']) / np.asarray(s['L'])) ** 2) / (4 * np.log(2))) for s in sess])
    return std, rngp, park


def flags_and_ratio(vals_recent, vals_all, k):
    """For day k: statistic from sessions before k, and its ratio to the median of the 60 before those."""
    if k < W + 3:
        return None, None
    stat = vals_recent(k)
    med = np.median(vals_all[k - 1 - W:k - 1])
    return stat > med, stat / (med + 1e-12)


def main(hist_dir):
    sess = bnf_data.load_history(hist_dir)
    days = [s['day'] for s in sess]
    std, rngp, park = measures(sess)
    cfgs = {
        'shipped': (lambda k: std[k - 1], std),
        'range1': (lambda k: rngp[k - 1], rngp),
        'std3': (lambda k: np.mean(std[k - 3:k]), std),
        'range3': (lambda k: np.mean(rngp[k - 3:k]), rngp),
        'park1': (lambda k: park[k - 1], park),
    }
    hot, ratio = {}, {}
    for name, (fn, arr) in cfgs.items():
        hot[name], ratio[name] = {}, {}
        for k in range(W + 3, len(sess)):
            h, r = flags_and_ratio(fn, arr, k)
            hot[name][days[k]], ratio[name][days[k]] = h, r
    ok_days = [d for d in days[W + 3:]]

    c = RP._ReplayCfg()
    c.VOLUME_MODE = 'off'
    c.REQUIRE_HOT_REGIME = False
    trades, _ = RP.run(sess, 'strategy', c=c)
    trades = [t for t in trades if t['day'] in set(ok_days)]
    print(f'{len(sess)} sessions; {len(trades)} trades with the gate off, on {len(ok_days)} sessions with a full window\n')

    def per_session(sel):
        tot = {d: 0.0 for d in ok_days}
        for t in trades:
            if sel(t['day']):
                tot[t['day']] += t['pct']
        return np.array([tot[d] for d in ok_days])

    variants = {n: (lambda d, n=n: bool(hot[n].get(d))) for n in cfgs}
    variants['none'] = lambda d: True
    ref = per_session(variants['shipped'])
    n_s = len(ok_days)
    h = n_s // 2

    print(f'{"variant":<9} {"hot days":>8} {"trades":>7} {"mean/trade":>11} {"95% CI":>17} {"win":>5} {"halves":>16} '
          f'{"knife-edge":>10} {"flips/day":>9}  vs shipped (paired, total %/session)')
    for name, sel in variants.items():
        tr = [t for t in trades if sel(t['day'])]
        x = np.array([t['pct'] for t in tr])
        m = x.mean()
        bs = [rng.choice(x, len(x)).mean() for _ in range(2000)]
        hd = sum(1 for d in ok_days if sel(d)) / n_s * 100
        if name == 'none':
            ke, fl = float('nan'), float('nan')
        else:
            rr = np.array([ratio[name][d] for d in ok_days])
            ke = np.mean(np.abs(rr - 1) < 0.03) * 100
            hh = np.array([hot[name][d] for d in ok_days], int)
            fl = np.mean(hh[1:] != hh[:-1]) * 100
        ses = per_session(sel)
        diff = ses - ref
        bd = [diff[rng.integers(0, n_s, n_s)].mean() for _ in range(2000)]
        dtxt = '-' if name == 'shipped' else f'{diff.mean():+.4f}  CI[{np.percentile(bd, 2.5):+.4f},{np.percentile(bd, 97.5):+.4f}]  halves {diff[:h].mean():+.4f}/{diff[h:].mean():+.4f}'
        d1 = [t['pct'] for t in tr if ok_days.index(t['day']) < h]
        d2 = [t['pct'] for t in tr if ok_days.index(t['day']) >= h]
        print(f'{name:<9} {hd:7.0f}% {len(x):7d} {m:+10.3f}% [{np.percentile(bs, 2.5):+.3f},{np.percentile(bs, 97.5):+.3f}] '
              f'{(x > 0).mean() * 100:4.0f}% {np.mean(d1):+7.3f}/{np.mean(d2):+.3f} {ke:9.0f}% {fl:8.0f}%  {dtxt}')

    print('\nThe last 6 sessions under each definition (HOT / cold), ratio to its threshold:')
    print(f'{"day":<12}' + ''.join(f'{n:>16}' for n in cfgs))
    for d in days[-6:]:
        print(f'{d:<12}' + ''.join(f'{("HOT" if hot[n].get(d) else "cold") + " " + format(ratio[n].get(d) or 0, ".2f"):>16}' for n in cfgs))
    print('\nBar: a variant replaces the shipped gate only if it beats it on mean, on the paired CI, and in both halves.')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else cfg.HISTORY_DIR)
