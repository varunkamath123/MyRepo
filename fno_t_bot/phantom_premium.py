# -*- coding: utf-8 -*-
"""Phantom SHORT-premium book -- are we standing on the wrong side of the trade?

WHY THIS EXISTS
---------------
Four independent measurements in this project all point the same way:

  1. VRP: implied vol systematically exceeds realised. Option BUYERS pay it.
  2. Straddle EV -41.6% over 4,566 observations.
  3. 90.7% of our entries buy vol the index never delivers (median 34%
     overpayment).
  4. Oct 1 2026 study on 1,082 instrument-sessions of real index bars: the
     median 10:00->14:30 window reaches a best-case excursion of only
     0.31-0.36%, against a median break-even requirement of 0.51% taken from
     this book's own real-priced trades. Even a PERFECT directional oracle
     leaves 70-75% of entries unable to reach break-even.

Taken together those say the losing variable was never direction -- it was
SIDE. We are long premium in a window that structurally cannot deliver the
move the premium is charging for. The live book agrees: -Rs5,865 over 36
real-priced trades, with entry logic that 7 separate methods could not
distinguish from a coin flip.

This module does not trade. It MEASURES. Every session it opens one
direction-neutral, defined-risk SHORT-premium structure per instrument, marks
it against real quotes, closes it on explicit rules, and writes the result.
In 6-8 weeks that is a real answer to "should we be sellers?", earned forward
on traded prices rather than argued from a backtest we cannot price.

WHY A SHORT IRON BUTTERFLY
--------------------------
Sell the ATM straddle, buy wings PHANTOM_PREM_WING_GAPS strikes out.

  * Direction-neutral by construction. We have no directional edge (7 methods,
    DSR 0.0%, random-entry p=0.857), so a structure that needed one would be
    testing the thing we already know is absent.
  * Its breakevens sit exactly at the straddle premium -- precisely the move
    the market is charging for. So its P&L IS the test of finding (4): does
    the index stay inside the move implied by the premium?
  * Defined risk. Working capital is Rs26,000; nothing naked is appropriate
    here. The wings cap loss at (width - credit) per unit whatever the index
    does.
  * It is the exact inverse of what the live engines do, which makes the
    comparison against the champion book direct rather than analogical.

REAL QUOTES ONLY
----------------
All four legs must return a broker LTP or the structure is skipped for the
day. Same discipline as PAPER_REQUIRE_REAL_LTP, same reason: Black-Scholes
mis-prices real premiums here by 41-123%, so a modelled phantom would not
measure anything -- it would restate the model's own assumptions back to us.
A skipped day is honest; a modelled day is noise that looks like evidence.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

import config

try:
    from options_bot import round_trip_costs, IST
except Exception:                      # imported before options_bot is ready
    round_trip_costs = IST = None


def _log_dir() -> str:
    return getattr(config, 'LOG_DIRECTORY', 'logs')


def _write(instrument: str, rec: dict) -> None:
    try:
        os.makedirs(_log_dir(), exist_ok=True)
        p = os.path.join(_log_dir(),
                         f'phantom_premium_{instrument}_{rec["date"]}.jsonl')
        with open(p, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(rec) + '\n')
    except Exception:
        pass


def _legs_for(bot, atm: int) -> list:
    """The four legs as (role, option_type, strike). Shorts first."""
    w = int(getattr(config, 'PHANTOM_PREM_WING_GAPS', 2)) * bot.strike_gap
    return [('short', 'CALL', atm),      ('short', 'PUT', atm),
            ('long',  'CALL', atm + w),  ('long',  'PUT', atm - w)]


def _quote_all(bot, legs: list, index_px: float, hv: float):
    """Quote every leg with a real LTP. Returns ((prices, symbols), None)
    or (None, reason)."""
    px, sym = {}, {}
    for role, typ, k in legs:
        p, s, src = bot._quote_option(k, typ, index_px, hv)
        if src != 'LTP' or not p or p <= 0:
            return None, f"{typ} {k} had no broker quote (src={src})"
        px[(role, typ)], sym[(role, typ)] = float(p), s
    return (px, sym), None


def _net_cost(px: dict) -> float:
    """Cost to CLOSE the structure now -- what we would pay to buy it back."""
    return ((px[('short', 'CALL')] + px[('short', 'PUT')])
            - (px[('long', 'CALL')] + px[('long', 'PUT')]))



def _rv_iv(bot, hv):
    """Same construction as options_bot._rv_iv_blocked: hv*100 / iv_pct."""
    iv = getattr(bot, '_last_atm_iv', None)
    src = 'chain' if iv else ('vix' if getattr(bot, '_last_vix', None) else None)
    iv = iv or getattr(bot, '_last_vix', None)
    if not iv or not hv or iv <= 0:
        return None, None
    return round(hv * 100.0 / iv, 3), src


def _divergence(bot, index_px):
    """Weighted top-10 basket 30-min return minus the index's own.

    Measures whether the heavyweights have moved further than the index has.
    It does NOT call direction (rho +0.012, p=0.63) but it does flag that a
    bigger move is coming (payability 22.7% -> 40.4% when |value| > 0.1%).

    Best-effort and cached once per session: 10 history calls a day, not per
    bar. Any failure returns None rather than guessing -- a fabricated context
    field would quietly corrupt the very comparison this exists to enable.
    """
    if not getattr(config, 'PHANTOM_PREM_CONTEXT_STOCKS', False) or not bot.fyers:
        return None, None
    W = getattr(config, 'PHANTOM_PREM_TOP10_WEIGHTS', {}) or {}
    if not W:
        return None, None
    today = datetime.now(IST).strftime('%Y-%m-%d')
    cache = getattr(bot, '_pprem_stk_cache', None)
    if not cache or cache.get('day') != today:
        cache = {'day': today, 'data': {}}
        for sym in W:
            try:
                r = bot.fyers.history({'symbol': sym, 'resolution': '5',
                                       'date_format': '1', 'range_from': today,
                                       'range_to': today, 'cont_flag': '1'})
                if r.get('s') == 'ok' and r.get('candles'):
                    cache['data'][sym] = [c[4] for c in r['candles']]   # closes
            except Exception:
                pass
        bot._pprem_stk_cache = cache
    closes = cache.get('data') or {}
    if len(closes) < 7:
        return None, len(closes)
    wsum = wret = 0.0
    for sym, w in W.items():
        c = closes.get(sym)
        if not c or len(c) < 7:
            continue
        wsum += w
        wret += w * ((c[-1] - c[-7]) / c[-7] * 100.0)
    if wsum <= 0:
        return None, len(closes)
    try:
        bars = getattr(bot, '_idx_closes', None)
        idx_r = ((index_px - float(bars.iloc[-7])) / float(bars.iloc[-7]) * 100.0
                 ) if bars is not None and len(bars) >= 7 else None
    except Exception:
        idx_r = None
    if idx_r is None:
        return None, len(closes)
    return round(wret / wsum - idx_r, 4), len(closes)


def _context(bot, index_px, hv):
    """Magnitude context at entry. Never raises; missing fields stay None."""
    out = dict(rv_iv=None, rv_iv_src=None, divergence=None, n_constituents=None)
    if not getattr(config, 'PHANTOM_PREM_CONTEXT', False):
        return out
    try:
        out['rv_iv'], out['rv_iv_src'] = _rv_iv(bot, hv)
    except Exception:
        pass
    try:
        out['divergence'], out['n_constituents'] = _divergence(bot, index_px)
    except Exception:
        pass
    return out

def open_book(bot, index_px: float, hv: float) -> None:
    """Open the day's structure once, at or after PHANTOM_PREM_TIME."""
    try:
        if not getattr(config, 'PHANTOM_PREMIUM_ENABLED', False):
            return
        if getattr(bot, '_pprem_open', None) or getattr(bot, '_pprem_done', False):
            return
        now = datetime.now(IST)
        if now.strftime('%H:%M') < str(getattr(config, 'PHANTOM_PREM_TIME', '10:00')):
            return

        bot._pprem_done = True          # one attempt per session, win or lose
        atm  = int(round(index_px / bot.strike_gap) * bot.strike_gap)
        legs = _legs_for(bot, atm)
        got, why = _quote_all(bot, legs, index_px, hv)
        if got is None:
            bot.logger.info(
                f"  [PHANTOM-PREM] {bot.instrument}: skipped -- {why}. Not "
                f"modelling it; a BS-priced phantom would measure nothing.")
            return
        px, sym = got
        credit = _net_cost(px)
        width  = int(getattr(config, 'PHANTOM_PREM_WING_GAPS', 2)) * bot.strike_gap
        if credit <= 0:
            bot.logger.info(f"  [PHANTOM-PREM] {bot.instrument}: skipped -- "
                            f"non-positive credit Rs{credit:.2f}")
            return

        lots = int(getattr(config, 'PHANTOM_PREM_LOTS', 1))
        qty  = bot.lot_size * lots
        bot._pprem_open = dict(
            instrument=bot.instrument, date=now.strftime('%Y-%m-%d'),
            entry_time=now, entry_index=float(index_px), atm=atm,
            width=width, credit=credit, qty=qty, lots=lots,
            entry_px={f'{r}_{t}': px[(r, t)] for r, t, _ in legs},
            strikes={f'{r}_{t}': k for r, t, k in legs},
            symbols={f'{r}_{t}': sym[(r, t)] for r, t, _ in legs},
            peak_pnl=0.0, trough_pnl=0.0,
            max_loss=(width - credit) * qty,
            ctx=_context(bot, index_px, hv),
        )
        bot.logger.info(
            f"  [PHANTOM-PREM] {bot.instrument}: SHORT iron fly {atm} +/-{width}"
            f" -- credit Rs{credit:.2f} x{qty} = Rs{credit*qty:,.0f}, max loss "
            f"Rs{(width-credit)*qty:,.0f}, breakevens {atm-credit:,.0f} / "
            f"{atm+credit:,.0f} ({credit/index_px*100:.2f}% move either way)"
        )
    except Exception as exc:
        try:
            bot.logger.debug(f"  [PHANTOM-PREM] open failed: {exc}")
        except Exception:
            pass


def mark(bot, index_px: float, hv: float, force_close: bool = False) -> None:
    """Mark the open structure against real quotes; close it on the rules."""
    try:
        pos = getattr(bot, '_pprem_open', None)
        if not pos:
            return
        now  = datetime.now(IST)
        legs = [('short', 'CALL', pos['strikes']['short_CALL']),
                ('short', 'PUT',  pos['strikes']['short_PUT']),
                ('long',  'CALL', pos['strikes']['long_CALL']),
                ('long',  'PUT',  pos['strikes']['long_PUT'])]
        got, why = _quote_all(bot, legs, index_px, hv)
        if got is None:
            # Cannot mark without real quotes. Hold unless forced out, in which
            # case settle at intrinsic -- on a defined-risk structure that is
            # exact, not an estimate.
            if not force_close:
                return
            cost = min(abs(index_px - pos['atm']), pos['width'])
            px = None
        else:
            px = got[0]
            cost = _net_cost(px)

        pnl_unit = pos['credit'] - cost
        pnl_pct  = pnl_unit / pos['credit'] if pos['credit'] else 0.0
        pos['peak_pnl']   = max(pos['peak_pnl'], pnl_unit)
        pos['trough_pnl'] = min(pos['trough_pnl'], pnl_unit)
        minutes = (now - pos['entry_time']).total_seconds() / 60.0

        # Self-detect the force-close. The bot's EOD branch is gated on
        # `self.positions` -- the CHAMPION's book -- so on a day with no live
        # trade (Oct 5 2026 was the first) the phantom never settled and the
        # session was lost. This book is independent and must close on its own
        # clock, whatever the champion did.
        if not force_close:
            fc = str(getattr(config, 'FORCE_CLOSE_TIME', '15:00'))
            if now.strftime('%H:%M') >= fc:
                force_close = True

        stop   = float(getattr(config, 'PHANTOM_PREM_STOP', 1.00))
        target = float(getattr(config, 'PHANTOM_PREM_TARGET', 0.50))
        reason = None
        if force_close:
            reason = f"EOD Force-Close ({getattr(config,'FORCE_CLOSE_TIME','14:30')})"
        elif pnl_pct <= -stop:
            reason = f"Stop ({stop*100:.0f}% of credit)"
        elif pnl_pct >= target:
            reason = f"Target ({target*100:.0f}% of credit)"
        if not reason:
            return

        costs = 0.0                      # four legs, each a round trip
        if round_trip_costs:
            for r, t, _ in legs:
                e = pos['entry_px'][f'{r}_{t}']
                x = px[(r, t)] if px else e
                costs += round_trip_costs(e, x, pos['qty'])
        pnl_net = pnl_unit * pos['qty'] - costs

        _write(pos['instrument'], dict(
            strategy='phantom_premium', structure='short_iron_fly',
            instrument=pos['instrument'], date=pos['date'],
            entry_time=pos['entry_time'].isoformat(), exit_time=now.isoformat(),
            held_min=round(minutes, 1),
            atm=pos['atm'], width=pos['width'], lots=pos['lots'], qty=pos['qty'],
            entry_index=round(pos['entry_index'], 2), exit_index=round(index_px, 2),
            index_move_pct=round((index_px - pos['entry_index'])
                                 / pos['entry_index'] * 100, 3),
            credit=round(pos['credit'], 2), exit_cost=round(cost, 2),
            breakeven_pct=round(pos['credit'] / pos['entry_index'] * 100, 3),
            pnl_unit=round(pnl_unit, 2), pnl_pct_of_credit=round(pnl_pct * 100, 2),
            peak_pnl=round(pos['peak_pnl'], 2), trough_pnl=round(pos['trough_pnl'], 2),
            max_loss=round(pos['max_loss'], 2),
            costs=round(costs, 2), pnl_net=round(pnl_net, 2),
            exit_reason=reason, px_src='LTP' if px else 'intrinsic-settle',
            entry_px=pos['entry_px'], strikes=pos['strikes'],
            **(pos.get('ctx') or {}),
        ))
        verdict = "KEPT" if pnl_net > 0 else "LOST"
        bot.logger.info(
            f"  [PHANTOM-PREM] {bot.instrument} short iron fly {verdict} "
            f"Rs{pnl_net:+,.0f} ({pnl_pct*100:+.0f}% of credit) -- {reason} | "
            f"index moved {(index_px-pos['entry_index'])/pos['entry_index']*100:+.2f}%"
            f" vs {pos['credit']/pos['entry_index']*100:.2f}% breakeven"
        )
        bot._pprem_open = None
    except Exception as exc:
        try:
            bot.logger.debug(f"  [PHANTOM-PREM] mark failed: {exc}")
        except Exception:
            pass


def reset_day(bot) -> None:
    bot._pprem_open = None
    bot._pprem_done = False
    bot._pprem_stk_cache = None
