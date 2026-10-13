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
import capital_gate as cg

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


print(f'\n{P} passed, {F} failed')
sys.exit(1 if F else 0)
