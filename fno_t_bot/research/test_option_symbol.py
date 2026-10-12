# -*- coding: utf-8 -*-
"""Regression test for the Oct/Nov/Dec weekly option symbol month code.

From Sep 24 2026 every NIFTY and SENSEX weekly quote silently failed and fell
back to Black-Scholes, because build_option_symbol mapped October to 'A'
(continuing the alphabet past 9) rather than 'O' (the month's first letter).
BS pricing carries a measured 41-123% error in this project, so several days of
P&L -- real trades and counterfactual phantoms alike -- were fiction.

The failure was silent, which is what made it expensive. These tests pin the
format so it cannot recur, including in November and December.
"""
import os, sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fyers_orders import build_option_symbol, alt_month_symbol

P = F = 0


def ck(name, got, want):
    global P, F
    if got == want:
        P += 1
        print(f'  PASS  {name}')
    else:
        F += 1
        print(f'  FAIL  {name}\n          got  {got}\n          want {want}')



# --- STRIKE WALK (Sep 28 2026) -------------------------------------------
# A contract that will not quote is usually a one-strike problem, not a reason
# to fabricate a Black-Scholes price or drop the trade. resolve_quotable_option
# walks outward from the ideal strike; a dead NEIGHBOURHOOD means the expiry is
# wrong, which is a different bug and must be reported differently.
from fyers_orders import resolve_quotable_option
import config as _cfg


class _FakeFyers:
    """Quotes only the strikes it was told about."""
    def __init__(self, live):
        self.live = set(live)
        self.asked = []

    def quotes(self, req):
        sym = req['symbols']
        self.asked.append(sym)
        if sym in self.live:
            return {'s': 'ok', 'd': [{'v': {'lp': 123.45}}]}
        return {'s': 'ok', 'd': [{'v': {}}]}        # valid transport, no price


_exp = date(2026, 10, 6)
_ideal = 22850

# exact strike available -> take it, zero steps
_f = _FakeFyers([build_option_symbol('NIFTY', 22850, 'PUT', _exp)])
_ltp, _sym, _k, _step = resolve_quotable_option(_f, 'NIFTY', _ideal, 'PUT', _exp)
ck('exact strike quotes -> used, 0 steps', (_k, _step), (22850, 0))

# exact dead, one strike up alive -> walk one
_f = _FakeFyers([build_option_symbol('NIFTY', 22900, 'PUT', _exp)])
_ltp, _sym, _k, _step = resolve_quotable_option(_f, 'NIFTY', _ideal, 'PUT', _exp)
ck('dead strike -> walks to the next one', (_k, _step), (22900, 1))

# whole neighbourhood dead -> no price, and the caller can tell
_f = _FakeFyers([])
_ltp, _sym, _k, _tried = resolve_quotable_option(_f, 'NIFTY', _ideal, 'PUT', _exp)
ck('dead neighbourhood -> no price returned', _ltp, None)
ck('dead neighbourhood -> tried 5 strikes (expiry is the suspect)',
   len(_tried), 5)

print('=' * 74)
print('OPTION SYMBOL — Oct/Nov/Dec weekly month code')
print('=' * 74)

# Known-good: these exact strings returned real LTPs in the logs, so they are
# ground truth for the format, not a guess.
ck('Sep weekly unchanged (this shape resolved live)',
   build_option_symbol('NIFTY', 23800, 'CALL', date(2026, 9, 8)),
   'NSE:NIFTY2690823800CE')
ck('monthly format unchanged (this shape resolved live)',
   build_option_symbol('NIFTY', 23200, 'CALL', date(2026, 9, 24)),
   'NSE:NIFTY26SEP23200CE')

# The bug and its siblings
ck('October weekly uses O, not A',
   build_option_symbol('NIFTY', 22850, 'PUT', date(2026, 10, 6)),
   'NSE:NIFTY26O0622850PE')
ck('October weekly SENSEX uses O',
   build_option_symbol('SENSEX', 73000, 'CALL', date(2026, 10, 1)),
   'BSE:SENSEX26O0173000CE')
ck('November weekly uses N, not B',
   build_option_symbol('NIFTY', 22850, 'PUT', date(2026, 11, 3)),
   'NSE:NIFTY26N0322850PE')
ck('December weekly uses D, not C',
   build_option_symbol('NIFTY', 22850, 'PUT', date(2026, 12, 1)),
   'NSE:NIFTY26D0122850PE')

# no A/B/C may survive anywhere in a weekly symbol
for m, d in ((10, 6), (11, 3), (12, 1)):
    sym = build_option_symbol('NIFTY', 22850, 'PUT', date(2026, m, d))
    body = sym.split('NIFTY')[1]
    ck(f'month {m}: no stale A/B/C in {sym}',
       any(ch in body[2:3] for ch in 'OND'), True)

# the self-verifying alternate
ck('alternate flips O back to A',
   alt_month_symbol('NSE:NIFTY26O0622850PE'), 'NSE:NIFTY26A0622850PE')
ck('alternate flips N back to B',
   alt_month_symbol('NSE:NIFTY26N0322850PE'), 'NSE:NIFTY26B0322850PE')
ck('alternate flips D back to C',
   alt_month_symbol('NSE:NIFTY26D0122850PE'), 'NSE:NIFTY26C0122850PE')
ck('alternate is None for a digit month (nothing to flip)',
   alt_month_symbol('NSE:NIFTY2690823800CE'), None)
ck('alternate is None for a monthly symbol',
   alt_month_symbol('NSE:BANKNIFTY26OCT22850PE'), None)

print('=' * 74)
print('\n--- weekly expiry must never land on an UNLISTED contract (Oct 12 2026) ---')
# SEBI lists only ONE weekly per index. Walking forward to the NEXT target
# weekday when the nearest is inside MIN_DAYS_TO_EXPIRY produced a symbol that
# does not exist: on Mon Oct 12 2026 it asked for the Oct 20 NIFTY weekly,
# which returned no quote, so NIFTY could not trade at all and the premium
# archive wrote 74 snapshots with zero legs.
import datetime as _dt
from unittest import mock as _mock


def _expiry_on(day, instrument='NIFTY', min_days=2):
    """get_next_expiry as if today were `day`."""
    import fyers_orders as _fo
    import config
    _old = config.MIN_DAYS_TO_EXPIRY
    config.MIN_DAYS_TO_EXPIRY = min_days

    class _FakeDT(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _dt.datetime(day.year, day.month, day.day, 10, 0,
                                tzinfo=getattr(_fo, 'IST', None))
    try:
        with _mock.patch.object(_fo, 'datetime', _FakeDT):
            return _fo.get_next_expiry(instrument)
    finally:
        config.MIN_DAYS_TO_EXPIRY = _old


# NIFTY expiry weekday is Tuesday.
mon = _dt.date(2026, 10, 12)      # Monday, expiry Tue Oct 13 -> only 1 DTE
e = _expiry_on(mon)
ck('Monday before expiry does NOT return the unlisted Oct 20 weekly',
   e == _dt.date(2026, 10, 20), False)
ck('it returns the Oct 27 MONTHLY instead', e, _dt.date(2026, 10, 27))
ck('and that expiry clears MIN_DAYS_TO_EXPIRY', (e - mon).days >= 2, True)

wed = _dt.date(2026, 10, 7)       # Wednesday, next Tue Oct 13 is 6 days out
e2 = _expiry_on(wed)
ck('a normal day still takes the nearest weekly', e2, _dt.date(2026, 10, 13))

tue = _dt.date(2026, 10, 13)      # expiry day itself -> 0 DTE
e3 = _expiry_on(tue)
ck('expiry day itself never returns today', e3 > tue, True)
ck('expiry day does not return the unlisted next weekly',
   e3 == _dt.date(2026, 10, 20), False)

e4 = _expiry_on(_dt.date(2026, 10, 12), min_days=1)
ck('with min_days=1 the nearest weekly IS usable', e4, _dt.date(2026, 10, 13))


print(f'RESULT: {P} passed, {F} failed')
print('=' * 74)
sys.exit(1 if F else 0)
