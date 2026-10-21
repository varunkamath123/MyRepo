# -*- coding: utf-8 -*-
"""Replay the strategy over history, bar by bar, with the SAME decision code the
live runner uses (bnf_strategy), and compare it with a random entry that shares
every exit rule.

    python bnf_replay.py [HISTORY_DIR] [--random-seeds N]

TWO CAVEATS THAT MATTER.
  * A replay on the data the thresholds were chosen from is IN-SAMPLE. It checks
    that the code does what it says and gives a sense of trade frequency; it is
    not evidence of an edge. The forward paper record is the real test.
  * Index-level, not option-level. Historical option premiums do not exist, so P&L
    is in % of spot and premium moves are inferred through
    PREMIUM_PCT_PER_INDEX_PCT. Costs use REPLAY_COST_INDEX_PCT.

THE BASELINE. A strategy's number only means something next to what a coin-flip
earns under the same exits, so the baseline enters at a random bar inside the
entry window in a random direction, with the same coil-based invalidation, the
same stale rule, the same trail and the same hard limits.
"""
from __future__ import annotations

import math
import os
import random
import statistics as st
import sys
from collections import Counter

import numpy as np

import bnf_config as cfg
import bnf_data
import bnf_strategy as S


def _view(s: dict, i: int) -> dict:
    return dict(H=s['H'][:i + 1], L=s['L'][:i + 1], C=s['C'][:i + 1],
                V=s['V'][:i + 1], T=s['T'][:i + 1])


def _random_decision(bars: dict, i: int, direction: int, c=cfg) -> S.EntryDecision:
    """An entry with no signal behind it, but the SAME thesis geometry, so the
    exits being compared are identical."""
    H, L, C = bars['H'], bars['L'], bars['C']
    lo = i - c.COIL_BARS + 1
    coil_hi, coil_lo = float(np.max(H[lo:i + 1])), float(np.min(L[lo:i + 1]))
    buf = c.THESIS_BREAK_BUFFER_PCT / 100
    inv = coil_lo * (1 - buf) if direction > 0 else coil_hi * (1 + buf)
    th = dict(level='-', level_value=None, direction=direction, coil_hi=coil_hi,
              coil_lo=coil_lo, invalidation=float(inv), atr=S.atr(H, L, i, 14),
              entry_bar=i, entry_px=float(C[i]), mode='random')
    return S.EntryDecision(True, direction, th, {})


def _close(pos: dict, dec: dict, s: dict, i: int, c=cfg) -> dict:
    pct = dec['exit_pct'] - c.REPLAY_COST_INDEX_PCT
    return dict(day=s['day'], entry_t=s['T'][pos['entry_bar']], exit_t=s['T'][i],
                dir=pos['dir'], level=pos['thesis'].get('level'), kind=dec['kind'],
                pct=pct, bars=i - pos['entry_bar'], best_fav=pos['best_fav'],
                worst_adv=pos['worst_adv'])


def run(sessions: list, mode: str = 'strategy', seed: int = 0, c=cfg) -> tuple:
    """Returns (trades, gate_stats)."""
    rng = random.Random(seed)
    trades, gate_pass, gate_only_fail, n_eval = [], Counter(), Counter(), 0
    for k in range(1, len(sessions)):
        s = sessions[k]
        n = len(s['T'])
        ctx = S.build_day_context(sessions[:k], n_bars=n, cfg=c)
        state = dict(trades_today=0, open_positions=0, last_loss_bar=None)
        pos = None
        # pick the random entry bars for the baseline up front
        rand_bar = None
        if mode == 'random':
            window = [j for j in range(7, n - c.COIL_BARS - 2)
                      if c.ENTRY_START <= S.bar_close_time(s['T'][j], c) <= c.ENTRY_END]
            rand_bar = rng.choice(window) if window else None
            rand_dir = rng.choice((1, -1))
        for i in range(n):
            bars = _view(s, i)
            if pos is not None:
                dec = S.on_bar_exit(pos, bars, i, c, replay=True)
                if dec is None and i == n - 1:
                    dec = dict(kind='force_close', reason='session end',
                               exit_pct=(s['C'][i] - pos['entry_px']) / pos['entry_px'] * 100 * pos['dir'])
                if dec is not None:
                    t = _close(pos, dec, s, i, c)
                    trades.append(t)
                    state['open_positions'] = 0
                    if t['pct'] < 0:
                        state['last_loss_bar'] = i
                    pos = None
                    continue                      # no same-bar re-entry
                continue
            if i < 7 or i >= n - 2:
                continue
            if mode == 'random':
                if i == rand_bar and state['trades_today'] == 0:
                    d = _random_decision(bars, i, rand_dir, c)
                    pos = S.new_position(d, 1.0, 1)
                    state['trades_today'] += 1
                    state['open_positions'] = 1
                continue
            d = S.evaluate_entry(bars, i, ctx, state, c)
            if c.ENTRY_START <= S.bar_close_time(s['T'][i], c) <= c.ENTRY_END:
                n_eval += 1
                fails = d.blocked_by()
                for g, (ok, _) in d.gates.items():
                    gate_pass[g] += int(ok)
                if len(fails) == 1:
                    gate_only_fail[fails[0]] += 1
            if d.enter:
                pos = S.new_position(d, 1.0, 1)
                state['trades_today'] += 1
                state['open_positions'] = 1
    return trades, dict(evaluated=n_eval, passed=gate_pass, only_fail=gate_only_fail)


def summarise(label: str, trades: list, sessions_n: int) -> dict:
    if not trades:
        print(f'  {label:34} no trades')
        return {}
    r = [t['pct'] for t in trades]
    n = len(r)
    m = st.mean(r)
    sd = st.stdev(r) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else 0.0
    h = n // 2
    a, b = (st.mean(r[:h]) if h else 0.0), (st.mean(r[h:]) if n - h else 0.0)
    win = 100 * sum(1 for x in r if x > 0) / n
    kinds = Counter(t['kind'] for t in trades)
    print(f'  {label:34} n={n:3} ({n / sessions_n:.2f}/session)  mean {m:+.3f}%  win {win:4.1f}%'
          f'  CI[{m - 1.96 * se:+.3f},{m + 1.96 * se:+.3f}]  halves {a:+.3f}/{b:+.3f}')
    print(f'  {"":34} exits: ' + ', '.join(f'{k} {v}' for k, v in kinds.most_common()))
    return dict(n=n, mean=m, lo=m - 1.96 * se, hi=m + 1.96 * se, halves=(a, b))


def main(argv: list) -> None:
    import argparse
    ap = argparse.ArgumentParser(description='Replay the strategy over history vs a random baseline')
    ap.add_argument('history_dir', nargs='?', default=cfg.HISTORY_DIR)
    ap.add_argument('--random-seeds', type=int, default=10)
    args = ap.parse_args(argv)
    hist, seeds = args.history_dir, args.random_seeds

    probs = cfg.validate()
    if probs:
        print('CONFIG PROBLEMS:', probs)
        sys.exit(2)
    sessions = bnf_data.load_history(hist)
    print('=' * 112)
    print('BANKNIFTY breakout strategy — REPLAY (index-level, IN-SAMPLE, same decision code as live)')
    print('=' * 112)
    print(f'  {len(sessions)} sessions {sessions[0]["day"]} .. {sessions[-1]["day"]}   '
          f'cost {cfg.REPLAY_COST_INDEX_PCT}% of spot   direction={cfg.DIRECTION}  '
          f'volume={cfg.VOLUME_MODE}  hot_only={cfg.REQUIRE_HOT_REGIME}')
    print(f'  hard stop {cfg.HARD_STOP_PREMIUM_PCT * 100:.0f}% premium = {S.stop_index_pct():.3f}% spot,  '
          f'target {cfg.TARGET_PREMIUM_PCT * 100:.0f}% = {S.target_index_pct():.3f}% spot,  '
          f'trail {cfg.TRAIL_ATR_MULT} ATR after +{cfg.TRAIL_ARM_INDEX_PCT}%,  '
          f'stale after {cfg.THESIS_CONFIRM_BARS} bars without {cfg.THESIS_CONFIRM_PCT}%\n')
    n_s = len(sessions) - 1
    trades, gates = run(sessions, 'strategy')
    print('--- the strategy ---')
    res = summarise('STRATEGY', trades, n_s)

    print('\n--- random entry, identical exits (the baseline to beat) ---')
    means = []
    for sd in range(seeds):
        t, _ = run(sessions, 'random', seed=sd)
        if t:
            means.append(st.mean([x['pct'] for x in t]))
    summarise('RANDOM (seed 0)', run(sessions, 'random', seed=0)[0], n_s)
    if means:
        print(f'  {"RANDOM across seeds":34} mean of means {st.mean(means):+.3f}%   '
              f'range [{min(means):+.3f}, {max(means):+.3f}]  ({len(means)} seeds)')
        if res:
            beat = sum(1 for m in means if res['mean'] > m)
            print(f'  strategy mean beats {beat}/{len(means)} random seeds')

    print('\n--- which gate blocks entries? (config-vs-strategy diagnosis) ---')
    ev = gates['evaluated']
    print(f'  {ev} bar evaluations inside the entry window with no open position')
    for g in ('window', 'capacity', 'regime', 'level', 'coil', 'volume'):
        p = gates['passed'].get(g, 0)
        print(f'    {g:9} passes {100 * p / max(ev, 1):5.1f}%   '
              f'is the ONLY failing gate on {gates["only_fail"].get(g, 0):5d} bars')
    print('  A gate that passes rarely and is often the only one failing is the one to')
    print('  loosen in bnf_config.py. If every gate passes often but results are poor,')
    print('  the thesis itself is the problem, not the thresholds.')

    if trades:
        print('\n--- by direction / level ---')
        for key in ('dir', 'level'):
            groups = {}
            for t in trades:
                groups.setdefault(t[key], []).append(t['pct'])
            for k, v in sorted(groups.items(), key=lambda kv: str(kv[0])):
                print(f'    {key}={str(k):6} n={len(v):3} mean {st.mean(v):+.3f}%')


if __name__ == '__main__':
    main(sys.argv[1:])
