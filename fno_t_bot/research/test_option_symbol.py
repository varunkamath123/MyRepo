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
print('\n--- expiry: date math PROPOSES, the broker DISPOSES ---')
# Two failures in four days, with OPPOSITE causes, both from trusting the
# calendar:
#
#   Oct 12  SEBI lists one weekly per index at a time. Walking forward past a
#           near weekly asked for an Oct 20 NIFTY contract that did not exist,
#           so NIFTY could not trade and the archive wrote 74 empty snapshots.
#           The fix was to fall back to the monthly.
#   Oct 15  That fix caused its own bug. Today IS SENSEX's expiry weekday, so
#           the nearest target weekday was today (0 DTE, inside min_days) and
#           the new code jumped straight to the Oct 29 monthly -- SKIPPING the
#           Oct 22 weekly, which was listed and quoting at 708.10. SENSEX
#           recorded a 14-DTE contract for three sessions when a 7-DTE one was
#           available: different theta, different delta, different economics.
#           Meanwhile NIFTY had NO October weekly after Oct 13 at all, while
#           Nov 3 quoted fine.
#
# No calendar rule survives both. So get_next_expiry is now honest date math
# (the forward walk) and nothing more, and get_quotable_expiry ASKS THE BROKER
# which candidate is listed. These tests pin that division of labour.
import datetime as _dt
from unittest import mock as _mock
import fyers_orders as _fo


def _at(day, fn, instrument='NIFTY', min_days=2):
    """Run fn() as if today were `day`."""
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
            return fn()
    finally:
        config.MIN_DAYS_TO_EXPIRY = _old


def _expiry_on(day, instrument='NIFTY', min_days=2):
    return _at(day, lambda: _fo.get_next_expiry(instrument), instrument, min_days)


# -- the forward walk, restored ----------------------------------------------
mon = _dt.date(2026, 10, 12)          # Mon; NIFTY expiry Tue Oct 13 is 1 DTE
ck('date math walks to the NEXT weekly, it does not skip to the monthly',
   _expiry_on(mon), _dt.date(2026, 10, 20))

wed = _dt.date(2026, 10, 7)
ck('a normal day takes the nearest weekly', _expiry_on(wed), _dt.date(2026, 10, 13))

tue = _dt.date(2026, 10, 13)          # expiry day itself, 0 DTE
ck('expiry day never returns today', _expiry_on(tue) > tue, True)

ck('with min_days=1 the nearest weekly IS usable',
   _expiry_on(mon, min_days=1), _dt.date(2026, 10, 13))

# THE Oct 15 REGRESSION. Thu Oct 15 is SENSEX's own expiry weekday.
thu = _dt.date(2026, 10, 15)
_sx = _expiry_on(thu, instrument='SENSEX')
ck('SENSEX on its expiry weekday takes the NEXT weekly (Oct 22)',
   _sx, _dt.date(2026, 10, 22))
ck('and NOT the Oct 29 monthly, which skipped a listed contract',
   _sx == _dt.date(2026, 10, 29), False)
ck('every expiry returned clears MIN_DAYS_TO_EXPIRY',
   all((_expiry_on(thu, instrument=i) - thu).days >= 2
       for i in ('NIFTY', 'BANKNIFTY', 'SENSEX')), True)

# -- candidate generation ----------------------------------------------------
_cands = _at(mon, lambda: _fo.expiry_candidates('NIFTY'))
ck('candidates are sorted and unique', _cands == sorted(set(_cands)), True)
ck('candidates start at what date math would pick', _cands[0], _expiry_on(mon))
ck('candidates all clear min_days', all((c - mon).days >= 2 for c in _cands), True)
ck('candidates include the monthly as well as weeklies',
   _dt.date(2026, 10, 27) in _cands, True)
ck('candidates reach past the dead October window (Nov 3)',
   _dt.date(2026, 11, 3) in _cands, True)


# -- validation against a broker ---------------------------------------------
def _quotable(day, listed, instrument='NIFTY', px=23000.0, logger=None):
    _fo._QUOTABLE_EXPIRY_CACHE.clear()
    f = _FakeFyers(listed)
    out = _at(day, lambda: _fo.get_quotable_expiry(f, instrument, px, logger),
              instrument)
    return out, f


# the real Oct 15 NIFTY tape: no October weekly quotes, the monthly does
_oct27 = _fo.build_option_symbol('NIFTY', 23000, 'PUT', _dt.date(2026, 10, 27))
e, f = _quotable(thu, [_oct27])
ck('skips unlisted weeklies and returns the expiry that QUOTES',
   e, _dt.date(2026, 10, 27))
ck('it actually probed the broker', len(f.asked) > 1, True)

# nearest is listed -> taken immediately, no wasted probes
_oct20 = _fo.build_option_symbol('NIFTY', 23000, 'PUT', _dt.date(2026, 10, 20))
e, f = _quotable(mon, [_oct20, _oct27])
ck('when the nearest IS listed it is taken', e, _dt.date(2026, 10, 20))
ck('and only one probe is spent', len(f.asked), 1)


# nothing quotes -> fall back, loudly, rather than returning None
class _Rec:
    def __init__(self):
        self.msgs = []

    def info(self, m):
        self.msgs.append(('info', m))

    def error(self, m):
        self.msgs.append(('error', m))

    def warning(self, m):
        self.msgs.append(('warn', m))


_lg = _Rec()
e, f = _quotable(thu, [], logger=_lg)
ck('a totally dead chain still returns a date, not None', e, _expiry_on(thu))
ck('and it is reported at ERROR, not swallowed',
   any(lv == 'error' for lv, _ in _lg.msgs), True)

_lg = _Rec()
_quotable(thu, [_oct27], logger=_lg)
ck('a skipped expiry is logged at INFO',
   any(lv == 'info' and 'not listed' in m for lv, m in _lg.msgs), True)

_lg = _Rec()
_quotable(mon, [_oct20, _oct27], logger=_lg)
ck('taking the nearest logs nothing (no noise on the happy path)',
   _lg.msgs, [])

# -- the fallback that keeps backtests identical -----------------------------
_fo._QUOTABLE_EXPIRY_CACHE.clear()
ck('no broker session -> identical to get_next_expiry',
   _at(mon, lambda: _fo.get_quotable_expiry(None, 'NIFTY', 23000.0)),
   _expiry_on(mon))
for _i in ('NIFTY', 'BANKNIFTY', 'SENSEX'):
    _fo._QUOTABLE_EXPIRY_CACHE.clear()
    ck(f'{_i}: no-broker fallback matches date math',
       _at(thu, lambda i=_i: _fo.get_quotable_expiry(None, i, 23000.0), _i),
       _expiry_on(thu, instrument=_i))


class _Boom:
    def quotes(self, req):
        raise RuntimeError('broker down')


_fo._QUOTABLE_EXPIRY_CACHE.clear()
ck('a broker that raises is survivable',
   _at(thu, lambda: _fo.get_quotable_expiry(_Boom(), 'NIFTY', 23000.0)),
   _expiry_on(thu))

# -- caching: this costs quotes, so it must be paid once per session ---------
_fo._QUOTABLE_EXPIRY_CACHE.clear()
f = _FakeFyers([_oct27])
_first = _at(thu, lambda: _fo.get_quotable_expiry(f, 'NIFTY', 23000.0))
_n = len(f.asked)
_again = _at(thu, lambda: _fo.get_quotable_expiry(f, 'NIFTY', 23000.0))
ck('second call in the same session is cached', (_again, len(f.asked)), (_first, _n))
_at(thu, lambda: _fo.get_quotable_expiry(f, 'SENSEX', 85000.0), 'SENSEX')
ck('the cache is per instrument, not global', len(f.asked) > _n, True)
_n2 = len(f.asked)
_at(_dt.date(2026, 10, 16), lambda: _fo.get_quotable_expiry(f, 'NIFTY', 23000.0))
ck('and per day, so a new session re-probes', len(f.asked) > _n2, True)
_fo._QUOTABLE_EXPIRY_CACHE.clear()



print('\n--- probing must not waste quota or misread a rate limit ---')
# Both defects below were found by running the validator against the live
# broker, not by a test, which is why they are pinned here now.


class _Quota:
    """Counts quotes and answers 429 after a budget, like the real broker."""

    def __init__(self, listed, budget=99):
        self.listed = set(listed)
        self.budget = budget
        self.asked = []

    def quotes(self, req):
        sym = req['symbols']
        self.asked.append(sym)
        if len(self.asked) > self.budget:
            return {'s': 'error', 'code': 429, 'message': 'request limit reached'}
        if sym in self.listed:
            return {'s': 'ok', 'd': [{'v': {'lp': 123.45}}]}
        return {'s': 'ok', 'd': [{'v': {}}]}


# 1. monthly_expiry_only must keep weeklies out of the candidate list
_bnf = _at(thu, lambda: _fo.expiry_candidates('BANKNIFTY', limit=4), 'BANKNIFTY')
ck('BANKNIFTY candidates contain no weeklies',
   all(c.day >= 22 for c in _bnf), True)
ck('BANKNIFTY candidates start at the monthly date math picks',
   _bnf[0], _expiry_on(thu, instrument='BANKNIFTY'))
ck('BANKNIFTY does NOT offer the Oct 20 weekly it cannot trade',
   _dt.date(2026, 10, 20) in _bnf, False)
_nf = _at(thu, lambda: _fo.expiry_candidates('NIFTY', limit=4))
ck('NIFTY, which DOES have weeklies, still gets them',
   _dt.date(2026, 10, 20) in _nf, True)

# 2. probe budget: one bare quote per candidate, list kept short
_fo._QUOTABLE_EXPIRY_CACHE.clear()
_q = _Quota([_oct27])
_at(thu, lambda: _fo.get_quotable_expiry(_q, 'NIFTY', 23000.0))
ck('one quote per candidate, no alternate-convention retry',
   len(_q.asked) <= 4, True)
ck('and each probe is a distinct symbol', len(set(_q.asked)), len(_q.asked))
_fo._QUOTABLE_EXPIRY_CACHE.clear()

# 3. a 429 is NOT evidence that a contract is unlisted
_fo._QUOTABLE_EXPIRY_CACHE.clear()
_lg = _Rec()
_q = _Quota([_oct27], budget=0)        # throttled on the very first probe
_e = _at(thu, lambda: _fo.get_quotable_expiry(_q, 'NIFTY', 23000.0, _lg))
ck('a rate limit falls back to date math', _e, _expiry_on(thu))
ck('and is reported as a WARNING, not an ERROR',
   ([lv for lv, _ in _lg.msgs], any('rate-limited' in m for _, m in _lg.msgs)),
   (['warn'], True))
ck('it must NOT claim "data will be EMPTY" — nothing was learned',
   any('EMPTY' in m for _, m in _lg.msgs), False)
ck('a throttled answer is NOT cached, so the next call re-probes',
   ('NIFTY', thu) in _fo._QUOTABLE_EXPIRY_CACHE, False)

# the re-probe must then succeed once the limit clears
_q2 = _Quota([_oct27])
_e2 = _at(thu, lambda: _fo.get_quotable_expiry(_q2, 'NIFTY', 23000.0))
ck('after re-probing it finds the listed expiry', _e2, _dt.date(2026, 10, 27))
_fo._QUOTABLE_EXPIRY_CACHE.clear()

# 4. a genuinely dead chain still reports ERROR and caches
_lg = _Rec()
_q = _Quota([])
_e = _at(thu, lambda: _fo.get_quotable_expiry(_q, 'NIFTY', 23000.0, _lg))
ck('a genuinely unlisted chain is still an ERROR',
   any(lv == 'error' for lv, _ in _lg.msgs), True)
ck('and that IS cached, since it was actually established',
   ('NIFTY', thu) in _fo._QUOTABLE_EXPIRY_CACHE, True)
_fo._QUOTABLE_EXPIRY_CACHE.clear()

print('\n--- a dead STRIKE must not be mistaken for a dead EXPIRY ---')
# Found live Oct 15 2026: BANKNIFTY's only listed expiry (Oct 27) quoted at
# strike 55800 for 524.50 while 55750 and 55850 both returned nothing. A
# single-strike probe landing one step off would have declared that expiry
# unlisted and pushed the instrument to the Nov 24 monthly. The same run
# showed NIFTY Oct 20 dead across 3 consecutive strikes (0/3), which is what
# a genuinely unlisted expiry looks like.
import config as _cfg

_nstep = int(_cfg.INSTRUMENTS['NIFTY'].get('strike_step', 50) or 50)

# only the strike one step ABOVE the ATM quotes
_up = _fo.build_option_symbol('NIFTY', 23000 + _nstep, 'PUT', _dt.date(2026, 10, 20))
e, f = _quotable(thu, [_up])
ck('an expiry proven by the strike one step UP is accepted',
   e, _dt.date(2026, 10, 20))

# only the strike one step BELOW quotes
_dn = _fo.build_option_symbol('NIFTY', 23000 - _nstep, 'PUT', _dt.date(2026, 10, 20))
e, f = _quotable(thu, [_dn])
ck('an expiry proven by the strike one step DOWN is accepted',
   e, _dt.date(2026, 10, 20))

# the ATM itself still wins immediately, without spending neighbour probes
_atm20 = _fo.build_option_symbol('NIFTY', 23000, 'PUT', _dt.date(2026, 10, 20))
e, f = _quotable(thu, [_atm20])
ck('a quoting ATM short-circuits on the first probe', (e, len(f.asked)),
   (_dt.date(2026, 10, 20), 1))

# a dead NEIGHBOURHOOD is still treated as the wrong expiry — the NIFTY case
e, f = _quotable(thu, [_oct27])
ck('a dead neighbourhood still rolls to the next expiry',
   e, _dt.date(2026, 10, 27))
ck('and it spent neighbour probes to establish that', len(f.asked) >= 3, True)
ck('while staying inside the probe budget', len(f.asked) <= 12, True)

# the throttle check must still win over the neighbourhood walk
_fo._QUOTABLE_EXPIRY_CACHE.clear()
_lg = _Rec()
_q = _Quota([_up], budget=0)
e = _at(thu, lambda: _fo.get_quotable_expiry(_q, 'NIFTY', 23000.0, _lg))
ck('a 429 on the first neighbour aborts rather than walking on',
   (e, len(_q.asked)), (_expiry_on(thu), 1))
ck('and still reports it as unverified, not empty',
   any('rate-limited' in m for _, m in _lg.msgs), True)
_fo._QUOTABLE_EXPIRY_CACHE.clear()

print('\n--- one expiry per instrument per session, used by EVERY path ---')
# Before this, six call sites in options_bot each called get_next_expiry()
# independently. On Oct 15 2026 that meant the premium recorder archived the
# Oct 27 NIFTY contract while enter_trade would have priced and placed the
# Oct 20 one, which the exchange had not listed. The DTE override was the
# dangerous one: it writes config.DAYS_TO_EXPIRY, so it sets T for every
# Black-Scholes price in the process.
import logging as _logging
import pandas as _pd

_logging.disable(_logging.CRITICAL)
try:
    from options_bot import TradingBot
    _b = TradingBot('NIFTY')
finally:
    _logging.disable(_logging.NOTSET)


def _bot_expiry(day, fyers, closes=(23000.0,)):
    _fo._QUOTABLE_EXPIRY_CACHE.clear()
    _b.fyers = fyers
    _b._idx_closes = _pd.Series(list(closes)) if closes is not None else None
    _logging.disable(_logging.CRITICAL)
    try:
        return _at(day, lambda: _b._expiry())
    finally:
        _logging.disable(_logging.NOTSET)
        _fo._QUOTABLE_EXPIRY_CACHE.clear()


ck('no broker session -> plain date math (backtests unchanged)',
   _bot_expiry(thu, None), _expiry_on(thu))
ck('with a broker it VALIDATES and skips the unlisted weeklies',
   _bot_expiry(thu, _FakeFyers([_oct27])), _dt.date(2026, 10, 27))
ck('no usable spot -> date math rather than an exception',
   _bot_expiry(thu, _FakeFyers([_oct27]), closes=None), _expiry_on(thu))
ck('an empty close series is also handled',
   _bot_expiry(thu, _FakeFyers([_oct27]), closes=()), _expiry_on(thu))
ck('a broker that raises -> date math, not a crash',
   _bot_expiry(thu, _Boom()), _expiry_on(thu))
ck('a nonsense spot does not propagate',
   _bot_expiry(thu, _FakeFyers([_oct27]), closes=(0.0,)), _expiry_on(thu))

# THE POINT OF THE HELPER: every consumer must agree.
_fo._QUOTABLE_EXPIRY_CACHE.clear()
_shared = _FakeFyers([_oct27])
_b.fyers = _shared
_b._idx_closes = _pd.Series([23000.0])
_logging.disable(_logging.CRITICAL)
try:
    _via_bot = _at(thu, lambda: _b._expiry())
    _via_rec = _at(thu, lambda: _fo.get_quotable_expiry(_shared, 'NIFTY', 23000.0))
finally:
    _logging.disable(_logging.NOTSET)
ck('the bot and the recorder resolve the SAME contract', _via_bot, _via_rec)
ck('and the DTE that drives every BS price matches it',
   max((_via_bot - thu).days, 1), 12)
ck('which is NOT the 5 DTE raw date math would have claimed',
   max((_expiry_on(thu) - thu).days, 1) == 12, False)
_fo._QUOTABLE_EXPIRY_CACHE.clear()
print(f'RESULT: {P} passed, {F} failed')
print('=' * 74)
sys.exit(1 if F else 0)
