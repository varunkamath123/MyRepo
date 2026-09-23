# -*- coding: utf-8 -*-
"""Unit-test the counterfactual book.

No Fyers session here, so quotes fall back to Black-Scholes. What matters is the
bookkeeping: one phantom per setup, the live exit stack applied unchanged, a row
written on close, and total isolation from the real book.
"""
import glob, io, os, sys, logging
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
import counterfactual as CF
from options_bot import TradingBot, IST

P = F = 0


def ck(name, cond, detail=''):
    global P, F
    if cond:
        P += 1
        print(f'  PASS  {name}')
    else:
        F += 1
        print(f'  FAIL  {name}  {detail}')


TMP = os.path.join(os.environ.get('TEMP', '.'), 'cf_test')
config.LOG_DIRECTORY = TMP
config.COUNTERFACTUAL_ENABLED = True
# record() refuses to track a phantom with no runway left before force-close.
# That is correct in production and makes this test depend on wall-clock time,
# so push the close out for the duration of the run.
_REAL_FC = config.FORCE_CLOSE_TIME
config.FORCE_CLOSE_TIME = '23:59'

print('=' * 72)
print(f'COUNTERFACTUAL — enabled={config.COUNTERFACTUAL_ENABLED}')
print('=' * 72)

bot = TradingBot('NIFTY')
bot.positions = []
CF.reset_day(bot)

CF.record(bot, 'PUT', 23800.0, 'REV_CHASE', 'rev chase 0.99 > 0.4', 0.10)
ck('records a phantom', len(getattr(bot, '_phantoms', [])) == 1,
   str(len(getattr(bot, '_phantoms', []))))
ck('never touches the real book', len(bot.positions) == 0)

# dedupe: the same gate refusing the same setup again must not stack
CF.record(bot, 'PUT', 23790.0, 'REV_CHASE', 'rev chase 0.98 > 0.4', 0.10)
CF.record(bot, 'PUT', 23780.0, 'REV_CHASE', 'rev chase 0.97 > 0.4', 0.10)
ck('dedupes repeats of the same setup', len(bot._phantoms) == 1,
   f'{len(bot._phantoms)} phantoms after 3 identical blocks')

# a different gate, and the other direction, are separate questions
CF.record(bot, 'PUT', 23800.0, 'RV_IV', 'rv_iv 0.9 >= 0.7', 0.10)
CF.record(bot, 'CALL', 23800.0, 'REV_CHASE', 'rev chase 0.99 > 0.4', 0.10)
ck('different gate tracked separately', len(bot._phantoms) == 3,
   str(len(bot._phantoms)))

if bot._phantoms:
    p = bot._phantoms[0]
    ck('strike is ATM for the index', p['strike'] % bot.strike_gap == 0,
       str(p['strike']))
    ck('entry premium is positive', p['entry_price'] > 0, str(p['entry_price']))

# a PUT with the index falling hard should close profitably
bot._phantoms = [q for q in bot._phantoms if q['type'] == 'PUT'
                 and q['gate'] == 'REV_CHASE']
bot._phantoms[0]['entry_time'] = datetime.now(IST) - timedelta(minutes=50)
CF.mark(bot, 23300.0, 0.10)              # index down 500 pts -> PUT deep ITM
ck('profitable phantom closes and clears', len(bot._phantoms) == 0,
   f'{len(bot._phantoms)} still open')

rows = []
for f in glob.glob(os.path.join(TMP, 'counterfactual_NIFTY_*.jsonl')):
    for line in io.open(f, encoding='utf-8'):
        line = line.strip()
        if line:
            import json
            rows.append(json.loads(line))
ck('wrote a closed-phantom row', len(rows) >= 1, str(len(rows)))
if rows:
    r = rows[-1]
    ck('row carries the blocking gate', r.get('gate') == 'REV_CHASE',
       str(r.get('gate')))
    ck('row carries a P&L verdict', 'pnl_net' in r and 'pnl_pct' in r)
    ck('falling index made the PUT profitable', r['pnl_net'] > 0,
       f"pnl_net {r.get('pnl_net')}")
    print(f"    {r['gate']} {r['type']} {r['strike']}: "
          f"Rs{r['entry_price']}->Rs{r['exit_price']} = {r['pnl_pct']:+.1f}% "
          f"(Rs{r['pnl_net']:+,.0f}) via {r['exit_reason']}")

# the disable flag must be absolute
config.COUNTERFACTUAL_ENABLED = False
CF.reset_day(bot)
CF.record(bot, 'PUT', 23800.0, 'RV_IV', 'x', 0.10)
ck('respects the disable flag', len(getattr(bot, '_phantoms', [])) == 0)
config.COUNTERFACTUAL_ENABLED = True

# and it must never raise into the caller, whatever it is handed
try:
    CF.record(bot, None, None, 'BAD', 'x', None)
    CF.mark(bot, None, None)
    ck('swallows bad input instead of breaking the bot', True)
except Exception as exc:
    ck('swallows bad input instead of breaking the bot', False, str(exc))

# the runway guard itself is worth a test, now that we know it bites
config.FORCE_CLOSE_TIME = '00:01'        # everything is 'past the close'
CF.reset_day(bot)
CF.record(bot, 'PUT', 23800.0, 'RV_IV', 'x', 0.10)
ck('no phantom once the runway is gone', len(getattr(bot, '_phantoms', [])) == 0,
   f'{len(getattr(bot, "_phantoms", []))} opened past force-close')
config.FORCE_CLOSE_TIME = _REAL_FC


# --- UNDIRECTED GATES (Sep 23 2026) --------------------------------------
config.FORCE_CLOSE_TIME = '23:59'   # runway guard again; restored below
# VIX-GATE fires BEFORE any signal exists, so there is no direction to
# simulate. Guessing one would invent a counterfactual the bot never formed.
# record_undirected() logs BOTH legs so the analysis can bound the gate.
CF.reset_day(bot)
bot.positions = []
CF.record_undirected(bot, 23800.0, 'VIX_LOW', 'VIX 10.4 < 11', 0.10,
                     extra=dict(vix=10.4))
ck('undirected gate records BOTH legs', len(bot._phantoms) == 2,
   f'{len(bot._phantoms)} phantoms')
if len(bot._phantoms) == 2:
    dirs = sorted(q['type'] for q in bot._phantoms)
    ck('one CALL and one PUT', dirs == ['CALL', 'PUT'], str(dirs))
    ck('both tagged undirected',
       all(q['extra'].get('undirected') for q in bot._phantoms))
    ck('both carry the VIX reading',
       all(q['extra'].get('vix') == 10.4 for q in bot._phantoms))
ck('still never touches the real book', len(bot.positions) == 0)
# and it must dedupe like any other gate -- VIX-GATE fired ~400x on Sep 23
CF.record_undirected(bot, 23790.0, 'VIX_LOW', 'VIX 10.3 < 11', 0.10)
CF.record_undirected(bot, 23780.0, 'VIX_LOW', 'VIX 10.4 < 11', 0.10)
ck('dedupes the ~400 repeats into 2', len(bot._phantoms) == 2,
   f'{len(bot._phantoms)} after 3 calls')
config.FORCE_CLOSE_TIME = _REAL_FC

import shutil
shutil.rmtree(TMP, ignore_errors=True)
print('=' * 72)
print(f'RESULT: {P} passed, {F} failed')
print('=' * 72)
sys.exit(1 if F else 0)
