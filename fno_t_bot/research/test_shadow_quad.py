# -*- coding: utf-8 -*-
"""Unit-test the ADX/VIX/OI/PCR shadow engine.

Two things have to be true for this engine to be worth anything.

First, it must never touch the live book -- so there is a test that a full
observe() leaves the bot's positions untouched and places no orders.

Second, and more subtly: EVERY evaluation must be logged, including the bars
where it does not fire and the bars where inputs are missing. If only fires
were recorded there would be no base rate to compare against, and a 55% hit
rate on self-selected bars would look like an edge. Most of these tests are
about that -- abstaining rather than guessing, and recording the abstention.
"""
import glob, io, json, logging, os, sys, tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
import shadow_quad as SQ

P = F = 0


def ck(name, cond, detail=''):
    global P, F
    if cond:
        P += 1
        print(f'  PASS  {name}')
    else:
        F += 1
        print(f'  FAIL  {name}  {detail}')


TMP = os.path.join(tempfile.gettempdir(), 'shq_test')
os.makedirs(TMP, exist_ok=True)


def clear():
    for f in glob.glob(os.path.join(TMP, '*.jsonl')):
        os.remove(f)


clear()
config.LOG_DIRECTORY = TMP
config.SHADOW_QUAD_ENABLED = True
config.SHADOW_QUAD_LOG_EVERY_MIN = 5
config.SHADOW_QUAD_FIRE_THRESHOLD = 3
config.SHADOW_QUAD_ADX_FLOOR = 20
config.SHADOW_QUAD_DI_SPREAD = 8
config.SHADOW_QUAD_PCR_BULL = 1.10
config.SHADOW_QUAD_PCR_BEAR = 0.90
config.SHADOW_QUAD_MP_MIN_DIST = 0.20
config.SHADOW_QUAD_VIX_MIN = 11
config.SHADOW_QUAD_VIX_MAX = 22

try:
    import pandas as pd
except Exception:
    sys.exit('needs pandas')

SPOT = 22500.0


class FakeBot:
    def __init__(self, vix=14.0):
        self.instrument = 'NIFTY'
        self.logger = logging.getLogger('shq')
        self.logger.addHandler(logging.NullHandler())
        self._last_vix = vix
        self._shq_last = None
        self.positions = []
        self.orders_placed = 0

    def place_order(self, *a, **k):
        self.orders_placed += 1
        raise AssertionError('shadow engine must never place an order')


def frame(adx=30.0, di_p=30.0, di_m=15.0):
    return pd.DataFrame({'ADX': [adx], 'DI_plus': [di_p], 'DI_minus': [di_m],
                         'Close': [SPOT]})


def chain(pcr=1.0, mp=22500.0, call_oi=None, put_oi=None):
    st = {}
    for k, v in (call_oi or {}).items():
        st.setdefault(float(k), {})['call_oi'] = v
    for k, v in (put_oi or {}).items():
        st.setdefault(float(k), {})['put_oi'] = v
    return {'pcr': pcr, 'max_pain': mp, 'strikes': st}


def rows():
    out = []
    for f in glob.glob(os.path.join(TMP, 'shadow_quad_NIFTY_*.jsonl')):
        out += [json.loads(x) for x in io.open(f, encoding='utf-8') if x.strip()]
    return out


print('\n--- ADX / DI component ---')
ck('DI+ dominance votes CALL', SQ._adx_vote(30, 30, 15)[0] == +1)
ck('DI- dominance votes PUT', SQ._adx_vote(30, 15, 30)[0] == -1)
ck('ADX below the floor abstains', SQ._adx_vote(15, 40, 10)[0] == 0,
   SQ._adx_vote(15, 40, 10))
ck('DI spread under threshold abstains', SQ._adx_vote(30, 22, 18)[0] == 0)
ck('missing ADX abstains rather than guessing', SQ._adx_vote(None, 30, 15)[0] == 0)
ck('missing DI abstains', SQ._adx_vote(30, None, 15)[0] == 0)

print('\n--- PCR component ---')
ck('high PCR (put writing) votes CALL', SQ._pcr_vote(1.30)[0] == +1)
ck('low PCR (call writing) votes PUT', SQ._pcr_vote(0.70)[0] == -1)
ck('mid PCR abstains', SQ._pcr_vote(1.00)[0] == 0)
ck('PCR exactly at the bull edge votes', SQ._pcr_vote(1.10)[0] == +1)
ck('missing PCR abstains', SQ._pcr_vote(None)[0] == 0)

print('\n--- MaxPain component (attractor, not wall) ---')
ck('MaxPain above spot votes CALL', SQ._maxpain_vote(22500, 22600)[0] == +1)
ck('MaxPain below spot votes PUT', SQ._maxpain_vote(22500, 22400)[0] == -1)
ck('MaxPain inside the dead band abstains',
   SQ._maxpain_vote(22500, 22510)[0] == 0, SQ._maxpain_vote(22500, 22510))
ck('missing MaxPain abstains', SQ._maxpain_vote(22500, None)[0] == 0)

print('\n--- OI wall component ---')
ck('nearer put wall votes CALL (support closer)',
   SQ._wall_vote(SPOT, chain(call_oi={23000: 500000}, put_oi={22450: 400000})['strikes'])[0] == +1)
ck('nearer call wall votes PUT (resistance closer)',
   SQ._wall_vote(SPOT, chain(call_oi={22550: 500000}, put_oi={22000: 400000})['strikes'])[0] == -1)
ck('only one side present abstains',
   SQ._wall_vote(SPOT, chain(call_oi={22600: 500000})['strikes'])[0] == 0)
ck('empty chain abstains', SQ._wall_vote(SPOT, {})[0] == 0)

print('\n--- score, VIX filter, threshold ---')
b = FakeBot()
# all four bullish: ADX DI+, PCR high, MaxPain above, put wall nearer
r = SQ.evaluate(b, frame(), chain(pcr=1.3, mp=22650,
                                  call_oi={23000: 5e5}, put_oi={22450: 4e5}), SPOT)
ck('four bullish components score +4', r['score'] == 4, r['score'] if r else None)
ck('and it fires CALL', r['direction'] == 'CALL')
r = SQ.evaluate(b, frame(di_p=15, di_m=30), chain(pcr=0.7, mp=22350,
                                                  call_oi={22550: 5e5}, put_oi={22000: 4e5}), SPOT)
ck('four bearish components score -4', r['score'] == -4, r['score'])
ck('and it fires PUT', r['direction'] == 'PUT')

# two agreeing is below the threshold of 3
r = SQ.evaluate(b, frame(), chain(pcr=1.3, mp=22500, call_oi={}, put_oi={}), SPOT)
ck('score below threshold does NOT fire', r['fired'] is False, r['score'])
ck('but the score is still recorded', isinstance(r['score'], int))

b_lo = FakeBot(vix=9.0)
r = SQ.evaluate(b_lo, frame(), chain(pcr=1.3, mp=22650,
                                     call_oi={23000: 5e5}, put_oi={22450: 4e5}), SPOT)
ck('VIX below the band blocks the fire', r['fired'] is False and r['score'] == 4,
   (r['fired'], r['score']))
ck('and vix_ok records why', r['vix_ok'] is False)
b_hi = FakeBot(vix=30.0)
r = SQ.evaluate(b_hi, frame(), chain(pcr=1.3, mp=22650,
                                     call_oi={23000: 5e5}, put_oi={22450: 4e5}), SPOT)
ck('VIX above the band blocks the fire too', r['fired'] is False)

print('\n--- logging: every evaluation, not just the fires ---')
clear()
b = FakeBot()
SQ.observe(b, frame(), chain(pcr=1.0, mp=22500), SPOT)   # scores 0, no fire
r = rows()
ck('a NON-firing bar is still logged', len(r) == 1, len(r))
if r:
    ck('record says it did not fire', r[0]['fired'] is False)
    ck('component votes present', set(r[0]['votes']) == {'adx', 'pcr', 'maxpain', 'wall'},
       r[0].get('votes'))
    ck('RAW inputs present so thresholds can be re-cut offline',
       set(r[0]['raw']) >= {'adx', 'di_plus', 'di_minus', 'pcr', 'max_pain'},
       r[0].get('raw'))
    ck('human-readable reasons present', set(r[0]['why']) == {'adx', 'pcr', 'maxpain', 'wall'})
    ck('vix recorded even when it blocks', 'vix' in r[0] and 'vix_ok' in r[0])

clear()
b = FakeBot()
SQ.observe(b, frame(adx=None), chain(pcr=None, mp=None), SPOT)
r = rows()
ck('a bar with MISSING inputs is still logged (base rate needs it)', len(r) == 1)
if r:
    ck('and every component abstained rather than guessing',
       all(v == 0 for v in r[0]['votes'].values()), r[0]['votes'])

print('\n--- it must not touch the live book ---')
b = FakeBot()
before = list(b.positions)
SQ.observe(b, frame(), chain(pcr=1.3, mp=22650,
                             call_oi={23000: 5e5}, put_oi={22450: 4e5}), SPOT)
ck('positions untouched', b.positions == before)
ck('no order placed', b.orders_placed == 0)

print('\n--- throttle, flag, reset ---')
clear()
b = FakeBot()
SQ.observe(b, frame(), chain(), SPOT)
SQ.observe(b, frame(), chain(), SPOT)
ck('second call inside the interval is skipped', len(rows()) == 1, len(rows()))
if SQ._late('IST'):
    b._shq_last = datetime.now(SQ._late('IST')) - timedelta(minutes=6)
    SQ.observe(b, frame(), chain(), SPOT)
    ck('logs again after the interval', len(rows()) == 2, len(rows()))

clear()
config.SHADOW_QUAD_ENABLED = False
b = FakeBot()
SQ.observe(b, frame(), chain(), SPOT)
ck('SHADOW_QUAD_ENABLED=False respected', not rows())
config.SHADOW_QUAD_ENABLED = True

b = FakeBot()
SQ.observe(b, frame(), chain(), SPOT)
SQ.reset_day(b)
ck('reset_day clears the throttle', b._shq_last is None)

b = FakeBot()
ck('garbage frame raises nothing', SQ.evaluate(b, pd.DataFrame({'Close': [1]}), {}, SPOT) is not None
   or True)
SQ.observe(b, pd.DataFrame(), None, SPOT)
ck('empty frame + no chain does not raise', True)

print(f'\n{P} passed, {F} failed')
sys.exit(1 if F else 0)
