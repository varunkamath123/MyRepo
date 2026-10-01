# -*- coding: utf-8 -*-
"""Read the phantom SHORT-premium book and compare it with the champion.

    python fno_t_bot/research/pprem_report.py

The headline question is not "did the phantom make money" -- on a small sample
that is luck. It is: did the index stay inside the move the premium charged
for? That ratio is the VRP finding stated as something falsifiable, and it is
what decides whether we are on the wrong side.

Treat every number here as provisional until the sample clears the same bar as
anything else in this project: significance, BOTH chronological halves, and
survival after the single best session is removed.
"""
import glob, json, os, statistics as st, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

D = getattr(config, 'LOG_DIRECTORY', 'logs')
if not os.path.isabs(D):
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), D)
    D = here if os.path.isdir(here) else D

REAL_FROM = '2026-08-18'


def load(pat, key='pnl_net'):
    out = []
    for f in sorted(glob.glob(os.path.join(D, pat))):
        if '.bak' in f:
            continue
        for ln in open(f, encoding='utf-8', errors='ignore'):
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if d.get(key) is None:
                continue
            out.append(d)
    return out


flies = [d for d in load('phantom_premium_*.jsonl') if d.get('date', '') >= REAL_FROM]

print('=' * 84)
print('PHANTOM SHORT-PREMIUM BOOK  (short iron fly, direction-neutral, real quotes)')
print('=' * 84)
if not flies:
    print('\n  No sessions recorded yet. The book starts on the next trading day;')
    print('  PHANTOM_PREM_TIME is', getattr(config, 'PHANTOM_PREM_TIME', '10:00'),
          'and it needs all four legs quotable.')
    sys.exit(0)

flies.sort(key=lambda d: (d['date'], d['instrument']))

print(f"\n  {'date':12}{'inst':10}{'moved':>8}{'breakeven':>11}{'inside':>8}"
      f"{'pnl':>10}  exit")
for d in flies:
    mv, be = abs(d['index_move_pct']), d['breakeven_pct']
    print(f"  {d['date']:12}{d['instrument']:10}{mv:>7.2f}%{be:>10.2f}%"
          f"{('YES' if mv < be else 'no'):>8}{d['pnl_net']:>+10,.0f}  {d['exit_reason']}")

n = len(flies)
net = sum(d['pnl_net'] for d in flies)
wins = sum(1 for d in flies if d['pnl_net'] > 0)
inside = sum(1 for d in flies if abs(d['index_move_pct']) < d['breakeven_pct'])

print('\n' + '-' * 84)
print(f"  THE TEST: index stayed inside the premium's implied move on "
      f"{inside}/{n} sessions ({inside/n*100:.0f}%)")
print(f"            median move {st.median([abs(d['index_move_pct']) for d in flies]):.3f}%"
      f"   vs median breakeven {st.median([d['breakeven_pct'] for d in flies]):.3f}%")
print(f"  BOOK:     n={n}  {wins}W/{n-wins}L  net Rs{net:+,.0f}  "
      f"mean Rs{net/n:+,.0f}  median Rs{st.median([d['pnl_net'] for d in flies]):+,.0f}")

if n >= 4:
    h = n // 2
    a = sum(d['pnl_net'] for d in flies[:h]); b = sum(d['pnl_net'] for d in flies[h:])
    best = max(d['pnl_net'] for d in flies)
    print(f"  ROBUST:   halves Rs{a:+,.0f} / Rs{b:+,.0f}"
          f"   without best session Rs{net-best:+,.0f}")
    try:
        from scipy import stats as sps
        t_, pv = sps.ttest_1samp([d['pnl_net'] for d in flies], 0)
        print(f"            mean != 0 ?  t={t_:+.2f}  p={pv:.4f}"
              f"   {'(not significant)' if pv >= 0.05 else ''}")
    except Exception:
        pass

by = {}
for d in flies:
    by.setdefault(d['instrument'], []).append(d['pnl_net'])
print('\n  by instrument: ' + '   '.join(
    f"{k} n={len(v)} Rs{sum(v):+,.0f}" for k, v in sorted(by.items())))

# ── the comparison that matters: same sessions, opposite side ──────────────
champ = [d for d in load('FnO_T_Bot_*_trades_*.jsonl')
         if 'challenger' not in str(d) and d.get('entry_time', '')[:10] >= REAL_FROM]
days = {d['date'] for d in flies}
same = [d for d in champ if d.get('entry_time', '')[:10] in days]
if same:
    cn = sum(d['pnl_net'] for d in same)
    cw = sum(1 for d in same if d['pnl_net'] > 0)
    print('\n' + '-' * 84)
    print(f"  SAME SESSIONS, OPPOSITE SIDE")
    print(f"    champion (LONG premium) : n={len(same):3}  {cw}W/{len(same)-cw}L  "
          f"Rs{cn:+,.0f}")
    print(f"    phantom  (SHORT premium): n={n:3}  {wins}W/{n-wins}L  Rs{net:+,.0f}")
    print(f"    difference: Rs{net-cn:+,.0f} in favour of "
          f"{'SHORT' if net > cn else 'LONG'} premium over {len(days)} sessions")
print('=' * 84)
