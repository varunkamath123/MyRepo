# -*- coding: utf-8 -*-
"""Would a graded SCORE of the entry gates beat the binary pass/fail?

Run:  python gate_score_study.py <history_dir>

PRE-REGISTERED before any result was seen.
  Setup     Every decision bar 10:30-13:45 on every session is a hypothetical entry in the strategy's
            own direction ('toward' the nearest level), with the strategy's own exits (replay mode,
            so index-level) and the replay cost. Direction, thesis and exits come from bnf_strategy.
  Score     Equal-weight sum of four standardised margins (NOTHING fitted to outcomes):
              +z(previous-session vol / its 60-session median)   regime margin
              -z(coil ratio)                                      tighter is better
              -z(distance to the nearest level, %)                closer is better
              +z(relative volume, last 3 bars)                    only on sessions with volume
            z-scores use the pooled distribution of all bars.
  Questions Q1  Is mean outcome monotone in score quintile?  Q2  top quintile vs bottom quintile,
            session-cluster bootstrap CI, both halves.  Q3  does the top quintile beat the binary
            rule (every gate passes) and a random entry in the same window?
  Bar       A score helps only if Q2's CI excludes 0 AND both halves agree AND the top quintile
            beats the binary rule's mean. Otherwise a score is just a different way to be wrong.
  Note      Weights are equal on purpose: weights tuned on this history would be tuned on the sample
            being tested, which is how the parked 0-100 scorer ended up inverted.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import bnf_config as cfg            # noqa: E402
import bnf_data                     # noqa: E402
import bnf_strategy as S            # noqa: E402

COST = cfg.REPLAY_COST_INDEX_PCT
rng = np.random.default_rng(5)


class Relaxed:
    """Config with every gate wide open, so evaluate_entry returns direction and thesis on every bar."""

    def __init__(self):
        self.__dict__.update({k: getattr(cfg, k) for k in dir(cfg) if k.isupper()})
        self.REQUIRE_HOT_REGIME = False
        self.COIL_RATIO_MAX = 1e9
        self.LEVEL_PROX_PCT = 1e9
        self.VOLUME_MODE = 'off'
        self.OI_GATE_ENABLED = False


def main(hist_dir):
    sess = bnf_data.load_history(hist_dir)
    rel = Relaxed()
    rvs = [S.session_rv(s['C']) for s in sess]
    rows = []
    for k in range(25, len(sess)):
        s = sess[k]
        ctx = S.build_day_context(sess[:k], n_bars=len(s['T']), cfg=cfg)
        hot_ratio = rvs[k - 1] / (np.median(rvs[k - 1 - cfg.HOT_REGIME_WINDOW:k - 1]) + 1e-12)
        has_vol = float(np.sum(s['V'])) > 0 and np.all(np.isfinite(ctx['vbase'][:20]))
        for i in range(15, len(s['T'])):
            ct = S.bar_close_time(s['T'][i], cfg)
            if not (cfg.ENTRY_START <= ct <= cfg.ENTRY_END):
                continue
            d = S.evaluate_entry(s, i, ctx, dict(trades_today=0, open_positions=0, last_loss_bar=None), rel)
            if not d.enter:
                continue
            levels = S.key_levels(ctx, s['H'], s['L'], i, rel)
            px = float(s['C'][i])
            (up, _), (dn, _) = S.nearest_levels(levels, px)
            dist = min([abs(x - px) / px * 100 for x in (up, dn) if x] or [9e9])
            cr = S.compression_ratio(s['H'], s['L'], i, cfg.COIL_BARS)
            if cr is None or dist > 5:
                continue
            vr = np.nan
            if has_vol:
                vb = np.asarray(ctx['vbase'][max(0, i - 2):i + 1], float)
                vv = np.asarray(s['V'][max(0, i - 2):i + 1], float)
                if len(vb) == 3 and np.all(vb > 0):
                    vr = float(np.mean(vv / vb))
            pos = S.new_position(d, 1000.0, 1)
            out = None
            for j in range(i + 1, len(s['T'])):
                r = S.on_bar_exit(pos, s, j, cfg, replay=True)
                if r:
                    out = r['exit_pct']
                    break
            if out is None:
                out = (float(s['C'][-1]) / px - 1) * 100 * d.direction
            passes = (hot_ratio > 1 and cr <= cfg.COIL_RATIO_MAX and dist <= cfg.LEVEL_PROX_PCT)
            rows.append(dict(k=k, hot=hot_ratio, coil=cr, dist=dist, vol=vr, out=out - COST, binary=passes))
    import pandas as pd
    D = pd.DataFrame(rows)
    print(f'{len(sess)} sessions, {len(D)} hypothetical entries from {D.k.nunique()} sessions')

    def z(x):
        return (x - np.nanmean(x)) / (np.nanstd(x) + 1e-12)
    D['score'] = z(D.hot) - z(D.coil) - z(D.dist) + np.nan_to_num(z(D.vol), nan=0.0)   # missing volume = neutral
    D['q'] = pd.qcut(D.score, 5, labels=False)

    def cluster_ci(x, sess_ids, n=2000):
        ids = np.unique(sess_ids)
        by = {i: x[sess_ids == i] for i in ids}
        b = []
        for _ in range(n):
            pick = rng.choice(ids, len(ids))
            v = np.concatenate([by[i] for i in pick])
            b.append(v.mean())
        return np.percentile(b, 2.5), np.percentile(b, 97.5)

    print('\nQ1  mean outcome by score quintile (% of spot, net of cost; 1 = lowest score, 5 = highest)')
    for q in range(5):
        m = D.q == q
        x = D.out[m].values
        lo, hi = cluster_ci(x, D.k[m].values)
        print(f'   Q{q + 1}: n={m.sum():5d}  mean {x.mean():+.3f}%  CI[{lo:+.3f},{hi:+.3f}]  win {(x > 0).mean() * 100:4.1f}%')
    top, bot = D[D.q == 4], D[D.q == 0]
    h = D.k.median()
    diff = top.out.mean() - bot.out.mean()
    print(f'\nQ2  top minus bottom quintile: {diff:+.3f}%   halves: '
          f'{D[(D.q == 4) & (D.k <= h)].out.mean() - D[(D.q == 0) & (D.k <= h)].out.mean():+.3f}% / '
          f'{D[(D.q == 4) & (D.k > h)].out.mean() - D[(D.q == 0) & (D.k > h)].out.mean():+.3f}%')
    bs = []
    ids = D.k.unique()
    for _ in range(1500):
        pick = rng.choice(ids, len(ids))
        sub = D[D.k.isin(pick)]
        bs.append(sub[sub.q == 4].out.mean() - sub[sub.q == 0].out.mean())
    print(f'    cluster-bootstrap CI of the difference: [{np.percentile(bs, 2.5):+.3f}, {np.percentile(bs, 97.5):+.3f}]')

    bin_ = D[D.binary]
    allm = D.out.mean()
    print(f'\nQ3  binary rule (every gate passes): n={len(bin_)}  mean {bin_.out.mean():+.3f}%  win {(bin_.out > 0).mean() * 100:.1f}%')
    print(f'    top score quintile:             n={len(top)}  mean {top.out.mean():+.3f}%')
    print(f'    ANY bar in the window (random-like baseline): n={len(D)}  mean {allm:+.3f}%')
    thr = D.score.quantile(1 - len(bin_) / len(D))
    same = D[D.score >= thr]
    print(f'    score threshold chosen to take the SAME number of trades as the binary rule: n={len(same)}  mean {same.out.mean():+.3f}%')
    print('\nReading it: a graded score earns its keep only if the top end is reliably better than the bottom AND than the binary rule.')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else cfg.HISTORY_DIR)
