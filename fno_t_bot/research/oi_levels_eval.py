# -*- coding: utf-8 -*-
"""Do the dynamic OI/PCR levels beat the 51% baseline?

    python fno_t_bot/research/oi_levels_eval.py

THE BAR. On Oct 2 2026, eight static level definitions were measured across
17,300 touches on 1,227 sessions -- OR30/OR60 high and low, prior-day high /
low / close, round numbers, static OI walls. Every one rejected price between
47.9% and 54.7%, and repeated rejection added nothing (touch #1 52.1%, #2
51.5%, #3 51.3%, #4+ 52.6%). So ~51% is chance here, and a level definition
only earns a trigger by clearly beating it -- on a sample that includes a
chronological holdout, not on the first 30 rows that look encouraging.

This reads the levels the live bot logged and asks the same question of them.
It reports the holdout split by default BECAUSE the temptation is to stop at
the headline number.
"""
import glob, json, os, sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

BASELINE = 51.0
TOL_ATR = 0.15      # "touch" = within this many ATR of the level
THR_ATR = 0.25      # reject/break must clear this
FWD_BARS = 6        # 30 minutes to classify the reaction

D = getattr(config, 'LOG_DIRECTORY', 'logs')
if not os.path.isabs(D):
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), D)
    D = here if os.path.isdir(here) else D

try:
    import pandas as pd, numpy as np
except Exception:
    sys.exit('needs pandas/numpy')

SUB = {'NIFTY': 'nifty_5min', 'BANKNIFTY': 'banknifty_5min', 'SENSEX': 'sensex_5min'}
DATA = os.environ.get('FNO_DATA', os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data'))

rows = defaultdict(list)
for f in sorted(glob.glob(os.path.join(D, 'oi_levels_*.jsonl'))):
    for ln in open(f, encoding='utf-8', errors='ignore'):
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if d.get('ts') and d.get('instrument'):
            rows[d['instrument']].append(d)

print('=' * 90)
print('DYNAMIC OI/PCR LEVELS  vs  the ~51% static baseline')
print('=' * 90)
if not rows:
    print('\n  No levels logged yet. The bot writes one snapshot every'
          f' {getattr(config, "OI_LEVELS_LOG_EVERY_MIN", 5)} minutes while the')
    print('  market is open; come back after a session or two.')
    sys.exit(0)

for inst, recs in sorted(rows.items()):
    recs.sort(key=lambda z: z['ts'])
    bars = {}
    for p in glob.glob(os.path.join(DATA, SUB.get(inst, ''), '*.csv')):
        try:
            b = pd.read_csv(p)
            b['ts'] = pd.to_datetime(b['ts'], errors='coerce')
            b = b.dropna(subset=['ts']).sort_values('ts').set_index('ts')
            b = b[~b.index.duplicated(keep='first')]
            if len(b):
                bars[b.index[0].date()] = b
        except Exception:
            pass
    out = []
    for r in recs:
        try:
            ts = pd.to_datetime(r['ts']).tz_localize(None)
        except Exception:
            continue
        df = bars.get(ts.date())
        if df is None:
            continue
        idx = df.index.tz_localize(None) if df.index.tz is not None else df.index
        atr = (df['High'] - df['Low']).rolling(14).mean()
        a = atr[idx <= ts].dropna()
        if not len(a) or a.iloc[-1] <= 0:
            continue
        a = float(a.iloc[-1])
        fw = df[(idx > ts)].head(FWD_BARS)
        if len(fw) < 3:
            continue
        for side, key in (('R', 'nearest_res'), ('S', 'nearest_sup')):
            lvl = r.get(key)
            if not lvl:
                continue
            spot = float(r['spot'])
            if abs(spot - lvl) > TOL_ATR * a:
                continue                      # not a touch
            above = spot >= lvl
            thr = THR_ATR * a
            brk = (fw['Close'].min() < lvl - thr) if above else (fw['Close'].max() > lvl + thr)
            rej = (fw['Close'].max() > lvl + thr) if above else (fw['Close'].min() < lvl - thr)
            out.append(dict(ts=ts, side=side, rejected=int(rej and not brk),
                            broke=int(brk and not rej),
                            pcr_bias=r.get('pcr_bias'), spot=spot))
    if not out:
        print(f'\n{inst}: {len(recs)} snapshots logged, 0 were AT a level yet')
        continue
    O = pd.DataFrame(out).sort_values('ts')
    h = len(O) // 2
    pr = O['rejected'].mean() * 100
    print(f'\n{inst}: {len(recs)} snapshots, {len(O)} touches')
    print(f"  P(reject) {pr:.1f}%   vs baseline {BASELINE:.0f}%   "
          f"{'BEATS IT' if pr > BASELINE + 3 else 'no better than chance'}")
    print(f"  P(break)  {O['broke'].mean()*100:.1f}%")
    if h >= 20:
        print(f"  holdout: first half {O['rejected'].iloc[:h].mean()*100:.1f}%   "
              f"second half {O['rejected'].iloc[h:].mean()*100:.1f}%")
    else:
        print('  holdout: not enough touches yet to split')
    for b_ in ('bullish', 'bearish', 'neutral'):
        g = O[O['pcr_bias'] == b_]
        if len(g) >= 20:
            print(f"    pcr_bias={b_:8} n={len(g):>4}  P(reject) {g['rejected'].mean()*100:.1f}%")
print('\n' + '=' * 90)
print('  A level definition earns a trigger by beating 51% in BOTH halves.')
print('  Anything else is the same coin flip the other eight definitions were.')
