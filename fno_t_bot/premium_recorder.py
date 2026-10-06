# -*- coding: utf-8 -*-
"""Record the data that becomes untestable the moment it is not captured.

THE PROBLEM THIS SOLVES
-----------------------
Fyers delists expired option contracts. Every historical option symbol tested
on Oct 2 2026 -- weeklies and monthlies, August and September -- returned
"Invalid symbol provided". That single fact is why:

  * only 1 of this book's 36 real-priced trades could be re-priced for real
  * the one that could be showed a plausible, internally consistent model was
    wrong by 6.7x (modelled +Rs1,483 vs actual +Rs221)
  * every option-strategy conclusion before 2026-08-18 is Black-Scholes or
    index-proxy, and BS mis-prices real premiums here by 41-123%
  * the only route to real-premium backtesting was a Breeze session token

None of that is a data problem. It is a CAPTURE problem. A premium that is
recorded today is still a premium in six months; one that is not is gone
permanently. From the day this runs, the archive builds itself and Breeze
stops being the bottleneck for anything going forward.

WHAT IS RECORDED, AND WHY EACH EARNS ITS PLACE
----------------------------------------------
  option premiums (ATM +/- PREMIUM_REC_STRIKES, both legs)
      The core. Makes every future option backtest real-priced instead of
      modelled. Without it we repeat the 6.7x error indefinitely.

  bid / ask per contract
      We have NO spread data at all. Every backtest and every paper fill in
      this project marks at LTP, which systematically flatters results --
      paper and backtest agree with each other and both flatter reality.
      Spread is also the reason an 11-point index edge is untradeable, and
      we currently cannot even measure it.

  index futures, near and next month
      Basis and term structure. Completely absent from the archive, and the
      Oct 2 strike-OI work pointed at futures as the instrument an 11-point
      edge actually fits. Cannot be evaluated without this.

  per-strike OI and volume
      oi_levels logs the LEVELS it computes; this logs the raw distribution
      they came from, so a different level definition can be tested later
      against the same tape without re-collecting.

  spot and India VIX at the same instant
      Alignment. The existing stores sample these on different clocks, which
      forces interpolation and quietly adds error.

DESIGN
------
Defensive by construction. The quote payload is stored field-by-field from
whatever the broker returns rather than mapped onto an assumed schema -- a
field that is missing today but appears later is captured automatically, and
a renamed field does not silently become null. One batched quotes call per
instrument per interval; never blocks trading; any failure is skipped and
logged, never guessed at.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

import config

try:
    from options_bot import IST
except Exception:
    IST = None

def _late(name):
    """Resolve a name from options_bot at CALL time, not import time.

    options_bot imports these shadow modules at its line ~43, long before it
    defines round_trip_costs (line ~74) and IST (line ~65) -- so the
    module-level `from options_bot import ...` silently bound None in every
    one of them. Oct 6 2026 was the proof: phantom rows carried naive
    timestamps and `costs: 0.0`, which overstated every phantom P&L by the
    whole four-leg commission. Resolving on first use closes the cycle.
    """
    g = globals()
    if g.get(name) is None:
        try:
            import options_bot as _ob
            g[name] = getattr(_ob, name, None)
        except Exception:
            pass
    return g.get(name)


# Fields worth keeping if the broker returns them. Anything not listed is
# dropped to keep the file small; anything listed but absent is simply absent.
_KEEP = ('lp', 'bid', 'ask', 'bid_size', 'ask_size', 'spread', 'volume', 'v',
         'oi', 'open_interest', 'prev_oi', 'iv', 'ch', 'chp', 'tot_buy_qty',
         'tot_sell_qty', 'low_price', 'high_price', 'open_price', 'prev_close_price')


def _log_dir() -> str:
    return getattr(config, 'LOG_DIRECTORY', 'logs')


def _slim(v: dict) -> dict:
    return {k: v[k] for k in _KEEP if k in v and v[k] is not None}



_FUT_PREFIX = {'NIFTY': 'NSE:NIFTY', 'BANKNIFTY': 'NSE:BANKNIFTY'}


def _futures(instrument: str) -> list:
    """Near and next month futures, derived from today's date.

    Hardcoding 'NSE:NIFTY26OCTFUT' in config would silently go stale every
    month and the symbol would just stop quoting -- a gap that looks like
    missing data rather than a bug. Derived symbols cannot drift. SENSEX
    futures are a BSE product the bot does not quote, so it is omitted.
    """
    pre = _FUT_PREFIX.get(instrument)
    if not pre:
        return []
    now = datetime.now(_late('IST')) if _late('IST') else datetime.now()
    out = []
    y, m = now.year, now.month
    for lbl in ('fut_near', 'fut_next'):
        mon = ('JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC'.split())[m - 1]
        out.append((lbl, f"{pre}{str(y)[2:]}{mon}FUT"))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out

def _symbols(bot, spot: float) -> tuple:
    """(option symbols, [(label, symbol)] extras). Empty on any failure."""
    try:
        from fyers_orders import build_option_symbol, get_next_expiry
        n = int(getattr(config, 'PREMIUM_REC_STRIKES', 4))
        gap = bot.strike_gap
        atm = int(round(spot / gap) * gap)
        exp = get_next_expiry(bot.instrument)
        opts = []
        for i in range(-n, n + 1):
            k = atm + i * gap
            for t in ('CALL', 'PUT'):
                s = build_option_symbol(bot.instrument, k, t, exp)
                if s:
                    opts.append((k, t, s))
        extra = list((getattr(config, 'PREMIUM_REC_EXTRA', {}) or {}).get(
            bot.instrument, []))
        extra += _futures(bot.instrument)
        return opts, extra, atm, exp
    except Exception:
        return [], [], None, None


def record(bot, spot: float, oc: dict | None = None) -> None:
    """One snapshot. Throttled, batched, and silent on failure."""
    try:
        if not getattr(config, 'PREMIUM_RECORDER_ENABLED', False):
            return
        if not bot.fyers or not spot or spot <= 0:
            return
        now = datetime.now(_late('IST')) if _late('IST') else datetime.now()
        every = int(getattr(config, 'PREMIUM_REC_EVERY_MIN', 5))
        last = getattr(bot, '_premrec_last', None)
        if last is not None and (now - last).total_seconds() < every * 60:
            return
        bot._premrec_last = now

        opts, extra, atm, exp = _symbols(bot, spot)
        if not opts:
            return
        syms = [s for _, _, s in opts] + [s for _, s in extra]

        # One batched call. If the broker caps the batch, split it -- still far
        # cheaper than one call per contract.
        quotes = {}
        probe = None
        failed = []
        CH = int(getattr(config, 'PREMIUM_REC_BATCH', 25))

        def _ask(chunk):
            """Fetch one chunk. Returns the rows, or None if the call failed.

            Oct 6 2026: NIFTY lost a 65-minute window and BANKNIFTY a
            136-minute one, while oi_levels -- called on the very next line of
            the same loop -- logged 75 snapshots with no gaps at all. The
            failure was inside here and completely silent, because a bad chunk
            just hit `continue`. A single unquotable symbol (a rolled futures
            month, an illiquid wing) takes the whole batch down with it, so a
            failed chunk is now retried symbol-by-symbol and whatever does
            fail is logged rather than vanishing.
            """
            try:
                r = bot.fyers.quotes({'symbols': ','.join(chunk)})
            except Exception as exc:
                return None, str(exc)[:80]
            if not isinstance(r, dict) or r.get('s') != 'ok':
                return None, str((r or {}).get('message'))[:80]
            return (r.get('d') or []), None

        for i in range(0, len(syms), CH):
            chunk = syms[i:i + CH]
            rowset, err = _ask(chunk)
            if rowset is None and len(chunk) > 1:
                # one poisoned symbol should not cost us the other twenty
                rowset = []
                for one in chunk:
                    sub, suberr = _ask([one])
                    if sub:
                        rowset += sub
                    else:
                        failed.append(one)
            elif rowset is None:
                failed.append(chunk[0])
                rowset = []
            for row in rowset:
                nm = row.get('n') or row.get('symbol')
                v = row.get('v')
                if nm and isinstance(v, dict):
                    quotes[nm] = _slim(v)
                    # Schema probe: the FULL payload of one contract, once per
                    # session. _KEEP was written without being able to inspect
                    # a live quote (the token rolls daily and was stale when
                    # this was built), so if the broker names a field
                    # differently the slimmed legs would come back quietly
                    # near-empty and nobody would notice for weeks. This makes
                    # the real schema self-documenting from day one, at the
                    # cost of one extra dict per day.
                    if probe is None:
                        probe = {'symbol': nm, 'raw': v}
        if failed:
            bot.logger.info(
                f"  [PREM-REC] {bot.instrument}: {len(failed)} symbol(s) would "
                f"not quote, recorded the rest — {', '.join(failed[:4])}"
            )
        if not quotes:
            bot.logger.info(
                f"  [PREM-REC] {bot.instrument}: NO symbol quoted this "
                f"snapshot — archive gap. Tried {len(syms)}."
            )
            return

        # Open interest comes from the CHAIN, not from quotes(): the Oct 5
        # schema probe showed fyers.quotes() returns lp/bid/ask/volume/spread
        # but no OI field at all. Without this the archive would have captured
        # premiums and silently no OI -- the exact failure the probe exists to
        # catch, caught on day one.
        chain = (oc or {}).get('strikes') or {}

        def _oi(strike, typ):
            v = chain.get(strike) or chain.get(float(strike)) or chain.get(str(strike))
            if not isinstance(v, dict):
                return {}
            key = 'call_oi' if typ == 'CALL' else 'put_oi'
            iv_key = 'call_iv' if typ == 'CALL' else 'put_iv'
            out = {}
            if v.get(key) is not None:
                out['oi'] = v[key]
            if v.get(iv_key) is not None:
                out['iv'] = v[iv_key]
            return out

        legs = []
        for k, t, s in opts:
            q = quotes.get(s)
            if not q:
                continue
            # Merge, do NOT double-splat: if the broker ever starts returning
            # an 'oi' field, dict(**q, **_oi(...)) raises TypeError on the
            # duplicate key and the whole snapshot is silently dropped by the
            # outer except. Chain OI wins, since it is the authoritative source.
            leg = dict(strike=k, type=t, symbol=s)
            leg.update(q)
            leg.update(_oi(k, t))
            legs.append(leg)
        rec = dict(
            schema=1, instrument=bot.instrument,
            ts=now.isoformat(), spot=round(float(spot), 2),
            atm=atm, expiry=str(exp) if exp else None,
            n_legs=len(legs), legs=legs,
            extra={lbl: quotes.get(sym) for lbl, sym in extra if quotes.get(sym)},
            vix=getattr(bot, '_last_vix', None),
            atm_iv=getattr(bot, '_last_atm_iv', None),
        )
        if not getattr(bot, '_premrec_probed', False) and probe:
            bot._premrec_probed = True
            rec['schema_probe'] = probe
        try:
            os.makedirs(_log_dir(), exist_ok=True)
            p = os.path.join(_log_dir(),
                             f'premiums_{bot.instrument}_'
                             f'{now.strftime("%Y-%m-%d")}.jsonl')
            with open(p, 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(rec, default=str) + '\n')
        except Exception:
            pass
    except Exception:
        try:
            bot.logger.debug('  [PREM-REC] snapshot failed')
        except Exception:
            pass


def reset_day(bot) -> None:
    bot._premrec_last = None
    bot._premrec_probed = False
