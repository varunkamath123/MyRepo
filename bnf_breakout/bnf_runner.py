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
import logging.handlers     # at MODULE level: inside a function it rebinds `logging` as a local
import os
import sys
import time
from datetime import date, datetime, timedelta

import numpy as np

import bnf_broker
import bnf_chain
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
                 state_dir=None, log_dir=None, logger=None, is_holiday=None,
                 reload_client=None, chain=None, pcr_hist=None, chain_log=None):
        self.b, self.c, self.sleep = broker, c, sleep
        self.clock = clock or (lambda: datetime.now(IST))
        self.state_dir = state_dir or c.STATE_DIR
        self.log_dir = log_dir or os.path.join(self.state_dir, 'logs')
        self.log = logger or logging.getLogger('bnf')
        self._history = history                    # injected in tests
        self._holiday_fn = is_holiday                # injected in tests; None = NSE calendar
        self._reload = reload_client                 # builds a fresh Fyers client from today's token
        self._client_day = None
        self.chain = chain                           # ChainRecorder the OI gate reads, or None
        self.chain_log = chain_log                   # a second recorder that only LOGS (NSE: has IV), or None
        self.pcr_hist = pcr_hist                     # PcrHistory or None
        self._last_spot = None
        self.ctx = None
        self.day = None
        self.state = None
        self.mark = None                           # last option mark (price, pct)
        self._last_note = ''
        self._ctx_tried = None

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
    def _is_holiday(self, d: date) -> bool:
        """One source of truth for "is the exchange shut", used both to decide which
        past sessions are genuinely missing and whether the market is open today.
        Injectable so tests do not depend on which calendar the host happens to have
        (a test that passed on one machine and failed on another is how this was
        found: the host knew Oct 20 2026 was a holiday, the laptop did not)."""
        if self._holiday_fn is not None:
            return bool(self._holiday_fn(d))
        try:
            if self.c.LEGACY_DIR not in sys.path:
                sys.path.insert(0, self.c.LEGACY_DIR)
            import nse_holidays
            return bool(nse_holidays.is_nse_holiday(d))
        except Exception:
            return False


    def _missing_days(self, sessions: list, today: str) -> list:
        """Trading days between the last stored session and today with no file."""
        if not sessions:
            return []
        d = date.fromisoformat(sessions[-1]['day']) + timedelta(days=1)
        end = date.fromisoformat(today)
        out = []
        while d < end:
            if d.weekday() < 5 and not self._is_holiday(d):
                out.append(d.isoformat())
            d += timedelta(days=1)
        return out

    def _build_ctx(self, day: str) -> dict:
        """Build the day context from history, repairing any missing sessions.

        The nightly collector runs after the close. On a day the API quota is
        already spent it fails, and the 'previous session' read from the last file
        would silently be two sessions old: wrong PDH/PDL, wrong volatility regime,
        and no error anywhere. (Oct 21 2026 is exactly that day: the quota ran out
        at 13:05 and the 15:33 backfill never happened.) So: find the holes, fill
        them with one API request per missing day, and if that fails mark the
        context stale so entries are refused instead of mis-set.
        """
        sessions = list(self._history if self._history is not None else
                        bnf_data.load_history(self.c.HISTORY_DIR, before=day, last_n=90))
        stale, reason = False, ''
        if not sessions:
            stale, reason = True, 'no history'
        else:
            missing = self._missing_days(sessions, day)
            if len(missing) > 4:
                stale, reason = True, f'{len(missing)} sessions missing from history'
            else:
                for m in missing:
                    sess = self.b.get_day(m) if self.b else None
                    if sess is None:
                        stale, reason = True, f'session {m} missing and could not be fetched'
                        break
                    sessions.append(sess)
                    self.log.info(f'[DAY] filled missing session {m} from the API ({len(sess["T"])} bars)')
        ctx = S.build_day_context(sessions, n_bars=75, cfg=self.c)
        ctx['stale'], ctx['stale_reason'] = stale, reason
        ctx['n_sessions'] = len(sessions)
        if stale:
            self.log.error(f'[DAY] context is STALE: {reason} - no entries until it is repaired')
        return ctx

    def _load_day(self, day: str):
        self.ctx = self._build_ctx(day)
        self._ctx_tried = self.clock()
        self.day = day
        self.state = self._load_state(day)
        self._save()
        hot = self.ctx.get('hot')
        self.log.info(f"[DAY] {day}: {self.ctx['n_sessions']} prior sessions, PDH={self.ctx['pdh']} "
                      f"PDL={self.ctx['pdl']} regime={'hot' if hot else 'cold' if hot is False else 'unknown'}"
                      f"{' STALE' if self.ctx.get('stale') else ''}")

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
        today = self.clock().strftime('%Y-%m-%d')
        miss = self._missing_days(sessions, today) if sessions else []
        out.append(('history recency', len(miss) <= 4,
                    f"{len(miss)} trading day(s) missing before {today}: {miss[:4]} "
                    f"({'the runner fetches these itself, 1 API request each' if miss else 'none'})"))
        ctx = S.build_day_context(sessions, 75, self.c) if sessions else {}
        out.append(('regime', ctx.get('hot') is not None,
                    f"hot={ctx.get('hot')} (unknown means <21 sessions)"))
        out.append(('volume baseline', bool(ctx) and bool((~np.isnan(ctx['vbase'])).any()),
                    'volume baseline built' if ctx else 'no history'))
        return out

    # ── time helpers ──────────────────────────────────────────────────────
    def _market_open(self, now: datetime) -> bool:
        if now.weekday() >= 5 or self._is_holiday(now.date()):
            return False
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

        # Recorders that do not use the Fyers client (NSE's public page) start here, whether or not a
        # client exists. Each starts a background fetch if this slot is unrecorded and returns at once,
        # so none can delay the loop below.
        for rec in (self.chain, self.chain_log):
            if rec is not None and not getattr(rec, 'needs_client', False):
                try:
                    rec.trigger(now, self._last_spot)
                except Exception:
                    pass

        if not self._ensure_client(now, day):
            return 120.0

        if self.chain is not None and getattr(self.chain, 'needs_client', False):
            try:
                self.chain.trigger(now, self._last_spot)     # the Fyers chain: needs the client built just above
            except Exception:
                pass

        pos = self.state.get('position')
        if pos:
            return self._manage(now, pos)
        return self._scan(now)

    def _ensure_client(self, now: datetime, day: str) -> bool:
        """A client built at start-up holds the token of the day the service started.
        The shared auth job writes a new one every morning, so rebuild on the first
        market-open step of each day, and again after any auth reply. Without a
        client nothing is traded and nothing is priced."""
        if self._reload is None:
            return True
        if getattr(self.b, 'auth_failed', False):
            self._client_day = None
            self.b.auth_failed = False
        if self._client_day == day and self.b.f is not None:
            return True
        try:
            self.b.f = self._reload()
            self._client_day = day
            self.log.info('[AUTH] Fyers client (re)built from the current token')
            return True
        except Exception as e:
            self.log.error(f'[AUTH] cannot build a client yet: {e}')
            self._last_note = f'no client: {e}'
            return False

    # ── flat: look for a setup ────────────────────────────────────────────
    def _scan(self, now: datetime) -> float:
        wait = self._seconds_to_next_bar(now)
        hhmm = now.strftime('%H:%M')
        if self.ctx.get('stale') and (now - self._ctx_tried).total_seconds() >= 600:
            self.ctx = self._build_ctx(self.day)       # may have been a rate limit
            self._ctx_tried = now
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
        self._last_spot = float(bars['C'][i])
        if i <= self.state['last_bar_idx']:
            return wait                                   # nothing new yet
        self.state['last_bar_idx'] = i
        self._save()

        oi = self._oi_state(now, float(bars['C'][i]))
        d = S.evaluate_entry(bars, i, self.ctx, dict(
            trades_today=self.state['trades_today'], open_positions=0,
            last_loss_bar=self.state.get('last_loss_bar'), oi=oi), self.c)
        rec = dict(
            ts=now.isoformat(timespec='seconds'), bar=i, t=T[i], close=float(bars['C'][i]),
            enter=d.enter, blocked_by=d.blocked_by(),
            gates={k: [bool(ok), det] for k, (ok, det) in d.gates.items()}, oi=oi,
            oi_would_block=bool(d.oi_would_block))
        if not d.enter and d.thesis:
            # blocked ONLY by the OI gate: record what it would have done, so the gate can be
            # judged against the trades it stopped (research/oi_gate_shadow.py)
            rec['shadow'] = dict(direction=int(d.direction), level=d.thesis.get('level'),
                                 level_value=d.thesis.get('level_value'),
                                 invalidation=d.thesis.get('invalidation'),
                                 coil_hi=d.thesis.get('coil_hi'), coil_lo=d.thesis.get('coil_lo'),
                                 atr=d.thesis.get('atr'), entry_px=d.thesis.get('entry_px'))
        self._append(f'bnf_decisions_{self.day}.jsonl', rec)
        if not d.enter:
            self._last_note = f"no setup ({','.join(d.blocked_by())})"
            return wait

        self.log.info(f"[SETUP] bar {i} {T[i]} direction={'CALL' if d.direction > 0 else 'PUT'} "
                      f"level={d.thesis['level']} gates={ {k: v[1] for k, v in d.gates.items()} }")
        return self._enter(now, bars, i, d, wait)

    def _oi_state(self, now: datetime, spot: float):
        """The OI/PCR features for this bar, or None if there is no usable snapshot."""
        if not getattr(self.c, 'OI_GATE_ENABLED', False) or self.chain is None:
            return None
        try:
            snap = self.chain.snapshot(now)
            if not snap or snap.get('age_min') is None or snap['age_min'] > self.c.OI_MAX_SNAPSHOT_AGE_MIN:
                return None
            hhmm = str(snap['ts'])[11:16]
            prior = (self.pcr_hist.prior(self.day, hhmm, self.c.OI_PCR_HISTORY_SESSIONS)
                     if self.pcr_hist is not None else [])
            return S.oi_features(snap, spot, prior, self.c)
        except Exception as e:
            self.log.warning(f'[OI] could not build OI features: {e}')
            return None

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
                chain=(self.chain.status() if self.chain is not None else None),
                chain_log=(self.chain_log.status() if self.chain_log is not None else None),
                api_by_label=getattr(bb, 'by_label', None)))
        except Exception:
            pass


def _setup_logging(log_dir: str):
    os.makedirs(log_dir, exist_ok=True)
    lg = logging.getLogger('bnf')
    lg.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
    # Daily rotation. It used to be FileHandler(f"bnf_{start date}.log"): the name was fixed
    # when the SERVICE started, so a long-running process wrote every later day into a file
    # named for the day it was launched (Oct 22's lines sat in bnf_20261021.log).
    fh = logging.handlers.TimedRotatingFileHandler(os.path.join(log_dir, 'bnf.log'), when='midnight',
                                                   backupCount=90, encoding='utf-8')
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

    if '--preflight' in argv:
        # A preflight must never need the API or a fresh token: it checks only what
        # can be checked locally, so it can be run at any hour without side effects.
        checks = Runner(None).preflight()
        for name, ok, detail in checks:
            (log.info if ok else log.error)(f"[PREFLIGHT] {'OK  ' if ok else 'FAIL'} {name}: {detail}")
        return 0 if all(ok for _, ok, _ in checks) else 2

    try:
        fyers = bnf_broker.make_client()
    except bnf_broker.TokenError as e:
        # not fatal: the runner rebuilds the client when the market opens, by which
        # time the morning auth job has written today's token
        log.warning(f'[AUTH] no usable token yet ({e}); will retry when the market opens')
        fyers = None
    today = datetime.now(IST).strftime('%Y-%m-%d')
    budget = bnf_broker.ApiBudget(state_dir, cfg.MAX_API_CALLS_PER_DAY, cfg.API_MIN_GAP_SECONDS, today)
    broker = bnf_broker.Broker(fyers, budget, log)
    logs = os.path.join(state_dir, 'logs')
    nse = bnf_chain.ChainRecorder(logs, logger=log) if cfg.CHAIN_RECORD_ENABLED else None
    if cfg.CHAIN_RECORD_ENABLED and cfg.OI_SOURCE == 'fyers':
        # the gate reads the broker's chain for the series the bot trades; NSE is still logged (it has IV)
        chain = bnf_chain.ChainRecorder(logs, loader=bnf_chain.FyersChainSource(lambda: broker), logger=log,
                                        prefix='bnf_chainf', needs_client=True)
        chain_log = nse
        pcr_hist = bnf_chain.ChainFilePcrHistory(logs, 'bnf_chainf')
    else:
        chain, chain_log = nse, None
        pcr_hist = bnf_chain.PcrHistory(cfg.CHAIN_LEGACY_HISTORY_FILE, cfg.INSTRUMENT)
    runner = Runner(broker, logger=log, reload_client=bnf_broker.make_client,
                    chain=chain, pcr_hist=pcr_hist, chain_log=chain_log)

    checks = runner.preflight()
    for name, ok, detail in checks:
        (log.info if ok else log.error)(f"[PREFLIGHT] {'OK  ' if ok else 'FAIL'} {name}: {detail}")
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
