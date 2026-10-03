# -*- coding: utf-8 -*-
"""Unit-test the premium recorder.

This writes the archive that makes future backtests real-priced, so the bar is
that a recorded row must be TRUSTWORTHY or ABSENT -- never partial, never
inferred. The tests target exactly that: the defensive field capture (an
unknown field must not break it, a missing field must not become null), the
batching and throttle, and silence on every failure path.
"""
import glob, io, json, logging, os, sys, tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
import premium_recorder as PR

P = F = 0


def ck(name, cond, detail=''):
    global P, F
    if cond:
        P += 1
        print(f'  PASS  {name}')
    else:
        F += 1
        print(f'  FAIL  {name}  {detail}')


TMP = os.path.join(tempfile.gettempdir(), 'premrec_test')
os.makedirs(TMP, exist_ok=True)


def clear():
    for f in glob.glob(os.path.join(TMP, '*.jsonl')):
        os.remove(f)


clear()
config.LOG_DIRECTORY = TMP
config.PREMIUM_RECORDER_ENABLED = True
config.PREMIUM_REC_EVERY_MIN = 5
config.PREMIUM_REC_STRIKES = 2
config.PREMIUM_REC_BATCH = 4          # small, to force multiple chunks


class FakeFyers:
    def __init__(self, payload=None, fail=False, bad_status=False):
        self.payload = payload or {}
        self.fail = fail
        self.bad_status = bad_status
        self.calls = []

    def quotes(self, req):
        syms = req['symbols'].split(',')
        self.calls.append(syms)
        if self.fail:
            raise RuntimeError('broker down')
        if self.bad_status:
            return {'s': 'error', 'message': 'nope'}
        d = []
        for s in syms:
            v = self.payload.get(s)
            if v is not None:
                d.append({'n': s, 'v': v})
        return {'s': 'ok', 'd': d}


class FakeBot:
    def __init__(self, fy=None, inst='NIFTY'):
        self.instrument = inst
        self.strike_gap = 50
        self.lot_size = 75
        self.fyers = fy
        self.logger = logging.getLogger('premrec')
        self.logger.addHandler(logging.NullHandler())
        self._premrec_last = None
        self._last_vix = 13.5
        self._last_atm_iv = 12.1


def rows(inst='NIFTY'):
    out = []
    for f in glob.glob(os.path.join(TMP, f'premiums_{inst}_*.jsonl')):
        out += [json.loads(x) for x in io.open(f, encoding='utf-8') if x.strip()]
    return out


FULL = {'lp': 150.5, 'bid': 150.0, 'ask': 151.0, 'volume': 12345,
        'oi': 98765, 'iv': 12.4, 'ch': 2.5}

print('\n--- symbol construction ---')
b = FakeBot(FakeFyers())
opts, extra, atm, exp = PR._symbols(b, 22513.0)
ck('ATM snapped to the strike grid', atm == 22500, atm)
ck('ATM +/- 2 strikes, both legs = 10 contracts', len(opts) == 10, len(opts))
ck('both CE and PE at every strike',
   sorted({t for _, t, _ in opts}) == ['CALL', 'PUT'])
ck('strikes span 22400..22600',
   sorted({k for k, _, _ in opts}) == [22400, 22450, 22500, 22550, 22600],
   sorted({k for k, _, _ in opts}))
ck('futures derived, not hardcoded',
   any(l == 'fut_near' for l, _ in extra) and any(l == 'fut_next' for l, _ in extra),
   extra)
ck('SENSEX gets no NSE futures', PR._futures('SENSEX') == [])

print('\n--- capture is defensive ---')
clear()
payload = {}
for k, t, s in opts:
    payload[s] = dict(FULL)
payload[opts[0][2]]['some_new_broker_field'] = 'xyz'      # unknown field
payload[opts[1][2]].pop('bid')                            # missing field
payload[opts[2][2]]['ask'] = None                         # explicit null
b = FakeBot(FakeFyers(payload))
PR.record(b, 22513.0)
r = rows()
ck('writes exactly one snapshot row', len(r) == 1, len(r))
if r:
    legs = {(l['strike'], l['type']): l for l in r[0]['legs']}
    ck('all 10 legs captured', len(r[0]['legs']) == 10, len(r[0]['legs']))
    k0, t0, _ = opts[0]
    ck('unknown broker field is DROPPED, not stored blindly',
       'some_new_broker_field' not in legs[(k0, t0)])
    k1, t1, _ = opts[1]
    ck('missing field is ABSENT, not null',
       'bid' not in legs[(k1, t1)], legs[(k1, t1)])
    k2, t2, _ = opts[2]
    ck('explicit null is ABSENT, not stored as None',
       'ask' not in legs[(k2, t2)], legs[(k2, t2)])
    ck('bid AND ask captured where present',
       legs[(k0, t0)].get('bid') == 150.0 and legs[(k0, t0)].get('ask') == 151.0)
    ck('oi and volume captured', legs[(k0, t0)].get('oi') == 98765
       and legs[(k0, t0)].get('volume') == 12345)
    ck('spot/vix/atm_iv stamped at the same instant',
       r[0]['spot'] == 22513.0 and r[0]['vix'] == 13.5 and r[0]['atm_iv'] == 12.1)
    ck('schema version recorded', r[0].get('schema') == 1)
    ck('expiry recorded', bool(r[0].get('expiry')))

print('\n--- batching ---')
clear()
fy = FakeFyers(payload)
b = FakeBot(fy)
PR.record(b, 22513.0)
ck('splits into batches of PREMIUM_REC_BATCH', all(len(c) <= 4 for c in fy.calls),
   [len(c) for c in fy.calls])
ck('every symbol requested exactly once',
   sum(len(c) for c in fy.calls) == len(opts) + len(extra),
   sum(len(c) for c in fy.calls))

print('\n--- throttle ---')
clear()
b = FakeBot(FakeFyers(payload))
PR.record(b, 22513.0)
PR.record(b, 22514.0)
ck('second call inside the interval is skipped', len(rows()) == 1, len(rows()))
b._premrec_last = datetime.now(PR.IST) - timedelta(minutes=6) if PR.IST else None
if PR.IST:
    PR.record(b, 22515.0)
    ck('records again after the interval', len(rows()) == 2, len(rows()))

print('\n--- failure paths write NOTHING ---')
clear()
b = FakeBot(FakeFyers(payload, fail=True))
PR.record(b, 22513.0)
ck('broker exception -> no row', not rows())
clear()
b = FakeBot(FakeFyers(payload, bad_status=True))
PR.record(b, 22513.0)
ck('non-ok status -> no row', not rows())
clear()
b = FakeBot(FakeFyers({}))
PR.record(b, 22513.0)
ck('empty payload -> no row', not rows())
clear()
b = FakeBot(None)
PR.record(b, 22513.0)
ck('no broker session -> no row, no raise', not rows())
clear()
b = FakeBot(FakeFyers(payload))
PR.record(b, 0)
ck('zero spot -> no row', not rows())

clear()
config.PREMIUM_RECORDER_ENABLED = False
b = FakeBot(FakeFyers(payload))
PR.record(b, 22513.0)
ck('PREMIUM_RECORDER_ENABLED=False respected', not rows())
config.PREMIUM_RECORDER_ENABLED = True

print('\n--- partial chain still records what it has ---')
clear()
partial = {opts[0][2]: dict(FULL), opts[1][2]: dict(FULL)}
b = FakeBot(FakeFyers(partial))
PR.record(b, 22513.0)
r = rows()
ck('records the legs it got rather than discarding all',
   len(r) == 1 and r[0]['n_legs'] == 2, r[0]['n_legs'] if r else 'no row')

b2 = FakeBot(FakeFyers(payload))
PR.record(b2, 22513.0)
PR.reset_day(b2)
ck('reset_day clears the throttle', b2._premrec_last is None)

print('\n--- schema probe ---')
clear()
b = FakeBot(FakeFyers(payload))
PR.record(b, 22513.0)
r = rows()
ck('first snapshot of the day carries a schema probe',
   len(r) == 1 and 'schema_probe' in r[0], list(r[0]) if r else 'no row')
if r and 'schema_probe' in r[0]:
    ck('probe keeps the FULL raw payload, unknown fields included',
       'some_new_broker_field' in r[0]['schema_probe']['raw'],
       r[0]['schema_probe']['raw'])
    ck('probe names the symbol it came from', bool(r[0]['schema_probe'].get('symbol')))
if PR.IST:
    b._premrec_last = datetime.now(PR.IST) - timedelta(minutes=6)
    PR.record(b, 22514.0)
    r2 = rows()
    ck('probe appears ONCE per session, not on every snapshot',
       len(r2) == 2 and 'schema_probe' not in r2[1],
       [('schema_probe' in x) for x in r2])
PR.reset_day(b)
ck('reset_day re-arms the probe for the next session',
   getattr(b, '_premrec_probed', True) is False)


print(f'\n{P} passed, {F} failed')
sys.exit(1 if F else 0)
