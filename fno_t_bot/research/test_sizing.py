# -*- coding: utf-8 -*-
"""Unit-test capital-scaled position sizing.

Three defects are under test here, all found Oct 13 2026:

  1. the sizing book ignored configured capital entirely -- it read
     SENSEX_LIVE_START_CAPITAL plus cumulative paper P&L, so raising
     TOTAL_CAPITAL to Rs300,000 moved the log line and nothing else
  2. DYN_MAX_LOTS_LADDER was the binding ceiling and STOPPED at 4 lots for any
     book at or above Rs1L, so Rs3L, Rs30L and Rs3cr all resolved to 4
  3. conviction scaling, if switched on against any currently logged field,
     would put MORE size on WORSE trades

The last one is why most of these tests assert that the conviction hook stays
inert, and that it only ever acts when explicitly configured.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
import tempfile as _hermetic_tf
import capital_gate as cg

# Hermetic: the sizing book now reads trade logs, so a test that touches it
# must never see the real logs/ directory. Without this the static-book tests
# pass only on a machine that happens to have no trade files.
config.LOG_DIRECTORY = _hermetic_tf.mkdtemp()
_SHIP = {k: getattr(config, k, None) for k in (
    'COMPOUND_ENABLED', 'COMPOUND_FROM_DATE', 'COMPOUND_FLOOR_PCT',
    'TARGET_RISK_PCT_OF_BOOK', 'LOT_SIZING_MODE', 'DD_THROTTLE_ENABLED',
    'DD_THROTTLE_FULL', 'DD_THROTTLE_MIN_MULT', 'PORTFOLIO_HEAT_ENABLED',
    'PORTFOLIO_HEAT_PCT')}

P = F = 0


def ck(name, cond, detail=''):
    global P, F
    if cond:
        P += 1
        print(f'  PASS  {name}')
    else:
        F += 1
        print(f'  FAIL  {name}  {detail}')


print('\n--- the sizing book must read CONFIGURED capital ---')
_orig = {i: config.INSTRUMENT_STRATEGY[i]['capital'] for i in ('NIFTY', 'BANKNIFTY', 'SENSEX')}
try:
    for i in _orig:
        config.INSTRUMENT_STRATEGY[i]['capital'] = 250_000
    ck('reads the per-instrument allocation', cg.get_sizing_book('NIFTY') == 250_000,
       cg.get_sizing_book('NIFTY'))
    ck('and it is NOT the old SENSEX_LIVE_START_CAPITAL baseline',
       cg.get_sizing_book('NIFTY') != float(config.SENSEX_LIVE_START_CAPITAL))
    cap, lots, book = cg.get_risk_params(None, 'NIFTY')
    ck('risk cap scales with it (10% of book)', abs(cap - 25_000) < 1, cap)
    ck('book reported back is the configured one', book == 250_000, book)
finally:
    for i, v in _orig.items():
        config.INSTRUMENT_STRATEGY[i]['capital'] = v

print('\n--- the ladder must no longer be the binding ceiling ---')
cap, lots, book = cg.get_risk_params(None, 'NIFTY')
ck('ceiling is ABSOLUTE_MAX_LOTS, not a ladder tier',
   lots == getattr(config, 'ABSOLUTE_MAX_LOTS', 50), lots)
ck('and that is well above the old top tier of 4', lots > 4, lots)

print('\n--- affordable lots scale continuously with capital ---')
import logging
from options_bot import TradingBot
logging.disable(logging.CRITICAL)
b = TradingBot('NIFTY')
logging.disable(logging.NOTSET)

seen = {}
for capital in (100_000, 300_000, 1_000_000):
    for i in _orig:
        config.INSTRUMENT_STRATEGY[i]['capital'] = capital
    b._risk_params_date = None            # bust the daily cache
    seen[capital] = b._affordable_lots(150.0, 0.25)
for i, v in _orig.items():
    config.INSTRUMENT_STRATEGY[i]['capital'] = v
b._risk_params_date = None
print('      ' + '  '.join(f'Rs{k:,}->{v} lots' for k, v in seen.items()))
ck('more capital buys more lots', seen[300_000] > seen[100_000], seen)
ck('and it keeps scaling past the old 4-lot wall', seen[1_000_000] > 4, seen)
ck('3x the capital gives roughly 3x the lots',
   2 <= seen[300_000] / max(seen[100_000], 1) <= 4, seen)

print('\n--- both caps bind, whichever is tighter ---')
ck('a dearer option buys fewer lots',
   b._affordable_lots(400.0, 0.25) < b._affordable_lots(80.0, 0.25),
   (b._affordable_lots(400.0, 0.25), b._affordable_lots(80.0, 0.25)))
n = b._affordable_lots(150.0, 0.25)
cap, _, book = cg.get_risk_params(None, 'NIFTY')
ck('risk of the chosen size stays inside the risk cap',
   150.0 * b.lot_size * 0.25 * n <= cap + 1e-6,
   (150.0 * b.lot_size * 0.25 * n, cap))
ck('outlay stays inside MAX_DEPLOY_PCT of the book',
   150.0 * b.lot_size * n <= book * config.MAX_DEPLOY_PCT + 1e-6,
   (150.0 * b.lot_size * n, book * config.MAX_DEPLOY_PCT))
ck('a wider stop reduces the affordable count',
   b._affordable_lots(150.0, 0.50) <= b._affordable_lots(150.0, 0.25))
ck('zero premium is refused rather than divided by', b._affordable_lots(0, 0.25) == 0)
ck('negative premium refused', b._affordable_lots(-5, 0.25) == 0)
ck('never exceeds ABSOLUTE_MAX_LOTS',
   b._affordable_lots(0.5, 0.25) <= config.ABSOLUTE_MAX_LOTS,
   b._affordable_lots(0.5, 0.25))

print('\n--- conviction hook is INERT unless explicitly configured ---')
ck('default source is None', getattr(config, 'CONVICTION_LOT_SOURCE', None) is None)
for sig in ({'unified_score': 95}, {'unified_score': 10}, {'rev_score': 3},
            {'exp_move_pct': 1.2}, {}):
    ck(f'{sig or "empty signal"} -> 1 lot', b._conviction_lots(sig) == 1,
       b._conviction_lots(sig))

_src, _lad = config.CONVICTION_LOT_SOURCE, config.CONVICTION_LOT_LADDER
try:
    config.CONVICTION_LOT_SOURCE = 'unified_score'
    config.CONVICTION_LOT_LADDER = [(0, 1), (70, 2), (90, 3)]
    ck('when configured, it reads the ladder', b._conviction_lots({'unified_score': 95}) == 3,
       b._conviction_lots({'unified_score': 95}))
    ck('mid tier', b._conviction_lots({'unified_score': 75}) == 2)
    ck('below every tier -> 1', b._conviction_lots({'unified_score': 10}) == 1)
    ck('missing field -> 1, not an exception', b._conviction_lots({}) == 1)
    ck('non-numeric field -> 1', b._conviction_lots({'unified_score': 'high'}) == 1)
finally:
    config.CONVICTION_LOT_SOURCE, config.CONVICTION_LOT_LADDER = _src, _lad
ck('restored to inert after the test', b._conviction_lots({'unified_score': 95}) == 1)

print('\n--- enter_trade INTEGRATION (the gap that let Oct 13 through) ---')
# The first draft of the capital-sizing block referenced _stop_pct ~80 lines
# before it was assigned. It raised UnboundLocalError inside enter_trade and
# killed a live SENSEX entry on a day the index rose 1.50%. There were 28 tests
# on _affordable_lots and NONE that actually called enter_trade, so nothing
# caught it. This does.
_prev = (config.PAPER_REQUIRE_REAL_LTP, config.PAPER_TRADE_MODE)
try:
    config.PAPER_REQUIRE_REAL_LTP = False      # no broker in a unit test
    config.PAPER_TRADE_MODE = True
    for mode in ('capital', 'fixed'):
        config.LOT_SIZING_MODE = mode
        for typ in ('CALL', 'PUT'):
            sig = {'type': typ, 'price': 22500.0, 'adx': 30.0, 'strength': 1,
                   'lots': 1, 'path': 'REV', 'otm_strikes': 0,
                   'otm_reason': 'test', 'rev_score': 3}
            err = None
            try:
                logging.disable(logging.CRITICAL)
                b.enter_trade(dict(sig), 0.12, lots=1)
            except (UnboundLocalError, NameError) as e:
                err = e
            except Exception:
                err = None                     # other failures are not this bug
            finally:
                logging.disable(logging.NOTSET)
            ck(f'enter_trade({typ}, mode={mode}) raises no Unbound/NameError',
               err is None, err)
finally:
    config.PAPER_REQUIRE_REAL_LTP, config.PAPER_TRADE_MODE = _prev
    config.LOT_SIZING_MODE = 'capital'

ck('the sizing block sits AFTER _stop_pct is assigned',
   (lambda src: src.index('_stop_pct  = (') < src.index('self._affordable_lots(float(_fb_price)'))(
       __import__('io').open(os.path.join(os.path.dirname(os.path.dirname(
           os.path.abspath(__file__))), 'options_bot.py'), encoding='utf-8').read()),
   'ordering regression')



def eq(name, got, want):
    # ck() in THIS file takes a boolean, not (got, want). The first draft of the
    # compounding tests passed (got, want) to it, so any truthy 'got' passed:
    # ck('wins compound the book', _book(), 110_000) would have passed for
    # ANY nonzero book. eq() actually compares.
    ck(name, got == want, f'got {got!r}, want {want!r}')

print('\n--- COMPOUNDING: size follows realised equity (Oct 21 2026) ---')
# Why this exists. LOT_SIZING_MODE was 'fixed', so on Oct 21 the bot placed the
# same 1-lot trades at Rs1L, Rs3L, Rs10L and Rs30L -- run through the real
# _affordable_lots those accounts would have been 1/1/1, 3/3/3, 12/11/11 and
# 37/33/34 lots. Account size, and therefore compounding, did nothing.
import json as _json, os as _os, tempfile as _tf, datetime as _dtm

_ld0 = config.LOG_DIRECTORY
_cfg0 = dict(_SHIP)
_tmp = _tf.mkdtemp()
config.LOG_DIRECTORY = _tmp
for _i in _orig:
    config.INSTRUMENT_STRATEGY[_i]['capital'] = 100_000


def _put(inst, day, pnl, px_src='LTP'):
    p = _os.path.join(_tmp, f'FnO_T_Bot_{inst}_trades_{day}.jsonl')
    with open(p, 'a', encoding='utf-8') as fh:
        fh.write(_json.dumps({'pnl_net': pnl, 'px_src': px_src}) + '\n')
    cg._PNL_CACHE.clear()


def _book(inst='NIFTY'):
    cg._PNL_CACHE.clear()
    return cg.get_sizing_book(inst)


config.COMPOUND_ENABLED = True
config.COMPOUND_FROM_DATE = '2026-10-16'
config.COMPOUND_FLOOR_PCT = 0.25

eq('no closed trades -> book is exactly the starting capital', _book(), 100_000)

_put('NIFTY', '2026-10-16', 4_000)
_put('NIFTY', '2026-10-21', 6_000)
eq('wins compound the book', _book(), 110_000)
eq('and it is per instrument -- BANKNIFTY is untouched', _book('BANKNIFTY'), 100_000)

_put('NIFTY', '2026-10-17', -3_000)
eq('a loss shrinks it again', _book(), 107_000)

_put('NIFTY', '2026-09-01', 50_000)
eq('P&L from before COMPOUND_FROM_DATE is ignored', _book(), 107_000)

_put('NIFTY', '2026-10-20', 99_999, px_src='BS')
eq('Black-Scholes-priced trades are excluded (modelled, not observed)',
   _book(), 107_000)

_put('NIFTY', '2026-10-22', -500_000)
eq('a catastrophic run cannot size the book to zero or below -- floor',
   _book(), 25_000)
eq('and the floor is 25% of start', _book() / 100_000, 0.25)

config.COMPOUND_ENABLED = False
eq('switch off -> static configured capital again', _book(), 100_000)
config.COMPOUND_ENABLED = True

# files for another instrument must not leak in
_put('SENSEX', '2026-10-21', 7_777)
eq('another instrument\'s trades do not leak into NIFTY\'s book',
   _book('NIFTY'), 25_000)
eq('SENSEX sees its own', _book('SENSEX'), 107_777)

print('\n--- the risk budget is a PERCENTAGE of that book ---')
config.TARGET_RISK_PCT_OF_BOOK = 0.03
config.LOT_SIZING_MODE = 'risk'
for _i in _orig:
    config.INSTRUMENT_STRATEGY[_i]['capital'] = 100_000
for _f in _os.listdir(_tmp):
    _os.remove(_os.path.join(_tmp, _f))
cg._PNL_CACHE.clear()


def _lots_for(book_pnl, premium=162.90, inst='NIFTY'):
    """Lots the real risk-mode arithmetic yields at a given realised P&L."""
    for _f in _os.listdir(_tmp):
        _os.remove(_os.path.join(_tmp, _f))
    if book_pnl:
        _put(inst, '2026-10-21', book_pnl)
    cg._PNL_CACHE.clear()
    book = cg.get_sizing_book(inst)
    bot_ = {'NIFTY': b}.get(inst, b)
    one = premium * bot_.lot_size * 0.25
    tgt = book * config.TARGET_RISK_PCT_OF_BOOK
    return max(1, int(tgt // one)), book, tgt, one


l0, bk0, t0, one0 = _lots_for(0)
eq('Rs1L book @3% -> Rs3,000 budget buys 1 lot of NIFTY (Rs2,647 each)',
   (l0, round(t0)), (1, 3000))
l1, bk1, _, _ = _lots_for(200_000)
ck('after +Rs2L of realised profit the SAME rule buys more lots', l1 > l0, (l0, l1))
eq('...and the book reports the compounded equity', bk1, 300_000)
lN, bkN, _, _ = _lots_for(-60_000)
ck('after a Rs60k loss it buys no more than before (de-levers)', lN <= l0, (l0, lN))
ck('and never fewer than one lot', lN >= 1, lN)

seq = [_lots_for(p)[0] for p in (0, 50_000, 100_000, 200_000, 400_000, 800_000)]
ck('lot count is monotone non-decreasing in realised profit',
   all(seq[i] <= seq[i + 1] for i in range(len(seq) - 1)), seq)
ck('and actually grows over that range', seq[-1] > seq[0], seq)

# floor, not round: realised risk may never exceed the stated budget
ok_floor = True
for pnl in (0, 7_000, 23_000, 61_000, 150_000):
    lots_, book_, tgt_, one_ = _lots_for(pnl)
    if lots_ > 1 and lots_ * one_ > tgt_ + 1e-6:
        ok_floor = False
ck('realised risk never exceeds the stated budget (floored, not rounded)',
   ok_floor)

print('\n--- end to end: enter_trade sizes off the compounded book ---')
# This is the class of test that was missing on Oct 13, when a sizing block that
# referenced _stop_pct before assignment killed a live entry.
_prev2 = (config.PAPER_REQUIRE_REAL_LTP, config.PAPER_TRADE_MODE)
try:
    config.PAPER_REQUIRE_REAL_LTP = False
    config.PAPER_TRADE_MODE = True
    got = {}
    for label, pnl in (('flat book', 0), ('+Rs300k book', 300_000)):
        for _f in _os.listdir(_tmp):
            _os.remove(_os.path.join(_tmp, _f))
        if pnl:
            _put('NIFTY', '2026-10-21', pnl)
        cg._PNL_CACHE.clear()
        b.positions.clear()
        b.trades_today = 0
        err = None
        sig = {'type': 'PUT', 'price': 22500.0, 'adx': 30.0, 'strength': 1,
               'lots': 1, 'path': 'REV', 'otm_strikes': 0,
               'otm_reason': 'test', 'rev_score': 3}
        try:
            logging.disable(logging.CRITICAL)
            b.enter_trade(dict(sig), 0.12, lots=1)
        except (UnboundLocalError, NameError, KeyError, TypeError) as e:
            err = e
        except Exception:
            err = None
        finally:
            logging.disable(logging.NOTSET)
        ck(f'enter_trade({label}) raises no Unbound/Name/Key/TypeError', err is None, err)
        got[label] = (b.positions[-1].get('lots') if b.positions else None)
    print('      lots opened: ' + ', '.join(f'{k}={v}' for k, v in got.items()))
    ck('a position actually opened in both cases',
       all(v is not None for v in got.values()), got)
    ck('the compounded book opens MORE lots than the flat one',
       (got['+Rs300k book'] or 0) > (got['flat book'] or 0), got)
finally:
    config.PAPER_REQUIRE_REAL_LTP, config.PAPER_TRADE_MODE = _prev2
    b.positions.clear()
    b.trades_today = 0


print('\n--- DRAWDOWN THROTTLE: risk falls as the book drops below its own peak ---')
config.DD_THROTTLE_ENABLED = True
config.DD_THROTTLE_FULL = 0.25
config.DD_THROTTLE_MIN_MULT = 0.25
config.COMPOUND_FROM_DATE = '2026-10-16'
for _f in _os.listdir(_tmp):
    _os.remove(_os.path.join(_tmp, _f))
cg._PNL_CACHE.clear()


def _dd(pnls, inst='NIFTY', px_src='LTP'):
    for _f in _os.listdir(_tmp):
        _os.remove(_os.path.join(_tmp, _f))
    for n_, p_ in enumerate(pnls):
        _put(inst, f'2026-10-{16 + n_:02d}', p_, px_src)
    cg._PNL_CACHE.clear()
    return cg.drawdown_state(inst)


d0, m0 = _dd([])
eq('no trades -> no drawdown, full risk', (d0, m0), (0.0, 1.0))
d1, m1 = _dd([5_000, 5_000, 5_000])
eq('a book at its peak -> no drawdown, full risk', (d1, m1), (0.0, 1.0))
d2, m2 = _dd([20_000, -10_000])      # peak 120k, equity 110k -> dd 8.33%
eq('drawdown is measured from the RUNNING PEAK, not from the start',
   round(d2, 4), round(10_000 / 120_000, 4))
eq('and the multiplier de-levers linearly: 1 - 0.0833/0.25 = 0.667',
   round(m2, 3), round(1 - (10_000 / 120_000) / 0.25, 3))
d3, m3 = _dd([20_000, -10_000, 30_000])   # new high 140k
eq('recovering to a new high restores full risk', (d3, m3), (0.0, 1.0))
d4, m4 = _dd([-60_000])
eq('a deep drawdown bottoms at the floor multiplier, not zero', m4, 0.25)
ck('the multiplier never exceeds 1', max(m0, m1, m2, m3, m4) <= 1.0)
ck('and is monotone: deeper drawdown never raises risk',
   _dd([-5_000])[1] >= _dd([-10_000])[1] >= _dd([-20_000])[1] >= _dd([-40_000])[1])
_, mb = _dd([-50_000], px_src='BS')
eq('Black-Scholes trades do not count toward drawdown', mb, 1.0)
config.DD_THROTTLE_ENABLED = False
eq('switched off -> always full risk', _dd([-60_000]), (0.0, 1.0))
config.DD_THROTTLE_ENABLED = True

print('\n--- PORTFOLIO HEAT: three correlated bots are one bet ---')
import time as _tm
import portfolio_heat as ph
_ph_ld = config.LOG_DIRECTORY
for _f in _os.listdir(_tmp):
    _os.remove(_os.path.join(_tmp, _f))
config.PORTFOLIO_HEAT_ENABLED = True
config.PORTFOLIO_HEAT_PCT = 0.04

_pos = [{'lots': 2, 'entry_price': 100.0, 'risk_inr': 1_500.0}]
ph.publish('BANKNIFTY', _pos, 30)
eq('a bot sees ANOTHER bot\'s open risk', ph.others_open_risk('NIFTY')[0], 1_500.0)
eq('...but never counts its own', ph.others_open_risk('BANKNIFTY')[0], 0.0)
ph.publish('SENSEX', [{'lots': 1, 'entry_price': 500.0, 'risk_inr': 2_500.0}], 20)
eq('risk sums across the other two', ph.others_open_risk('NIFTY')[0], 4_000.0)

eq('position_risk prefers the value recorded at entry',
   ph.position_risk({'risk_inr': 1234.5, 'lots': 9, 'entry_price': 9}, 65), 1234.5)
eq('and falls back to lots x lot_size x entry x stop',
   round(ph.position_risk({'lots': 2, 'entry_price': 100.0}, 50), 2),
   round(2 * 50 * 100.0 * config.STOP_LOSS, 2))
eq('a malformed position contributes zero rather than raising',
   ph.position_risk({'lots': 'x', 'entry_price': None}, 65), 0.0)

_p = ph._path('BANKNIFTY')
_old = _tm.time() - 13 * 3600
_os.utime(_p, (_old, _old))
eq('a stale file (>12h, e.g. a crashed bot) is ignored so it cannot starve the others',
   ph.others_open_risk('NIFTY')[0], 2_500.0)

with open(ph._path('SENSEX'), 'w', encoding='utf-8') as fh:
    _json.dump({'risk': 9_999, 'date': '2020-01-01', 'ts': _tm.time()}, fh)
eq('a file from a previous session is ignored',
   ph.others_open_risk('NIFTY')[0], 0.0)

with open(ph._path('SENSEX'), 'w', encoding='utf-8') as fh:
    fh.write('{not json')
try:
    _x = ph.others_open_risk('NIFTY')[0]
    eq('a corrupt file is skipped, not fatal', _x, 0.0)
except Exception as _e:
    ck('a corrupt file is skipped, not fatal', False, _e)

for _f in _os.listdir(_tmp):
    _os.remove(_os.path.join(_tmp, _f))
ph.publish('BANKNIFTY', [{'risk_inr': 9_000.0}], 30)
eq('headroom = cap - others: 4% of Rs300k = Rs12k, minus Rs9k = Rs3k',
   ph.headroom('NIFTY', 300_000), 3_000.0)
ph.publish('SENSEX', [{'risk_inr': 9_000.0}], 20)
eq('headroom is clamped at zero when the others already exceed the cap',
   ph.headroom('NIFTY', 300_000), 0.0)
config.PORTFOLIO_HEAT_ENABLED = False
eq('disabled -> None, meaning no limit', ph.headroom('NIFTY', 300_000), None)
config.PORTFOLIO_HEAT_ENABLED = True
eq('zero book -> None rather than a divide or a bogus cap', ph.headroom('NIFTY', 0), None)
try:
    ph.publish('NIFTY', None, 65)
    ph.publish('NIFTY', [object()], 65)
    ck('publish never raises on garbage input', True)
except Exception as _e:
    ck('publish never raises on garbage input', False, _e)

print('\n--- end to end: heat sizes an entry DOWN but never blocks it ---')
_prev3 = (config.PAPER_REQUIRE_REAL_LTP, config.PAPER_TRADE_MODE)
try:
    config.PAPER_REQUIRE_REAL_LTP = False
    config.PAPER_TRADE_MODE = True
    config.TARGET_RISK_PCT_OF_BOOK = 0.05
    config.LOT_SIZING_MODE = 'risk'
    # a rich NIFTY book so the unconstrained answer is several lots
    for _i in _orig:
        config.INSTRUMENT_STRATEGY[_i]['capital'] = 1_000_000

    def _enter(heat_others):
        for _f in _os.listdir(_tmp):
            _os.remove(_os.path.join(_tmp, _f))
        cg._PNL_CACHE.clear()
        if heat_others:
            ph.publish('BANKNIFTY', [{'risk_inr': heat_others}], 30)
        b.positions.clear()
        b.trades_today = 0
        b._risk_params_date = None
        sig = {'type': 'PUT', 'price': 22500.0, 'adx': 30.0, 'strength': 1,
               'lots': 1, 'path': 'REV', 'otm_strikes': 0,
               'otm_reason': 'test', 'rev_score': 3}
        err = None
        try:
            logging.disable(logging.CRITICAL)
            b.enter_trade(dict(sig), 0.12, lots=1)
        except (UnboundLocalError, NameError, KeyError, TypeError) as e:
            err = e
        except Exception:
            err = None
        finally:
            logging.disable(logging.NOTSET)
        pos = b.positions[-1] if b.positions else None
        return err, pos

    e0, p0 = _enter(0)
    ck('no other bot holding risk: enter_trade raises nothing', e0 is None, e0)
    ck('...and opens a position', p0 is not None)
    free_lots = (p0 or {}).get('lots', 0)
    print(f'      unconstrained lots: {free_lots}')
    ck('a Rs10L index book opens several lots', free_lots >= 2, free_lots)
    ck('the position records its rupee risk for the other bots to see',
       (p0 or {}).get('risk_inr', 0) > 0, (p0 or {}).get('risk_inr'))
    ck('heat was published on entry and matches that risk',
       abs(ph.others_open_risk('BANKNIFTY')[0] - (p0 or {}).get('risk_inr', -1)) < 0.01,
       ph.others_open_risk('BANKNIFTY'))
    ck('the sizing rationale is written into the trade record',
       'heat_room' in str((p0 or {}).get('size_reason', '')),
       (p0 or {}).get('size_reason'))

    # others already hold nearly the whole 4%-of-total cap
    _tot_book = 3_000_000
    e1, p1 = _enter(0.04 * _tot_book - 500)
    ck('with the cap nearly used, enter_trade still raises nothing', e1 is None, e1)
    ck('...and STILL opens a position -- heat sizes down, it never blocks',
       p1 is not None)
    capped_lots = (p1 or {}).get('lots', 99)
    print(f'      lots with heat room of ~Rs500: {capped_lots}')
    ck('it is cut to the one-lot floor', capped_lots == 1, capped_lots)
    ck('and that is strictly fewer than unconstrained', capped_lots < free_lots,
       (capped_lots, free_lots))

    # closing the position must clear this bot's published heat
    try:
        logging.disable(logging.CRITICAL)
        b.check_exits(22500.0, 0.12, force_close=True)
    except Exception:
        pass
    finally:
        logging.disable(logging.NOTSET)
    ck('after a force-close the position is gone', len(b.positions) == 0, len(b.positions))
    ck('and this bot\'s published heat is cleared',
       ph.others_open_risk('SENSEX')[0] == 0.0 or
       _json.load(open(ph._path('NIFTY')))['risk'] == 0.0)
finally:
    config.PAPER_REQUIRE_REAL_LTP, config.PAPER_TRADE_MODE = _prev3
    b.positions.clear()
    b.trades_today = 0

# restore everything this block touched
config.LOG_DIRECTORY = _ld0
for _k, _v in _cfg0.items():
    if _v is not None:
        setattr(config, _k, _v)
for _i, _v in _orig.items():
    config.INSTRUMENT_STRATEGY[_i]['capital'] = _v
cg._PNL_CACHE.clear()
b._risk_params_date = None
eq('config restored: compounding ships ENABLED', config.COMPOUND_ENABLED, True)
eq('config restored: sizing mode ships as risk', config.LOT_SIZING_MODE, 'risk')

print(f'\n{P} passed, {F} failed')
sys.exit(1 if F else 0)
