# -*- coding: utf-8 -*-
"""Shadow arms: forward, virtual "what if" versions of the exit rules, recorded beside the real trade.

ARMS
  StaleShadow       follows every trade the STALE rule closes, as if it had stayed open (all the OTHER
                    rules still apply). Question: is the stale rule saving money or costing it?
  ProfitWaitShadow  follows EVERY entry from the moment it opens. While the position has not yet been
                    profitable (net of costs) the stale, thesis-broken and trail rules are ignored;
                    once it has been, the existing rules apply as normal. Question: does waiting for a
                    profit before exiting beat exiting on those rules at once? (replay: research/
                    profit_wait_study.py)

WHAT THEY ARE. Virtual continuations of paper positions. They place no order (there is no order path
anywhere in this package) and never touch the real position, the real state or the real trade log.
Always in force for a shadow, whatever it is waiting for: the premium hard stop, the target, and the
15:00 force close.

WHAT THEY CANNOT BE TRUSTED TO SAY ALONE. The bot holds one position at a time. If a real trade had been
held longer, a later real entry would not have happened. Each record therefore also lists the real trades
that DID open while the shadow was alive, and their P&L, so the review can compare
  shadow P&L   against   (the real trade + anything entered while the shadow ran).

HOW THEY STAY SAFE.
  * Every call they make is non-essential: past the API budget they are refused first, and a throttle
    reply backs the whole broker off exactly as it would for any non-essential call.
  * Every entry point is wrapped; an exception here is logged and swallowed by the runner.
  * A shadow polls the premium every SHADOW_POLL_SECONDS (60), not every 20 s, and fetches bars only when
    a new bar has closed.
  * Each arm keeps its own state file, separate from the real state.
"""
from __future__ import annotations

import json
import os
import types
from datetime import datetime

import bnf_config as cfg
import bnf_data
import bnf_strategy as S


def _no_stale_cfg(c):
    ns = types.SimpleNamespace(**{k: getattr(c, k) for k in dir(c) if k.isupper()})
    ns.THESIS_CONFIRM_BARS = 10 ** 6
    return ns


class _Arm:
    ARM = ''
    KIND = ''
    STATE = ''

    def __init__(self, broker, c=cfg, state_dir: str | None = None, log_dir: str | None = None,
                 append=None, day_fn=None, logger=None):
        self.b, self.c, self.log = broker, c, logger
        self.state_dir = state_dir or c.STATE_DIR
        self.log_dir = log_dir or os.path.join(self.state_dir, 'logs')
        self._append = append
        self._day = day_fn or (lambda: datetime.now(bnf_data.IST).strftime('%Y-%m-%d'))
        self.open: list = []
        self.day = None
        self.calls = 0
        self.closed_today = 0
        self._warned = 0.0
        self._load()

    # ── persistence ───────────────────────────────────────────────────────
    @property
    def _path(self):
        return os.path.join(self.state_dir, self.STATE)

    def _load(self):
        try:
            with open(self._path, encoding='utf-8') as fh:
                st = json.load(fh)
            self.day, self.open = st.get('day'), st.get('open', [])
        except Exception:
            self.day, self.open = None, []

    def _save(self):
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = self._path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(dict(day=self.day, open=self.open), fh, default=_jsonable)
            os.replace(tmp, self._path)
        except Exception as e:
            self._say('warning', f'[SHADOW:{self.ARM}] could not save state: {e}')

    def _say(self, level, msg):
        if self.log:
            getattr(self.log, level)(msg)

    def active(self) -> bool:
        return bool(self.open)

    def status(self) -> dict:
        return dict(open=len(self.open), closed_today=self.closed_today, calls=self.calls)

    # ── building a shadow ─────────────────────────────────────────────────
    def _new(self, pos: dict, now: datetime, last_bar_idx: int) -> dict:
        self.day = self._day()
        return dict(
            arm=self.ARM, symbol=pos['symbol'], opt=pos['opt'], strike=pos['strike'], expiry=pos['expiry'],
            lots=pos['lots'], entry_time=pos['entry_time'], entry_premium=pos['entry_premium'],
            pos=dict(dir=pos['dir'], entry_bar=pos['entry_bar'], entry_px=pos['entry_px'],
                     entry_premium=pos['entry_premium'], lots=pos['lots'], thesis=dict(pos['thesis']),
                     best_fav=pos['best_fav'], worst_adv=pos['worst_adv'], best_px=pos['best_px'],
                     confirmed=bool(pos.get('confirmed'))),
            last_idx=int(last_bar_idx), prem_best=pos.get('prem_best', 0.0), prem_worst=pos.get('prem_worst', 0.0),
            last_poll=0.0, last_mark=None, polls=0, opened=now.isoformat(timespec='seconds'), real=None)

    # ── hooks the arms fill in ────────────────────────────────────────────
    def _decide(self, sh, view, idx):
        raise NotImplementedError

    def _on_mark(self, sh, mark, pct):
        pass

    def _real_of(self, sh):
        return sh.get('real')

    # ── one pass ──────────────────────────────────────────────────────────
    def step(self, now: datetime):
        if not self.open:
            return
        today = now.strftime('%Y-%m-%d')
        for sh in list(self.open):
            if sh['entry_time'][:10] != today:
                self._abandon(sh, now, 'carried across a day boundary; not priced')
                continue
            try:
                self._work(now, sh)
            except Exception as e:
                import time as _t
                if _t.time() - self._warned > 600:
                    self._warned = _t.time()
                    self._say('warning', f'[SHADOW:{self.ARM}] {sh["symbol"]}: {e}')

    def _work(self, now: datetime, sh: dict):
        hhmm = now.strftime('%H:%M')
        pos = sh['pos']
        if hhmm >= self.c.FORCE_CLOSE:
            return self._close(now, sh, dict(kind='force_close', reason=f'end of day {self.c.FORCE_CLOSE}'))

        epoch = now.timestamp()
        if epoch - sh['last_poll'] >= self.c.SHADOW_POLL_SECONDS:
            sh['last_poll'] = epoch
            self.calls += 1
            q, st = self.b.quote(sh['symbol'], essential=False)
            if q is not None:
                mark = q['bid'] if q.get('bid') else q['ltp'] * (1 - self.c.SLIPPAGE_PCT)
                pct = (mark - sh['entry_premium']) / sh['entry_premium']
                sh['last_mark'] = mark
                sh['polls'] += 1
                sh['prem_best'] = max(sh['prem_best'], pct)
                sh['prem_worst'] = min(sh['prem_worst'], pct)
                self._on_mark(sh, mark, pct)
                if self._append:
                    self._append(f'bnf_shadow_path_{self._day()}.jsonl', dict(
                        ts=now.isoformat(timespec='seconds'), arm=self.ARM, symbol=sh['symbol'], bid=q.get('bid'),
                        ask=q.get('ask'), mark=round(mark, 2), pct=round(pct * 100, 3)))
                dec = S.check_premium(pos, pct, self.c)
                if dec:
                    return self._close(now, sh, dec, q)

        secs = (now.hour * 3600 + now.minute * 60 + now.second) - (9 * 3600 + 15 * 60) - 2
        closed_idx = secs // (self.c.BAR_MINUTES * 60) - 1
        if closed_idx > sh['last_idx']:
            self.calls += 1
            bars, st = self.b.get_bars(self._day(), now, essential=False)
            if bars is not None and bnf_data.bars_are_contiguous(bars['T'], self.c.BAR_MINUTES):
                last = len(bars['T']) - 1
                for idx in range(sh['last_idx'] + 1, last + 1):
                    view = {k: v[:idx + 1] for k, v in bars.items()}
                    dec = self._decide(sh, view, idx)
                    sh['last_idx'] = idx
                    if dec:
                        return self._close(now, sh, dec)
        self._save()

    # ── the real trades alongside ─────────────────────────────────────────
    def _trades_today(self) -> list:
        out = []
        try:
            with open(os.path.join(self.log_dir, f'bnf_trades_{self._day()}.jsonl'), encoding='utf-8') as fh:
                for ln in fh:
                    try:
                        out.append(json.loads(ln))
                    except Exception:
                        continue
        except Exception:
            pass
        return out

    def _overlap(self, after_iso: str, until_iso: str):
        """Real trades that opened while this shadow was alive (they could not have, had the real trade
        been held as the shadow was): count and net P&L."""
        n, pnl = 0, 0.0
        for t in self._trades_today():
            if after_iso < t.get('entry_time', '') <= until_iso:
                n += 1
                pnl += float(t.get('pnl_net', 0.0))
        return n, pnl

    # ── closing ───────────────────────────────────────────────────────────
    def _close(self, now: datetime, sh: dict, dec: dict, q=None):
        late = now.strftime('%H:%M') >= '15:25'
        real = self._real_of(sh)
        if real is None and not late:
            return                                # the real trade has not been recorded yet; compare next pass
        priced = q is not None
        if q is None:
            self.calls += 1
            q, _ = self.b.quote(sh['symbol'], essential=False)
            priced = q is not None
        if priced:
            fill = q['bid'] if q.get('bid') else q['ltp'] * (1 - self.c.SLIPPAGE_PCT)
        elif sh.get('last_mark') and late:
            fill = sh['last_mark']               # out of time: last known mark, flagged unpriced
        else:
            return                                # try again next pass; a shadow close is never urgent
        qty = sh['lots'] * self.c.LOT_SIZE
        gross = (fill - sh['entry_premium']) * qty
        cost = S.round_trip_cost(sh['entry_premium'], fill, sh['lots'], self.c)
        net = gross - cost
        held = (now - datetime.fromisoformat(sh['entry_time'])).total_seconds() / 60
        shadow = dict(exit_time=now.isoformat(timespec='seconds'), kind=dec['kind'], reason=dec['reason'],
                      exit_priced=priced, exit_premium=round(fill, 2),
                      pnl_pct=round((fill / sh['entry_premium'] - 1) * 100, 2),
                      max_pnl_pct=round(sh['prem_best'] * 100, 2), min_pnl_pct=round(sh['prem_worst'] * 100, 2),
                      held_min=round(held, 1), gross=round(gross, 2), costs=round(cost, 2), pnl_net=round(net, 2))
        rec = dict(kind=self.KIND, arm=self.ARM, symbol=sh['symbol'], opt=sh['opt'], strike=sh['strike'],
                   lots=sh['lots'], entry_time=sh['entry_time'], entry_premium=round(sh['entry_premium'], 2),
                   real=real, shadow=shadow)
        if sh.get('profitable') is not None:
            shadow['became_profitable'] = bool(sh.get('profitable'))
        if real is not None:
            n_ov, pnl_ov = self._overlap(real['exit_time'], now.isoformat(timespec='seconds'))
            rec.update(delta_vs_real=round(net - real['pnl_net'], 2),
                       real_trades_during_shadow=n_ov, real_pnl_during_shadow=round(pnl_ov, 2),
                       delta_vs_real_incl_overlap=round(net - (real['pnl_net'] + pnl_ov), 2))
        else:
            rec.update(delta_vs_real=None, note='real trade record not found')
        if self._append:
            self._append(f'bnf_shadow_{self._day()}.jsonl', rec)
        self.open.remove(sh)
        self.closed_today += 1
        self._save()
        self._say('info', f"[SHADOW:{self.ARM}] {sh['symbol']} would have {dec['kind']} at {fill:.2f} "
                          f"(Rs{net:+,.0f}" + (f" vs real Rs{real['pnl_net']:+,.0f}" if real else '') + ')')

    def _abandon(self, sh: dict, now: datetime, why: str):
        if self._append:
            self._append(f'bnf_shadow_{sh["entry_time"][:10]}.jsonl', dict(
                kind=self.KIND + '_abandoned', arm=self.ARM, symbol=sh['symbol'], entry_time=sh['entry_time'],
                real=sh.get('real'), note=why))
        self.open.remove(sh)
        self._save()


class StaleShadow(_Arm):
    """Follows a trade the stale rule closed, as if it had NOT been closed."""
    ARM = 'stale_off'
    KIND = 'stale_shadow'
    STATE = 'shadow_state.json'

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.nostale = _no_stale_cfg(self.c)

    def open_from(self, pos: dict, now: datetime, real_rec: dict, last_bar_idx: int):
        """Called by the runner right after the stale exit has been recorded."""
        sh = self._new(pos, now, last_bar_idx)
        sh['real'] = dict(exit_time=real_rec['exit_time'], exit_premium=real_rec['exit_premium'],
                          pnl_net=real_rec['pnl_net'], pnl_pct=real_rec['pnl_pct'], reason=real_rec['reason'],
                          kind=real_rec.get('kind', 'thesis_stale'))
        self.open.append(sh)
        self._save()
        self._say('info', f"[SHADOW:{self.ARM}] following {pos['symbol']} past its stale exit "
                          f"(real result Rs{real_rec['pnl_net']:+,.0f})")

    def _decide(self, sh, view, idx):
        return S.on_bar_exit(sh['pos'], view, idx, self.nostale, replay=False)


class ProfitWaitShadow(_Arm):
    """Follows EVERY entry; the discretionary exits wait until the position has been net profitable."""
    ARM = 'profit_wait'
    KIND = 'profit_wait_shadow'
    STATE = 'shadow_pw_state.json'

    def open_from_entry(self, pos: dict, now: datetime, last_bar_idx: int):
        """Called by the runner right after a real position opens."""
        sh = self._new(pos, now, last_bar_idx)
        sh['profitable'] = False
        self.open.append(sh)
        self._save()
        self._say('info', f"[SHADOW:{self.ARM}] following {pos['symbol']} from entry")

    def _on_mark(self, sh, mark, pct):
        if not sh.get('profitable'):
            net = ((mark - sh['entry_premium']) * sh['lots'] * self.c.LOT_SIZE
                   - S.round_trip_cost(sh['entry_premium'], mark, sh['lots'], self.c))
            if net > self.c.PROFIT_WAIT_MIN_NET_RS:
                sh['profitable'] = True

    def _decide(self, sh, view, idx):
        return S.on_bar_exit_profit_wait(sh['pos'], view, idx, self.c, False, profitable_now=bool(sh.get('profitable')))

    def _real_of(self, sh):
        """The real trade that opened with this shadow, once it has been recorded."""
        for t in self._trades_today():
            if t.get('entry_time') == sh['entry_time'] and t.get('symbol') == sh['symbol']:
                return dict(exit_time=t['exit_time'], exit_premium=t['exit_premium'], pnl_net=t['pnl_net'],
                            pnl_pct=t['pnl_pct'], reason=t['reason'], kind=t['kind'])
        return None


def _jsonable(o):
    try:
        import numpy as np
        if isinstance(o, np.generic):
            return o.item()
        if isinstance(o, np.ndarray):
            return o.tolist()
    except Exception:
        pass
    return str(o)
