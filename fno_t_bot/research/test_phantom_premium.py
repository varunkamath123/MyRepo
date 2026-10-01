# -*- coding: utf-8 -*-
"""Unit-test the phantom SHORT-premium book.

The module's whole value is that its numbers are trustworthy, so these tests
target the arithmetic and the guards rather than anything market-dependent:
leg construction, credit, the real-quote refusal, the exit rules, and -- most
importantly -- that loss is actually bounded by the wings. A short-premium
structure whose loss is NOT bounded is not the thing we claim to be measuring.

Quotes are stubbed, never Black-Scholes: production requires four real LTPs,
and a test that silently accepted modelled prices would not be testing the
guard that matters.
"""
import io, json, glob, os, sys, logging, tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
import phantom_premium as PP

P = F = 0


def ck(name, cond, detail=''):
    global P, F
    if cond:
        P += 1
        print(f'  PASS  {name}')
    else:
        F += 1
        print(f'  FAIL  {name}  {detail}')


# tempfile.gettempdir() rather than $TEMP: the latter is Windows-only, so on
# EC2 it fell back to '.' -- which ec2-user cannot write -- and the suite died
# before asserting anything. Tests have to run where the bot runs.
TMP = os.path.join(tempfile.gettempdir(), 'pprem_test')
os.makedirs(TMP, exist_ok=True)
for f in glob.glob(os.path.join(TMP, '*.jsonl')):
    os.remove(f)
config.LOG_DIRECTORY = TMP
config.PHANTOM_PREMIUM_ENABLED = True
config.PHANTOM_PREM_WING_GAPS = 2
config.PHANTOM_PREM_LOTS = 1
config.PHANTOM_PREM_STOP = 1.00
config.PHANTOM_PREM_TARGET = 0.50
config.PHANTOM_PREM_TIME = '00:00'          # always "open" during a test run
config.FORCE_CLOSE_TIME = '23:59'


class FakeBot:
    """Minimal stand-in: just the attributes phantom_premium touches."""

    def __init__(self, quotes=None, gap=50, lot=75, inst='NIFTY'):
        self.instrument = inst
        self.strike_gap = gap
        self.lot_size = lot
        self.fyers = None
        self.logger = logging.getLogger('pprem_test')
        self.logger.addHandler(logging.NullHandler())
        self._pprem_open = None
        self._pprem_done = False
        self.quotes = quotes or {}
        self.asked = []

    def _quote_option(self, strike, opt_type, underlying, hv):
        self.asked.append((strike, opt_type))
        v = self.quotes.get((strike, opt_type))
        if v is None:
            return 0.0, None, 'BS'          # no real quote available
        return float(v), f'SYM{strike}{opt_type}', 'LTP'


def fly(atm=22500, gap=50, w=2, straddle=200.0, wing=50.0):
    """Quotes for a symmetric fly: ATM legs `straddle`/2 each, wings `wing`/2."""
    return {
        (atm, 'CALL'): straddle / 2, (atm, 'PUT'): straddle / 2,
        (atm + w * gap, 'CALL'): wing / 2, (atm - w * gap, 'PUT'): wing / 2,
    }


def rows(inst='NIFTY'):
    out = []
    for f in glob.glob(os.path.join(TMP, f'phantom_premium_{inst}_*.jsonl')):
        for ln in io.open(f, encoding='utf-8'):
            if ln.strip():
                out.append(json.loads(ln))
    return out


print('\n--- leg construction & credit ---')
b = FakeBot(fly())
PP.open_book(b, 22510.0, 12.0)
ck('opens a structure', b._pprem_open is not None)
if b._pprem_open:
    p = b._pprem_open
    ck('ATM snapped to nearest strike', p['atm'] == 22500, p['atm'])
    ck('wing width = gaps x strike_gap', p['width'] == 100, p['width'])
    ck('short legs at ATM',
       p['strikes']['short_CALL'] == 22500 and p['strikes']['short_PUT'] == 22500)
    ck('long legs at ATM +/- width',
       p['strikes']['long_CALL'] == 22600 and p['strikes']['long_PUT'] == 22400,
       p['strikes'])
    ck('credit = straddle - wings = 200 - 50 = 150',
       abs(p['credit'] - 150.0) < 1e-6, p['credit'])
    ck('qty = lot_size x lots', p['qty'] == 75, p['qty'])
    ck('max loss = (width - credit) x qty',
       abs(p['max_loss'] - (100 - 150) * 75) < 1e-6, p['max_loss'])
ck('quoted exactly 4 legs', len(b.asked) == 4, b.asked)

print('\n--- guards ---')
b = FakeBot(fly()); config.PHANTOM_PREMIUM_ENABLED = False
PP.open_book(b, 22510.0, 12.0)
ck('respects PHANTOM_PREMIUM_ENABLED=False', b._pprem_open is None)
config.PHANTOM_PREMIUM_ENABLED = True

q = fly(); q.pop((22600, 'CALL'))                 # one leg unquotable
b = FakeBot(q)
PP.open_book(b, 22510.0, 12.0)
ck('refuses when ANY leg lacks a real LTP', b._pprem_open is None)
ck('marks the day attempted so it does not retry forever', b._pprem_done is True)

b = FakeBot(fly(straddle=40.0, wing=100.0))       # wings dearer than straddle
PP.open_book(b, 22510.0, 12.0)
ck('refuses non-positive credit', b._pprem_open is None)

config.PHANTOM_PREM_TIME = '23:58'
b = FakeBot(fly())
PP.open_book(b, 22510.0, 12.0)
ck('does not open before PHANTOM_PREM_TIME', b._pprem_open is None)
ck('and does NOT burn the attempt', b._pprem_done is False)
config.PHANTOM_PREM_TIME = '00:00'

b = FakeBot(fly())
PP.open_book(b, 22510.0, 12.0)
first = b._pprem_open
PP.open_book(b, 22510.0, 12.0)
ck('opens at most once per session', b._pprem_open is first)

print('\n--- exits ---')
# Target: structure decays to half the credit -> +50% of credit
b = FakeBot(fly()); PP.open_book(b, 22510.0, 12.0)
b.quotes = fly(straddle=100.0, wing=50.0)         # close cost = 100 - 50 = 50
PP.mark(b, 22505.0, 12.0)
r = rows()
ck('target fires at >= 50% of credit', len(r) == 1 and 'Target' in r[0]['exit_reason'],
   r[0]['exit_reason'] if r else 'no row')
if r:
    ck('exit_cost = shorts - wings', abs(r[0]['exit_cost'] - 50.0) < 1e-6,
       r[0]['exit_cost'])
    ck('pnl_unit = credit - exit_cost', abs(r[0]['pnl_unit'] - (150 - 50)) < 1e-6,
       r[0]['pnl_unit'])
    ck('P&L is positive when the index sits still', r[0]['pnl_net'] > 0, r[0]['pnl_net'])
    ck('breakeven_pct recorded', r[0]['breakeven_pct'] > 0)
ck('position cleared after close', b._pprem_open is None)

for f in glob.glob(os.path.join(TMP, '*.jsonl')):
    os.remove(f)

# Stop: structure doubles against us -> -100% of credit
b = FakeBot(fly()); PP.open_book(b, 22510.0, 12.0)
b.quotes = fly(straddle=350.0, wing=50.0)         # cost to close now 325
PP.mark(b, 22700.0, 12.0)
r = rows()
ck('stop fires at <= -100% of credit', len(r) == 1 and 'Stop' in r[0]['exit_reason'],
   r[0]['exit_reason'] if r else 'no row')
if r:
    ck('loss recorded as negative', r[0]['pnl_net'] < 0, r[0]['pnl_net'])

for f in glob.glob(os.path.join(TMP, '*.jsonl')):
    os.remove(f)

print('\n--- the property that matters: loss is bounded ---')
# Index blows far through the upper wing. Intrinsic cost caps at the width.
b = FakeBot(fly()); PP.open_book(b, 22510.0, 12.0)
b.quotes = {}                                     # quotes vanish
PP.mark(b, 29000.0, 12.0, force_close=True)       # index 6,500 pts away
r = rows()
ck('settles at force-close even with no quotes', len(r) == 1)
if r:
    ck('px_src flags the intrinsic settle', r[0]['px_src'] == 'intrinsic-settle',
       r[0]['px_src'])
    ck('exit cost capped at the wing width (not 6,500)',
       abs(r[0]['exit_cost'] - 100) < 1e-6, r[0]['exit_cost'])
    worst = (100 - 150) * 75                      # (width - credit) x qty
    ck('loss never exceeds the defined maximum',
       r[0]['pnl_net'] >= worst - 1e-6, f"{r[0]['pnl_net']} vs floor {worst}")

for f in glob.glob(os.path.join(TMP, '*.jsonl')):
    os.remove(f)

print('\n--- holding while unquotable, and reset ---')
b = FakeBot(fly()); PP.open_book(b, 22510.0, 12.0)
b.quotes = {}
PP.mark(b, 22505.0, 12.0)                         # not forced
ck('holds (does not fabricate) when quotes drop mid-session',
   b._pprem_open is not None and not rows())

PP.reset_day(b)
ck('reset_day clears the open structure', b._pprem_open is None)
ck('reset_day re-arms the daily attempt', b._pprem_done is False)

print(f'\n{P} passed, {F} failed')
sys.exit(1 if F else 0)
