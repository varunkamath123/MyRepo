# -*- coding: utf-8 -*-
"""Unit-test the dynamic OI/PCR level calculator.

These levels have to clear a 51% bar before they can ever trigger a trade, so
the arithmetic behind them has to be right first. The tests target the three
things that make this definition different from the eight already falsified:
proximity weighting, OI CHANGE (building vs unwinding), and the PCR discount.
"""
import glob, io, json, logging, os, sys, tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
import oi_levels as OL

P = F = 0


def ck(name, cond, detail=''):
    global P, F
    if cond:
        P += 1
        print(f'  PASS  {name}')
    else:
        F += 1
        print(f'  FAIL  {name}  {detail}')


TMP = os.path.join(tempfile.gettempdir(), 'oilv_test')
os.makedirs(TMP, exist_ok=True)
for f in glob.glob(os.path.join(TMP, '*.jsonl')):
    os.remove(f)
config.LOG_DIRECTORY = TMP
config.OI_LEVELS_ENABLED = True
config.OI_LEVELS_LOG_EVERY_MIN = 5
config.OI_LEVEL_PCR_BULL = 1.15
config.OI_LEVEL_PCR_BEAR = 0.85
config.OI_LEVEL_PCR_ADJ = 0.25


class FakeBot:
    def __init__(self, inst='NIFTY'):
        self.instrument = inst
        self.logger = logging.getLogger('oilv')
        self.logger.addHandler(logging.NullHandler())
        self._oi_lv_prev = None
        self._oi_lv_logged = None


def chain(pcr=1.0, **oi):
    """oi kwargs like c22600=100000, p22400=90000."""
    st = {}
    for k, v in oi.items():
        side, strike = k[0], float(k[1:])
        d = st.setdefault(strike, {'call_oi': 0, 'put_oi': 0})
        d['call_oi' if side == 'c' else 'put_oi'] = v
    return {'pcr': pcr, 'max_pain': 22500.0, 'strikes': st}


SPOT = 22500.0

print('\n--- structure ---')
oc = chain(c22600=100000, c22700=50000, p22400=90000, p22300=40000)
lv = OL.compute(oc, SPOT, 'NIFTY')
ck('returns levels', lv is not None)
ck('resistance strikes are all ABOVE spot',
   all(r['strike'] > SPOT for r in lv['resistance']), lv['resistance'])
ck('support strikes are all BELOW spot',
   all(s['strike'] < SPOT for s in lv['support']), lv['support'])
ck('nearest_res is the top-scored resistance', lv['nearest_res'] == 22600, lv['nearest_res'])
ck('nearest_sup is the top-scored support', lv['nearest_sup'] == 22400, lv['nearest_sup'])

print('\n--- bigger OI outranks smaller, nearer outranks further ---')
oc = chain(c22600=50000, c22700=50000)
lv = OL.compute(oc, SPOT, 'NIFTY')
ck('equal OI -> nearer strike wins on proximity',
   lv['nearest_res'] == 22600, lv['resistance'])
oc = chain(c22600=10000, c22700=400000)
lv = OL.compute(oc, SPOT, 'NIFTY')
ck('a far wall 40x larger still outranks a tiny near one',
   lv['nearest_res'] == 22700, lv['resistance'])

print('\n--- the dynamic part: building vs unwinding ---')
prev = OL.compute(chain(c22600=100000, c22700=100000), SPOT, 'NIFTY')
# same sizes now, but 22700 is building and 22600 is unwinding
now = OL.compute(chain(c22600=100000, c22700=100000), SPOT, 'NIFTY', prev)
base = {r['strike']: r['score'] for r in now['resistance']}
prev2 = OL.compute(chain(c22600=140000, c22700=60000), SPOT, 'NIFTY')
now2 = OL.compute(chain(c22600=100000, c22700=100000), SPOT, 'NIFTY', prev2)
sc2 = {r['strike']: r['score'] for r in now2['resistance']}
ck('unwinding strike scores LOWER than when flat', sc2[22600] < base[22600],
   f"{sc2[22600]} vs {base[22600]}")
ck('building strike scores HIGHER than when flat', sc2[22700] > base[22700],
   f"{sc2[22700]} vs {base[22700]}")
ck('d_oi is reported', any(r.get('d_oi') for r in now2['resistance']))

print('\n--- PCR discount ---')
oc_n = chain(pcr=1.00, c22600=100000, p22400=100000)
oc_b = chain(pcr=1.40, c22600=100000, p22400=100000)   # put-heavy -> bullish
oc_r = chain(pcr=0.60, c22600=100000, p22400=100000)   # call-heavy -> bearish
n, b, r = (OL.compute(x, SPOT, 'NIFTY') for x in (oc_n, oc_b, oc_r))
ck('neutral PCR -> no bias', n['pcr_bias'] == 'neutral', n['pcr_bias'])
ck('high PCR flagged bullish', b['pcr_bias'] == 'bullish')
ck('low PCR flagged bearish', r['pcr_bias'] == 'bearish')
ck('bullish PCR strengthens SUPPORT',
   b['support'][0]['score'] > n['support'][0]['score'])
ck('bullish PCR weakens RESISTANCE',
   b['resistance'][0]['score'] < n['resistance'][0]['score'])
ck('bearish PCR strengthens RESISTANCE',
   r['resistance'][0]['score'] > n['resistance'][0]['score'])
ck('PCR never invents a level -- same strikes either way',
   [x['strike'] for x in b['resistance']] == [x['strike'] for x in n['resistance']])

print('\n--- refuses rather than guesses ---')
ck('empty chain -> None', OL.compute({'strikes': {}}, SPOT, 'NIFTY') is None)
ck('no chain key -> None', OL.compute({}, SPOT, 'NIFTY') is None)
ck('zero spot -> None', OL.compute(chain(c22600=1000), 0, 'NIFTY') is None)
ck('all-zero OI -> None', OL.compute(chain(c22600=0, p22400=0), SPOT, 'NIFTY') is None)
ck('garbage input raises nothing', OL.compute({'strikes': {'x': None}}, SPOT, 'NIFTY') is None)

print('\n--- update(): logging, throttle, flag ---')
b = FakeBot()
oc = chain(pcr=1.0, c22600=100000, p22400=90000)
OL.update(b, oc, SPOT)
rows = []
for f in glob.glob(os.path.join(TMP, 'oi_levels_NIFTY_*.jsonl')):
    rows += [json.loads(x) for x in io.open(f, encoding='utf-8') if x.strip()]
ck('writes one row', len(rows) == 1, len(rows))
if rows:
    ck('row carries levels and spot',
       {'resistance', 'support', 'spot', 'pcr', 'instrument'} <= set(rows[0]))
    ck('raw chain is NOT logged (it would bloat the file)', '_raw' not in rows[0])
OL.update(b, oc, SPOT)
rows2 = []
for f in glob.glob(os.path.join(TMP, 'oi_levels_NIFTY_*.jsonl')):
    rows2 += [json.loads(x) for x in io.open(f, encoding='utf-8') if x.strip()]
ck('throttled: an immediate second call does not re-log', len(rows2) == 1, len(rows2))
b._oi_lv_logged = datetime.now(OL.IST) - timedelta(minutes=10) if OL.IST else None
if OL.IST:
    OL.update(b, oc, SPOT)
    rows3 = []
    for f in glob.glob(os.path.join(TMP, 'oi_levels_NIFTY_*.jsonl')):
        rows3 += [json.loads(x) for x in io.open(f, encoding='utf-8') if x.strip()]
    ck('logs again once the interval has passed', len(rows3) == 2, len(rows3))

ck('prev snapshot retained for the next dOI', b._oi_lv_prev is not None)
config.OI_LEVELS_ENABLED = False
b2 = FakeBot()
OL.update(b2, oc, SPOT)
ck('OI_LEVELS_ENABLED=False is respected', b2._oi_lv_prev is None)
config.OI_LEVELS_ENABLED = True

OL.reset_day(b)
ck('reset_day clears the snapshot', b._oi_lv_prev is None)
ck('reset_day clears the log timer', b._oi_lv_logged is None)

print(f'\n{P} passed, {F} failed')
sys.exit(1 if F else 0)
