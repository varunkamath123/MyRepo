# -*- coding: utf-8 -*-
"""Review of the overnight-carry shadow arm (bnf_overnight.py).

Run:  python overnight_review.py <live_logs_dir>      (the folder holding bnf_overnight_<date>.jsonl)

PRE-REGISTERED RULE (EVALUATION.md, amendment 2026-11-27). The ONLY pair that is judged is the primary one,
entry 15:20 -> exit 09:20, and only these four structures: straddle long, straddle short, ce long, pe long.
  * Below 60 carried nights, or with fewer than 10 that crossed a weekend or holiday: DESCRIPTIVE ONLY, no decision.
  * At >= 60 nights and >= 10 weekend nights, a side is "worth a live test" only if the bootstrap 95% CI of its mean
    net P&L per night (real bid/ask, exact costs) is above 0 AND both halves of the record are above 0.
  * Every other exit time and structure printed below is EXPLORATORY: choosing the best of many afterwards is the
    mistake this rule exists to prevent. A different pair can only be judged on sessions recorded AFTER it was chosen.
  * SHORT results exclude margin and cannot show tail risk (a 4%+ gap is rare and will not be in a 60-night sample);
    a passing short side is a reason to look at margin and tail, never a reason to sell premium.
"""
import glob
import json
import os
import sys

import numpy as np

RNG = np.random.default_rng(7)


def load(logs):
    out = []
    for f in sorted(glob.glob(os.path.join(logs, 'bnf_overnight_*.jsonl'))):
        for ln in open(f, encoding='utf-8', errors='ignore'):
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if r.get('kind') == 'overnight_carry':
                out.append(r)
    out.sort(key=lambda r: r['entry_day'])
    return out


def ci(x, n=4000):
    x = np.asarray(x, float)
    if len(x) < 2:
        return (float('nan'), float('nan'))
    b = [RNG.choice(x, len(x)).mean() for _ in range(n)]
    return float(np.percentile(b, 2.5)), float(np.percentile(b, 97.5))


def line(label, x):
    x = np.asarray(x, float)
    if not len(x):
        print(f'  {label:<34} n=0')
        return
    lo, hi = ci(x)
    h = len(x) // 2
    h1, h2 = (x[:h].mean() if h else float('nan')), (x[h:].mean() if len(x) - h else float('nan'))
    print(f'  {label:<34} n={len(x):3d} mean Rs{x.mean():+8.0f} CI[{lo:+7.0f},{hi:+7.0f}] win {np.mean(x > 0) * 100:3.0f}% '
          f'halves {h1:+7.0f}/{h2:+7.0f} worst {x.min():+7.0f} best {x.max():+7.0f}')


def get(recs, pair, key, field='net'):
    out = []
    for r in recs:
        p = (r.get('pairs') or {}).get(pair) or {}
        v = p.get(key)
        if v and v.get(field) is not None:
            out.append(v[field])
    return out


def main(logs):
    recs = load(logs)
    if not recs:
        print('no overnight_carry records yet')
        return
    wk = [r for r in recs if r['weekend']]
    print(f'{len(recs)} carried nights ({len(wk)} crossed a weekend or holiday), {recs[0]["entry_day"]} .. {recs[-1]["exit_day"]}')
    print(f'DTE at entry {min(r["dte_at_entry"] for r in recs)}..{max(r["dte_at_entry"] for r in recs)} days; '
          f'median straddle debit Rs{np.median([r["primary_pair"]["straddle_long"]["capital"] for r in recs if r.get("primary_pair")]):,.0f} per lot')
    prim = None
    for r in recs:
        if r.get('primary_pair'):
            prim = f'E{r["primary"]["entry"]}->X{r["primary"]["exit"]}'
            break
    if not prim:
        print('no record has the primary pair')
        return

    print(f'\nPRIMARY PAIR {prim}  (the only pair the rule judges)')
    for k in ('straddle_long', 'straddle_short', 'ce_long', 'pe_long'):
        line(k, get(recs, prim, k))
    print('  of which the weekend/holiday nights only:')
    for k in ('straddle_long', 'straddle_short'):
        line('  ' + k, [r['pairs'][prim][k]['net'] for r in wk if r.get('pairs', {}).get(prim, {}).get(k)])

    print('\nWHERE THE MONEY GOES (straddle, primary pair, mean per night)')
    for side in ('long', 'short'):
        g = get(recs, prim, f'straddle_{side}', 'gross')
        sc = get(recs, prim, f'straddle_{side}', 'spread_cost')
        cs = get(recs, prim, f'straddle_{side}', 'cost')
        mg = get(recs, prim, f'straddle_{side}', 'mid_gross')
        if g:
            print(f'  {side:<6} at MID Rs{np.mean(mg):+7.0f}  spread -Rs{np.mean(sc):6.0f}  costs -Rs{np.mean(cs):6.0f}  => net Rs{np.mean(g) - np.mean(cs):+7.0f}')

    print('\nWHAT MOVED (primary pair)')
    mv = [r['pairs'][prim]['spot_move_pct'] for r in recs if r.get('pairs', {}).get(prim) and r['pairs'][prim].get('spot_move_pct') is not None]
    vx = [r['pairs'][prim]['vix_change'] for r in recs if r.get('pairs', {}).get(prim) and r['pairs'][prim].get('vix_change') is not None]
    sl = [r['pairs'][prim]['straddle_long']['net'] for r in recs if r.get('pairs', {}).get(prim) and r['pairs'][prim].get('spot_move_pct') is not None
          and r['pairs'][prim].get('straddle_long')]
    if mv:
        print(f'  index move 15:20->09:20: mean {np.mean(mv):+.3f}%  mean |move| {np.mean(np.abs(mv)):.3f}%  largest {max(mv, key=abs):+.2f}%')
        deb = np.mean([r['pairs'][prim]['straddle_long']['capital'] for r in recs if r['pairs'][prim].get('straddle_long')])
        spot = np.mean([r['entry_samples'][0]['spot'] for r in recs])
        print(f'  the straddle costs ~{deb / (30 * spot) * 100:.2f}% of spot; a long straddle needs a move bigger than that, '
              f'both ways, to cover its decay and spread')
    if vx:
        print(f'  VIX change: mean {np.mean(vx):+.2f} points')
        if len(vx) == len(sl) and len(vx) > 5:
            print(f'  correlation of straddle P&L with the |index move|: {np.corrcoef(np.abs(mv), sl)[0, 1]:+.2f};  with the VIX change: {np.corrcoef(vx, sl)[0, 1]:+.2f}')

    print('\nEXIT TIMES (EXPLORATORY: do not pick the best one and call it a finding)')
    labels = sorted({k.split('->X')[1] for r in recs for k in r['pairs'] if k.startswith('E' + prim[1:6])})
    for lab in labels:
        key = f'{prim.split("->")[0]}->X{lab}'
        line(f'straddle long  exit {lab}', get(recs, key, 'straddle_long'))
    for lab in labels:
        key = f'{prim.split("->")[0]}->X{lab}'
        line(f'straddle short exit {lab}', get(recs, key, 'straddle_short'))

    print('\nEXECUTION (what a live order would have met)')
    for k in ('entry_buy_CE_1lot', 'entry_buy_CE_4lot', 'exit_sell_CE_1lot', 'exit_sell_CE_4lot'):
        v = [r['exec'][k] for r in recs if r.get('exec', {}).get(k) and r['exec'][k].get('slip_vs_best_pct') is not None]
        if v:
            print(f'  {k:<20} filled {np.mean([i["filled"] / i["requested"] for i in v]) * 100:3.0f}% of the quantity within 5 levels, '
                  f'mean slippage vs best {np.mean([i["slip_vs_best_pct"] for i in v]):+.2f}%')
    tops = [r['exec']['entry_buy_CE_meta']['top_size'] for r in recs if r.get('exec', {}).get('entry_buy_CE_meta')]
    if tops:
        print(f'  top-of-book size on the ATM call at entry: median {np.median(tops):.0f} units (one lot = 30)')

    n_ok, nw = len(recs) >= 60, len(wk) >= 10
    print('\nVERDICT RULE:', 'EVALUABLE' if (n_ok and nw) else f'DESCRIPTIVE ONLY ({len(recs)}/60 nights, {len(wk)}/10 weekend nights)')
    if n_ok and nw:
        for k in ('straddle_long', 'straddle_short', 'ce_long', 'pe_long'):
            x = np.array(get(recs, prim, k))
            lo, _ = ci(x)
            h = len(x) // 2
            print(f'  {k:<16} CI lower {lo:+.0f}, halves {x[:h].mean():+.0f}/{x[h:].mean():+.0f} -> '
                  f'{"worth a live test" if lo > 0 and x[:h].mean() > 0 and x[h:].mean() > 0 else "not shown"}')


if __name__ == '__main__':
    main(sys.argv[1])
