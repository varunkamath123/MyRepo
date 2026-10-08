# -*- coding: utf-8 -*-
"""Does MiroFish's pre-market lean predict the session's direction?

    python fno_t_bot/research/mirofish_eval.py

WHY THIS IS WORTH MEASURING. Twelve price-derived methods have failed to call
direction in this project -- MACD, OI drift, level breaks, volume confirmation,
constituent breadth, repeated rejection, phantom-futures sign, and the rest.
Every one of them is a function of price, which is also the reason they are
cheap to test and the reason they have no edge left in them. MiroFish is the
only signal here that is NOT a function of price: it reads RBI policy, FII/DII
flows, global cues and the scheduled-event calendar, then states a lean.

WHY IT HAD NO TRACK RECORD. mirofish_scores.json is overwritten on every run,
so until Oct 8 2026 the swarm had produced hundreds of calls and retained
none. Nothing could be asked of it. The history file this reads is append-only
from that date.

WHY THE ARCHIVE IS CLEAN. The pre-market call is generated at ~08:45, before
the open, so no hindsight can leak into it. The 14:35 run is a second, later
call and is reported separately -- it is NOT comparable, since by then most of
the session has happened.

THE BAR. Direction here is a coin flip: the unconditional base rate of a down
session is 47-53% depending on instrument. A lean is only interesting if it
beats its own base rate by a clear margin, in BOTH chronological halves, on a
sample that is not three days long. At one call per instrument per day this
accumulates slowly -- 30 sessions is 60 observations, which is thin. Do not
act on the first encouraging number.
"""
import glob, json, os, sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

HIST_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), 'logs', 'mirofish_history.jsonl'),
    '/opt/trading_bot/live_bot/logs/mirofish_history.jsonl',
]
DATA = os.environ.get('FNO_DATA', '/opt/trading_bot/data')
SUB = {'NIFTY': 'nifty_5min', 'BANKNIFTY': 'banknifty_5min'}

hist = next((p for p in HIST_CANDIDATES if os.path.exists(p)), None)
print('=' * 86)
print('MIROFISH pre-market lean  vs  the session that followed')
print('=' * 86)
if not hist:
    print('\n  No history file yet. The swarm appends to logs/mirofish_history.jsonl')
    print('  from Oct 8 2026; before that every call was overwritten and lost.')
    sys.exit(0)

try:
    import pandas as pd
except Exception:
    sys.exit('needs pandas')

rows = []
for ln in open(hist, encoding='utf-8', errors='ignore'):
    ln = ln.strip()
    if not ln:
        continue
    try:
        rows.append(json.loads(ln))
    except Exception:
        pass

# keep the EARLIEST call per date (the pre-market one); report later runs apart
by_date = {}
late = 0
for r in sorted(rows, key=lambda z: z.get('generated_at', '')):
    d = r.get('date')
    if not d:
        continue
    t = (r.get('generated_at') or '')[11:16]
    if d not in by_date:
        by_date[d] = r
    elif t >= '12:00':
        late += 1

print(f'\n  {len(rows)} calls archived over {len(by_date)} sessions'
      f'   ({late} later-in-day reruns held out)')

res = defaultdict(list)
for d, r in sorted(by_date.items()):
    for inst in SUB:
        blk = r.get(inst)
        if not isinstance(blk, dict) or not blk.get('lean'):
            continue
        p = os.path.join(DATA, SUB[inst], f"{SUB[inst]}_{d.replace('-','')}.csv")
        if not os.path.exists(p):
            continue
        try:
            b = pd.read_csv(p)
            b['ts'] = pd.to_datetime(b['ts'], errors='coerce')
            b = b.dropna(subset=['ts']).sort_values('ts')
        except Exception:
            continue
        if len(b) < 20:
            continue
        o, c = float(b['Open'].iloc[0]), float(b['Close'].iloc[-1])
        move = (c - o) / o * 100
        res[inst].append(dict(date=d, lean=blk['lean'], score=blk.get('score'),
                              move=move))

if not any(res.values()):
    print('\n  Calls archived but no matching index bars yet.')
    sys.exit(0)

for inst, v in sorted(res.items()):
    R = pd.DataFrame(v).sort_values('date')
    base_dn = (R['move'] < 0).mean() * 100
    print(f'\n{inst}: n={len(R)}   base rate P(down session) = {base_dn:.1f}%')
    print(f"  {'lean':10}{'n':>5}{'mean move':>12}{'% correct':>12}")
    for lean in ('bearish', 'neutral', 'bullish'):
        g = R[R['lean'] == lean]
        if not len(g):
            continue
        if lean == 'neutral':
            ok = '      --   '
        else:
            want = -1 if lean == 'bearish' else 1
            ok = f"{( (g['move'] * want > 0).mean() * 100):>11.1f}%"
        print(f"  {lean:10}{len(g):>5}{g['move'].mean():>+11.2f}%{ok}")
    directional = R[R['lean'] != 'neutral']
    if len(directional) >= 4:
        want = directional['lean'].map({'bearish': -1, 'bullish': 1})
        hit = (directional['move'] * want > 0).mean() * 100
        h = len(directional) // 2
        h1 = (directional['move'].iloc[:h] * want.iloc[:h] > 0).mean() * 100
        h2 = (directional['move'].iloc[h:] * want.iloc[h:] > 0).mean() * 100
        print(f"  DIRECTIONAL calls only: n={len(directional)}  hit {hit:.1f}%"
              f"   halves {h1:.0f}% / {h2:.0f}%")
    if len(R) < 20:
        print('  (far too few sessions to read anything into this)')
print('\n' + '=' * 86)
print('  A lean earns attention by beating its own base rate in BOTH halves,')
print('  on more than a handful of sessions. One good call is one good call.')
