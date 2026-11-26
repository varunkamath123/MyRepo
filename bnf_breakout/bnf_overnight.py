# -*- coding: utf-8 -*-
"""Overnight carry shadow: what is it worth to HOLD an option overnight? Forward, virtual, no orders.

THE QUESTION. The live strategy closes everything at 15:00. If an option were bought near the close and held
until the next session, would it pay, net of the real bid/ask, costs and the overnight change in implied
volatility? Neither a backtest nor Black-Scholes can answer it (Fyers delists expired contracts; the dominant
overnight factor is IV moving between close and open), so this arm measures it with real quotes, every
session, and logs everything a live version would need.

WHAT IT DOES (every trading day, independent of the real strategy; it never touches the real position, state
or trade log, and places no order: there is no order path anywhere in this package).
  ENTRY   at OVN_ENTRY_SAMPLES[0] (15:20) it picks the contract the strategy itself would trade (the nearest
          monthly with >= MIN_DAYS_TO_EXPIRY left), the ATM strike from the index at that moment, and records
          the ATM call and put plus a wing call/put OVN_WING_POINTS away (a strangle). Later entry samples
          (15:25, 15:28) re-quote the same contracts, to show how the price moves into the close.
  EXIT    on the next session it re-quotes the same contracts at each of OVN_EXIT_SAMPLES (09:16 ... 15:25).
          One record is written per carried position, with the P&L of EVERY structure on BOTH sides:
            ce / pe / straddle / strangle   x   long (buy at ask, sell at bid) / short (sell at bid, buy at ask)
          priced at the real bid/ask, with exact round-trip costs, plus the same at MID (the difference is the
          spread cost) and the official close -> official open change (previous-close and open prices that the
          exchange reports for each contract).
  DETAIL FOR GOING LIVE  at the primary entry and exit it also records the 5-level market depth of the ATM
          legs: best size, how a 1-lot and a MAX_LOTS order would walk the book, open interest, circuit limits,
          tick size. A position is held with the NRML product (INTRADAY would be squared off at 15:20).

WHAT IT CANNOT SAY. Short structures need margin that is not modelled here; the paper P&L of a short side
excludes margin cost and hides the tail (a rare gap of 4%+ cannot be in a 60-session sample). Read the
short side as "what the market paid", never as "safe".

HOW IT STAYS SAFE (same rules as bnf_shadow.py). Every call is non-essential: past the API budget it is
refused first. The runner wraps every entry point, so an exception here is logged and swallowed. State lives
in its own file. About 15 requests a day.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime

import bnf_broker as BR
import bnf_config as cfg
import bnf_data
import bnf_strategy as S

STRUCTS = {'ce': ('CE',), 'pe': ('PE',), 'straddle': ('CE', 'PE'), 'strangle': ('WCE', 'WPE')}


def _hm(s: str) -> int:
    return int(s[:2]) * 60 + int(s[3:5])


def _secs(now: datetime) -> int:
    return now.hour * 3600 + now.minute * 60 + now.second


def _num(x):
    try:
        v = float(x)
    except Exception:
        return None
    return v if v > 0 else None


# ── pure pricing helpers (unit-tested) ─────────────────────────────────────
def leg_from_quote(v: dict):
    """The fields worth keeping from a Fyers quote of one option; None when there is no price."""
    lp = _num(v.get('lp'))
    if lp is None:
        return None
    return dict(bid=_num(v.get('bid')), ask=_num(v.get('ask')), ltp=lp, spread=v.get('spread'),
                volume=v.get('volume'), open=_num(v.get('open_price')), high=_num(v.get('high_price')),
                low=_num(v.get('low_price')), prev_close=_num(v.get('prev_close_price')),
                atp=_num(v.get('atp')), tt=v.get('tt'))


def buy_fill(leg: dict, c=cfg):
    a = leg.get('ask')
    return (a, False) if a else (leg['ltp'] * (1 + c.SLIPPAGE_PCT), True)


def sell_fill(leg: dict, c=cfg):
    b = leg.get('bid')
    return (b, False) if b else (leg['ltp'] * (1 - c.SLIPPAGE_PCT), True)


def mid(leg: dict) -> float:
    b, a = leg.get('bid'), leg.get('ask')
    return (b + a) / 2.0 if b and a else leg['ltp']


def walk_book(levels, qty: int) -> dict:
    """Volume-weighted fill for `qty` units against [[price, volume], ...] (best first)."""
    need, cost, got = qty, 0.0, 0
    for p, v in levels or []:
        take = min(int(v), need)
        cost += float(p) * take
        got += take
        need -= take
        if need <= 0:
            break
    return dict(requested=qty, filled=got, vwap=(round(cost / got, 2) if got else None))


def structure_result(legs_in: dict, legs_out: dict, names: tuple, side: str, lots: int, c=cfg):
    """One structure, one side, entry sample -> exit sample. None if any leg is missing at either end."""
    if any(n not in legs_in or n not in legs_out for n in names):
        return None
    q = lots * c.LOT_SIZE
    gross = cost = mid_gross = cap = 0.0
    approx = False
    for n in names:
        e, x = legs_in[n], legs_out[n]
        if side == 'long':
            pe_, a1 = buy_fill(e, c)
            px_, a2 = sell_fill(x, c)
            gross += (px_ - pe_) * q
            cost += S.round_trip_cost(pe_, px_, lots, c)
            mid_gross += (mid(x) - mid(e)) * q
            cap += pe_ * q                                   # the debit paid
        else:
            pe_, a1 = sell_fill(e, c)
            px_, a2 = buy_fill(x, c)
            gross += (pe_ - px_) * q
            cost += S.round_trip_cost(px_, pe_, lots, c)
            mid_gross += (mid(e) - mid(x)) * q
            cap += pe_ * q                                   # the credit received (margin NOT modelled)
        approx = approx or a1 or a2
    net = gross - cost
    return dict(gross=round(gross, 1), cost=round(cost, 1), net=round(net, 1), mid_gross=round(mid_gross, 1),
                spread_cost=round(mid_gross - gross, 1), capital=round(cap, 1),
                net_pct=round(net / cap * 100, 3) if cap else None, approx=bool(approx))


def pair_result(e: dict, x: dict, lots: int, c=cfg) -> dict:
    out = {}
    for sname, names in STRUCTS.items():
        for side in ('long', 'short'):
            r = structure_result(e['legs'], x['legs'], names, side, lots, c)
            if r is not None:
                out[f'{sname}_{side}'] = r
    out['spot_move_pct'] = round((x['spot'] / e['spot'] - 1) * 100, 3) if e.get('spot') and x.get('spot') else None
    out['vix_change'] = round(x['vix'] - e['vix'], 3) if e.get('vix') and x.get('vix') else None
    return out


def official_pair(x_first: dict, names_all=('CE', 'PE', 'WCE', 'WPE')) -> dict:
    """Official close -> official open per contract, from the exit day's first sample (prev_close, open)."""
    legs = {}
    for n in names_all:
        l = x_first['legs'].get(n)
        if l and l.get('prev_close') and l.get('open'):
            legs[n] = dict(prev_close=l['prev_close'], open=l['open'], change=round(l['open'] - l['prev_close'], 2))
    res = {}
    for sname, names in STRUCTS.items():
        if all(n in legs for n in names):
            res[sname] = round(sum(legs[n]['change'] for n in names), 2)       # long, per unit, no spread
    return dict(legs=legs, long_per_unit=res)


def exec_block(depth_in: dict, depth_out: dict, lots: int, c=cfg) -> dict:
    """What an order of `lots` and MAX_LOTS would have got against the recorded 5-level book."""
    out = {}
    for tag, d, key in (('entry_buy', depth_in, 'asks'), ('exit_sell', depth_out, 'bids')):
        for n in ('CE', 'PE'):
            dd = (d or {}).get(n)
            if not dd:
                continue
            lv = dd.get(key) or []
            best = lv[0][0] if lv else None
            for L in sorted({lots, c.MAX_LOTS}):
                w = walk_book(lv, L * c.LOT_SIZE)
                w['best'] = best
                w['slip_vs_best_pct'] = (round((w['vwap'] / best - 1) * 100 * (1 if key == 'asks' else -1), 3)
                                         if w['vwap'] and best else None)
                out[f'{tag}_{n}_{L}lot'] = w
            out[f'{tag}_{n}_meta'] = dict(oi=dd.get('oi'), pdoi=dd.get('pdoi'), tick=dd.get('tick'),
                                          upper_ckt=dd.get('upper'), lower_ckt=dd.get('lower'),
                                          top_size=(lv[0][1] if lv else None), tot_buy=dd.get('tot_buy'),
                                          tot_sell=dd.get('tot_sell'), ltt=dd.get('ltt'))
    return out


def depth_compact(d: dict) -> dict:
    def lv(x):
        return [[float(i['price']), int(i['volume'])] for i in (x or [])[:5] if i.get('price')]
    return dict(bids=lv(d.get('bids')), asks=lv(d.get('ask')), tot_buy=d.get('totalbuyqty'),
                tot_sell=d.get('totalsellqty'), oi=d.get('oi'), pdoi=d.get('pdoi'), tick=d.get('tick_Size'),
                upper=d.get('upper_ckt'), lower=d.get('lower_ckt'), ltt=d.get('ltt'), volume=d.get('v'))


# ── the arm ─────────────────────────────────────────────────────────────────
class OvernightShadow:
    ARM = 'overnight'
    STATE = 'shadow_overnight_state.json'

    def __init__(self, broker, c=cfg, state_dir: str | None = None, log_dir: str | None = None,
                 append=None, day_fn=None, logger=None):
        self.b, self.c, self.log = broker, c, logger
        self.state_dir = state_dir or c.STATE_DIR
        self.log_dir = log_dir or os.path.join(self.state_dir, 'logs')
        self._append = append
        self._day = day_fn or (lambda: datetime.now(bnf_data.IST).strftime('%Y-%m-%d'))
        self.calls = 0
        self.closed_today = 0
        self._count_day = None
        self._warned = 0.0
        self.st = dict(last_entry_day=None, open=[])
        self._load()

    # persistence
    @property
    def _path(self):
        return os.path.join(self.state_dir, self.STATE)

    def _load(self):
        try:
            with open(self._path, encoding='utf-8') as fh:
                st = json.load(fh)
            self.st = dict(last_entry_day=st.get('last_entry_day'), open=st.get('open', []))
        except Exception:
            self.st = dict(last_entry_day=None, open=[])

    def _save(self):
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = self._path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(self.st, fh)
            os.replace(tmp, self._path)
        except Exception as e:
            self._say('warning', f'[OVN] could not save state: {e}')

    def _say(self, level, msg):
        if self.log:
            getattr(self.log, level)(msg)

    def _write(self, day: str, rec: dict):
        if self._append:
            self._append(f'bnf_overnight_{day}.jsonl', rec)

    def status(self) -> dict:
        return dict(open=len(self.st['open']), closed_today=self.closed_today, calls=self.calls,
                    last_entry_day=self.st.get('last_entry_day'))

    # ── which sample times are still owed ─────────────────────────────────
    def _tol(self, label: str, primary: bool) -> int:
        return int(self.c.OVN_PRIMARY_TOLERANCE_MIN if primary else self.c.OVN_SAMPLE_TOLERANCE_MIN)

    def _pending(self, today: str):
        """[(minute_of_day, tolerance, kind, pos|None, label)] still owed right now."""
        out = []
        ent = self.c.OVN_ENTRY_SAMPLES
        todays = next((p for p in self.st['open'] if p['entry_day'] == today), None)
        if self.st.get('last_entry_day') != today:
            out.append((_hm(ent[0]), self._tol(ent[0], True), 'entry', None, ent[0]))
        elif todays:
            done = {s['label'] for s in todays['entry_samples']} | set(todays.get('entry_missed', []))
            out += [(_hm(l), self._tol(l, False), 'entry', todays, l) for l in ent[1:] if l not in done]
        for p in self.st['open']:
            if p['entry_day'] < today:
                done = {s['label'] for s in p['exit_samples']} | set(p.get('exit_missed', []))
                out += [(_hm(l), self._tol(l, l == self.c.OVN_EXIT_PRIMARY), 'exit', p, l)
                        for l in self.c.OVN_EXIT_SAMPLES if l not in done]
        return out

    def wake_secs(self, now: datetime):
        """Seconds the runner may sleep before this arm next needs a look, or None if nothing is due soon."""
        try:
            today = now.strftime('%Y-%m-%d')
            s = _secs(now)
            best = None
            for t, tol, *_ in self._pending(today):
                if t * 60 <= s <= (t + tol) * 60:
                    cand = 15.0
                elif s < t * 60 and t * 60 - s <= 600:
                    cand = max(t * 60 - s - 5.0, 1.0)
                else:
                    continue
                best = cand if best is None else min(best, cand)
            return best
        except Exception:
            return None

    # ── one pass ──────────────────────────────────────────────────────────
    def step(self, now: datetime):
        today = now.strftime('%Y-%m-%d')
        if self._count_day != today:
            self._count_day, self.closed_today = today, 0
        s = _secs(now)
        owed = sorted(self._pending(today), key=lambda r: r[0])
        for t, tol, kind, pos, label in owed:
            if s < t * 60:
                continue
            if s > (t + tol) * 60:
                self._missed(today, kind, pos, label, now)
                continue
            try:
                if kind == 'entry' and pos is None:
                    self._open(now, today, label)
                else:
                    self._take(now, today, pos, kind, label)
            except Exception as e:
                import time as _t
                if _t.time() - self._warned > 600:
                    self._warned = _t.time()
                    self._say('warning', f'[OVN] {kind} {label}: {e}')
            break                                                   # one sample per pass
        self._finish_due(now, today)
        self._save()

    def _missed(self, today, kind, pos, label, now):
        if kind == 'entry' and pos is None:
            self.st['last_entry_day'] = today
            self._write(today, dict(kind='overnight_no_entry', day=today, reason=f'primary entry {label} not sampled in time',
                                    ts=now.isoformat(timespec='seconds')))
        elif kind == 'entry':
            pos.setdefault('entry_missed', []).append(label)
        else:
            pos.setdefault('exit_missed', []).append(label)

    # ── entry ─────────────────────────────────────────────────────────────
    def _open(self, now, today, label):
        c = self.c
        self.calls += 1
        q1, st = self.b.quotes_multi([c.INDEX_SYMBOL, c.VIX_SYMBOL], essential=False)
        if q1 is None or not _num((q1.get(c.INDEX_SYMBOL) or {}).get('lp')):
            self._last_fail = st
            return
        spot = float(q1[c.INDEX_SYMBOL]['lp'])
        d0 = date.fromisoformat(today)
        exp = self.b._expiry[1] if getattr(self.b, '_expiry', None) and self.b._expiry[0] == d0 else None
        order = [exp] if exp else BR.expiry_candidates(d0, c)
        atm = BR.atm_strike(spot, c)
        W = int(c.OVN_WING_POINTS)
        for e in order[:2]:
            for k in (0, 1, -1):
                strike = atm + k * c.STRIKE_GAP
                names = dict(CE=BR.option_symbol(strike, 1, e, c), PE=BR.option_symbol(strike, -1, e, c))
                if W > 0:
                    names['WCE'] = BR.option_symbol(strike + W, 1, e, c)
                    names['WPE'] = BR.option_symbol(strike - W, -1, e, c)
                self.calls += 1
                qs, st = self.b.quotes_multi(list(names.values()) + [c.INDEX_SYMBOL, c.VIX_SYMBOL], essential=False)
                if qs is None:
                    return                                          # throttled / budget: retry while in the window
                smp = self._sample_from(qs, names, label, now)
                if 'CE' in smp['legs'] and 'PE' in smp['legs']:
                    pos = dict(entry_day=today, entry_ts=now.isoformat(timespec='seconds'), strike=strike,
                               expiry=e.isoformat(), dte=(e - d0).days, weekday=now.strftime('%a'),
                               lots=int(c.OVN_LOTS), symbols=names, spot_at_entry=spot,
                               entry_samples=[smp], exit_samples=[], depth_entry=None, depth_exit=None)
                    if c.OVN_DEPTH:
                        pos['depth_entry'] = self._depth(names)
                    self.st['open'].append(pos)
                    self.st['last_entry_day'] = today
                    self._write(today, dict(kind='overnight_sample', phase='entry', day=today, sample=smp, strike=strike,
                                            expiry=pos['expiry'], dte=pos['dte']))
                    self._say('info', f'[OVN] entered {strike} {pos["expiry"]} straddle (virtual) at {label}: '
                                      f'CE ask {smp["legs"]["CE"]["ask"]} PE ask {smp["legs"]["PE"]["ask"]}')
                    return
        # nothing priced at any probe: keep trying until the window closes

    def _sample_from(self, qs: dict, names: dict, label: str, now: datetime) -> dict:
        c = self.c
        legs = {}
        for n, sym in names.items():
            l = leg_from_quote(qs.get(sym) or {})
            if l:
                legs[n] = l
        ix, vx = qs.get(c.INDEX_SYMBOL) or {}, qs.get(c.VIX_SYMBOL) or {}
        return dict(label=label, ts=now.isoformat(timespec='seconds'), spot=_num(ix.get('lp')),
                    spot_open=_num(ix.get('open_price')), spot_high=_num(ix.get('high_price')),
                    spot_low=_num(ix.get('low_price')), spot_prev_close=_num(ix.get('prev_close_price')),
                    vix=_num(vx.get('lp')), vix_prev_close=_num(vx.get('prev_close_price')), legs=legs)

    def _depth(self, names: dict) -> dict:
        out = {}
        for n in ('CE', 'PE'):
            self.calls += 1
            d, _ = self.b.depth(names[n], essential=False)
            if d:
                out[n] = depth_compact(d)
        return out or None

    # ── later samples ─────────────────────────────────────────────────────
    def _take(self, now, today, pos, kind, label):
        c = self.c
        self.calls += 1
        qs, st = self.b.quotes_multi(list(pos['symbols'].values()) + [c.INDEX_SYMBOL, c.VIX_SYMBOL], essential=False)
        if qs is None:
            return
        smp = self._sample_from(qs, pos['symbols'], label, now)
        if 'CE' not in smp['legs'] or 'PE' not in smp['legs']:
            return
        phase = 'entry' if kind == 'entry' else 'exit'
        pos['entry_samples' if kind == 'entry' else 'exit_samples'].append(smp)
        if kind == 'exit' and label == c.OVN_EXIT_PRIMARY and c.OVN_DEPTH and pos.get('depth_exit') is None:
            pos['depth_exit'] = self._depth(pos['symbols'])
        self._write(today, dict(kind='overnight_sample', phase=phase, day=today, entry_day=pos['entry_day'], sample=smp))

    # ── finishing ─────────────────────────────────────────────────────────
    def _finish_due(self, now, today):
        for pos in list(self.st['open']):
            if pos['entry_day'] >= today:
                continue
            nights = (date.fromisoformat(today) - date.fromisoformat(pos['entry_day'])).days
            if nights > int(self.c.OVN_MAX_HOLD_DAYS):
                self._abandon(pos, now, f'no exit session within {self.c.OVN_MAX_HOLD_DAYS} days')
                continue
            owed = [r for r in self._pending(today) if r[3] is pos and r[2] == 'exit']
            if not owed or _secs(now) >= _hm('15:30') * 60:
                self._finalize(pos, now, today, nights)

    def _abandon(self, pos, now, why):
        self._write(now.strftime('%Y-%m-%d'), dict(kind='overnight_abandoned', entry_day=pos['entry_day'], reason=why))
        self.st['open'].remove(pos)

    def _finalize(self, pos, now, today, nights):
        c = self.c
        ent = pos['entry_samples']
        prim_e = next((s for s in ent if s['label'] == c.OVN_ENTRY_SAMPLES[0]), ent[0])
        exits = pos['exit_samples']
        pairs = {}
        for x in exits:
            pairs[f"E{prim_e['label']}->X{x['label']}"] = pair_result(prim_e, x, pos['lots'], c)
        prim_x = next((s for s in exits if s['label'] == c.OVN_EXIT_PRIMARY), None)
        if prim_x:
            for e in ent:
                if e is not prim_e:
                    pairs[f"E{e['label']}->X{prim_x['label']}"] = pair_result(e, prim_x, pos['lots'], c)
        gap = None
        if exits and exits[0].get('spot_open') and prim_e.get('spot'):
            gap = round((exits[0]['spot_open'] / prim_e['spot'] - 1) * 100, 3)
        rec = dict(
            kind='overnight_carry', entry_day=pos['entry_day'], exit_day=today, nights=nights,
            weekend=(nights >= 2), weekday=pos['weekday'], dte_at_entry=pos['dte'], dte_at_exit=pos['dte'] - nights,
            expiry=pos['expiry'], strike=pos['strike'], wing_points=int(c.OVN_WING_POINTS), lots=pos['lots'],
            lot_size=c.LOT_SIZE, product='NRML (INTRADAY/MIS would be squared off by the broker)',
            symbols=pos['symbols'], primary=dict(entry=c.OVN_ENTRY_SAMPLES[0], exit=c.OVN_EXIT_PRIMARY),
            primary_pair=pairs.get(f"E{prim_e['label']}->X{c.OVN_EXIT_PRIMARY}"),
            open_gap_pct=gap, official_close_to_open=(official_pair(exits[0]) if exits else None),
            exec=exec_block(pos.get('depth_entry'), pos.get('depth_exit'), pos['lots'], c),
            pairs=pairs, entry_samples=ent, exit_samples=exits,
            exit_missed=pos.get('exit_missed', []), entry_missed=pos.get('entry_missed', []),
            note='short results exclude margin and cannot show tail risk; see bnf_overnight.py')
        self._write(today, rec)
        self.st['open'].remove(pos)
        self.closed_today += 1
        pp = rec['primary_pair'] or {}
        self._say('info', f"[OVN] {pos['entry_day']}->{today} straddle long net "
                          f"{(pp.get('straddle_long') or {}).get('net')} short net {(pp.get('straddle_short') or {}).get('net')}")
