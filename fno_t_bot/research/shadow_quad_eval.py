# -*- coding: utf-8 -*-
"""Evaluate the ADX/VIX/OI/PCR shadow engine against its pre-registration.

    python fno_t_bot/research/shadow_quad_eval.py

PRE-REGISTERED, from REBUILD_PLAN.md and config.py, before any data existed:

  Hypothesis   at FIRE_THRESHOLD = 3, the signalled direction beats that
               instrument's own base rate in BOTH chronological halves.
  Kill         after 30 sessions, if the hit rate sits inside base rate +/- 3pp
               or flips sign between halves, the engine is dead and that gets
               written into REBUILD_PLAN.md. A threshold sweep afterwards does
               NOT rescue it -- the sweep is the failure mode, not the remedy.

This script prints the pre-registered test FIRST and alone. The offline
re-cuts (other thresholds, component subsets) are printed afterwards, behind
an explicit multiple-testing count, because that is what they are: additional
searching, each one of which lowers what any single result is worth. They are
there to inform the next hypothesis, not to be mined for this one.
"""
import glob, json, os, sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

D = getattr(config, 'LOG_DIRECTORY', 'logs')
if not os.path.isabs(D):
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), D)
    D = here if os.path.isdir(here) else D
DATA = os.environ.get('FNO_DATA', '/opt/trading_bot/data')
SUB = {'NIFTY': 'nifty_5min', 'BANKNIFTY': 'banknifty_5min', 'SENSEX': 'sensex_5min'}
HORIZON = '15:00'          # the live force-close
MIN_SESSIONS = 30

try:
    import pandas as pd, numpy as np
except Exception:
    sys.exit('needs pandas/numpy')

recs = defaultdict(list)
for f in sorted(glob.glob(os.path.join(D, 'shadow_quad_*.jsonl'))):
    for ln in open(f, encoding='utf-8', errors='ignore'):
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if d.get('instrument') and d.get('ts'):
            recs[d['instrument']].append(d)

print('=' * 90)
print('SHADOW ADX/VIX/OI/PCR  —  pre-registered test')
print('=' * 90)
if not recs:
    print('\n  Nothing logged yet. The engine writes every'
          f' {getattr(config, "SHADOW_QUAD_LOG_EVERY_MIN", 5)} minutes while the market is open.')
    sys.exit(0)

bars_cache = {}


def fwd_move(inst, ts):
    """Index move from ts to the force-close, in % — the outcome the signal
    is claiming to predict."""
    day = ts[:10]
    key = (inst, day)
    if key not in bars_cache:
        p = os.path.join(DATA, SUB.get(inst, ''), f"{SUB.get(inst,'')}_{day.replace('-','')}.csv")
        b = None
        if os.path.exists(p):
            try:
                b = pd.read_csv(p)
                b['ts'] = pd.to_datetime(b['ts'], errors='coerce')
                b = b.dropna(subset=['ts']).sort_values('ts')
                if b['ts'].dt.tz is not None:
                    b['ts'] = b['ts'].dt.tz_localize(None)
            except Exception:
                b = None
        bars_cache[key] = b
    b = bars_cache[key]
    if b is None or not len(b):
        return None
    t = pd.to_datetime(ts).tz_localize(None) if pd.to_datetime(ts).tzinfo else pd.to_datetime(ts)
    at = b[b['ts'] <= t]
    fw = b[(b['ts'] > t) & (b['ts'].dt.strftime('%H:%M') <= HORIZON)]
    if not len(at) or len(fw) < 2:
        return None
    s = float(at['Close'].iloc[-1])
    return (float(fw['Close'].iloc[-1]) - s) / s * 100.0


def build(inst, rows):
    out = []
    for d in rows:
        m = fwd_move(inst, d['ts'])
        if m is None:
            continue
        out.append(dict(ts=d['ts'], day=d['ts'][:10], score=d.get('score'),
                        fired=d.get('fired'), direction=d.get('direction'),
                        vix_ok=d.get('vix_ok'), move=m,
                        **{f'v_{k}': v for k, v in (d.get('votes') or {}).items()}))
    return pd.DataFrame(out).sort_values('ts') if out else pd.DataFrame()


def report(lbl, g, base, n_sessions):
    if len(g) < 10:
        print(f'    {lbl:34} n={len(g):>4}  (too few)')
        return
    want = g['direction'].map({'CALL': 1, 'PUT': -1})
    hit = (g['move'] * want > 0).mean() * 100
    h = len(g) // 2
    h1 = (g['move'].iloc[:h] * want.iloc[:h] > 0).mean() * 100
    h2 = (g['move'].iloc[h:] * want.iloc[h:] > 0).mean() * 100
    edge = hit - base
    verdict = ''
    if n_sessions >= MIN_SESSIONS:
        if abs(edge) <= 3 or (h1 - base) * (h2 - base) <= 0:
            verdict = '  <- KILL criterion met'
        elif edge > 3:
            verdict = '  <- beats base in both halves'
    print(f'    {lbl:34} n={len(g):>4}  hit {hit:5.1f}%  (base {base:5.1f}%, '
          f'edge {edge:+5.1f}pp)  halves {h1:4.1f}/{h2:4.1f}%{verdict}')


summary = {}
for inst, rows in sorted(recs.items()):
    R = build(inst, rows)
    if not len(R):
        print(f'\n{inst}: {len(rows)} records logged, no matching index bars yet')
        continue
    ns = R['day'].nunique()
    base = (R['move'] > 0).mean() * 100      # base rate of an UP move to 15:00
    summary[inst] = (ns, len(R))
    print(f'\n{inst}: {len(R)} evaluations over {ns} sessions'
          f'   base rate P(up to {HORIZON}) = {base:.1f}%')
    fired = R[R['fired'] == True]
    report(f'FIRED at threshold '
           f'{getattr(config, "SHADOW_QUAD_FIRE_THRESHOLD", 3)}', fired, base, ns)
    if ns < MIN_SESSIONS:
        print(f'    (needs {MIN_SESSIONS} sessions before the kill criterion applies;'
              f' {MIN_SESSIONS - ns} to go)')

print('\n' + '=' * 90)
print('OFFLINE RE-CUTS — additional searching. Each one costs credibility.')
print('=' * 90)
tried = 0
for inst, rows in sorted(recs.items()):
    R = build(inst, rows)
    if not len(R):
        continue
    ns = R['day'].nunique()
    base = (R['move'] > 0).mean() * 100
    print(f'\n{inst}:')
    for thr in (2, 3, 4):
        g = R[(R['score'].abs() >= thr) & (R['vix_ok'] == True)].copy()
        g['direction'] = np.where(g['score'] > 0, 'CALL', 'PUT')
        tried += 1
        report(f'threshold {thr} of 4', g, base, ns)
    for comp in ('v_adx', 'v_pcr', 'v_maxpain', 'v_wall'):
        if comp not in R.columns:
            continue
        g = R[(R[comp] != 0) & (R['vix_ok'] == True)].copy()
        g['direction'] = np.where(g[comp] > 0, 'CALL', 'PUT')
        tried += 1
        report(f'{comp[2:]} alone', g, base, ns)
    g = R[R['vix_ok'] == False].copy()
    if len(g) >= 10:
        g2 = g[g['score'].abs() >= 3].copy()
        if len(g2) >= 10:
            g2['direction'] = np.where(g2['score'] > 0, 'CALL', 'PUT')
            tried += 1
            report('score>=3 but VIX outside band', g2, base, ns)

print(f'\n  {tried} re-cuts printed above. With that much searching, expect'
      f' roughly {tried * 0.05:.1f} of them')
print('  to clear a 5% threshold by chance alone. The pre-registered test is the')
print('  only one that counts for THIS engine; the rest inform the next hypothesis.')
if summary:
    worst = min(v[0] for v in summary.values())
    if worst < MIN_SESSIONS:
        print(f'\n  VERDICT: not yet due. {worst}/{MIN_SESSIONS} sessions on the'
              ' thinnest instrument.')
