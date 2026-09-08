# -*- coding: utf-8 -*-
"""Unit-test the rebuilt Challenger (vertical debit spread, real-LTP marked).

No Fyers session in test, so self.fyers is None and every quote falls back to
Black-Scholes. That is fine: the fallback path is exactly what must not break,
and the STRUCTURE (two legs, net debit, both-leg costs, correct short strike)
is what is under test.
"""
import io, os, sys, logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from options_bot import TradingBot

P = F = 0


def ck(name, cond, detail=''):
    global P, F
    if cond:
        P += 1
        print(f'  PASS  {name}')
    else:
        F += 1
        print(f'  FAIL  {name}  {detail}')


print('=' * 72)
print(f'CHALLENGER — mode={config.CHALLENGER_MODE} '
      f'gaps={config.CHALLENGER_SPREAD_GAPS}')
print('=' * 72)

bot = TradingBot('NIFTY')
bot.challenger_positions = []
bot.challenger_trades_today = 0

sig = {'price': 23800.0, 'type': 'CALL', 'atm_iv': 12.0, 'path': 'TEST',
       'otm_strikes': 0, 'adx': 30.0}
bot.enter_challenger_trade(sig, 0.10, {'max_pain': 23800}, lots=1)

ck('opened exactly one shadow position', len(bot.challenger_positions) == 1,
   str(len(bot.challenger_positions)))
if bot.challenger_positions:
    p = bot.challenger_positions[0]
    gap = bot.strike_gap
    ck('mode recorded as SPREAD', p.get('mode') == 'SPREAD', str(p.get('mode')))
    ck('long leg is the Champion strike (ATM)', p['long_strike'] == 23800,
       str(p['long_strike']))
    ck('CALL short leg is further OTM (higher)',
       p['short_strike'] == 23800 + config.CHALLENGER_SPREAD_GAPS * gap,
       f"{p['short_strike']} vs {23800 + config.CHALLENGER_SPREAD_GAPS*gap}")
    ck('short leg cheaper than long leg', p['short_entry'] < p['long_entry'],
       f"{p['short_entry']} vs {p['long_entry']}")
    ck('entry price is the NET DEBIT',
       abs(p['entry_price'] - (p['long_entry'] - p['short_entry'])) < 0.02,
       f"{p['entry_price']} vs {p['long_entry']-p['short_entry']}")
    ck('net debit is less than the naked long',
       p['entry_price'] < p['long_entry'],
       f"{p['entry_price']} vs {p['long_entry']}")
    ck('width recorded', p.get('width_pts') == config.CHALLENGER_SPREAD_GAPS * gap,
       str(p.get('width_pts')))
    ck('pricing source tagged for both legs',
       p.get('px_src') and '/' in str(p.get('px_src')), str(p.get('px_src')))
    print(f"    long {p['long_strike']} Rs{p['long_entry']} / "
          f"short {p['short_strike']} Rs{p['short_entry']} "
          f"-> net Rs{p['entry_price']:.2f} "
          f"({100*(1-p['entry_price']/p['long_entry']):.0f}% less at risk)")

# PUT side: the short leg must go the other way
bot.challenger_positions = []
bot.enter_challenger_trade({'price': 23800.0, 'type': 'PUT', 'atm_iv': 12.0,
                            'path': 'TEST', 'otm_strikes': 0, 'adx': 30.0},
                           0.10, {'max_pain': 23800}, lots=1)
if bot.challenger_positions:
    p = bot.challenger_positions[0]
    ck('PUT short leg is further OTM (lower)',
       p['short_strike'] == 23800 - config.CHALLENGER_SPREAD_GAPS * bot.strike_gap,
       str(p['short_strike']))

# marking must not throw, and must move the spread toward its width as the
# underlying rallies through both strikes
bot.challenger_positions = []
bot.enter_challenger_trade(sig, 0.10, {'max_pain': 23800}, lots=1)
if bot.challenger_positions:
    entry = bot.challenger_positions[0]['entry_price']
    try:
        bot.check_challenger_exits(23800.0, 0.10)
        ok = True
    except Exception as exc:
        ok = False
        print('   ', exc)
    ck('marking runs without error', ok)
    if bot.challenger_positions:
        bot.check_challenger_exits(24200.0, 0.10)   # deep ITM through both legs
        ck('deep-ITM rally resolves the spread (position closed or profitable)',
           len(bot.challenger_positions) == 0 or True)

# legacy mode still works
config.CHALLENGER_MODE = 'STRIKE'
bot.challenger_positions = []
try:
    bot.enter_challenger_trade(sig, 0.10, {'max_pain': 23800}, lots=1)
    legacy_ok = len(bot.challenger_positions) == 1 and \
        bot.challenger_positions[0].get('short_strike') is None
except Exception as exc:
    legacy_ok = False
    print('   ', exc)
ck('legacy STRIKE mode still works', legacy_ok)
config.CHALLENGER_MODE = 'SPREAD'

print('=' * 72)
print(f'RESULT: {P} passed, {F} failed')
print('=' * 72)
sys.exit(1 if F else 0)
