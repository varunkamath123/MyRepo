# -*- coding: utf-8 -*-
"""BANKNIFTY breakout strategy: the live PAPER runner.

    python bnf_runner.py            # run (loops forever; systemd restarts it)
    python bnf_runner.py --status   # print the last heartbeat and budget
    python bnf_runner.py --preflight

PAPER ONLY. There is no order endpoint anywhere in this package. It reads prices
and records what it would have done, filling at the ask / bid (or LTP +/- a
slippage) so the paper result is not flattered.

WHAT IT COSTS IN API REQUESTS, because the parked system died of this:
    one bars call per CLOSED 5-minute bar            ~75/day
    one option quote per EXIT_POLL_SECONDS, ONLY while a position is open
    a handful to resolve the contract at entry
    expected ~300-900/day against a 5,000 plan limit, hard-capped at
    MAX_API_CALLS_PER_DAY with exits exempt from the cap.

RESTART SAFETY. The open position, the day's counters and the last bar processed
are persisted after every change, so a crash mid-trade resumes managing it.

Every decision, including every "no", is logged with the state of each gate so a
poor result can be attributed to the config or to the thesis.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta

import numpy as np

import bnf_broker
import bnf_config as cfg
import bnf_data
import bnf_strategy as S

IST = bnf_data.IST


def _jsonable(o):
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if hasattr(o, 'item'):
        try:
            return o.item()
        except Exception:
            pass
    if hasattr(o, 'isoformat'):
        return o.isoformat()
    return o


def _atomic_write(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(_jsonable(obj), fh)
    os.replace(tmp, path)


class Runner:
    def __init__(self, broker, c=cfg, clock=None, sleep=time.sleep, history=None,
                 state_dir=None, log_dir=None, logger=None):
        self.b, self.c, self.sleep = broker, c, sleep
        self.clock = clock or (lambda: datetime.now(IST))
        self.state_dir = state_dir or c.STATE_DIR
        self.log_dir = log_dir or os.path.join(self.state_dir, 'logs')
        self.log = logger or logging.getLogger('bnf')
        self._history = history                    # injected in tests
        self.ctx = None
        self.day = None
        self.state = None
        self.mark = None                           # last option mark (price, pct)
        self._last_note = ''

    # ── persistence ───────────────────────────────────────────────────────
    @property
    def _state_path(self):
        return os.path.join(self.state_dir, 'state.json')

    def _fresh(self, day: str) -> dict:
        return dict(date=day, trades_today=0, last_loss_bar=None, last_bar_idx=-1,
                    position=None, loop_errors=0)

    def _load_state(self, day: str) -> dict:
        try:
            with open(self._state_path, encoding='utf-8') as fh:
                st = json.load(fh)
            if st.get('date') == day:
                return st
            if st.get('position'):
                self.log.error(f"[STATE] a position survived from {st.get('date')} "
                               f"({st['position'].get('symbol')}); it should have been force-closed. "
                               f"Recording it as abandoned.")
                self._append(f'bnf_trades_{st.get("date")}.jsonl',
                             dict(kind='abandoned', symbol=st['position'].get('symbol'),
                                  note='carried across a day boundary; not priced', pnl_net=0.0))
        except Exception:
            pass
        return self._fresh(day)

    def _save(self):
        _atomic_write(self._state_path, self.state)

    def _append(self, name: str, rec: dict):
        os.makedirs(self.log_dir, exist_ok=True)
        with open(os.path.join(self.log_dir, name), 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(_jsonable(rec)) + '\n')

    def _trade_pnls(self) -> list:
        out = []
        for f in sorted(glob.glob(os.path.join(self.log_dir, 'bnf_trades_*.jsonl'))):
            with open(f, encoding='utf-8', errors='ignore') as fh:
                for ln in fh:
                    try:
                        d = json.loads(ln)
                    except Exception:
                        continue
                    if d.get('pnl_net') is not None and d.get('kind') != 'abandoned':
                        out.append(float(d['pnl_net']))
        return out

    # ── day setup ─────────────────────────────────────────────────────────
    def _load_day(self, day: str):
        sessions = self._history if self._history is not None else \
            bnf_data.load_history(self.c.HISTORY_DIR, before=day, last_n=90)
        self.ctx = S.build_day_context(sessions, n_bars=75, cfg=self.c)
        self.day = day
        self.state = self._load_state(day)
        self._save()
        hot = self.ctx.get('hot')
        self.log.info(f"[DAY] {day}: {len(sessions)} prior sessions, PDH={self.ctx['pdh']} "
                      f"PDL={self.ctx['pdl']} regime={'hot' if hot else 'cold' if hot is False else 'unknown'}")

    def preflight(self) -> list:
        """(check, ok, detail). A failing check means do NOT trade today."""
        out = []
        probs = self.c.validate()
        out.append(('config', not probs, '; '.join(probs) or 'coherent'))
        sessions = self._history if self._history is not None else \
            bnf_data.load_history(self.c.HISTORY_DIR, last_n=90)
        ok = len(sessions) >= 25
        out.append(('history', ok, f'{len(sessions)} sessions, last '
                    f"{sessions[-1]['day'] if sessions else 'none'}"))
        ctx = S.build_day_context(sessions, 75, self.c) if sessions else {}
        out.append(('regime', ctx.get('hot') is not None,
                    f"hot={ctx.get('hot')} (unknown means <21 sessions)"))
        out.append(('volume baseline', bool(ctx) and bool((~np.isnan(ctx['vbase'])).any()),
                    'volume baseline built' if ctx else 'no history'))
        return out

    # ── time helpers ──────────────────────────────────────────────────────
    def _market_open(self, now: datetime) -> bool:
        if now.weekday() >= 5:
            return False
        try:
            if self.c.LEGACY_DIR not in sys.path:
                sys.path.insert(0, self.c.LEGACY_DIR)
            import nse_holidays
            if nse_holidays.is_nse_holiday(now.date()):
                return False
        except Exception:
            pass
        hhmm = now.strftime('%H:%M')
        return '09:10' <= hhmm <= '15:35'

    def _seconds_to_next_bar(self, now: datetime) -> float:
        m = self.c.BAR_MINUTES
        nxt = now.replace(second=0, microsecond=0)
        nxt += timedelta(minutes=m - (nxt.minute % m))
        return max((nxt - now).total_seconds() + 4.0, 1.0)

    # ── paper fills ───────────────────────────────────────────────────────
    def _buy_fill(self, q: dict) -> float:
        return q['ask'] if q.get('ask') else q['ltp'] * (1 + self.c.SLIPPAGE_PCT)

    def _sell_fill(self, q: dict) -> float:
        return q['bid'] if q.get('bid') else q['ltp'] * (1 - self.c.SLIPPAGE_PCT)

    # ── one iteration ─────────────────────────────────────────────────────
    def step(self) -> float:
        """Do one unit of work; return how many seconds to sleep.

        The heartbeat is written here, unconditionally, in a finally block. It used
        to be written from individual branches, and the entry path was not one of
        them: the status file went quiet at exactly the moment a position opened.
        """
        now = self.clock()
        try:
            return self._step(now)
        finally:
            self._status(now, self._last_note)

    def _step(self, now: datetime) -> float:
        day = now.strftime('%Y-%m-%d')
        if self.day != day:
            self._load_day(day)
            self.b.budget.roll(day)
        if not self._market_open(now):
            self._last_note = 'market closed'
            return min(900.0, 60.0 if now.strftime('%H:%M') > '15:35' else 300.0)

        pos = self.state.get('position')
        if pos:
            return self._manage(now, pos)
        return self._scan(now)

    # ── flat: look for a setup ────────────────────────────────────────────
    def _scan(self, now: datetime) -> float:
        wait = self._seconds_to_next_bar(now)
        hhmm = now.strftime('%H:%M')
        if hhmm < self.c.ENTRY_START or hhmm > S._hhmm_add(self.c.ENTRY_END, self.c.BAR_MINUTES):
            self._last_note = 'outside entry window'
            return min(wait, 300.0)
        bars, st = self.b.get_bars(self.day, now)
        if bars is None:
            self.log.warning(f'[SCAN] no bars: {st}')
            self._last_note = f'no bars: {st}'
            return 60.0 if st != 'rate_limited' else 300.0
        T = bars['T']
        if not T or not bnf_data.bars_are_contiguous(T, self.c.BAR_MINUTES):
            self.log.warning(f'[SCAN] bars not contiguous / empty ({len(T)} bars) - skipping this bar')
            self._last_note = 'bars not contiguous'
            return min(wait, 60.0)
        i = len(T) - 1
        if i <= self.state['last_bar_idx']:
            return wait                                   # nothing new yet
        self.state['last_bar_idx'] = i
        self._save()

        d = S.evaluate_entry(bars, i, self.ctx, dict(
            trades_today=self.state['trades_today'], open_positions=0,
            last_loss_bar=self.state.get('last_loss_bar')), self.c)
        self._append(f'bnf_decisions_{self.day}.jsonl', dict(
            ts=now.isoformat(timespec='seconds'), bar=i, t=T[i], close=float(bars['C'][i]),
            enter=d.enter, blocked_by=d.blocked_by(),
            gates={k: [bool(ok), det] for k, (ok, det) in d.gates.items()}))
        if not d.enter:
            self._last_note = f"no setup ({','.join(d.blocked_by())})"
            return wait

        self.log.info(f"[SETUP] bar {i} {T[i]} direction={'CALL' if d.direction > 0 else 'PUT'} "
                      f"level={d.thesis['level']} gates={ {k: v[1] for k, v in d.gates.items()} }")
        return self._enter(now, bars, i, d, wait)

    def _enter(self, now, bars, i, d, wait) -> float:
        spot = float(bars['C'][i])
        res, st = self.b.resolve_option(spot, d.direction, now.date())
        if res is None:
            self.log.warning(f'[ENTRY] could not resolve a contract: {st}')
            return 300.0 if st == 'rate_limited' else wait
        q = res['quote']
        fill = self._buy_fill(q)
        bs = S.book_state(self._trade_pnls(), self.c)
        sz = S.size_lots(fill, bs, self.c)
        if sz['lots'] < 1:
            self.log.warning(f"[ENTRY] skipped: {sz['why']}")
            self._append(f'bnf_decisions_{self.day}.jsonl', dict(
                ts=now.isoformat(timespec='seconds'), bar=i, note='sized to zero', why=sz['why']))
            return wait
        pos = S.new_position(d, fill, sz['lots'])
        pos.update(symbol=res['symbol'], strike=res['strike'], expiry=str(res['expiry']),
                   opt='CE' if d.direction > 0 else 'PE', entry_time=now.isoformat(timespec='seconds'),
                   entry_quote=q, sizing=sz['why'], book_before=bs['book'],
                   entry_gates={k: [bool(ok), det] for k, (ok, det) in d.gates.items()},
                   prem_best=0.0, prem_worst=0.0, polls=0)
        self.state['position'] = pos
        self.state['trades_today'] += 1
        self._save()
        self.log.info(f"[ENTRY] {pos['opt']} {res['symbol']} {sz['lots']} lot(s) @ {fill:.2f} "
                      f"(ltp {q['ltp']}, ask {q.get('ask')}) | {sz['why']}")
        self._last_note = f"entered {pos['symbol']} x{sz['lots']}"
        return float(self.c.EXIT_POLL_SECONDS)

    # ── in a position ─────────────────────────────────────────────────────
    def _manage(self, now: datetime, pos: dict) -> float:
        hhmm = now.strftime('%H:%M')
        if hhmm >= self.c.FORCE_CLOSE:
            return self._exit(now, pos, dict(kind='force_close', reason=f'end of day {self.c.FORCE_CLOSE}'))

        # 1. mark the premium (essential: this is the risk control)
        q, st = self.b.quote(pos['symbol'], essential=True)
        if q is None:
            self.log.warning(f"[MANAGE] cannot price {pos['symbol']}: {st}")
            self._last_note = f'position UNPRICED: {st}'
            return 30.0 if st != 'rate_limited' else 120.0
        mark = self._sell_fill(q)
        pct = (mark - pos['entry_premium']) / pos['entry_premium']
        pos['polls'] += 1
        pos['prem_best'] = max(pos['prem_best'], pct)
        pos['prem_worst'] = min(pos['prem_worst'], pct)
        self.mark = (mark, pct)
        self._append(f'bnf_path_{self.day}.jsonl', dict(
            ts=now.isoformat(timespec='seconds'), symbol=pos['symbol'], ltp=q['ltp'],
            bid=q.get('bid'), ask=q.get('ask'), mark=round(mark, 2), pct=round(pct * 100, 3)))
        dec = S.check_premium(pos, pct, self.c)
        if dec:
            return self._exit(now, pos, dec, q)

        # 2. any newly CLOSED bar -> thesis / trail checks
        bars, st = (self.b.get_bars(self.day, now, essential=True)
                    if self._new_bar_due(now) else (None, 'not due'))
        if bars is not None and bnf_data.bars_are_contiguous(bars['T'], self.c.BAR_MINUTES):
            last = len(bars['T']) - 1
            for idx in range(max(self.state['last_bar_idx'], pos['entry_bar']) + 1, last + 1):
                view = {k: (v[:idx + 1] if k != 'T' else v[:idx + 1]) for k, v in bars.items()}
                dec = S.on_bar_exit(pos, view, idx, self.c, replay=False)
                self.state['last_bar_idx'] = idx
                if dec:
                    return self._exit(now, pos, dec, q)
            self._save()
        else:
            self._save()
        self._last_note = f"in {pos['symbol']} {pct * 100:+.1f}%"
        return float(self.c.EXIT_POLL_SECONDS)

    def _new_bar_due(self, now: datetime) -> bool:
        """Has a bar closed since the last one we processed? Bar i (opening at
        09:15 + 5i min) is closed 2 seconds after 09:15 + 5(i+1) min."""
        secs = (now.hour * 3600 + now.minute * 60 + now.second) - (9 * 3600 + 15 * 60) - 2
        closed_idx = secs // (self.c.BAR_MINUTES * 60) - 1
        return closed_idx > self.state['last_bar_idx']

    def _exit(self, now, pos, dec, q=None) -> float:
        if q is None:
            q, st = self.b.quote(pos['symbol'], essential=True)
        priced = q is not None
        if priced:
            fill = self._sell_fill(q)
        elif self.mark:
            fill = self.mark[0]                      # last known mark, flagged below
            self.log.error(f"[EXIT] could not price {pos['symbol']}; using the last mark {fill:.2f}")
        else:
            self.log.error('[EXIT] no price and no mark; will retry')
            return 30.0
        qty = pos['lots'] * self.c.LOT_SIZE
        gross = (fill - pos['entry_premium']) * qty
        cost = S.round_trip_cost(pos['entry_premium'], fill, pos['lots'], self.c)
        net = gross - cost
        held = (now - datetime.fromisoformat(pos['entry_time'])).total_seconds() / 60
        rec = dict(
            kind=dec['kind'], reason=dec['reason'], exit_priced=priced,
            symbol=pos['symbol'], opt=pos['opt'], strike=pos['strike'], expiry=pos['expiry'],
            lots=pos['lots'], entry_time=pos['entry_time'], exit_time=now.isoformat(timespec='seconds'),
            held_min=round(held, 1), entry_premium=round(pos['entry_premium'], 2),
            exit_premium=round(fill, 2), pnl_pct=round((fill / pos['entry_premium'] - 1) * 100, 2),
            max_pnl_pct=round(pos['prem_best'] * 100, 2), min_pnl_pct=round(pos['prem_worst'] * 100, 2),
            index_best_fav=round(pos['best_fav'], 3), index_worst_adv=round(pos['worst_adv'], 3),
            gross=round(gross, 2), costs=round(cost, 2), pnl_net=round(net, 2),
            thesis=pos['thesis'], entry_gates=pos.get('entry_gates'), sizing=pos.get('sizing'),
            book_before=pos.get('book_before'), polls=pos['polls'],
            api_calls_today=getattr(getattr(self.b, 'budget', None), 'calls', None))
        self._append(f'bnf_trades_{self.day}.jsonl', rec)
        if net < 0:
            self.state['last_loss_bar'] = self.state['last_bar_idx']
        self.state['position'] = None
        self.mark = None
        self._save()
        self.log.info(f"[EXIT] {dec['kind']}: {dec['reason']} | {pos['symbol']} {fill:.2f} "
                      f"({rec['pnl_pct']:+.1f}%)  net Rs{net:+,.0f}")
        self._last_note = f"exited: {dec['kind']}"
        return 5.0

    # ── heartbeat ─────────────────────────────────────────────────────────
    def _status(self, now, note: str):
        if self.state is None:
            return
        try:
            bb = getattr(self.b, 'budget', None)
            _atomic_write(os.path.join(self.state_dir, 'status.json'), dict(
                ts=now.isoformat(timespec='seconds'), day=self.day, note=note,
                trades_today=self.state.get('trades_today') if self.state else None,
                in_position=bool(self.state and self.state.get('position')),
                api_calls=getattr(bb, 'calls', None), api_budget=self.c.MAX_API_CALLS_PER_DAY,
                api_by_label=getattr(bb, 'by_label', None)))
        except Exception:
            pass


def _setup_logging(log_dir: str):
    os.makedirs(log_dir, exist_ok=True)
    lg = logging.getLogger('bnf')
    lg.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
    fh = logging.FileHandler(os.path.join(log_dir, f"bnf_{datetime.now(IST):%Y%m%d}.log"), encoding='utf-8')
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    lg.handlers[:] = [fh, sh]
    return lg


def main(argv: list) -> int:
    state_dir = cfg.STATE_DIR
    if '--status' in argv:
        for f in ('status.json', 'api_budget.json'):
            try:
                print(f, open(os.path.join(state_dir, f)).read())
            except Exception as e:
                print(f, 'unreadable:', e)
        return 0
    log = _setup_logging(os.path.join(state_dir, 'logs'))
    probs = cfg.validate()
    if probs:
        log.error(f'CONFIG INVALID: {probs}')
        return 2
    log.info('BANKNIFTY breakout runner starting (PAPER ONLY, no order path exists)')

    fyers, budget = None, None
    while fyers is None:
        try:
            fyers = bnf_broker.make_client()
        except bnf_broker.TokenError as e:
            log.error(f'[AUTH] {e} - retrying in 10 minutes')
            time.sleep(600)
    today = datetime.now(IST).strftime('%Y-%m-%d')
    budget = bnf_broker.ApiBudget(state_dir, cfg.MAX_API_CALLS_PER_DAY, cfg.API_MIN_GAP_SECONDS, today)
    runner = Runner(bnf_broker.Broker(fyers, budget, log), logger=log)

    checks = runner.preflight()
    for name, ok, detail in checks:
        (log.info if ok else log.error)(f"[PREFLIGHT] {'OK  ' if ok else 'FAIL'} {name}: {detail}")
    if '--preflight' in argv:
        return 0 if all(ok for _, ok, _ in checks) else 2
    hard = [n for n, ok, _ in checks if not ok and n in ('config', 'history')]
    if hard:
        log.error(f'[PREFLIGHT] refusing to trade: {hard}')
        return 2

    while True:
        try:
            secs = runner.step()
        except Exception:
            log.exception('[LOOP] unhandled error')
            secs = 30.0
        time.sleep(max(secs, 1.0))


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
