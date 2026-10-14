# -*- coding: utf-8 -*-
"""Evaluate lot-sizing rules on SHADOW data, out of sample.

    python fno_t_bot/research/sizing_eval.py

WHY THIS EXISTS. Quantum matters more than direction in this book. Rescaling
the 41 booked trades on Oct 14 2026 -- identical entries, identical exits, only
the lot count changed -- spanned Rs58,000:

    size UP on unified_score      -Rs51,253
    size UP on move_cover         -Rs49,638
    size UP on rv_iv              -Rs39,518
    capital-scaled 4x             -Rs38,655
    1 lot flat (actual)            -Rs9,664
    equal-risk                    -Rs11,885
    size DOWN on unified_score     -Rs4,111
    size DOWN on exp_move_pct      +Rs6,656

That is a larger effect than anything twelve direction methods produced. But
every one of those numbers came from the SAME 41 trades the rules were mined
on, so none of it is evidence. "Size down on exp_move_pct" in particular is
just a restatement of the rho = -0.364 measured on those trades, selected as
one of ten variants -- circular by construction.

This script runs the same rules against the counterfactual book instead, which
is out of sample relative to those 41 trades, and prints the chronological
split for each.

PRE-REGISTERED (config.py, written before the shadow data existed):
  Test     >= 30 shadow observations per cohort
  Earns it a rule beats 1-lot flat in BOTH chronological halves
  Fails    anything that only works pooled, or needs its threshold moved

KNOWN GAP, stated rather than hidden: unified_score, exp_move_pct and
move_cover are computed inside enter_trade, downstream of the signal, so they
do not exist on a shadow record. The one rule that looked positive therefore
cannot be tested here at all. What IS available: entry_adx, chase_pos, rv_iv
and the path score fields.
"""
import glob, json, os, statistics as st, sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

D = getattr(config, 'LOG_DIRECTORY', 'logs')
if not os.path.isabs(D):
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), D)
    D = here if os.path.isdir(here) else D
MIN_N = 30
LOT = {'NIFTY': 65, 'BANKNIFTY': 30, 'SENSEX': 20}

rows = []
for f in sorted(glob.glob(os.path.join(D, 'counterfactual_*.jsonl'))):
    for ln in open(f, encoding='utf-8', errors='ignore'):
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if d.get('pnl_net') is None or d.get('px_src') != 'LTP':
            continue
        day = d.get('date', '')
        if '2026-09-24' <= day <= '2026-09-28' and d.get('instrument') in ('NIFTY', 'SENSEX'):
            continue
        rows.append(d)
rows.sort(key=lambda d: (d['date'], d.get('entry_time', '')))

print('=' * 92)
print('LOT SIZING on shadow data  —  out of sample vs the 41 booked trades')
print('=' * 92)
if not rows:
    print('\n  No real-priced counterfactual records yet.')
    sys.exit(0)

ns = len({d['date'] for d in rows})
print(f'\n  {len(rows)} shadow observations over {ns} sessions'
      f'   (all tracked at 1 lot)')
flat = sum(d['pnl_net'] for d in rows)
print(f'  1-lot flat baseline: Rs{flat:+,.0f}')
if len(rows) < MIN_N:
    print(f'  NOT YET DUE: {len(rows)}/{MIN_N} observations.'
          f' Nothing below should be acted on.')

avail = defaultdict(int)
for d in rows:
    for k in ('entry_adx', 'chase_pos', 'rv_iv', 'rev_score', 'trend_leg_atr'):
        if d.get(k) is not None:
            avail[k] += 1
print('\n  field coverage on shadow records: ' +
      '  '.join(f'{k} {v}/{len(rows)}' for k, v in sorted(avail.items())) or '  (none)')
print('  missing by construction (computed inside enter_trade): '
      'unified_score, exp_move_pct, move_cover')


def quint(vals, v):
    s = sorted(x for x in vals if x is not None)
    if not s or v is None:
        return 2
    import bisect
    return min(4, bisect.bisect_left(s, v) * 5 // max(len(s), 1))


def report(name, fn, tried):
    h = len(rows) // 2
    tot = a = b = 0.0
    lots = []
    for i, d in enumerate(rows):
        m = max(1, int(fn(d)))
        lots.append(m)
        v = d['pnl_net'] * m
        tot += v
        if i < h:
            a += v
        else:
            b += v
    ha = sum(d['pnl_net'] for d in rows[:h])
    hb = sum(d['pnl_net'] for d in rows[h:])
    beats_both = (a > ha) and (b > hb)
    verdict = ''
    if len(rows) >= MIN_N:
        verdict = '  <- beats flat in BOTH halves' if beats_both else ''
    print(f'  {name:34} Rs{tot:>+10,.0f}   avg {st.mean(lots):.1f} lots'
          f'   halves Rs{a:+,.0f} / Rs{b:+,.0f}{verdict}')
    return tried + 1


adx = [d.get('entry_adx') for d in rows]
rv = [d.get('rv_iv') for d in rows]
ch = [d.get('chase_pos') for d in rows]
h = len(rows) // 2
print(f'\n  {"rule":34} {"total":>13}   split of the flat baseline: '
      f'Rs{sum(d["pnl_net"] for d in rows[:h]):+,.0f} / '
      f'Rs{sum(d["pnl_net"] for d in rows[h:]):+,.0f}')
t = 0
t = report('1 lot flat', lambda d: 1, t)
t = report('equal-risk (target Rs2,500)',
           lambda d: max(1, min(10, round(2500 / max(
               d['entry_price'] * LOT.get(d['instrument'], 50) * 0.25, 1)))), t)
t = report('size UP on rv_iv', lambda d: 1 + quint(rv, d.get('rv_iv')), t)
t = report('size DOWN on rv_iv', lambda d: 4 - quint(rv, d.get('rv_iv')), t)
t = report('size UP on entry_adx', lambda d: 1 + quint(adx, d.get('entry_adx')), t)
t = report('size DOWN on entry_adx', lambda d: 4 - quint(adx, d.get('entry_adx')), t)
t = report('size DOWN on chase_pos (good entry)',
           lambda d: 4 - quint(ch, d.get('chase_pos')), t)
t = report('size UP on chase_pos', lambda d: 1 + quint(ch, d.get('chase_pos')), t)

print(f'\n  {t} rules tried. Expect roughly {t * 0.05:.1f} to clear a 5% bar by chance.')
print('  A rule earns LOT_SIZING_MODE only by beating flat in BOTH halves on')
print(f'  >= {MIN_N} observations. Pooled-only wins are the failure mode, not the result.')
