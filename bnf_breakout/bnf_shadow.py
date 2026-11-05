# -*- coding: utf-8 -*-
"""Shadow arm: what would a trade have done if the STALE rule had not closed it?

WHY. The stale rule ('no 0.15% follow-through within 6 bars') closes a quarter to a half of all
trades. It protected some (Oct 29, Nov 3) and cost others (Nov 5: two trades closed for +1,252 and
+9 on a day the index rose 1.2%). One day cannot settle it, and the replay cannot price the premium.
So every trade the stale rule closes is FOLLOWED here, forward, on live data, and the question is
answered by the record: for each such trade, the real result (closed by the stale rule) beside the
shadow result (kept open and managed by every OTHER rule).

WHAT IT IS. A virtual continuation of a paper position. It places no order (there is no order path
anywhere in this package) and it never touches the real position, the real state or the real
trade log. After the stale exit it applies, to the same thesis, the same rules the real position
would have had: the premium stop/target, thesis-broken, the ATR trail and the 15:00 force close.
Only `THESIS_CONFIRM_BARS` is switched off for it.

WHAT IT CANNOT BE TRUSTED TO SAY ALONE. The bot holds one position at a time. If the real trade had
not been closed, a later real entry would not have happened. Each record therefore also lists the
real trades that DID open while the shadow was alive, and their P&L, so the review can compare
  shadow P&L   against   (the stale-closed trade + anything entered while it ran).

HOW IT STAYS SAFE.
  * Every call it makes is non-essential: past the API budget it is refused first, and a throttle
    reply backs the whole broker off exactly as it would for any non-essential call.
  * Every entry point is wrapped; an exception here is logged and swallowed by the runner.
  * It polls the premium every SHADOW_POLL_SECONDS (60), not every 20 s, and fetches bars only when
    a new bar has closed, so a shadow costs on the order of 150-250 requests a day.
  * Its state is its own file (`shadow_state.json`), separate from the real state.
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


class StaleShadow:
    def __init__(self, broker, c=cfg, state_dir: str | None = None, log_dir: str | None = None,
                 append=None, day_fn=None, logger=None):
        self.b, self.c, self.log = broker, c, logger
        self.state_dir = state_dir or c.STATE_DIR
        self.log_dir = log_dir or os.path.join(self.state_dir, 'logs')
        self._append = append
        self._day = day_fn or (lambda: datetime.now(bnf_data.IST).strftime('%Y-%m-%d'))
        self.nostale = _no_stale_cfg(c)
        self.open: list = []
        self.day = None
        self.calls = 0                      # requests this arm made (quote + bars), for the heartbeat
        self.closed_today = 0
        self._warned = 0.0
        self._load()

    # ── persistence ───────────────────────────────────────────────────────
    @property
    def _path(self):
        return os.path.join(self.state_dir, 'shadow_state.json')

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
            self._say('warning', f'[SHADOW] could not save state: {e}')

    def _say(self, level, msg):
        if self.log:
            getattr(self.log, level)(msg)

    def active(self) -> bool:
        return bool(self.open)

    def status(self) -> dict:
        return dict(open=len(self.open), closed_today=self.closed_today, calls=self.calls)

    # ── opening a shadow ──────────────────────────────────────────────────
    def open_from(self, pos: dict, now: datetime, real_rec: dict, last_bar_idx: int):
        """Called by the runner right after the stale exit has been recorded."""
        self.day = self._day()
        th = pos['thesis']
        sh = dict(
            symbol=pos['symbol'], opt=pos['opt'], strike=pos['strike'], expiry=pos['expiry'],
            lots=pos['lots'], entry_time=pos['entry_time'], entry_premium=pos['entry_premium'],
            real=dict(exit_time=real_rec['exit_time'], exit_premium=real_rec['exit_premium'],
                      pnl_net=real_rec['pnl_net'], pnl_pct=real_rec['pnl_pct'], reason=real_rec['reason']),
            pos=dict(dir=pos['dir'], entry_bar=pos['entry_bar'], entry_px=pos['entry_px'],
                     entry_premium=pos['entry_premium'], lots=pos['lots'], thesis=dict(th),
                     best_fav=pos['best_fav'], worst_adv=pos['worst_adv'], best_px=pos['best_px'],
                     confirmed=bool(pos.get('confirmed'))),
            last_idx=int(last_bar_idx), prem_best=pos.get('prem_best', 0.0), prem_worst=pos.get('prem_worst', 0.0),
            last_poll=0.0, last_mark=None, polls=0, opened=now.isoformat(timespec='seconds'))
        self.open.append(sh)
        self._save()
        self._say('info', f"[SHADOW] following {pos['symbol']} past its stale exit (real result Rs{real_rec['pnl_net']:+,.0f})")

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
                    self._say('warning', f'[SHADOW] {sh["symbol"]}: {e}')

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
                if self._append:
                    self._append(f'bnf_shadow_path_{self._day()}.jsonl', dict(
                        ts=now.isoformat(timespec='seconds'), symbol=sh['symbol'], bid=q.get('bid'),
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
                    dec = S.on_bar_exit(pos, view, idx, self.nostale, replay=False)
                    sh['last_idx'] = idx
                    if dec:
                        return self._close(now, sh, dec)
        self._save()

    # ── closing ───────────────────────────────────────────────────────────
    def _overlap(self, after_iso: str, until_iso: str):
        """Real trades that opened while this shadow was alive (they could not have, had the stale
        trade stayed open): count and net P&L."""
        n, pnl = 0, 0.0
        try:
            p = os.path.join(self.log_dir, f'bnf_trades_{self._day()}.jsonl')
            with open(p, encoding='utf-8') as fh:
                for ln in fh:
                    t = json.loads(ln)
                    if after_iso < t.get('entry_time', '') <= until_iso:
                        n += 1
                        pnl += float(t.get('pnl_net', 0.0))
        except Exception:
            pass
        return n, pnl

    def _close(self, now: datetime, sh: dict, dec: dict, q=None):
        priced = q is not None
        if q is None:
            self.calls += 1
            q, _ = self.b.quote(sh['symbol'], essential=False)
            priced = q is not None
        if priced:
            fill = q['bid'] if q.get('bid') else q['ltp'] * (1 - self.c.SLIPPAGE_PCT)
        elif sh.get('last_mark') and now.strftime('%H:%M') >= '15:25':
            fill = sh['last_mark']               # out of time: last known mark, flagged unpriced
        else:
            return                                # try again next pass; a shadow close is never urgent
        qty = sh['lots'] * self.c.LOT_SIZE
        gross = (fill - sh['entry_premium']) * qty
        cost = S.round_trip_cost(sh['entry_premium'], fill, sh['lots'], self.c)
        net = gross - cost
        n_ov, pnl_ov = self._overlap(sh['real']['exit_time'], now.isoformat(timespec='seconds'))
        held = (now - datetime.fromisoformat(sh['entry_time'])).total_seconds() / 60
        rec = dict(
            kind='stale_shadow', symbol=sh['symbol'], opt=sh['opt'], strike=sh['strike'], lots=sh['lots'],
            entry_time=sh['entry_time'], entry_premium=round(sh['entry_premium'], 2),
            real=sh['real'],
            shadow=dict(exit_time=now.isoformat(timespec='seconds'), kind=dec['kind'], reason=dec['reason'],
                        exit_priced=priced, exit_premium=round(fill, 2),
                        pnl_pct=round((fill / sh['entry_premium'] - 1) * 100, 2),
                        max_pnl_pct=round(sh['prem_best'] * 100, 2), min_pnl_pct=round(sh['prem_worst'] * 100, 2),
                        held_min=round(held, 1), gross=round(gross, 2), costs=round(cost, 2), pnl_net=round(net, 2)),
            delta_vs_real=round(net - sh['real']['pnl_net'], 2),
            real_trades_during_shadow=n_ov, real_pnl_during_shadow=round(pnl_ov, 2),
            delta_vs_real_incl_overlap=round(net - (sh['real']['pnl_net'] + pnl_ov), 2))
        if self._append:
            self._append(f'bnf_shadow_{self._day()}.jsonl', rec)
        self.open.remove(sh)
        self.closed_today += 1
        self._save()
        self._say('info', f"[SHADOW] {sh['symbol']} would have {dec['kind']} at {fill:.2f} "
                          f"(Rs{net:+,.0f} vs real Rs{sh['real']['pnl_net']:+,.0f}; "
                          f"{n_ov} real trade(s) in between, Rs{pnl_ov:+,.0f})")

    def _abandon(self, sh: dict, now: datetime, why: str):
        if self._append:
            self._append(f'bnf_shadow_{sh["entry_time"][:10]}.jsonl', dict(
                kind='stale_shadow_abandoned', symbol=sh['symbol'], entry_time=sh['entry_time'],
                real=sh['real'], note=why))
        self.open.remove(sh)
        self._save()


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
