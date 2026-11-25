# -*- coding: utf-8 -*-
"""What if every live trade had been taken on the OPPOSITE side?

Run:  python opposite_review.py <live_logs_dir> <history_dir>

For each real trade, at the same decision bar, the opposite option is bought (ATM, at the ask) and run through the
strategy's own exits, priced with Black-Scholes at the implied vol solved from the REAL trade's entry premium (flat),
bid/ask haircut and ~Rs100 round-trip cost, exactly as research/earlier_entry_review.py does. Two versions:

  FLIP   the opposite side with the same coil-edge thesis logic (invalidation is the OTHER edge of the coil).
  AWAY   the strategy's own DIRECTION='away' mode: fade the level; the thesis is that the level HOLDS and the
         invalidation is the level itself (+ buffer). Built by evaluate_entry with DIRECTION='away', so it is what
         the live code would have done had it been configured so.

Reported for all trades, for the losers only, and for the winners only. 'Do the opposite of the losers' is a
HINDSIGHT choice: you cannot know in advance which trades will lose, so the honest comparison is FLIPPING EVERYTHING
(the 'all' row). The 'losers only' row is an upper bound that no rule can reach.
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..'))
import earlier_entry_review as ER   # noqa: E402
import bnf_config as cfg            # noqa: E402
import bnf_data                     # noqa: E402
import bnf_strategy as S            # noqa: E402


def main(logs, hist):
    sess = [s for s in bnf_data.load_history(hist) if len(s['T']) == 75]
    by = {s['day']: k for k, s in enumerate(sess)}
    c_live = ER.Cfg(COIL_GATE_ENABLED=True)                 # the real gates, as live
    c_away = ER.Cfg(COIL_GATE_ENABLED=True, DIRECTION='away')
    rows = []
    for t in ER.load_trades(logs):
        day = t['entry_time'][:10]
        if day not in by:
            continue
        k = by[day]
        s = sess[k]
        th = t['thesis']
        i0 = th['entry_bar']
        T0 = ER.T_to(day, i0, t['expiry'])
        iv = ER.solve_iv(t['opt'], t['entry_premium'] / 1.0018, th['entry_px'], t['strike'], T0)
        ctx = S.build_day_context(sess[:k], n_bars=75, cfg=cfg)
        view = {kk: v[:i0 + 1] for kk, v in s.items() if kk in ('H', 'L', 'C', 'T', 'O', 'V')}
        st = dict(trades_today=0, open_positions=0, last_loss_bar=None)
        d_away = S.evaluate_entry(view, i0, ctx, st, c_away)
        away = flip = None
        if d_away.enter:
            _, ka, na, _ = ER.simulate(s, i0, d_away, iv, t['expiry'], c_live)
            away = (na, ka, d_away.direction)
        # FLIP: same bar, opposite direction, coil-edge thesis (built from the real thesis' coil)
        dr = -th['direction']
        inv = th['coil_lo'] * (1 - 0.0005) if dr > 0 else th['coil_hi'] * (1 + 0.0005)
        th2 = dict(th)
        th2.update(direction=dr, invalidation=float(inv))
        for kk in ('oi', 'oi_would_block', 'oi_detail'):
            th2.pop(kk, None)
        dflip = S.EntryDecision(True, dr, th2, {})
        _, kf, nf, _ = ER.simulate(s, i0, dflip, iv, t['expiry'], c_live)
        flip = (nf, kf, dr)
        rows.append(dict(day=day, time=t['entry_time'][11:16], side=t['opt'], level=th['level'], real=t['pnl_net'], rk=t['kind'],
                         flip=flip, away=away))

    def tab(sel, label):
        print(f'\n=== {label} ({len(sel)} trades) ===')
        print(f'{"trade":<24}{"real":>8} {"kind":<14}{"FLIP side":>11}{"net":>8} {"exit":<14}{"AWAY":>8}{"net":>8} {"exit":<13}')
        for r in sel:
            a = r['away']
            print(f'{r["day"][5:]} {r["time"]} {r["side"]:<3} {r["level"]:<5}{r["real"]:>8.0f} {r["rk"]:<14}'
                  f'{"CE" if r["flip"][2] > 0 else "PE":>11}{r["flip"][0]:>8.0f} {r["flip"][1]:<14}'
                  f'{("CE" if a[2] > 0 else "PE") if a else "-":>8}{(a[0] if a else 0):>8.0f} {(a[1] if a else "not entered"):<13}')

    losers = [r for r in rows if r['real'] <= 0]
    winners = [r for r in rows if r['real'] > 0]
    tab(losers, 'the LOSING trades, taken on the opposite side')

    def tot(sel, label):
        real = sum(r['real'] for r in sel)
        flip = sum(r['flip'][0] for r in sel)
        away = sum(r['away'][0] for r in sel if r['away'])
        n_away = sum(1 for r in sel if r['away'])
        fw = sum(1 for r in sel if r['flip'][0] > 0)
        aw = sum(1 for r in sel if r['away'] and r['away'][0] > 0)
        print(f'  {label:<26} n={len(sel):2d}  REAL Rs{real:+8.0f}   FLIP Rs{flip:+8.0f} ({fw} winners)   '
              f'AWAY Rs{away:+8.0f} ({aw} winners of {n_away} entered)')
    print('\nSUMMARY (modelled, 1 lot, net of costs)')
    tot(rows, 'ALL live trades')
    tot(losers, 'the losers only')
    tot(winners, 'the winners only')
    print('\nReading it: flipping only the losers is hindsight. Flipping ALL trades (first row) is the honest test of "do the opposite".')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
