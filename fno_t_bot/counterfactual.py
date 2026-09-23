# -*- coding: utf-8 -*-
"""Counterfactual book — "what if that blocked signal had been taken?"

WHY
---
Every gate in this bot is an assumption that some trades are not worth taking,
and none of them has ever been measured live. Sep 15 2026 is the case that
forced this: all three indices fell ~1.9% and closed on their lows, PATH_REV
produced the correct PUT direction all day, and PATH_REV_MAX_CHASE refused every
one of them as "the turn is already 99% done". SENSEX still had +494 index
points of PUT left after that reading. We had no way to price that mistake,
because a blocked signal left no trace beyond a log line.

This module opens a PHANTOM position whenever a gate refuses a signal, marks it
against real traded premiums on the same cycle as the live book, and closes it
on the same exit stack. The result is a per-gate P&L record: over enough
sessions it says, with numbers, whether each gate earns its keep.

WHAT IT IS NOT
--------------
Not a trading path. Nothing here places an order, touches self.positions,
affects sizing, or feeds any signal. It only reads quotes and writes a log. If
it throws, the caller swallows it -- a broken measurement must never stop the
bot from trading.

DEDUPE
------
A gate typically refuses the same setup on every cycle (Sep 15: the same REV
block logged 10-20x per instrument). One phantom is opened per
(instrument, date, gate, direction) so the record answers "would this SETUP
have worked", not "how many times did we log it".

READING THE OUTPUT
------------------
logs/counterfactual_<INSTRUMENT>_<date>.jsonl, one row per closed phantom:
  gate        which guard refused it (RV_IV, REV_CHASE, CHASE, RISK, TREND_LEG)
  pnl_pct     what the blocked trade would have returned, on real premiums
  pnl_net     the same in rupees, net of the usual round-trip costs
  exit_reason which exit rule would have closed it
A gate whose phantoms are consistently negative is doing its job. One whose
phantoms are consistently positive is costing money and should be challenged.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

import config

try:
    from options_bot import bs_price, round_trip_costs, IST
except Exception:                       # imported before options_bot is ready
    bs_price = round_trip_costs = IST = None


def _log_dir() -> str:
    return getattr(config, 'LOG_DIRECTORY', 'logs')


def _write(instrument: str, rec: dict) -> None:
    try:
        os.makedirs(_log_dir(), exist_ok=True)
        p = os.path.join(_log_dir(),
                         f'counterfactual_{instrument}_{rec["date"]}.jsonl')
        with open(p, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(rec) + '\n')
    except Exception:
        pass


def record(bot, direction: str, index_px: float, gate: str, reason: str,
           hv: float, extra: dict | None = None) -> None:
    """Open a phantom position for a signal a gate just refused.

    Safe to call from anywhere; never raises into the caller.
    """
    try:
        if not getattr(config, 'COUNTERFACTUAL_ENABLED', False):
            return
        now = datetime.now(IST)
        day = now.strftime('%Y-%m-%d')
        if not hasattr(bot, '_phantoms'):
            bot._phantoms = []
            bot._phantom_seen = set()
        key = (bot.instrument, day, gate, direction)
        if key in bot._phantom_seen:
            return                       # same setup, already recorded today
        if now.strftime('%H:%M') >= getattr(config, 'FORCE_CLOSE_TIME', '14:30'):
            return                       # no runway left to simulate

        strike = int(round(index_px / bot.strike_gap) * bot.strike_gap)
        px, sym, src = bot._quote_option(strike, direction, index_px, hv)
        if not px or px < getattr(config, 'MIN_OPTION_PRICE', 1.0):
            return

        bot._phantom_seen.add(key)
        bot._phantoms.append(dict(
            instrument=bot.instrument, date=day, gate=gate, reason=reason,
            type=direction, strike=strike, symbol=sym, px_src=src,
            entry_time=now, entry_price=float(px), entry_index=float(index_px),
            lot_size=bot.lot_size, peak_pct=0.0, extra=(extra or {}),
        ))
        bot.logger.info(
            f"  [COUNTERFACTUAL] {bot.instrument} {direction} blocked by {gate} "
            f"— tracking phantom {strike} @ ₹{px:.2f} ({src}) to see what "
            f"it would have done"
        )
    except Exception as exc:
        try:
            bot.logger.debug(f"  [COUNTERFACTUAL] record failed: {exc}")
        except Exception:
            pass



def record_undirected(bot, index_px: float, gate: str, reason: str, hv: float,
                      extra: dict | None = None) -> None:
    """For gates that fire BEFORE any signal exists, so there is no direction.

    VIX-GATE is the case: it sets can_enter=False before any path is evaluated,
    so nothing says whether we would have gone long or short. Guessing a
    direction (e.g. from the 15m SuperTrend) would invent a counterfactual the
    bot never actually formed -- REV fades that trend and TREND follows it.

    Instead record BOTH legs. The analysis then bounds the gate:
      both legs negative -> the gate was right whichever way we would have gone
      both positive      -> it cost us regardless
      split              -> the outcome hinged on direction, which this gate
                            was never deciding, so it says nothing either way
    Cheap: two phantoms per instrument per day, deduped like any other.
    """
    for d in ('CALL', 'PUT'):
        record(bot, d, index_px, gate, reason, hv,
               extra=dict(extra or {}, undirected=True))

def _exit_reason(pos, pnl_pct: float, minutes: float, force: bool) -> str | None:
    """The live exit stack, applied identically to phantoms."""
    if force:
        return f"EOD Force-Close ({getattr(config, 'FORCE_CLOSE_TIME', '14:30')})"
    if pnl_pct <= -config.STOP_LOSS:
        return f"Stop-Loss ({config.STOP_LOSS*100:.0f}%)"
    if pnl_pct >= config.BASE_TARGET:
        return f"Target ({config.BASE_TARGET*100:.0f}%)"
    if (getattr(config, 'USE_TRAILING_PROFIT', True)
            and pos['peak_pct'] > config.TRAILING_ACTIVATION
            and pnl_pct < pos['peak_pct'] - config.TRAILING_DISTANCE):
        return "Trailing Stop"
    if (getattr(config, 'NEVER_PROGRESS_ENABLED', False)
            and pnl_pct < 0
            and pos['peak_pct'] < getattr(config, 'NEVER_PROGRESS_MIN_PEAK', 0.05)
            and minutes >= getattr(config, 'NEVER_PROGRESS_MINUTES', 45)):
        return (f"Never-Progressed ({minutes:.0f}m, "
                f"peak +{pos['peak_pct']*100:.1f}%)")
    return None


def mark(bot, index_px: float, hv: float, force_close: bool = False) -> None:
    """Mark open phantoms against real premiums and close them on the live rules."""
    try:
        if not getattr(config, 'COUNTERFACTUAL_ENABLED', False):
            return
        phantoms = getattr(bot, '_phantoms', None)
        if not phantoms:
            return
        now = datetime.now(IST)
        still = []
        for pos in phantoms:
            cur = None
            if pos.get('symbol') and bot.fyers:
                try:
                    from fyers_orders import get_ltp
                    v = get_ltp(bot.fyers, pos['symbol'])
                    if v and v > 0:
                        cur = float(v)
                except Exception:
                    cur = None
            if cur is None:
                T = max(config.DAYS_TO_EXPIRY
                        - (now - pos['entry_time']).total_seconds() / 86400,
                        0.01) / 365
                cur = bs_price(pos['type'], index_px, pos['strike'], T, hv)
            pnl_pct = (cur - pos['entry_price']) / pos['entry_price']
            pos['peak_pct'] = max(pos['peak_pct'], pnl_pct)
            minutes = (now - pos['entry_time']).total_seconds() / 60.0
            why = _exit_reason(pos, pnl_pct, minutes, force_close)
            if not why:
                still.append(pos)
                continue
            costs = round_trip_costs(pos['entry_price'], cur, pos['lot_size'])
            pnl_net = (cur - pos['entry_price']) * pos['lot_size'] - costs
            _write(pos['instrument'], dict(
                strategy='counterfactual', instrument=pos['instrument'],
                date=pos['date'], gate=pos['gate'], reason=pos['reason'],
                type=pos['type'], strike=pos['strike'],
                entry_time=pos['entry_time'].isoformat(),
                exit_time=now.isoformat(), held_min=round(minutes, 1),
                entry_price=round(pos['entry_price'], 2), exit_price=round(cur, 2),
                entry_index=round(pos['entry_index'], 2),
                exit_index=round(index_px, 2),
                pnl_pct=round(pnl_pct * 100, 2),
                peak_pct=round(pos['peak_pct'] * 100, 2),
                costs=round(costs, 2), pnl_net=round(pnl_net, 2),
                exit_reason=why, px_src=pos['px_src'], **pos.get('extra', {}),
            ))
            icon = "would have WON " if pnl_net > 0 else "would have LOST"
            bot.logger.info(
                f"  [COUNTERFACTUAL] {pos['instrument']} {pos['type']} blocked by "
                f"{pos['gate']}: {icon} ₹{pnl_net:+,.0f} "
                f"({pnl_pct*100:+.1f}%, peak {pos['peak_pct']*100:+.1f}%) — {why}"
            )
        bot._phantoms = still
    except Exception as exc:
        try:
            bot.logger.debug(f"  [COUNTERFACTUAL] mark failed: {exc}")
        except Exception:
            pass


def reset_day(bot) -> None:
    """Clear phantom state at the start of a session."""
    bot._phantoms = []
    bot._phantom_seen = set()
