# -*- coding: utf-8 -*-
"""For each LIVE trade: was an earlier entry available, and what would it have done?

Run:  python earlier_entry_review.py <live_logs_dir> <history_dir>

  <live_logs_dir>  holds bnf_trades_<date>.jsonl (the real trades)
  <history_dir>    holds banknifty_5min_<date>.csv including every day a trade was taken

Method (model-based, because historical option quotes do not exist):
  * a CANDIDATE earlier entry is any decision bar before the real one on which every gate the live strategy uses
    passes EXCEPT the coil (window, regime, level; volume and OI are off / shadow, as live). The coil gate is the one
    that delays entries (it needs 15 bars, so nothing can enter before ~10:30, and it refuses trending impulses).
  * each candidate is run through the strategy's own exit rules (on_bar_exit) on the real bars and priced with
    Black-Scholes at the implied vol solved from THAT trade's real entry premium (flat across the day), with the
    same bid/ask haircut and ~Rs100 round-trip cost.  The model reproduces real exits to about +-Rs1,000 a trade.
  * the candidate's direction is whatever the strategy's 'toward' rule says at that bar; 'same side' candidates
    (same direction as the real trade) are reported separately because an opposite-side candidate is a different bet.

CAVEATS that matter: candidates are evaluated INDEPENDENTLY (an earlier trade would have occupied the single position
slot and might have blocked the real one); results are a sample of the live window (a +7.5% trend in 12 sessions);
and looking only at WINNING trades would flatter any earlier entry, so losing trades are reported too.
"""
import glob
import json
import math
import os
import sys
from math import exp, log, sqrt

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import bnf_config as cfg            # noqa: E402
import bnf_data                     # noqa: E402
import bnf_strategy as S            # noqa: E402
import datetime as dt               # noqa: E402

N = lambda x: 0.5 * (1 + math.erf(x / sqrt(2)))      # noqa: E731


def bs(Sx, K, T, v, opt, r=0.065):
    d1 = (log(Sx / K) + (r + .5 * v * v) * T) / (v * sqrt(T))
    c = Sx * N(d1) - K * exp(-r * T) * N(d1 - v * sqrt(T))
    return c if opt == 'CE' else c - Sx + K * exp(-r * T)


def solve_iv(opt, P, Sx, K, T):
    lo, hi = .02, 1.5
    for _ in range(80):
        m = (lo + hi) / 2
        if bs(Sx, K, T, m, opt) > P:
            hi = m
        else:
            lo = m
    return (lo + hi) / 2


class Cfg:
    def __init__(self, **kw):
        self.__dict__.update({k: getattr(cfg, k) for k in dir(cfg) if k.isupper()})
        self.COIL_GATE_ENABLED = False
        self.VOLUME_MODE = 'off'
        self.OI_GATE_ENABLED = False
        self.__dict__.update(kw)


def T_to(day, bar_idx, expiry_iso):
    y, m, d = (int(x) for x in expiry_iso.split('-'))
    exp_t = dt.datetime(y, m, d, 15, 30)
    y0, m0, d0 = (int(x) for x in day.split('-'))
    t = dt.datetime(y0, m0, d0) + dt.timedelta(hours=9, minutes=15 + 5 * (bar_idx + 1))
    return max((exp_t - t).total_seconds() / 86400 / 365, 1e-6)


def load_trades(logs):
    T = []
    for f in sorted(glob.glob(os.path.join(logs, 'bnf_trades_*.jsonl'))):
        for ln in open(f, encoding='utf-8', errors='ignore'):
            try:
                t = json.loads(ln)
            except Exception:
                continue
            if 'pnl_net' in t and t.get('thesis'):
                T.append(t)
    return T


def simulate(sess, i, d, iv, expiry, c):
    """Enter on decision bar i in the strategy's direction; return (exit bar, kind, net Rs)."""
    s = sess
    opt = 'CE' if d.direction > 0 else 'PE'
    K = int(round(float(s['C'][i]) / 100) * 100)
    e = bs(float(s['C'][i]), K, T_to(s['day'], i, expiry), iv, opt) * 1.0018
    pos = S.new_position(d, e, 1)
    j_exit, kind = len(s['T']) - 1, 'eod'
    for j in range(i + 1, len(s['T'])):
        r = S.on_bar_exit(pos, {k: v[:j + 1] for k, v in s.items() if k in ('H', 'L', 'C', 'T', 'O', 'V')}, j, c)
        if r:
            j_exit, kind = j, r['kind']
            break
    x = bs(float(s['C'][j_exit]), K, T_to(s['day'], j_exit, expiry), iv, opt) * 0.9982
    return j_exit, kind, (x - e) * cfg.LOT_SIZE - 100.0, opt


def main(logs, hist_dir):
    sess = [s for s in bnf_data.load_history(hist_dir) if len(s['T']) == 75]
    by = {s['day']: k for k, s in enumerate(sess)}
    c = Cfg()
    rows = []
    for t in load_trades(logs):
        day = t['entry_time'][:10]
        if day not in by:
            continue
        k = by[day]
        s = sess[k]
        th = t['thesis']
        i0 = th['entry_bar']
        expiry = t['expiry']
        opt0 = t['opt']
        T0 = T_to(day, i0, expiry)
        iv = solve_iv(opt0, t['entry_premium'] / 1.0018, th['entry_px'], t['strike'], T0)
        ctx = S.build_day_context(sess[:k], n_bars=75, cfg=cfg)
        cands = []
        for i in range(7, i0):
            ct = S.bar_close_time(s['T'][i], cfg)
            if not (cfg.ENTRY_START <= ct <= cfg.ENTRY_END):
                continue
            d = S.evaluate_entry({kk: v[:i + 1] for kk, v in s.items() if kk in ('H', 'L', 'C', 'T', 'O', 'V')}, i, ctx,
                                 dict(trades_today=0, open_positions=0, last_loss_bar=None), c)
            if not d.enter:
                continue
            j, kind, net, opt = simulate(s, i, d, iv, expiry, c)
            cr = S.compression_ratio(s['H'], s['L'], i, cfg.COIL_BARS)
            cands.append(dict(t=S.bar_close_time(s['T'][i], cfg), dir=d.direction, same=(opt == opt0), net=net, kind=kind,
                              exit=s['T'][j], coil=cr))
        rows.append(dict(day=day, t=t['entry_time'][11:16], opt=opt0, real=t['pnl_net'], kind=t['kind'], cands=cands,
                         coil_real=t['entry_gates']['coil'][1]))

    def show(label, sel):
        print(f'\n=== {label} ({len(sel)} trades) ===')
        print(f'{"trade":<22}{"real":>8}  {"earlier same-side candidates":>30}  {"earliest":>16} {"best":>8} {"worst":>8} {"mean":>8}   coil at real entry')
        for r in sel:
            same = [x for x in r['cands'] if x['same']]
            if same:
                e = same[0]
                print(f'{r["day"][5:]} {r["t"]} {r["opt"]:<4}{r["real"]:>8.0f}  {len(same):>14} of {len(r["cands"]):<2} candidates  '
                      f'{e["t"]} Rs{e["net"]:>6.0f}  {max(x["net"] for x in same):>8.0f} {min(x["net"] for x in same):>8.0f} '
                      f'{np.mean([x["net"] for x in same]):>8.0f}   {r["coil_real"]}')
            else:
                print(f'{r["day"][5:]} {r["t"]} {r["opt"]:<4}{r["real"]:>8.0f}  {"none":>14}')

    win = [r for r in rows if r['real'] > 0]
    lose = [r for r in rows if r['real'] <= 0]
    show('PROFITABLE live trades', win)
    show('LOSING live trades (the control)', lose)

    def summary(label, sel):
        has = [r for r in sel if any(x['same'] for x in r['cands'])]
        if not sel:
            return
        first = [next(x for x in r['cands'] if x['same'])['net'] for r in has]
        real = [r['real'] for r in has]
        allm = [np.mean([x['net'] for x in r['cands'] if x['same']]) for r in has]
        beat = sum(1 for a, b in zip(first, real) if a > b)
        print(f'\n{label}: {len(has)} of {len(sel)} trades had an earlier same-side candidate. '
              f'Earliest candidate beat the real trade in {beat} of {len(has)}; '
              f'mean real Rs{np.mean(real):+.0f} vs mean earliest candidate Rs{np.mean(first):+.0f} vs mean of ALL earlier candidates Rs{np.mean(allm):+.0f}.')
    summary('PROFITABLE', win)
    summary('LOSING', lose)
    allc = [x for r in rows for x in r['cands']]
    print(f'\nAll {len(allc)} earlier candidates on those days (any side): mean Rs{np.mean([x["net"] for x in allc]):+.0f}, '
          f'win rate {np.mean([x["net"] > 0 for x in allc]) * 100:.0f}%; same side as the real trade: '
          f'{np.mean([x["net"] for x in allc if x["same"]]):+.0f}; opposite side: {np.mean([x["net"] for x in allc if not x["same"]]):+.0f}')
    pre = [x for x in allc if x['coil'] is None]
    print(f'Candidates before the coil can even be computed (before ~10:30): {len(pre)}, mean Rs{np.mean([x["net"] for x in pre]):+.0f}' if pre else '')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
