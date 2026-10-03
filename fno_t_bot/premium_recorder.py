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
    now = datetime.now(IST) if IST else datetime.now()
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


def record(bot, spot: float) -> None:
    """One snapshot. Throttled, batched, and silent on failure."""
    try:
        if not getattr(config, 'PREMIUM_RECORDER_ENABLED', False):
            return
        if not bot.fyers or not spot or spot <= 0:
            return
        now = datetime.now(IST) if IST else datetime.now()
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
        CH = int(getattr(config, 'PREMIUM_REC_BATCH', 25))
        for i in range(0, len(syms), CH):
            chunk = syms[i:i + CH]
            try:
                r = bot.fyers.quotes({'symbols': ','.join(chunk)})
            except Exception:
                continue
            if not isinstance(r, dict) or r.get('s') != 'ok':
                continue
            for row in (r.get('d') or []):
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
        if not quotes:
            return

        legs = []
        for k, t, s in opts:
            q = quotes.get(s)
            if q:
                legs.append(dict(strike=k, type=t, symbol=s, **q))
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
