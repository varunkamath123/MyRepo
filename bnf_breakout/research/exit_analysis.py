# -*- coding: utf-8 -*-
"""How did the exit rules behave, and do they earn their place?

    python exit_analysis.py BNF_5MIN_DIR TODAY_DIR [YYYY-MM-DD]

PART 1 - one trade. Replays a real paper trade bar by bar through every exit rule, showing
how close each came to firing, then re-runs it with each rule varied to see what the exit
would have been. Premium after the real exit is NOT observed (the position was closed), so
those figures use the index move through PREMIUM_PCT_PER_INDEX_PCT and are labelled ESTIMATE.

PART 2 - history. Replays the as-built strategy over every stored session and switches rules
off or changes them ONE AT A TIME. It is IN-SAMPLE (the thresholds came from the same data)
and index-level, so it shows what each rule does and roughly what it is worth. It is not
permission to retune: a rule "improving" the in-sample mean by 0.02% on ~200 trades is noise,
and the confidence intervals printed beside every row say so.
"""
import json
import math
import os
import statistics as st
import sys
import types
from collections import Counter

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bnf_config                # noqa: E402
import bnf_data                  # noqa: E402
import bnf_replay as RP          # noqa: E402
import bnf_strategy as S         # noqa: E402


def ns(**over):
    d = {k: getattr(bnf_config, k) for k in dir(bnf_config) if k.isupper()}
    if 'OI_GATE_ENABLED' in d:
        d['OI_GATE_ENABLED'] = False          # no historical strike OI exists to replay it
    d.update(over)
    n = types.SimpleNamespace(**d)
    n.validate = lambda: []
    return n


# ── PART 1 ────────────────────────────────────────────────────────────────
def load_jsonl(p):
    return [json.loads(x) for x in open(p, encoding='utf-8') if x.strip()]


def part1(bnf_dir, today_dir, day):
    trades = load_jsonl(os.path.join(today_dir, f'bnf_trades_{day}.jsonl'))
    path = load_jsonl(os.path.join(today_dir, f'bnf_path_{day}.jsonl'))
    t = trades[0]
    th = t['thesis']
    d = th['direction']
    sess = [s for s in bnf_data.load_history(bnf_dir) if s['day'] == day][0]
    T, H, L, C = sess['T'], sess['H'], sess['L'], sess['C']
    e = th['entry_bar']
    ep = t['entry_premium']
    cfg0 = ns()
    print('=' * 112)
    print(f'PART 1 — the {day} trade: {t["opt"]} {t["symbol"]}  entry {t["entry_time"][11:19]} @ {ep}'
          f'  exit {t["exit_time"][11:19]} @ {t["exit_premium"]}  ({t["kind"]})  net Rs{t["pnl_net"]:+,.0f}')
    print('=' * 112)
    print(f'  thesis: {"PUT" if d < 0 else "CALL"} pressing {th["level"]} {th["level_value"]:.2f};'
          f' coil {th["coil_lo"]:.1f}-{th["coil_hi"]:.1f}; invalidation (close {"above" if d < 0 else "below"})'
          f' {th["invalidation"]:.1f}; entry index {th["entry_px"]:.2f}; ATR {th["atr"]:.1f}')
    print(f'  rules: hard stop -{cfg0.HARD_STOP_PREMIUM_PCT * 100:.0f}% premium (= {S.stop_index_pct(cfg0):.3f}% adverse),'
          f' target +{cfg0.TARGET_PREMIUM_PCT * 100:.0f}% (= {S.target_index_pct(cfg0):.3f}% favourable),'
          f' stale: < {cfg0.THESIS_CONFIRM_PCT}% follow-through after {cfg0.THESIS_CONFIRM_BARS} bars,'
          f' trail arms at +{cfg0.TRAIL_ARM_INDEX_PCT}% then {cfg0.TRAIL_ATR_MULT} ATR on a close')

    # what the premium really did
    prem = [(p['ts'][11:16], p['pct']) for p in path]
    print(f'\n  REAL premium while held: peak {max(x for _, x in prem):+.1f}%  trough {min(x for _, x in prem):+.1f}%'
          f'  last {prem[-1][1]:+.1f}%   (hard stop is -25%, target +55%: neither was close)')

    print('\n  bar-by-bar after entry (bar = open time; the rules are evaluated at each CLOSE):')
    print(f'  {"bar":>5} {"close":>9} {"fav%":>7} {"adv%":>7} {"best fav":>9} {"held":>5}  {"vs invalidation":>16}  state')
    pos = S.new_position(S.EntryDecision(True, d, th, {}), ep, 1)
    first = {}
    for i in range(e + 1, min(len(C), e + 22)):
        bars = dict(H=H[:i + 1], L=L[:i + 1], C=C[:i + 1], V=sess['V'][:i + 1], T=T[:i + 1])
        ent = th['entry_px']
        fav = ((H[i] - ent) / ent * 100) if d > 0 else ((ent - L[i]) / ent * 100)
        adv = ((ent - L[i]) / ent * 100) if d > 0 else ((H[i] - ent) / ent * 100)
        dec = S.on_bar_exit(pos, bars, i, cfg0, replay=True)
        dist = (th['invalidation'] - C[i]) if d < 0 else (C[i] - th['invalidation'])
        flag = ''
        if dec and 'first' not in first:
            first['first'] = (i, dec)
            flag = f'<- {dec["kind"].upper()}'
        print(f'  {T[i]:>5} {C[i]:>9,.1f} {fav:>+7.3f} {adv:>+7.3f} {pos["best_fav"]:>8.3f}% {i - e:>5} '
              f'{dist:>+15.1f}  {flag}')
        if dec and i > first['first'][0] + 3:
            break
    print('  (fav/adv are the bar\'s extreme against the entry index; "vs invalidation" is points of room before a')
    print('   close would break the thesis: negative means it already has.)')

    # variants of the stale rule and the confirm threshold, on this path
    print('\n  What the exit would have been under different rules (same trade, same path):')
    print(f'  {"rule set":46} {"exits at":>9} {"kind":>14} {"index move":>11} {"est. premium":>13} {"est. net Rs":>12}')
    qty = t['lots'] * bnf_config.LOT_SIZE
    cost = t['costs']

    def run_variant(label, **over):
        c = ns(**over)
        p2 = S.new_position(S.EntryDecision(True, d, th, {}), ep, 1)
        for i in range(e + 1, len(C) - 1):
            bars = dict(H=H[:i + 1], L=L[:i + 1], C=C[:i + 1], V=sess['V'][:i + 1], T=T[:i + 1])
            dec = S.on_bar_exit(p2, bars, i, c, replay=True)
            if dec:
                pct_idx = dec['exit_pct']
                # a real premium was observed up to the real exit; beyond it, only an estimate
                est = pct_idx * bnf_config.PREMIUM_PCT_PER_INDEX_PCT
                real = ''
                close_t = S._hhmm_add(T[i], 5)
                if close_t <= t['exit_time'][11:16]:
                    near = [x for tt, x in prem if tt <= close_t]
                    if near:
                        est, real = near[-1], '*'
                net = ep * (est / 100) * qty - cost
                print(f'  {label:46} {close_t:>9} {dec["kind"]:>14} {pct_idx:>+10.3f}% {est:>+11.1f}%{real:1} {net:>+12,.0f}')
                return
        print(f'  {label:46} (no exit before the end of the data)')

    run_variant('AS BUILT (stale after 6 bars, confirm 0.15%)')
    run_variant('stale after 4 bars', THESIS_CONFIRM_BARS=4)
    run_variant('stale after 8 bars', THESIS_CONFIRM_BARS=8)
    run_variant('stale after 12 bars', THESIS_CONFIRM_BARS=12)
    run_variant('confirm threshold 0.10% (it reached 0.112%)', THESIS_CONFIRM_PCT=0.10)
    run_variant('confirm threshold 0.25%', THESIS_CONFIRM_PCT=0.25)
    run_variant('NO stale rule (only broken/stop/target)', THESIS_CONFIRM_BARS=999)
    run_variant('NO stale and NO broken rule (stop/target only)', THESIS_CONFIRM_BARS=999, THESIS_BREAK_BUFFER_PCT=99.0)
    print('  (* = from the premium actually polled; other figures are ESTIMATES from the index move)')
    print('  Costs of Rs%.0f are taken from the real trade.' % cost)


# ── PART 2 ────────────────────────────────────────────────────────────────
def summ(trades):
    r = [x['pct'] for x in trades]
    n = len(r)
    if n < 10:
        return None
    m = st.mean(r)
    se = st.stdev(r) / math.sqrt(n)
    h = n // 2
    return dict(n=n, mean=m, lo=m - 1.96 * se, hi=m + 1.96 * se, win=100 * sum(1 for x in r if x > 0) / n,
                h1=st.mean(r[:h]), h2=st.mean(r[h:]), kinds=Counter(x['kind'] for x in trades),
                avg_bars=st.mean(x['bars'] for x in trades))


def part2(bnf_dir):
    sessions = bnf_data.load_history(bnf_dir)
    print('\n' + '=' * 112)
    print(f'PART 2 — are these rules worth having?  {len(sessions)} sessions, as-built entries, rules changed ONE at a time')
    print('=' * 112)
    print('  IN-SAMPLE and index-level. Compare rows by their confidence intervals, not their means.')
    print(f'  {"variant":44} {"n":>4} {"mean":>8} {"95% CI":>17} {"win":>5} {"bars":>5}  halves        exits')
    base_trades = None
    variants = [
        ('AS BUILT', {}),
        ('stale: 3 bars', dict(THESIS_CONFIRM_BARS=3)),
        ('stale: 4 bars', dict(THESIS_CONFIRM_BARS=4)),
        ('stale: 8 bars', dict(THESIS_CONFIRM_BARS=8)),
        ('stale: 12 bars', dict(THESIS_CONFIRM_BARS=12)),
        ('confirm 0.10% (looser)', dict(THESIS_CONFIRM_PCT=0.10)),
        ('confirm 0.25% (stricter)', dict(THESIS_CONFIRM_PCT=0.25)),
        ('NO stale rule', dict(THESIS_CONFIRM_BARS=999)),
        ('NO thesis-broken rule', dict(THESIS_BREAK_BUFFER_PCT=99.0)),
        ('NO stale and NO broken (price exits only)', dict(THESIS_CONFIRM_BARS=999, THESIS_BREAK_BUFFER_PCT=99.0)),
        ('NO trail', dict(TRAIL_ARM_INDEX_PCT=99.0)),
        ('trail 1.0 ATR', dict(TRAIL_ATR_MULT=1.0)),
        ('trail 2.5 ATR', dict(TRAIL_ATR_MULT=2.5)),
        ('hard stop 15% premium', dict(HARD_STOP_PREMIUM_PCT=0.15)),
        ('hard stop 35% premium', dict(HARD_STOP_PREMIUM_PCT=0.35)),
        ('target 35%', dict(TARGET_PREMIUM_PCT=0.35)),
        ('target 80%', dict(TARGET_PREMIUM_PCT=0.80)),
    ]
    for label, over in variants:
        c = ns(**over)
        trades, _ = RP.run(sessions, 'strategy', 0, c)
        s_ = summ(trades)
        if s_ is None:
            print(f'  {label:44} too few trades')
            continue
        if label == 'AS BUILT':
            base_trades = trades
        kinds = ' '.join(f'{k[:6]}{v}' for k, v in s_['kinds'].most_common(5))
        print(f'  {label:44} {s_["n"]:>4} {s_["mean"]:>+7.3f}% [{s_["lo"]:+.3f},{s_["hi"]:+.3f}] {s_["win"]:>4.0f}%'
              f' {s_["avg_bars"]:>5.1f}  {s_["h1"]:+.3f}/{s_["h2"]:+.3f}  {kinds}')
    if base_trades:
        print('\n  As built, by the rule that ended the trade:')
        by = {}
        for x in base_trades:
            by.setdefault(x['kind'], []).append(x['pct'])
        print(f'  {"exit rule":16} {"n":>4} {"mean":>8} {"best":>8} {"worst":>8}  {"share of trades":>16}')
        for k, v in sorted(by.items(), key=lambda kv: -len(kv[1])):
            print(f'  {k:16} {len(v):>4} {st.mean(v):>+7.3f}% {max(v):>+7.3f}% {min(v):>+7.3f}%  {100 * len(v) / len(base_trades):>14.0f}%')


def main():
    bnf = sys.argv[1]
    today = sys.argv[2]
    day = sys.argv[3] if len(sys.argv) > 3 else '2026-10-22'
    part1(bnf, today, day)
    part2(bnf)


if __name__ == '__main__':
    main()
