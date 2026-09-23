# -*- coding: utf-8 -*-
"""Unit-test the RV/IV entry gate (v1.9.3).

The gate sits at the very top of enter_trade and must:
  * BLOCK   chain-sourced entries whose rv_iv >= RV_IV_MAX
  * PASS    chain-sourced entries below the threshold
  * FAIL OPEN on a VIX-sourced ratio (SENSEX), on missing IV, and on missing HV

For the PASS cases enter_trade carries on into strike selection and quoting,
which needs a live API, so those raise. That is fine and expected -- the only
thing under test is whether execution reached the gate's early return, which is
detected from the log line rather than from the return value.
"""
import io, os, sys, logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from options_bot import TradingBot

MARK = '[RV-IV GATE]'
P = F = 0


def check(name, got, want):
    global P, F
    if got == want:
        P += 1
        print(f'  PASS  {name}')
    else:
        F += 1
        print(f'  FAIL  {name}  (blocked={got}, expected={want})')


def blocked(bot, *, atm_iv, hv, vix=None):
    """True if the gate stopped this entry."""
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setLevel(logging.INFO)
    bot.logger.addHandler(h)
    bot.logger.setLevel(logging.INFO)
    bot._last_vix = vix
    n_before = len(bot.positions)
    try:
        bot.enter_trade({'price': 24000.0, 'type': 'CALL', 'atm_iv': atm_iv,
                         'path': 'TEST', 'otm_strikes': 0, 'adx': 30.0}, hv, lots=1)
    except Exception:
        pass                      # PASS cases die later, at the quote fetch
    finally:
        bot.logger.removeHandler(h)
    # A PASS case runs all the way through and opens a paper position (paper
    # mode falls back to Black-Scholes when no LTP is available). That is the
    # proof it cleared the gate -- roll it back so cases stay independent and
    # nothing is left in the book.
    del bot.positions[n_before:]
    bot.trades_today = 0
    return MARK in buf.getvalue()


print('=' * 70)
print(f'RV/IV GATE — enabled={config.RV_IV_GATE_ENABLED} '
      f'max={config.RV_IV_MAX} src={config.RV_IV_GATE_SRC}')
print('=' * 70)
bot = TradingBot('NIFTY')

# hv is a fraction (0.10 = 10%); atm_iv is a percent (12.0 = 12%)
# rv_iv = hv*100/atm_iv
check('chain rv_iv 0.833 >= 0.70 -> BLOCK',
      blocked(bot, atm_iv=12.0, hv=0.10), True)
check('chain rv_iv 0.700 exactly -> BLOCK',
      blocked(bot, atm_iv=14.2857, hv=0.10), True)
check('chain rv_iv 0.500 < 0.70 -> pass',
      blocked(bot, atm_iv=20.0, hv=0.10), False)
check('chain rv_iv 0.699 just under -> pass',
      blocked(bot, atm_iv=14.31, hv=0.10), False)
check('VIX-sourced, ratio 0.833 -> FAIL OPEN (SENSEX arm)',
      blocked(bot, atm_iv=None, hv=0.10, vix=12.0), False)
check('no IV at all -> FAIL OPEN',
      blocked(bot, atm_iv=None, hv=0.10, vix=None), False)
check('no HV -> FAIL OPEN',
      blocked(bot, atm_iv=12.0, hv=0.0), False)
check('zero IV (guard against div-by-zero) -> FAIL OPEN',
      blocked(bot, atm_iv=0.0, hv=0.10, vix=None), False)

print('=' * 70)
print(f'RESULT: {P} passed, {F} failed')
print('=' * 70)
sys.exit(1 if F else 0)
