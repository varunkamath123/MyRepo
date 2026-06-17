# -*- coding: utf-8 -*-
"""
Path E — OI Breakout Scout
===========================
Fires a CALL or PUT entry when price breaks through a MAJOR/WALL OI level
with momentum confirmation — WITHOUT requiring an EMA 9/21 crossover.

Problem this solves
--------------------
EMA crossovers lag 2–5 bars (10–25 min) after a structural breakout.
By the time the EMA cross fires, the option premium has already moved 30–50%
and the risk/reward has deteriorated. Path E enters at the breakout candle
itself — the ideal entry point.

Entry conditions (no EMA cross required)
-----------------------------------------
  1. OI BOOST: price just broke through a MAJOR/WALL level (within 0.08%)
               detected via oi_zones.get_zone_signal()
  2. ADX >= PATH_E_ADX_MIN (28) — momentum confirms the move is real
  3. VWAP side: CALL only above VWAP, PUT only below VWAP
  4. HTF: at least one of 15m ST or 15m EMA must agree (not both opposing)
  5. Reversal Guard < 50 — not an already-exhausted move disguised as breakout

Timing: 10:00–14:00 IST (earlier than vX — catches pre-EMA breakouts)
Capital: PATH_E_CAPITAL = ₹10,000 per instrument (separate pool from A/B/C)
Sizing:  1 lot (conservative for new path; scale after paper validation)

Architecture
------------
  - Called from paper_bot.py every bar, independent of Path A/B/C positions
  - Per-bar dedup: evaluate_bar() exits immediately on duplicate bar timestamps
  - Separate _sim dict per instrument — concurrent with Path A/B/C is allowed
    (distinct capital pool; paper results will show if concurrency is a problem)
  - Daily reset at midnight: positions squared, capital restored, trade count reset

Exit rules: same as vX — Stop 40%, Target 130%, Trail from 55% / dist 20%,
            Force-close at FORCE_CLOSE_TIME (14:30)

Log tags:
  [PATH-E] ENTRY / EXIT / SKIP / NEAR-MISS
"""

from __future__ import annotations

import math
import logging
from datetime import time as dtime
from typing import Optional

import pandas as pd

import config
from oi_zones import get_zone_signal
from reversal_guard import compute_reversal_risk

# ── Constants (all overridable via config) ────────────────────────────────────

PATH_E_CAPITAL    = getattr(config, 'PATH_E_CAPITAL',    10_000)
PATH_E_ADX_MIN    = getattr(config, 'PATH_E_ADX_MIN',    28)
PATH_E_STOP       = getattr(config, 'PATH_E_STOP',       0.40)
PATH_E_TARGET     = getattr(config, 'PATH_E_TARGET',     1.30)
PATH_E_TRAIL_ACT  = getattr(config, 'PATH_E_TRAIL_ACT',  0.55)
PATH_E_TRAIL_DIST = getattr(config, 'PATH_E_TRAIL_DIST', 0.20)
PATH_E_MAX_TRADES = getattr(config, 'PATH_E_MAX_TRADES', 2)     # per instrument per day

_ES = getattr(config, 'PATH_E_ENTRY_START', '10:00')
_EE = getattr(config, 'PATH_E_ENTRY_END',   '14:00')
_ENTRY_START = dtime(*[int(x) for x in _ES.split(':')])
_ENTRY_END   = dtime(*[int(x) for x in _EE.split(':')])
_FORCE_CLOSE = dtime(*[int(x) for x in config.FORCE_CLOSE_TIME.split(':')])

# ── Module state (per-instrument) ─────────────────────────────────────────────
# All dicts keyed by instrument name ('NIFTY', 'BANKNIFTY', 'SENSEX')

_sim:          dict[str, Optional[dict]] = {}   # open sim position
_capital:      dict[str, float]          = {}   # remaining capital today
_last_bar_ts:  dict[str, object]         = {}   # dedup — last bar timestamp processed
_daily_trades: dict[str, int]            = {}   # trades taken today
_last_day:     dict[str, object]         = {}   # last trading day seen (for reset)


# ── Black-Scholes (calendar days) ─────────────────────────────────────────────

def _bs(opt_type: str, S: float, K: float, T: float, sigma: float,
        r: float = config.RISK_FREE_RATE) -> float:
    """Price a European option. T = DTE / 365 (calendar days)."""
    from scipy.stats import norm
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(S - K, 0.0) if opt_type == 'CALL' else max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt_type == 'CALL':
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def _round_trip_cost(entry_px: float, exit_px: float, units: int) -> float:
    bv = entry_px * units
    sv = exit_px  * units
    br = config.BROKERAGE_PER_ORDER * 2
    ex = (bv + sv) * config.NSE_EXCHANGE_CHARGE_RATE
    se = (bv + sv) * config.SEBI_CHARGES_RATE
    gt = (br + ex + se) * config.GST_RATE
    st = sv * config.STT_RATE
    sd = bv * config.STAMP_DUTY_RATE
    return br + ex + se + gt + st + sd


# ── Daily reset ───────────────────────────────────────────────────────────────

def _daily_reset(instrument: str, today) -> None:
    """Reset capital and trade count at the start of each new day."""
    if _last_day.get(instrument) == today:
        return
    _last_day[instrument]     = today
    _daily_trades[instrument] = 0
    _capital[instrument]      = PATH_E_CAPITAL
    # Square any leftover position from prior day (shouldn't exist with same-day exits)
    if _sim.get(instrument):
        _sim[instrument] = None


# ── Public entry point ────────────────────────────────────────────────────────

def evaluate_bar(
    instrument    : str,
    df            : pd.DataFrame,       # OHLCV + indicators, latest row = current bar
    htf           : dict,               # get_htf_context() → {supertrend_15m, ema_15m_trend}
    oi_zones      : Optional[dict],     # load_zones() result (may be None)
    inst_cfg      : dict,               # config.INSTRUMENTS[instrument]
    hv            : float,              # historical volatility (annualised)
    logger        : logging.Logger,
    now,                                # datetime (IST)
    in_window     : bool,               # True if within paper_bot's main loop window
    days_to_expiry: int,
) -> None:
    """
    Called every bar by paper_bot.py.
    Manages full Path E lifecycle: detect breakout → enter → manage → exit.
    """
    today = now.date()
    _daily_reset(instrument, today)

    # ── Always check open position (even outside entry window) ────────────────
    _check_exit(instrument, df, hv, logger, now, days_to_expiry, inst_cfg)

    # ── Entry gate ────────────────────────────────────────────────────────────
    if _sim.get(instrument):
        return   # already holding a Path E position

    if not in_window:
        return

    t = now.time()
    if not (_ENTRY_START <= t <= _ENTRY_END):
        return

    if _daily_trades.get(instrument, 0) >= PATH_E_MAX_TRADES:
        return

    # ── Per-bar dedup (don't re-evaluate the same bar on every 60s poll) ─────
    bar_ts = df.index[-1]
    if _last_bar_ts.get(instrument) == bar_ts:
        return
    _last_bar_ts[instrument] = bar_ts

    # ── No OI data → cannot detect wall breakout ──────────────────────────────
    if not oi_zones:
        return

    price = float(df.iloc[-1]['Close'])
    row   = df.iloc[-1]

    # ── 1. OI wall breakout detection ─────────────────────────────────────────
    # Check both directions: whichever gets a BOOST = just broke a wall
    signal_type : Optional[str] = None
    oz_result   : dict          = {}

    for stype in ('CALL', 'PUT'):
        oz = get_zone_signal(price, stype, oi_zones, dte=days_to_expiry)
        if oz['action'] == 'BOOST':
            signal_type = stype
            oz_result   = oz
            break

    if signal_type is None:
        return   # no wall breakout on this bar

    oz_reason = oz_result.get('reason', '')

    # ── 2. ADX momentum confirmation ──────────────────────────────────────────
    adx = float(row.get('ADX', 0) or 0)
    if adx < PATH_E_ADX_MIN:
        logger.info(
            f"  [PATH-E] {signal_type:4s} {instrument} | "
            f"OI breakout — ADX={adx:.1f} < {PATH_E_ADX_MIN} (weak momentum) | SKIP"
        )
        return

    # ── 3. VWAP directional filter ────────────────────────────────────────────
    vwap = float(row.get('VWAP', 0) or 0)
    if vwap > 0:
        if signal_type == 'CALL' and price < vwap:
            logger.info(
                f"  [PATH-E] {signal_type:4s} {instrument} | "
                f"CALL breakout but Px={price:,.0f} below VWAP={vwap:,.0f} | SKIP"
            )
            return
        if signal_type == 'PUT' and price > vwap:
            logger.info(
                f"  [PATH-E] {signal_type:4s} {instrument} | "
                f"PUT breakout but Px={price:,.0f} above VWAP={vwap:,.0f} | SKIP"
            )
            return

    # ── 4. HTF alignment (at least one indicator must agree) ──────────────────
    st_val   = htf.get('supertrend_15m')    # +1=BULL, -1=BEAR, None
    ema_15m  = htf.get('ema_15m_trend')     # 'BULL', 'BEAR', None
    expected = 'BULL' if signal_type == 'CALL' else 'BEAR'

    st_agree  = (st_val  is not None) and ({1:'BULL',-1:'BEAR'}.get(st_val) == expected)
    ema_agree = (ema_15m is not None) and (ema_15m == expected)

    # Both indicators present and BOTH opposing → skip
    if st_val is not None and ema_15m is not None and not st_agree and not ema_agree:
        logger.info(
            f"  [PATH-E] {signal_type:4s} {instrument} | "
            f"OI breakout — 15m ST={st_val} EMA={ema_15m} both oppose {signal_type} | SKIP"
        )
        return

    htf_note = (
        f"15m {'✓✓' if st_agree and ema_agree else '✓?' if st_agree or ema_agree else '?'}"
    )

    # ── 5. Reversal Guard — reject exhausted fakeouts ─────────────────────────
    try:
        rev = compute_reversal_risk(df, len(df) - 1, signal_type)
        if rev['skip']:
            logger.info(
                f"  [PATH-E] {signal_type:4s} {instrument} | "
                f"OI breakout — RevGuard HIGH ({rev['score']}/100) exhaustion | SKIP"
            )
            return
        rev_note = f"RevGuard={rev['score']}/100({rev['risk_level']})"
    except Exception:
        rev_note = 'RevGuard=err'

    # ── 6. Capital + sizing ───────────────────────────────────────────────────
    gap        = int(inst_cfg.get('strike_gap', 50))
    lot_size   = int(inst_cfg.get('lot_size', 1))
    atm_strike = int(round(price / gap) * gap)
    T          = max(days_to_expiry / 365, 0.001)
    premium    = _bs(signal_type, price, atm_strike, T, hv)

    if premium < config.MIN_OPTION_PRICE:
        logger.info(
            f"  [PATH-E] {signal_type:4s} {instrument} | "
            f"Premium ₹{premium:.2f} < min ₹{config.MIN_OPTION_PRICE} | SKIP"
        )
        return

    avail    = _capital.get(instrument, PATH_E_CAPITAL)
    max_lots = max(1, int(avail / (premium * lot_size)))
    lots     = min(1, max_lots)   # 1 lot — scale after paper validation
    cost     = premium * lot_size * lots

    if cost > avail:
        logger.info(
            f"  [PATH-E] {signal_type:4s} {instrument} | "
            f"Insufficient capital ₹{avail:.0f} < cost ₹{cost:.0f} | SKIP"
        )
        return

    # ── 7. Enter ──────────────────────────────────────────────────────────────
    _sim[instrument] = {
        'signal_type'  : signal_type,
        'strike'       : atm_strike,
        'entry_premium': premium,
        'lots'         : lots,
        'lot_size'     : lot_size,
        'entry_time'   : now,
        'trail_high'   : premium,
        'trailing'     : False,
        'stop'         : premium * (1 - PATH_E_STOP),
        'target'       : premium * (1 + PATH_E_TARGET),
    }
    _capital[instrument]      = avail - cost
    _daily_trades[instrument] = _daily_trades.get(instrument, 0) + 1

    logger.info(
        f"  [PAPER][PATH-E] ENTRY {signal_type:4s} {instrument} | "
        f"Px={price:,.0f} | Strike={atm_strike} | Option=₹{premium:.2f} | "
        f"ADX={adx:.1f} | {htf_note} | {rev_note} | Lots={lots} | "
        f"Wall: {oz_reason}"
    )


# ── Exit management ───────────────────────────────────────────────────────────

def _check_exit(
    instrument    : str,
    df            : pd.DataFrame,
    hv            : float,
    logger        : logging.Logger,
    now,
    days_to_expiry: int,
    inst_cfg      : dict,
) -> None:
    """Evaluate stop, target, trail, and forced exits for an open Path E position."""
    pos = _sim.get(instrument)
    if not pos:
        return

    t      = now.time()
    price  = float(df.iloc[-1]['Close'])
    T      = max(days_to_expiry / 365, 0.001)
    opt_px = _bs(pos['signal_type'], price, pos['strike'], T, hv)
    entry  = pos['entry_premium']

    if entry <= 0:
        return

    gain_pct = (opt_px - entry) / entry

    # Update trailing high
    if opt_px > pos['trail_high']:
        pos['trail_high'] = opt_px

    # Activate trailing stop
    if not pos['trailing'] and gain_pct >= PATH_E_TRAIL_ACT:
        pos['trailing'] = True
        logger.info(
            f"  [PATH-E] {pos['signal_type']:4s} {instrument} | "
            f"Trailing activated at +{gain_pct*100:.1f}% | "
            f"Peak=₹{pos['trail_high']:.2f}"
        )

    # Check exit conditions
    exit_reason: Optional[str] = None
    if t >= _FORCE_CLOSE:
        exit_reason = 'Force-Close'
    elif gain_pct <= -PATH_E_STOP:
        exit_reason = 'Stop-Loss'
    elif gain_pct >= PATH_E_TARGET:
        exit_reason = 'Target'
    elif pos['trailing'] and opt_px < pos['trail_high'] * (1 - PATH_E_TRAIL_DIST):
        exit_reason = 'Trail'

    if exit_reason is None:
        return

    # Settle position
    units = pos['lots'] * pos['lot_size']
    cost  = _round_trip_cost(entry, opt_px, units)
    pnl   = (opt_px - entry) * units - cost

    # Return entry capital + P&L to pool
    _capital[instrument] = (
        _capital.get(instrument, PATH_E_CAPITAL) + (entry * units) + pnl
    )

    logger.info(
        f"  [PAPER][PATH-E] EXIT  {pos['signal_type']:4s} {instrument} | "
        f"Entry=₹{entry:.2f} → Exit=₹{opt_px:.2f} | "
        f"P&L=₹{pnl:+,.2f} ({gain_pct*100:+.1f}%) | Costs=₹{cost:.2f} | "
        f"{exit_reason}"
    )

    _sim[instrument] = None
