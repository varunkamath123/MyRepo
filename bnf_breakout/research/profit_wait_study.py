# -*- coding: utf-8 -*-
"""'Wait until profitable, then exit by the existing rules' versus the exits as built (replay, index level).

Run:  python profit_wait_study.py <history_dir>

PRE-REGISTERED before any result was seen.
  Idea     (owner) A position that is not yet profitable is not closed by the stale, thesis-broken or
           trail rules. Once it has closed a bar net-profitable (index move in its favour beyond the
           round-trip cost, REPLAY_COST_INDEX_PCT), the existing rules apply as normal. The hard stop,
           the target and the 15:00 close are always on.
  Setup    The strategy's own entries (volume gate off, regime gate on, as shipped), 433 sessions.
           Leverage set to a realistic 30% premium per 1% index (the shipped 71.4 would make the
           hard stop 0.35% of spot). Costs are in each trade's pct.
  Compare  As-built exits vs profit-wait, same entries: trade count, win rate, average win, average
           loss, payoff ratio, expectancy with a bootstrap CI, total, worst trade, and the paired
           session-level difference in total outcome (CI, both halves).
  Bar      Profit-wait is worth shadowing forward if its paired difference is positive in BOTH halves.
           It is adopted only on the forward record. A replay cannot see premium paths, so 'profitable'
           here is judged on closes, not on the live net-of-cost premium.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import bnf_config as cfg            # noqa: E402
import bnf_data                     # noqa: E402
import bnf_replay as RP             # noqa: E402
import bnf_strategy as S            # noqa: E402

rng = np.random.default_rng(17)


def mk(**kw):
    c = RP._ReplayCfg()
    c.VOLUME_MODE = 'off'
    c.PREMIUM_PCT_PER_INDEX_PCT = 30.0
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def run_variant(sess, profit_wait: bool, min_net_pct: float):
    orig = S.on_bar_exit
    if profit_wait:
        def patched(pos, bars, i, cfg_=None, replay=False):
            c = float(bars['C'][i])
            cl = (c - pos['entry_px']) / pos['entry_px'] * 100 * pos['dir']
            if cl >= min_net_pct:
                pos['pw_on'] = True
            if pos.get('pw_on'):
                return orig(pos, bars, i, cfg_, replay)        # profitable at some close: the existing rules
            return S.on_bar_exit_profit_wait(pos, bars, i, cfg_, replay, profitable_now=False)
        # the replay module calls S.on_bar_exit; swap it for the duration of this run only
        RP.S.on_bar_exit = patched
    try:
        trades, _ = RP.run(sess, 'strategy', c=mk())
    finally:
        RP.S.on_bar_exit = orig
    return trades


def stats(name, tr):
    x = np.array([t['pct'] for t in tr])
    w, l = x[x > 0], x[x <= 0]
    pr = w.mean() / abs(l.mean())
    bs = [rng.choice(x, len(x)).mean() for _ in range(3000)]
    h = len(x) // 2
    print(f'{name:<26} n={len(x):3d} win {len(w) / len(x) * 100:3.0f}%  avg win {w.mean():+.2f}%  avg loss {l.mean():+.2f}%  payoff {pr:.2f}  '
          f'expectancy {x.mean():+.3f}% CI[{np.percentile(bs, 2.5):+.3f},{np.percentile(bs, 97.5):+.3f}]  total {x.sum():+.1f}%  '
          f'worst {x.min():+.2f}%  halves {x[:h].mean():+.3f}/{x[h:].mean():+.3f}')


def main(hist_dir):
    sess = [s for s in bnf_data.load_history(hist_dir) if len(s['T']) == 75]
    days = [s['day'] for s in sess]
    base = run_variant(sess, False, 0.0)
    cost = cfg.REPLAY_COST_INDEX_PCT
    print(f'{len(sess)} sessions; profitable = a bar closes beyond the cost ({cost}% of spot) in the trade\'s favour\n')
    stats('as built', base)
    results = {}
    for lab, thr in (('profit-wait (net of cost)', cost), ('profit-wait (any gain)', 0.0), ('profit-wait (cost x2)', 2 * cost)):
        tr = run_variant(sess, True, thr)
        results[lab] = tr
        stats(lab, tr)

    def per_day(tr):
        d = {x: 0.0 for x in days}
        for t in tr:
            d[t['day']] += t['pct']
        return np.array([d[x] for x in days])
    b = per_day(base)
    h = len(days) // 2
    print('\npaired session-level difference in total outcome vs as built (profit-wait minus as built, % of spot per session):')
    for lab, tr in results.items():
        diff = per_day(tr) - b
        bd = [diff[rng.integers(0, len(diff), len(diff))].mean() for _ in range(3000)]
        print(f'  {lab:<26} {diff.mean():+.4f}  CI[{np.percentile(bd, 2.5):+.4f},{np.percentile(bd, 97.5):+.4f}]  halves {diff[:h].mean():+.4f}/{diff[h:].mean():+.4f}')
    print('\nBar: shadow it forward only if the paired difference is positive in BOTH halves; adopt only on the forward record.')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else cfg.HISTORY_DIR)
