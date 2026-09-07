# -*- coding: utf-8 -*-
"""PATH_MR — multi-day mean reversion. SHADOW ONLY, logs and never trades.

WHY THIS EXISTS
---------------
Intraday direction has no edge in this book, established five separate ways
(DSR 0.0% over 3,072 variants, power analysis, random-entry baseline p=0.857,
attribute scan AUC 0.44-0.51, SYNFUT ADX/DI a coin flip over 350 signals).
Higher-timeframe direction is the one place that has NOT come back null:

  rho(5-day trend, 11:00->14:30 move) = -0.1807  p=0.0002  n=417 sessions
  (one observation per session -- no overlapping windows inflating n)

The sign is negative: indices FADE their own multi-day move. Replicated
independently on all three instruments in the recent period --
NIFTY -0.215, BANKNIFTY -0.273, SENSEX -0.349 -- and it shows a dose-response,
with the edge over a coin flip rising with move size:

  |5-day move|   0.01-0.41%  0.42-0.79%  0.80-1.22%  1.22-1.95%  1.96-7.20%
  edge (ATR)       -0.108      +0.064      +0.019      +0.108      +0.309

Top decile alone: n=42, 47.6% win, +0.348 ATR vs control -0.190, p=0.0445.

WHY IT IS SHADOW AND NOT LIVE
-----------------------------
1. DSR still FAILS -- 68.5% for the tertile rule, 59.3% for the top decile.
   Nominal significance at n=42 is exactly the regime that produced 3,072
   dead variants. Being right about the mechanism is not the same as having
   the power to prove it.
2. It is REGIME-DEPENDENT and we cannot yet say whether the regime lasts.
   SENSEX alone, held constant across the whole span:
     Jun'25-Jan'26  rho -0.052  p=0.53   nothing
     Jan'26-Sep'26  rho -0.221  p=0.0067 real
   Rolling thirds strengthen monotonically (-0.079, -0.114, -0.289). Either
   mean reversion genuinely intensified in 2026, or this is non-stationarity
   that will revert. Nothing in the data distinguishes those yet.
3. The live book is currently measuring the rv_iv gate (v1.9.3). Adding a
   second new path would confound that read. Paper mode makes shadow free:
   there is no capital cost to logging instead of trading, and the forward
   sample is the only thing that can settle this.

WHAT WOULD PROMOTE IT
---------------------
Roughly 60-80 more logged sessions with the top-decile edge holding, and a
DSR that clears once those are in. Until then this writes one line per
session and touches nothing.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, time as dtime

import config

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')

MR_ENTRY_TIME   = '11:00'   # the entry the effect was measured at
MR_LOOKBACK     = 5         # trading days; 3 was weaker (rho -0.104 vs -0.164)
MR_TERTILE_PCT  = 0.68      # |5-day move| tertile cut, in percent
MR_DECILE_PCT   = 1.96      # |5-day move| top-decile cut -- the strong cohort
MR_STOP_ATR     = 1.0
MR_TARGET_ATR   = 2.0


def _write(inst: str, rec: dict) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    p = os.path.join(LOG_DIR, f'path_mr_{inst}_{rec["date"]}.jsonl')
    try:
        with open(p, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec) + '\n')
    except Exception:
        pass


_fired: dict = {}


def evaluate(bot, instrument: str, df, now: datetime, logger=None) -> dict | None:
    """Log the PATH_MR read once per session. Returns the signal or None.

    Never opens a position. The caller ignores the return value; it exists so
    the logic is unit-testable without a live bot.
    """
    if not getattr(config, 'PATH_MR_SHADOW_ENABLED', False):
        return None
    key = (instrument, now.strftime('%Y-%m-%d'))
    if key in _fired:
        return None
    hh, mm = map(int, MR_ENTRY_TIME.split(':'))
    if now.time() < dtime(hh, mm):
        return None

    # prior N sessions' closes, from the frame the bot already holds
    try:
        days = sorted({d.date() for d in df.index})
        if len(days) < MR_LOOKBACK + 1:
            return None
        today = days[-1]
        ref_day = days[-(MR_LOOKBACK + 1)]
        ref_close = float(df[df.index.date == ref_day]['Close'].iloc[-1])
        todays = df[df.index.date == today]
        if len(todays) < 2 or ref_close <= 0:
            return None
        day_open = float(todays['Open'].iloc[0])
        px = float(todays['Close'].iloc[-1])
        atr = float(todays['ATR'].iloc[-1] or 0)
    except Exception:
        return None
    if atr <= 0:
        return None

    trend_pct = (day_open - ref_close) / ref_close * 100.0
    mag = abs(trend_pct)
    if mag < MR_TERTILE_PCT:
        band = 'NEUTRAL'
        direction = None
    else:
        direction = 'PUT' if trend_pct > 0 else 'CALL'   # fade the move
        band = 'STRONG' if mag >= MR_DECILE_PCT else 'NORMAL'

    _fired[key] = True
    rec = dict(strategy='path_mr_shadow', instrument=instrument,
               date=now.strftime('%Y-%m-%d'), time=now.strftime('%H:%M:%S'),
               lookback_days=MR_LOOKBACK, trend_pct=round(trend_pct, 3),
               magnitude=round(mag, 3), band=band, direction=direction,
               index=round(px, 2), atr=round(atr, 2),
               stop_pts=round(MR_STOP_ATR * atr, 1),
               target_pts=round(MR_TARGET_ATR * atr, 1))
    _write(instrument, rec)
    if logger:
        if direction:
            logger.info(
                f"  [PATH-MR shadow] {instrument} {direction} ({band}) — "
                f"{MR_LOOKBACK}d move {trend_pct:+.2f}% → fade it | "
                f"idx {px:,.1f} ATR {atr:.1f} | stop {MR_STOP_ATR*atr:.0f}pts "
                f"target {MR_TARGET_ATR*atr:.0f}pts | LOGGED, NOT TRADED"
            )
        else:
            logger.info(
                f"  [PATH-MR shadow] {instrument} NEUTRAL — {MR_LOOKBACK}d move "
                f"{trend_pct:+.2f}% below {MR_TERTILE_PCT}% cut, no signal"
            )
    return rec if direction else None
