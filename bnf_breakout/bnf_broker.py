# -*- coding: utf-8 -*-
"""Fyers adapter with a hard API budget. READ-ONLY: it never places an order.

The parked multi-strategy system exhausted Fyers' Standard-plan quota of 5,000
requests/day on Oct 19 (14:43) and Oct 21 (13:05), after which it could not even
price an open position. Every call here goes through one choke point that

  * counts the call against a daily budget persisted to disk (restart-proof),
  * keeps under 5 requests/second,
  * recognises a rate-limit reply by its MEANING -- the original guard tested
    'limit reached' but Fyers says "You've reached the Standard plan limit", so a
    throttle was misread as 'contract not listed' and a wrong answer cached,
  * refuses NON-essential calls past the budget but never refuses an exit.

Essential calls are exits and the position's own mark. Scanning for new entries is
not essential and is the first thing to go.
"""
from __future__ import annotations

import calendar
import json
import os
import time
from datetime import date, datetime

import bnf_config as cfg
import bnf_data

MONTHS = ('JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC')


def is_rate_limited(msg) -> bool:
    """Is this reply a quota/rate limit rather than a missing contract?

    Wide net on purpose: mistaking a missing contract for a throttle costs one
    wasted retry; mistaking a throttle for a missing contract silently empties a
    day. Real Fyers wordings this must catch:
        You've reached the Standard plan limit of 5,000 requests/day
        You've reached the Standard plan limit of 5 requests/second
        request limit reached
    """
    m = str(msg or '').lower()
    if not m:
        return False
    if 'rate limit' in m or 'too many request' in m:
        return True
    return 'limit' in m and ('reached' in m or 'exceed' in m or 'request' in m)


class TokenError(RuntimeError):
    pass


class ApiBudget:
    """Daily call counter persisted to disk, plus a minimum gap between calls."""

    def __init__(self, state_dir: str, max_calls: int, min_gap: float, today: str,
                 sleep=time.sleep, clock=time.monotonic):
        self.path = os.path.join(state_dir, 'api_budget.json')
        self.max_calls, self.min_gap, self.today = max_calls, min_gap, today
        self._sleep, self._clock = sleep, clock
        self._last = 0.0
        self.calls = 0
        self.by_label = {}
        self.blocked_until = 0.0            # wall-clock epoch
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding='utf-8') as fh:
                d = json.load(fh)
            if d.get('date') == self.today:
                self.calls = int(d.get('calls', 0))
                self.by_label = dict(d.get('by_label', {}))
                self.blocked_until = float(d.get('blocked_until', 0))
        except Exception:
            pass

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(dict(date=self.today, calls=self.calls, by_label=self.by_label,
                               blocked_until=self.blocked_until), fh)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def roll(self, day: str):
        """A new trading day: the plan's quota resets, so the counters do too."""
        if day != self.today:
            self.today, self.calls, self.by_label, self.blocked_until = day, 0, {}, 0.0
            self._save()

    def remaining(self) -> int:
        return max(self.max_calls - self.calls, 0)

    def allow(self, essential: bool, now_epoch: float | None = None) -> tuple:
        now_epoch = now_epoch if now_epoch is not None else time.time()
        if now_epoch < self.blocked_until:
            # a daily-cap reply is a fact about the whole account: even an exit
            # cannot succeed, so do not burn requests pretending otherwise
            return False, 'rate_limited'
        if not essential and self.calls >= self.max_calls:
            return False, f'budget {self.calls}/{self.max_calls} spent (non-essential refused)'
        return True, 'ok'

    def pace(self):
        gap = self._clock() - self._last
        if gap < self.min_gap:
            self._sleep(self.min_gap - gap)
        self._last = self._clock()

    def record(self, label: str):
        self.calls += 1
        self.by_label[label] = self.by_label.get(label, 0) + 1
        self._save()

    def block(self, seconds: float, now_epoch: float | None = None):
        self.blocked_until = (now_epoch if now_epoch is not None else time.time()) + seconds
        self._save()


def make_client(c=cfg, today: str | None = None):
    """Build a Fyers client from the token the shared auth job writes each morning."""
    import sys
    try:
        with open(c.TOKEN_FILE, encoding='utf-8') as fh:
            lines = fh.read().strip().split('\n')
    except Exception as e:
        raise TokenError(f'cannot read token file {c.TOKEN_FILE}: {e}')
    if len(lines) < 2:
        raise TokenError('token file malformed (expected token + timestamp)')
    today = today or datetime.now(bnf_data.IST).strftime('%Y-%m-%d')
    if lines[1][:10] != today:
        raise TokenError(f'token is dated {lines[1][:10]}, not today ({today}); the morning auth job has not run')
    if c.LEGACY_DIR not in sys.path:
        sys.path.insert(0, c.LEGACY_DIR)
    from fyers_auth import FyersAuth            # shared infra: owns the app id
    a = FyersAuth()
    a.access_token = lines[0]
    return a.get_fyers_client()


def last_tuesday(year: int, month: int) -> date:
    d = date(year, month, calendar.monthrange(year, month)[1])
    return d.replace(day=d.day - ((d.weekday() - cfg.EXPIRY_WEEKDAY) % 7))


def expiry_candidates(today: date, c=cfg) -> list:
    """The next few monthly expiries with enough days left, nearest first.

    A monthly option symbol encodes the MONTH ('26OCT55800CE'), not the day. So
    when an expiry that lands on a holiday is moved to the preceding day, the
    symbol is unchanged -- offering that day as a separate candidate would only
    probe identical symbols and then report the wrong expiry date.
    """
    out = []
    y, m = today.year, today.month
    for _ in range(4):
        e = last_tuesday(y, m)
        if (e - today).days >= c.MIN_DAYS_TO_EXPIRY:
            out.append(e)
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out[:3]


def option_symbol(strike: int, direction: int, expiry: date, c=cfg) -> str:
    return f"{c.OPTION_PREFIX}{expiry:%y}{MONTHS[expiry.month - 1]}{strike}{'CE' if direction > 0 else 'PE'}"


def atm_strike(spot: float, c=cfg) -> int:
    return int(round(spot / c.STRIKE_GAP) * c.STRIKE_GAP)


class Broker:
    def __init__(self, fyers, budget: ApiBudget, logger=None, c=cfg):
        self.f, self.budget, self.log, self.c = fyers, budget, logger, c
        self._expiry = None                       # (date, expiry) cache

    def _say(self, level: str, msg: str):
        if self.log:
            getattr(self.log, level)(msg)

    def _call(self, label: str, fn, essential: bool = False):
        """The ONLY route to the API. Returns (response|None, status)."""
        ok, why = self.budget.allow(essential)
        if not ok:
            return None, why
        self.budget.pace()
        try:
            resp = fn()
        except Exception as e:
            self.budget.record(label)
            self._say('error', f'[API] {label}: exception {e}')
            return None, f'exception: {e}'
        self.budget.record(label)
        if isinstance(resp, dict) and resp.get('s') != 'ok':
            msg = resp.get('message') or resp.get('errmsg') or ''
            if resp.get('code') == 429 or resp.get('code') == -360 or is_rate_limited(msg):
                secs = self.c.RATE_LIMIT_BACKOFF_SECONDS if 'day' in str(msg).lower() or resp.get('code') == -360 else 5
                self.budget.block(secs)
                self._say('warning', f'[API] {label}: RATE LIMITED ({str(msg)[:90]}) - backing off {secs}s')
                return None, 'rate_limited'
            return resp, f'error: {msg}'
        return resp, 'ok'

    # -- market data --------------------------------------------------------
    def get_bars(self, today: str, now: datetime, essential: bool = False):
        resp, st = self._call('bars', lambda: self.f.history(dict(
            symbol=self.c.INDEX_SYMBOL, resolution='5', date_format='1',
            range_from=today, range_to=today, cont_flag='1')), essential)
        if resp is None or st != 'ok':
            return None, st
        return bnf_data.candles_to_bars(resp.get('candles'), now, self.c.BAR_MINUTES), 'ok'

    def get_day(self, day: str):
        """One complete past session as a history-shaped dict, or None.

        The nightly collector runs after the close; on a day the API quota is
        already spent it fails, and the 'previous session' the strategy reads
        would silently be two sessions old. The runner calls this to fill such a
        hole itself: one request per missing day.
        """
        resp, st = self._call('day', lambda: self.f.history(dict(
            symbol=self.c.INDEX_SYMBOL, resolution='5', date_format='1',
            range_from=day, range_to=day, cont_flag='1')), essential=False)
        if resp is None or st != 'ok':
            return None
        y, m, d = (int(x) for x in day.split('-'))
        end_of_day = datetime(y, m, d, 23, 59, tzinfo=bnf_data.IST)
        b = bnf_data.candles_to_bars(resp.get('candles'), end_of_day, self.c.BAR_MINUTES)
        if len(b['T']) < 60 or not bnf_data.bars_are_contiguous(b['T'], self.c.BAR_MINUTES):
            return None
        b['day'] = day
        return b

    def quote(self, symbol: str, essential: bool = False):
        resp, st = self._call('quote', lambda: self.f.quotes(dict(symbols=symbol)), essential)
        if resp is None or st != 'ok':
            return None, st
        try:
            v = resp['d'][0]['v']
        except Exception:
            return None, 'malformed quote'
        ltp = next((float(v[k]) for k in ('lp', 'ltp', 'last_price') if v.get(k) is not None), None)
        if not ltp or ltp <= 0:
            return None, 'no price'
        bid, ask = v.get('bid'), v.get('ask')
        return dict(ltp=ltp,
                    bid=float(bid) if bid not in (None, 0, '0') else None,
                    ask=float(ask) if ask not in (None, 0, '0') else None), 'ok'

    def resolve_option(self, spot: float, direction: int, today: date):
        """Find a listed contract near the money. Probes a NEIGHBOURHOOD of strikes
        for each candidate expiry, because one dead strike is not a dead expiry
        (BANKNIFTY's only listed expiry quoted at 1 of 3 strikes on Oct 15), but a
        rate limit is never read as 'unlisted'."""
        atm = atm_strike(spot, self.c)
        order = [self._expiry[1]] if self._expiry and self._expiry[0] == today else expiry_candidates(today, self.c)
        for exp in order:
            for k in (0, 1, -1):
                sym = option_symbol(atm + k * self.c.STRIKE_GAP, direction, exp, self.c)
                q, st = self.quote(sym, essential=True)
                if st == 'rate_limited':
                    return None, 'rate_limited'
                if q:
                    self._expiry = (today, exp)
                    return dict(symbol=sym, strike=atm + k * self.c.STRIKE_GAP, expiry=exp, quote=q), 'ok'
        return None, 'no listed contract found'
