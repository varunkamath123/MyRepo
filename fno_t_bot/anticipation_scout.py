# -*- coding: utf-8 -*-
"""
anticipation_scout.py — Anticipation-first entry engine (SHADOW).
==================================================================
The book's confirmation paths (A/B) enter AFTER a breakout proves itself,
which structurally buys the extreme (chase_pos ~0.9+) and — per 52-trade
live analysis — bleeds (-EV). The only +EV live path (REV) works because
it ANTICIPATES: it buys the reversal at exhaustion, before it's proven.

This module generalises that: enter at a LEVEL that price is HOLDING,
in the direction of the hold, BEFORE the move — defined risk at the level.
  - Support hold  -> CALL  (price tested support below, rejected up)
  - Resistance rej-> PUT   (price tested resistance above, rejected down)

SHADOW ONLY. Never places a Fyers order. It logs each would-be entry with
its level/stop/target (in UNDERLYING terms) and tracks the UNDERLYING to
resolution — so it measures setup quality (does anticipation predict
direction?) with no option pricing, no API load, no BS distortion. A rough
rupee P&L uses ATM delta 0.5 x underlying-move x lot for comparability.

Validation question it answers over ~2-3 weeks of live bars:
  do anticipation entries (enter-at-level) beat confirmation entries
  (enter-at-extreme) on the SAME live tape?
If yes, we promote it to a live-order path. If no, we've learned it for free.

Called once per bar from options_bot main loop (guarded by
config.ANTICIPATION_SHADOW_ENABLED), same pattern as reversal_scout.
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime
from typing import Optional

import pandas as pd
import pytz

IST = pytz.timezone('Asia/Kolkata')

# ── Tunables (shadow — safe to iterate) ──────────────────────────────────────
WINDOW_START   = dtime(9, 45)    # after OR forms, before lunch drift
WINDOW_END     = dtime(13, 30)   # need >=60 min runway to 14:30
PROX_PCT       = 0.0030          # price within 0.30% of a level = "at the level"
TOUCH_PCT      = 0.0012          # recent bar wick within 0.12% of level = "tested it"
STOP_BEYOND    = 0.0015          # stop 0.15% beyond the level (defined risk)
RR             = 2.0             # target = RR x stop distance (2:1)
DI_MARGIN      = 6.0             # don't fight a strong opposing DI (falling-knife guard)
MAX_PER_DAY    = 2               # shadow trades per instrument per day
TOUCH_BARS     = 3               # look back this many bars for the level test
FORCE_CLOSE    = dtime(14, 30)

# ── Per-instrument shadow state (each bot = its own process) ─────────────────
_open:    dict[str, Optional[dict]] = {}
_count:   dict[str, int]            = {}
_pnl:     dict[str, float]          = {}
_last_ts: dict[str, object]         = {}


def daily_reset(instrument: str, logger: logging.Logger | None = None) -> None:
    prev = _pnl.get(instrument, 0.0)
    n    = _count.get(instrument, 0)
    if logger and n:
        logger.info(f"  [ANTICIP] {instrument}: yesterday {n} shadow setup(s), "
                    f"P&L=Rs{prev:+,.0f}")
    _open[instrument]  = None
    _count[instrument] = 0
    _pnl[instrument]   = 0.0


def _levels(instrument, price, or_high, or_low, vwap, oi_zones, pdh, pdl):
    """Return (supports_below, resistances_above) — sorted by proximity to price."""
    sup, res = [], []
    def add(container, val, name):
        try:
            v = float(val)
            if v > 0:
                container.append((v, name))
        except (TypeError, ValueError):
            pass
    # structural levels
    if or_low and price > or_low:   add(sup, or_low,  'OR_low')
    if or_high and price < or_high: add(res, or_high, 'OR_high')
    if vwap and price > vwap:        add(sup, vwap, 'VWAP')
    if vwap and price < vwap:        add(res, vwap, 'VWAP')
    if pdl and price > pdl:          add(sup, pdl, 'PDL')
    if pdh and price < pdh:          add(res, pdh, 'PDH')
    # OI walls (NIFTY/BANKNIFTY; SENSEX has none)
    if oi_zones:
        for s in oi_zones.get('support', []):
            k = s.get('strike')
            if k and float(k) < price and s.get('strength') in ('MAJOR', 'WALL'):
                add(sup, k, f"OIsup")
        for r in oi_zones.get('resistance', []):
            k = r.get('strike')
            if k and float(k) > price and r.get('strength') in ('MAJOR', 'WALL'):
                add(res, k, f"OIres")
    sup.sort(key=lambda x: price - x[0])   # nearest below first
    res.sort(key=lambda x: x[0] - price)   # nearest above first
    return sup, res


def evaluate_bar(instrument, df, oc, oi_zones, inst_cfg, logger, now,
                 or_high=None, or_low=None) -> None:
    """One-bar shadow evaluation. Wrapped in try/except by the caller."""
    if df is None or len(df) < TOUCH_BARS + 1:
        return
    bar_ts = df.index[-1]
    if _last_ts.get(instrument) == bar_ts:
        return                       # once per closed bar
    _last_ts[instrument] = bar_ts

    row    = df.iloc[-1]
    price  = float(row['Close'])
    lot    = int(inst_cfg.get('lot_size', 1))
    pdh    = float(row.get('PDH', 0) or 0)   # prev-day high (df column)
    pdl    = float(row.get('PDL', 0) or 0)   # prev-day low

    # ── update an open shadow setup ──────────────────────────────────────────
    pos = _open.get(instrument)
    if pos is not None:
        recent_hi = float(df['High'].iloc[-1])
        recent_lo = float(df['Low'].iloc[-1])
        done, exit_px, reason = False, price, None
        if pos['dir'] == 'CALL':
            if recent_lo <= pos['stop']:
                done, exit_px, reason = True, pos['stop'], 'Stop (level broke)'
            elif recent_hi >= pos['target']:
                done, exit_px, reason = True, pos['target'], 'Target'
        else:
            if recent_hi >= pos['stop']:
                done, exit_px, reason = True, pos['stop'], 'Stop (level broke)'
            elif recent_lo <= pos['target']:
                done, exit_px, reason = True, pos['target'], 'Target'
        if not done and now.time() >= FORCE_CLOSE:
            done, exit_px, reason = True, price, 'EOD'
        if done:
            move   = (exit_px - pos['entry']) if pos['dir'] == 'CALL' else (pos['entry'] - exit_px)
            pnl    = 0.5 * move * lot - 70.0        # ATM delta 0.5, ~Rs70 costs
            _pnl[instrument] = _pnl.get(instrument, 0.0) + pnl
            icon = '✅' if pnl > 0 else '❌'
            logger.info(
                f"  [ANTICIP-SHADOW] {icon} EXIT {pos['dir']} {instrument} | "
                f"{pos['level_name']}@{pos['level']:.0f} | {reason} | "
                f"idx {pos['entry']:.0f}->{exit_px:.0f} ({move:+.0f}pt) | "
                f"~Rs{pnl:+,.0f} | held {int((now - pos['t0']).total_seconds()/60)}m"
            )
            _open[instrument] = None
        return

    # ── look for a new setup ─────────────────────────────────────────────────
    if not (WINDOW_START <= now.time() <= WINDOW_END):
        return
    if _count.get(instrument, 0) >= MAX_PER_DAY:
        return

    vwap = float(row.get('VWAP', 0) or 0)
    dip  = float(row.get('DI_plus', 0) or 0)
    dim  = float(row.get('DI_minus', 0) or 0)
    adx  = float(row.get('ADX', 0) or 0)
    prev_close = float(df['Close'].iloc[-2])
    win_lo = float(df['Low'].iloc[-TOUCH_BARS:].min())
    win_hi = float(df['High'].iloc[-TOUCH_BARS:].max())

    sup, res = _levels(instrument, price, or_high, or_low, vwap, oi_zones, pdh, pdl)

    setup = None
    # Support hold -> CALL: near a support, recently tested it, now turning up,
    # and not being sold hard (DI+ not far below DI-).
    if sup:
        lvl, name = sup[0]
        near   = (price - lvl) / price <= PROX_PCT
        tested = (win_lo - lvl) / lvl <= TOUCH_PCT and win_lo >= lvl * (1 - STOP_BEYOND)
        holding= price > lvl and price >= prev_close
        ok_di  = dip >= dim - DI_MARGIN
        if near and tested and holding and ok_di:
            stop   = lvl * (1 - STOP_BEYOND)
            target = price + RR * (price - stop)
            setup  = dict(dir='CALL', level=lvl, level_name=name, stop=stop, target=target)
    # Resistance reject -> PUT
    if setup is None and res:
        lvl, name = res[0]
        near   = (lvl - price) / price <= PROX_PCT
        tested = (lvl - win_hi) / lvl <= TOUCH_PCT and win_hi <= lvl * (1 + STOP_BEYOND)
        holding= price < lvl and price <= prev_close
        ok_di  = dim >= dip - DI_MARGIN
        if near and tested and holding and ok_di:
            stop   = lvl * (1 + STOP_BEYOND)
            target = price - RR * (stop - price)
            setup  = dict(dir='PUT', level=lvl, level_name=name, stop=stop, target=target)

    if setup is None:
        return

    # chase_pos of this anticipation entry (for direct comparison vs breakouts)
    day = df[df.index.date == now.date()]
    d_hi, d_lo = float(day['High'].max()), float(day['Low'].min())
    rpos  = (price - d_lo) / (d_hi - d_lo) if d_hi > d_lo else 0.5
    chase = rpos if setup['dir'] == 'CALL' else 1.0 - rpos

    setup.update(entry=price, t0=now)
    _open[instrument]  = setup
    _count[instrument] = _count.get(instrument, 0) + 1
    logger.info(
        f"  [ANTICIP-SHADOW] ⚡ ENTRY {setup['dir']} {instrument} | "
        f"hold {setup['level_name']}@{setup['level']:.0f} | idx {price:.0f} | "
        f"stop {setup['stop']:.0f} target {setup['target']:.0f} | "
        f"ADX={adx:.0f} chase_pos={chase:.2f} | anticipatory entry (before breakout)"
    )
