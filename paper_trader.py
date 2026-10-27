"""
Kronos Futures — Daily Paper Trader (same-day close entry)
==========================================================
Runs once per day near market close (~15:20 IST) via cron.

Why near-close, same-day entry?
  The old flow generated a signal at 15:35 and only entered at the NEXT day's
  open — burning the overnight gap plus the first day of the forecast window.
  This version signals on the last 128 COMPLETE daily candles (through the last
  closed session) and enters immediately at the live price (~today's close),
  so the full multi-day forecast window is captured.

Workflow (single daily run):
  1. Fetch complete daily OHLCV (Upstox, fallback yfinance) — context bars
  2. Fetch live price (Upstox LTP) — the same-day entry/exit price
  3. PASS 1 — exits: for each open position, check stop/trail/Kronos-rev/ST-flip
     against the live price; close same-day if any fires
  4. PASS 2 — entries: if flat and no other position open, evaluate the Kronos
     signal; if all gates pass (incl. MiroFish news veto), ENTER at live price
  5. Log all actions to paper_trades.jsonl and print the daily summary

MiroFish news gate:
  Kronos reads price shape only — no news, macro, or flow awareness. Run
  mirofish_swarm.py before this script (see cron) to write mirofish_scores.json;
  a strongly opposing news lean vetoes an otherwise-qualified Kronos entry.
  Missing/stale MiroFish data does not block entries (gate is skipped, logged).

MiroFish shadow trades (shadow_state.json / shadow_trades.jsonl):
  When MiroFish is the ONLY gate that blocks an otherwise-qualified signal,
  a shadow Position opens at the same live price and is tracked under the
  exact same stop/trail/reversal/decay rules a real trade would follow --
  just outside the real one-slot-at-a-time capital constraint, so it never
  touches paper_state.json, capital, or realised_pnl. This answers "would
  MiroFish's veto have cost us money" with real forward data instead of
  guessing. Costs one extra Kronos call/day on days a real position already
  sits elsewhere, since shadow evaluation isn't gated by the real slot.
  See --status for shadow P&L alongside real P&L.

Morning news-exit scan (--morning-scan):
  Closes the ~24h blind spot where an open position's only qualitative check
  is the prior close-time run. Run mirofish_swarm.py again pre-market
  (~08:45 IST), then paper_trader.py --morning-scan (~09:20 IST): if the
  fresh score has flipped strongly against a held position, it exits early
  with reason NEWS_EXIT. Thresholds are tighter than the entry veto — this
  only fires on a clear overnight flip, not a moderate contradiction.

CONFIDENCE_DECAY — fires green or red (changed Oct 14 2026):
  A day's fresh Kronos read can quietly stop supporting the held direction
  (weak same-direction, NEUTRAL, or a sub-threshold opposing lean) without
  ever crossing the KRONOS_REV bar. This is conviction-based, not calendar-
  based — a time-based exit was tried and dropped because the strategy's
  best trades held 48 and 56 days, well past any defensible calendar cap.

  It used to be profit-gated (fire only when unrealized P&L >= 0, on the
  theory that the stop-loss should be the only loss-cutting mechanism). That
  left a position underwater from day one (the Aug 27 - Sep 9 2026 NIFTY
  trade, -43,046) with no way to act on vanished conviction, and the Oct 12
  BANKNIFTY short went NEUTRAL within a day with the same problem. The gate
  was removed on instruction.

  Whether firing at a loss actually helps is tracked, not assumed: when
  decay closes a RED position, a counterfactual copy keeps running under the
  OLD profit-gated rule (decay_cf_state.json / decay_cf_trades.jsonl, no
  capital at stake) until the old rules close it. Each closed pair records
  real P&L (decay fired) vs not-fired P&L; the running "value of firing
  decay" total is in --status. Decay exits taken while green aren't tracked
  in that book since old and new rules agree there.

  Oct 27 2026: the first closed pair came back at -28,023 (holding would have
  recovered to +1,006), but a lone pair settles nothing, so the rule stays and
  a second arm was added: EVERY decay exit (green or red) also keeps a copy
  that only decays after 2 consecutive unsupported reads. --status compares
  the live rule with that hysteresis rule on forward data.

Blocked-read shadow book (blocked_shadow_state.json / blocked_shadow_trades.jsonl):
  A read that clears the confidence bar but is stopped ONLY by the ADX or
  SuperTrend-alignment gate opens a shadow position at the live price, tracked
  under the live exit rules, no capital at stake (tagged ADX / ST / ADX+ST).
  The ADX gate runs before Kronos, so the live path never saw the read behind
  it -- evaluate_signal(diag=...) now takes that read (logging only; decisions
  unchanged). daily_reads.jsonl logs every gate-chain evaluation. Rule fixed in
  advance: no gate change on fewer than 30 closed trades per gate, and only if
  the result holds in BOTH chronological halves (3 weeks of hindsight = n=3).

Predicted target / risk:reward (instrumentation, not yet a gate):
  score_forecast() in core/candle_patterns.py already computes a price
  target, stop level, and risk:reward from Kronos's own predicted candles on
  every call -- previously computed and discarded. get_signal() now returns
  it; every qualified entry logs predicted_target/predicted_risk_reward/
  predicted_profit_inr, and every exit logs the predicted profit alongside
  the actual one. Not yet used to filter entries -- collecting real R:R data
  first (same approach as the hold-days check) before picking a threshold,
  rather than guessing one.

Capital: one lot per instrument, max one open position across all instruments.

Run manually:
    python paper_trader.py                  # daily run (signal + same-day entry/exit)
    python paper_trader.py --status         # print current positions + P&L
    python paper_trader.py --reset          # wipe all state (fresh start)
    python paper_trader.py --sensex         # include SENSEX in active instruments
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))

from backtest import (
    INSTRUMENT_PARAMS, _DEFAULT_PARAMS, EXIT_ON_KRONOS_REVERSAL,
    EXIT_ON_SUPERTREND_FLIP, MIN_ADX, FORECAST_BARS,
    compute_adx, supertrend, fetch_daily, get_signal,
)
from config import INSTRUMENTS

# Live Upstox data (complete daily bars + LTP). Falls back to yfinance if unavailable.
try:
    from brokers.upstox_data import load_daily_ohlcv as _upstox_daily, get_ltp as _upstox_ltp
    _UPSTOX_OK = True
except Exception:
    _UPSTOX_OK = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

TOTAL_CAPITAL     = 350_000          # INR — ₹3.5L paper capital
STATE_FILE        = "paper_state.json"
TRADES_LOG        = "paper_trades.jsonl"

# Instruments active for paper trading (SENSEX added if --sensex flag used)
ACTIVE_INSTRUMENTS = ["NIFTY", "BANKNIFTY"]

LOT_SIZES = {
    "NIFTY":     65,
    "BANKNIFTY": 30,
    "SENSEX":    20,
}

# MiroFish news veto: only block on a fairly strong OPPOSING lean, not a mild
# one — this is a veto for outright contradiction, not a confirmation filter.
MIROFISH_FILE            = Path(__file__).parent / "mirofish_scores.json"
MIROFISH_MAX_AGE_HOURS   = 6      # ignore stale data (e.g. yesterday's run)
MIROFISH_BEARISH_VETO    = 0.35   # score below this vetoes a LONG entry
MIROFISH_BULLISH_VETO    = 0.65   # score above this vetoes a SHORT entry

# Morning news-exit scan: closes the ~24h blind spot where an open position
# gets its only qualitative check at the prior close-time run. Thresholds are
# tighter than the entry veto — an existing position should only be cut early
# on a clear overnight flip, not the same moderate contradiction that would
# have merely blocked a new entry.
MORNING_NEWS_EXIT_BEARISH = 0.25  # LONG position exits if score <= this
MORNING_NEWS_EXIT_BULLISH = 0.75  # SHORT position exits if score >= this

# ── State schema ──────────────────────────────────────────────────────────────

@dataclass
class Position:
    instrument:    str
    direction:     str
    entry_date:    str          # ISO date string
    entry_price:   float
    lot_size:      int
    hwm:           float        # high-water mark for trail
    trail_active:  bool
    trail_stop:    float
    predicted_target:      float = 0.0   # Kronos's predicted close at entry, for later calibration
    predicted_risk_reward: float = 0.0   # Kronos's predicted R:R at entry
    entry_confidence:      float = 0.0   # Kronos confidence at entry -- baseline for decay tracking
    unsupported_streak:    int = 0       # consecutive daily reads that no longer support the position

@dataclass
class PaperState:
    capital:       float
    positions:     dict[str, Position]    # instrument -> Position
    realised_pnl:  float
    trade_count:   int


def _load_state() -> PaperState:
    if Path(STATE_FILE).exists():
        raw = json.loads(Path(STATE_FILE).read_text())
        positions = {k: Position(**v) for k, v in raw.get("positions", {}).items()}
        return PaperState(
            capital=raw["capital"],
            positions=positions,
            realised_pnl=raw.get("realised_pnl", 0.0),
            trade_count=raw.get("trade_count", 0),
        )
    return PaperState(
        capital=TOTAL_CAPITAL,
        positions={},
        realised_pnl=0.0,
        trade_count=0,
    )


def _save_state(state: PaperState):
    raw = {
        "capital":      state.capital,
        "realised_pnl": state.realised_pnl,
        "trade_count":  state.trade_count,
        "positions":    {k: asdict(v) for k, v in state.positions.items()},
    }
    Path(STATE_FILE).write_text(json.dumps(raw, indent=2))


# ── MiroFish shadow trades ──────────────────────────────────────────────────
# Tracks what WOULD have happened if a MiroFish veto hadn't blocked an entry
# that every other gate had already qualified. Reuses Position/check_exit
# unchanged -- a shadow trade obeys the exact same stop/trail/reversal/decay
# rules a real trade would, just outside the real one-slot-at-a-time capital
# constraint (it doesn't touch paper_state.json, capital, or realised_pnl).

SHADOW_STATE_FILE  = Path(__file__).parent / "shadow_state.json"
SHADOW_TRADES_LOG  = Path(__file__).parent / "shadow_trades.jsonl"

@dataclass
class ShadowState:
    positions:    dict[str, Position]   # instrument -> Position (reused as-is)
    realised_pnl: float
    trade_count:  int


def _load_shadow_state() -> ShadowState:
    if SHADOW_STATE_FILE.exists():
        raw = json.loads(SHADOW_STATE_FILE.read_text())
        positions = {k: Position(**v) for k, v in raw.get("positions", {}).items()}
        return ShadowState(
            positions=positions,
            realised_pnl=raw.get("realised_pnl", 0.0),
            trade_count=raw.get("trade_count", 0),
        )
    return ShadowState(positions={}, realised_pnl=0.0, trade_count=0)


def _save_shadow_state(state: ShadowState):
    raw = {
        "realised_pnl": state.realised_pnl,
        "trade_count":  state.trade_count,
        "positions":    {k: asdict(v) for k, v in state.positions.items()},
    }
    SHADOW_STATE_FILE.write_text(json.dumps(raw, indent=2))


def _log_shadow_trade(record: dict):
    with open(SHADOW_TRADES_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")


# ── CONFIDENCE_DECAY counterfactual book ────────────────────────────────────
# Live rule (since Oct 14 2026): CONFIDENCE_DECAY fires green or red. Whenever
# it closes a position that was RED, the old rule would have kept holding it,
# so a counterfactual copy of that position keeps running under the old
# profit-gated rule (check_exit(..., decay_requires_profit=True)) until the old
# rules close it. Each closed pair gives: real P&L (decay fired) vs
# counterfactual P&L (decay had not fired); delta > 0 means firing helped.
# Decay exits taken while GREEN are not tracked -- old and new rules agree.

DECAY_CF_FILE = Path(__file__).parent / "decay_cf_state.json"
DECAY_CF_LOG  = Path(__file__).parent / "decay_cf_trades.jsonl"


def _load_decay_cf() -> dict:
    cf = (json.loads(DECAY_CF_FILE.read_text()) if DECAY_CF_FILE.exists()
          else {"open": {}, "closed": 0, "sum_real": 0.0, "sum_cf": 0.0})
    # Second arm (added Oct 27 2026): EVERY decay exit, green or red, keeps a
    # copy running under "decay only after 2 consecutive unsupported days", so
    # a hysteresis rule can be compared with the live one on forward data.
    cf.setdefault("two_day", {"open": {}, "closed": 0, "sum_real": 0.0, "sum_arm": 0.0})
    return cf


def _save_decay_cf(cf: dict):
    DECAY_CF_FILE.write_text(json.dumps(cf, indent=2))


def _log_decay_cf(record: dict):
    with open(DECAY_CF_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")


# ── Blocked-read shadow book + daily read log ───────────────────────────────
# A read that clears the confidence bar but is stopped ONLY by a trend gate
# (ADX >= MIN_ADX, SuperTrend alignment) opens a shadow position at the same
# live price, tracked under the live exit rules, outside the real capital slot.
# Purpose: hindsight on Oct 9-26 2026 showed 2 blocked winners and 1 passed
# loser (n=3), too thin to retune a gate on; this builds the cohort forward.
# Evaluation rule (fixed in advance): no gate change on fewer than 30 closed
# trades per gate, and only if the result holds in BOTH chronological halves.
# daily_reads.jsonl records every gate-chain evaluation (incl. the Kronos read
# behind an ADX block, which the live path never consults).

BLOCKED_STATE_FILE = Path(__file__).parent / "blocked_shadow_state.json"
BLOCKED_TRADES_LOG = Path(__file__).parent / "blocked_shadow_trades.jsonl"
DAILY_READS_LOG    = Path(__file__).parent / "daily_reads.jsonl"


def _load_blocked() -> dict:
    if BLOCKED_STATE_FILE.exists():
        b = json.loads(BLOCKED_STATE_FILE.read_text())
    else:
        b = {}
    b.setdefault("positions", {})   # instrument -> {"pos": asdict(Position), "blocked_by": "ADX"|"ST"|"ADX+ST"}
    b.setdefault("stats", {})       # blocked_by -> {"n", "wins", "pnl"}
    return b


def _save_blocked(b: dict):
    BLOCKED_STATE_FILE.write_text(json.dumps(b, indent=2))


def _log_blocked(record: dict):
    with open(BLOCKED_TRADES_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")


def _log_daily_read(record: dict):
    with open(DAILY_READS_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")


def _blocked_by(d: dict, p: dict) -> Optional[str]:
    """Which trend gate(s) ALONE stopped a confidence-clearing read, else None."""
    if d.get("direction") not in ("LONG", "SHORT") or d.get("confidence") is None:
        return None
    if d["confidence"] < p["kronos_conf_min"]:
        return None
    aligned = ((d["direction"] == "LONG" and d.get("st") == "BULL") or
               (d["direction"] == "SHORT" and d.get("st") == "BEAR"))
    adx_ok = d.get("adx") is not None and d["adx"] >= MIN_ADX
    if adx_ok and aligned:
        return None
    if not adx_ok and not aligned:
        return "ADX+ST"
    return "ST" if adx_ok else "ADX"


# ── Data access: complete daily context + live entry price ────────────────────

def get_context(instrument: str, bars: int = 150) -> pd.DataFrame:
    """
    Return complete daily OHLCV bars (through the last closed session) as context.
    Prefers Upstox (reliable, no lag); falls back to yfinance.
    """
    if _UPSTOX_OK:
        try:
            df = _upstox_daily(instrument, bars=bars)
            if len(df) >= 60:
                return df
            log.warning("[%s] Upstox returned only %d daily bars — falling back to yfinance",
                        instrument, len(df))
        except Exception as e:
            log.warning("[%s] Upstox daily fetch failed (%s) — falling back to yfinance",
                        instrument, e)
    df = fetch_daily(instrument, years=1)
    # Drop a partial current-day bar if yfinance included one
    today = pd.Timestamp(date.today())
    df = df[pd.to_datetime(df["datetime"]).dt.normalize() < today].reset_index(drop=True)
    return df


def get_entry_price(instrument: str, context: pd.DataFrame) -> tuple[float, str]:
    """
    Return (price, source) for the same-day entry/exit fill.
    Prefers live Upstox LTP (~today's close during market hours);
    falls back to the last complete daily close.
    """
    if _UPSTOX_OK:
        try:
            return float(_upstox_ltp(instrument)), "upstox_ltp"
        except Exception as e:
            log.warning("[%s] LTP fetch failed (%s) — using last daily close", instrument, e)
    return float(context["close"].iloc[-1]), "last_close"


def get_mirofish(instrument: str) -> Optional[dict]:
    """
    Return {"lean": ..., "score": ..., "reasons": [...]} for `instrument` if
    mirofish_scores.json exists and is fresh, else None (gate is skipped).
    """
    if not MIROFISH_FILE.exists():
        return None
    try:
        raw = json.loads(MIROFISH_FILE.read_text())
        generated_at = datetime.fromisoformat(raw["generated_at"])
        age_hours = (datetime.now(generated_at.tzinfo) - generated_at).total_seconds() / 3600
        if age_hours > MIROFISH_MAX_AGE_HOURS:
            log.info("[%s] MiroFish data is %.1fh old (max %dh) — skipping news gate",
                     instrument, age_hours, MIROFISH_MAX_AGE_HOURS)
            return None
        return raw.get(instrument)
    except Exception as e:
        log.warning("[%s] Failed to read MiroFish data (%s) — skipping news gate", instrument, e)
        return None


def _log_trade(record: dict):
    with open(TRADES_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")


# ── Signal evaluation ─────────────────────────────────────────────────────────

def _read_behind_adx_gate(instrument: str, ctx: pd.DataFrame, p: dict, d: dict):
    """
    The ADX gate runs BEFORE Kronos, so on a ranging day the live path never
    sees Kronos's read at all. Take it anyway so a blocked read can be tracked
    forward. Logging/shadow only -- never changes the live decision, and any
    failure here is swallowed.
    """
    try:
        direction, confidence, _, rr, tgt = get_signal(ctx, force_fallback=False)
        d.update(direction=direction, confidence=float(confidence),
                 risk_reward=float(rr), price_target=float(tgt))
        log.info("[%s] (read behind ADX gate, shadow only) Kronos: %s conf=%.0f%% st=%s",
                 instrument, direction, confidence * 100, d.get("st"))
    except Exception as e:
        log.warning("[%s] Could not take the Kronos read behind the ADX gate: %s", instrument, e)


def evaluate_signal(instrument: str, df: pd.DataFrame,
                    diag: Optional[dict] = None) -> tuple[str, float, float, float, Optional[dict]]:
    """
    Evaluate the full entry gate chain on complete daily context `df`.
    Returns (direction, confidence, risk_reward, price_target, miro_block);
    direction is NEUTRAL if any gate fails (risk_reward/price_target are 0.0
    in that case). risk_reward/price_target come from Kronos's own predicted
    candles (see backtest.get_signal) -- logged for every qualified entry as
    a checkable prediction, not yet used as a gate (see module docstring).

    miro_block is None unless the MiroFish gate was the SPECIFIC reason this
    call returned NEUTRAL (every earlier gate passed) -- in that case it's a
    dict with the direction/confidence/risk_reward/price_target the trade
    would have had, plus the MiroFish score/reasons that vetoed it. This
    side channel exists only to seed a shadow trade (see cmd_daily_run) that
    tracks what would have happened had MiroFish not blocked the entry.

    diag, when passed, is filled with what the gate chain saw: adx, st,
    direction, confidence, risk_reward, price_target, and gate = the first gate
    that failed (ADX / NEUTRAL / CONF / ST) or PASS. With diag supplied, an ADX
    block also takes the Kronos read behind it (see _read_behind_adx_gate) so
    cmd_daily_run can track it in the blocked-read shadow book. The return
    value is identical with or without diag.
    """
    p = INSTRUMENT_PARAMS.get(instrument, _DEFAULT_PARAMS)
    ctx = df.copy()
    d = diag if diag is not None else {}
    d.update(adx=None, st=None, direction=None, confidence=None,
             risk_reward=0.0, price_target=0.0, gate=None)

    adx = compute_adx(ctx)
    d["adx"] = float(adx)
    if diag is not None:
        try:
            d["st"] = supertrend(ctx, period=p["supertrend_period"], multiplier=p["supertrend_multiplier"])
        except Exception:
            pass
    if adx < MIN_ADX:
        log.info("[%s] Gate FAIL: ADX %.1f < %d (ranging market)", instrument, adx, MIN_ADX)
        d["gate"] = "ADX"
        if diag is not None:
            _read_behind_adx_gate(instrument, ctx, p, d)
        return "NEUTRAL", 0.0, 0.0, 0.0, None

    direction, confidence, source, risk_reward, price_target = get_signal(ctx, force_fallback=False)
    d.update(direction=direction, confidence=float(confidence),
             risk_reward=float(risk_reward), price_target=float(price_target))
    log.info("[%s] Kronos: %s conf=%.0f%% src=%s adx=%.1f R:R=%.2f tgt=%.1f",
             instrument, direction, confidence * 100, source, adx, risk_reward, price_target)

    if direction == "NEUTRAL":
        d["gate"] = "NEUTRAL"
        return "NEUTRAL", 0.0, 0.0, 0.0, None
    if confidence < p["kronos_conf_min"]:
        log.info("[%s] Gate FAIL: confidence %.0f%% < %.0f%%",
                 instrument, confidence * 100, p["kronos_conf_min"] * 100)
        d["gate"] = "CONF"
        return "NEUTRAL", 0.0, 0.0, 0.0, None

    st = supertrend(ctx, period=p["supertrend_period"], multiplier=p["supertrend_multiplier"])
    d["st"] = st
    if direction == "LONG" and st != "BULL":
        log.info("[%s] Gate FAIL: SuperTrend %s not aligned with LONG", instrument, st)
        d["gate"] = "ST"
        return "NEUTRAL", 0.0, 0.0, 0.0, None
    if direction == "SHORT" and st != "BEAR":
        log.info("[%s] Gate FAIL: SuperTrend %s not aligned with SHORT", instrument, st)
        d["gate"] = "ST"
        return "NEUTRAL", 0.0, 0.0, 0.0, None
    d["gate"] = "PASS"

    mf = get_mirofish(instrument)
    if mf is not None:
        mf_score = float(mf["score"])
        if direction == "LONG" and mf_score < MIROFISH_BEARISH_VETO:
            log.info("[%s] Gate FAIL: MiroFish bearish (score=%.2f) vetoes LONG — %s",
                     instrument, mf_score, "; ".join(mf.get("reasons", [])[:2]))
            miro_block = {
                "direction": direction, "confidence": confidence,
                "risk_reward": risk_reward, "price_target": price_target,
                "mirofish_score": mf_score, "mirofish_reasons": mf.get("reasons", []),
            }
            return "NEUTRAL", 0.0, 0.0, 0.0, miro_block
        if direction == "SHORT" and mf_score > MIROFISH_BULLISH_VETO:
            log.info("[%s] Gate FAIL: MiroFish bullish (score=%.2f) vetoes SHORT — %s",
                     instrument, mf_score, "; ".join(mf.get("reasons", [])[:2]))
            miro_block = {
                "direction": direction, "confidence": confidence,
                "risk_reward": risk_reward, "price_target": price_target,
                "mirofish_score": mf_score, "mirofish_reasons": mf.get("reasons", []),
            }
            return "NEUTRAL", 0.0, 0.0, 0.0, miro_block
        log.info("[%s] MiroFish %s (score=%.2f) does not contradict %s",
                 instrument, mf.get("lean"), mf_score, direction)

    log.info("[%s] Gate PASS: %s conf=%.0f%% adx=%.1f st=%s R:R=%.2f tgt=%.1f — entry qualified",
             instrument, direction, confidence * 100, adx, st, risk_reward, price_target)
    return direction, confidence, risk_reward, price_target, None


# ── Exit check ────────────────────────────────────────────────────────────────

def check_exit(instrument: str, pos: Position, df: pd.DataFrame,
               price: float, decay_mode: str = "immediate",
               label: str = "") -> Optional[str]:
    """
    Return exit reason if the position should close at `price` (live), else None.
    Indicators/signal are computed on the complete daily context `df`; the P&L
    and stop/trail checks use the live `price`.

    decay_mode selects how CONFIDENCE_DECAY fires:
      "immediate"   live rule -- the first unsupported daily read, green or red.
      "profit_only" the OLD rule -- only while in profit (counterfactual book).
      "two_day"     only after 2 consecutive unsupported reads (hysteresis arm).
    pos.unsupported_streak counts consecutive unsupported reads and is mutated
    here, like the trail state. `label` tags log lines of the shadow books.
    """
    p = INSTRUMENT_PARAMS.get(instrument, _DEFAULT_PARAMS)

    pnl_pct = ((price - pos.entry_price) / pos.entry_price
               if pos.direction == "LONG"
               else (pos.entry_price - price) / pos.entry_price)
    in_profit = pnl_pct >= 0

    # Hard stop
    if pnl_pct <= -p["stop_loss_pct"]:
        return "STOP_LOSS"

    # Trail
    if pnl_pct >= p["trail_activate_pct"]:
        pos.trail_active = True
        if pos.direction == "LONG" and price > pos.hwm:
            pos.hwm = price
            pos.trail_stop = pos.hwm * (1 - p["trail_distance_pct"])
        elif pos.direction == "SHORT" and price < pos.hwm:
            pos.hwm = price
            pos.trail_stop = pos.hwm * (1 + p["trail_distance_pct"])

    if pos.trail_active:
        if pos.direction == "LONG" and price < pos.trail_stop:
            return "TRAIL_STOP"
        if pos.direction == "SHORT" and price > pos.trail_stop:
            return "TRAIL_STOP"

    # Signal exits
    try:
        direction, confidence, _, _, _ = get_signal(df, force_fallback=False)
        st = supertrend(df, period=p["supertrend_period"], multiplier=p["supertrend_multiplier"])

        # Log the daily read unconditionally (not just when an exit fires) --
        # previously this was computed every day but only ever surfaced on
        # the day an exit condition returned, so a trade that spent 13 days
        # underwater (profit-gate blocking CONFIDENCE_DECAY the whole time)
        # left no record of whether Kronos's conviction was actually fading.
        if direction == pos.direction:
            conf_delta_str = f"{(confidence - pos.entry_confidence) * 100:+.0f}pp vs entry"
        else:
            conf_delta_str = f"direction differs from entry ({pos.direction})"
        log.info("[%s]%s Signal check: %s conf=%.0f%%  (%s)  pnl=%s",
                 instrument, label, direction, confidence * 100, conf_delta_str,
                 "profit" if in_profit else "loss")

        if EXIT_ON_KRONOS_REVERSAL:
            opp = "SHORT" if pos.direction == "LONG" else "LONG"
            if direction == opp and confidence >= p["kronos_rev_conf_min"]:
                return "KRONOS_REV"

        # Confidence decay: today's read no longer independently supports
        # holding at entry-quality conviction (weak same-direction, NEUTRAL,
        # or a sub-threshold opposing lean). Fires green or red (live rule,
        # changed Oct 14 2026); the other decay_modes exist only to drive the
        # shadow arms (old profit-gated rule, 2-consecutive-day hysteresis).
        still_supported = (direction == pos.direction and confidence >= p["kronos_conf_min"])
        if still_supported:
            pos.unsupported_streak = 0
        else:
            pos.unsupported_streak += 1
            if (decay_mode == "immediate"
                    or (decay_mode == "profit_only" and in_profit)
                    or (decay_mode == "two_day" and pos.unsupported_streak >= 2)):
                return "CONFIDENCE_DECAY"

        if EXIT_ON_SUPERTREND_FLIP:
            if pos.direction == "LONG" and st == "BEAR":
                return "ST_FLIP"
            if pos.direction == "SHORT" and st == "BULL":
                return "ST_FLIP"
    except Exception as e:
        log.warning("[%s] Signal error during exit check: %s", instrument, e)

    return None


# ── Main commands ─────────────────────────────────────────────────────────────

def _close_position_record(inst: str, pos: Position, reason: str, price: float,
                           today: str) -> tuple[float, dict]:
    """Shared close-out math for a real or shadow Position. Returns (pnl_inr, log_record)."""
    pts = (price - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - price)
    pnl = pts * pos.lot_size
    predicted_profit_inr = None
    if pos.predicted_target:
        pred_pts = ((pos.predicted_target - pos.entry_price) if pos.direction == "LONG"
                   else (pos.entry_price - pos.predicted_target))
        predicted_profit_inr = pred_pts * pos.lot_size
    record = {
        "event":                "EXIT",
        "instrument":           inst,
        "direction":            pos.direction,
        "entry_date":           pos.entry_date,
        "entry_price":          pos.entry_price,
        "exit_date":            today,
        "exit_price":           price,
        "exit_reason":          reason,
        "pnl_pts":              pts,
        "pnl_inr":              pnl,
        "lot_size":             pos.lot_size,
        "predicted_target":     pos.predicted_target,
        "predicted_profit_inr": predicted_profit_inr,
    }
    return pnl, record


def cmd_daily_run(instruments: list[str]):
    """
    Single daily run near market close (~15:20 IST).
    PASS 1 closes any real exits at the live price; PASS 2 evaluates entries
    -- taking a real position when the slot is free, or opening a MiroFish
    shadow trade when MiroFish was the only gate that blocked an otherwise-
    qualified signal.
    """
    state = _load_state()
    shadow_state = _load_shadow_state()
    decay_cf = _load_decay_cf()
    blocked = _load_blocked()
    today = date.today().isoformat()

    # Fetch context + live price once per instrument
    ctx: dict[str, pd.DataFrame] = {}
    px:  dict[str, float]        = {}
    for inst in instruments:
        try:
            df = get_context(inst, bars=150)
            price, src = get_entry_price(inst, df)
            ctx[inst] = df
            px[inst]  = price
            log.info("[%s] context=%d bars  last_close=%.1f  live=%.1f (%s)",
                     inst, len(df), float(df["close"].iloc[-1]), price, src)
        except Exception as e:
            log.error("[%s] Data fetch failed: %s — skipping", inst, e)

    # ── PASS 1: real exits ──────────────────────────────────────────────────
    for inst in list(state.positions.keys()):
        if inst not in ctx:
            continue
        pos    = state.positions[inst]
        price  = px[inst]
        reason = check_exit(inst, pos, ctx[inst], price)
        if reason:
            pnl, record = _close_position_record(inst, pos, reason, price, today)
            state.realised_pnl += pnl
            state.trade_count  += 1
            log.info("[%s] EXIT %s @ %.1f  reason=%s  PnL INR %+.0f  "
                     "(predicted INR %s)  [held from %s]",
                     inst, pos.direction, price, reason, pnl,
                     f"{record['predicted_profit_inr']:+.0f}" if record['predicted_profit_inr'] is not None else "n/a",
                     pos.entry_date)
            _log_trade(record)
            if reason == "CONFIDENCE_DECAY":
                # Hysteresis arm: every decay exit (green or red) keeps a copy
                # running that only decays after 2 consecutive unsupported
                # reads. Today was unsupported day 1 (pos.unsupported_streak).
                decay_cf["two_day"]["open"][inst] = {
                    "pos":             asdict(pos),
                    "real_exit_date":  today,
                    "real_exit_price": price,
                    "real_pnl_inr":    pnl,
                }
                log.info("[DECAY-2D][%s] arm opened (real exit %.1f, INR %+.0f); tracking the hold "
                         "under the 2-consecutive-day rule", inst, price, pnl)
            if reason == "CONFIDENCE_DECAY" and pnl < 0:
                # The old rule would have held this red position; keep a copy
                # running under it so we can compare outcomes.
                decay_cf["open"][inst] = {
                    "pos":             asdict(pos),
                    "real_exit_date":  today,
                    "real_exit_price": price,
                    "real_pnl_inr":    pnl,
                }
                log.info("[DECAY-CF][%s] counterfactual opened: decay fired while RED "
                         "(real exit %.1f, INR %+.0f); tracking the hold under the old rule",
                         inst, price, pnl)
            del state.positions[inst]
        else:
            pts = ((price - pos.entry_price) if pos.direction == "LONG"
                   else (pos.entry_price - price))
            log.info("[%s] HOLD %s  entry=%.1f  live=%.1f  unrealised INR %+.0f",
                     inst, pos.direction, pos.entry_price, price, pts * pos.lot_size)

    # ── PASS 1b: MiroFish shadow exits (same rules, no real capital at stake) ─
    for inst in list(shadow_state.positions.keys()):
        if inst not in ctx:
            continue
        pos    = shadow_state.positions[inst]
        price  = px[inst]
        reason = check_exit(inst, pos, ctx[inst], price)
        if reason:
            pnl, record = _close_position_record(inst, pos, reason, price, today)
            shadow_state.realised_pnl += pnl
            shadow_state.trade_count  += 1
            log.info("[SHADOW][%s] EXIT %s @ %.1f  reason=%s  PnL INR %+.0f  [held from %s]",
                     inst, pos.direction, price, reason, pnl, pos.entry_date)
            _log_shadow_trade(record)
            del shadow_state.positions[inst]
        else:
            pts = ((price - pos.entry_price) if pos.direction == "LONG"
                   else (pos.entry_price - price))
            log.info("[SHADOW][%s] HOLD %s  entry=%.1f  live=%.1f  unrealised INR %+.0f",
                     inst, pos.direction, pos.entry_price, price, pts * pos.lot_size)

    # ── PASS 1c: decay counterfactual (OLD profit-gated rule, no capital) ───
    for inst in list(decay_cf["open"].keys()):
        if inst not in ctx:
            continue
        rec    = decay_cf["open"][inst]
        if rec["real_exit_date"] == today:
            continue        # opened this run: today's read already produced the decay exit
        cpos   = Position(**rec["pos"])
        price  = px[inst]
        reason = check_exit(inst, cpos, ctx[inst], price,
                            decay_mode="profit_only", label="[DECAY-CF]")
        if reason:
            cf_pnl, record = _close_position_record(inst, cpos, reason, price, today)
            value = rec["real_pnl_inr"] - cf_pnl        # > 0 => firing decay beat holding
            decay_cf["closed"]   += 1
            decay_cf["sum_real"] += rec["real_pnl_inr"]
            decay_cf["sum_cf"]   += cf_pnl
            record.update({
                "event":           "DECAY_CF_CLOSE",
                "real_exit_date":  rec["real_exit_date"],
                "real_exit_price": rec["real_exit_price"],
                "real_pnl_inr":    rec["real_pnl_inr"],
                "cf_pnl_inr":      cf_pnl,
                "decay_value_inr": value,
            })
            log.info("[DECAY-CF][%s] CLOSED via %s @ %.1f: not-fired INR %+.0f vs real INR %+.0f "
                     "-> firing decay was worth INR %+.0f",
                     inst, reason, price, cf_pnl, rec["real_pnl_inr"], value)
            _log_decay_cf(record)
            del decay_cf["open"][inst]
        else:
            rec["pos"] = asdict(cpos)                   # keeps hwm / trail progress
            pts = ((price - cpos.entry_price) if cpos.direction == "LONG"
                   else (cpos.entry_price - price))
            log.info("[DECAY-CF][%s] HOLD %s  entry=%.1f  live=%.1f  not-fired unrealised INR %+.0f "
                     "vs real exit INR %+.0f",
                     inst, cpos.direction, cpos.entry_price, price,
                     pts * cpos.lot_size, rec["real_pnl_inr"])

    # ── PASS 1c2: 2-consecutive-day decay arm (hysteresis, no capital) ──────
    two = decay_cf["two_day"]
    for inst in list(two["open"].keys()):
        if inst not in ctx:
            continue
        rec    = two["open"][inst]
        if rec["real_exit_date"] == today:
            continue        # opened this run: today's unsupported read is already streak 1 -- don't count it twice
        apos   = Position(**rec["pos"])
        price  = px[inst]
        reason = check_exit(inst, apos, ctx[inst], price,
                            decay_mode="two_day", label="[DECAY-2D]")
        if reason:
            arm_pnl, record = _close_position_record(inst, apos, reason, price, today)
            value = rec["real_pnl_inr"] - arm_pnl       # > 0 => the live rule beat the 2-day rule
            two["closed"]   += 1
            two["sum_real"] += rec["real_pnl_inr"]
            two["sum_arm"]  += arm_pnl
            record.update({
                "event":           "DECAY_2D_CLOSE",
                "real_exit_date":  rec["real_exit_date"],
                "real_exit_price": rec["real_exit_price"],
                "real_pnl_inr":    rec["real_pnl_inr"],
                "arm_pnl_inr":     arm_pnl,
                "live_vs_2d_inr":  value,
            })
            log.info("[DECAY-2D][%s] CLOSED via %s @ %.1f: 2-day rule INR %+.0f vs live rule INR %+.0f "
                     "-> live rule was worth INR %+.0f", inst, reason, price, arm_pnl, rec["real_pnl_inr"], value)
            _log_decay_cf(record)
            del two["open"][inst]
        else:
            rec["pos"] = asdict(apos)
            pts = ((price - apos.entry_price) if apos.direction == "LONG"
                   else (apos.entry_price - price))
            log.info("[DECAY-2D][%s] HOLD %s  entry=%.1f  live=%.1f  2-day unrealised INR %+.0f "
                     "vs live-rule exit INR %+.0f  (unsupported streak %d)",
                     inst, apos.direction, apos.entry_price, price, pts * apos.lot_size,
                     rec["real_pnl_inr"], apos.unsupported_streak)

    # ── PASS 1d: blocked-read shadow exits (live exit rules, no capital) ────
    for inst in list(blocked["positions"].keys()):
        if inst not in ctx:
            continue
        rec    = blocked["positions"][inst]
        bpos   = Position(**rec["pos"])
        price  = px[inst]
        reason = check_exit(inst, bpos, ctx[inst], price, label="[BLOCKED-SHADOW]")
        if reason:
            bpnl, record = _close_position_record(inst, bpos, reason, price, today)
            record.update({"event": "BLOCKED_SHADOW_EXIT", "blocked_by": rec["blocked_by"]})
            s = blocked["stats"].setdefault(rec["blocked_by"], {"n": 0, "wins": 0, "pnl": 0.0})
            s["n"] += 1
            s["wins"] += 1 if bpnl > 0 else 0
            s["pnl"] += bpnl
            log.info("[BLOCKED-SHADOW][%s] EXIT %s @ %.1f  reason=%s  PnL INR %+.0f  blocked_by=%s  [held from %s]",
                     inst, bpos.direction, price, reason, bpnl, rec["blocked_by"], bpos.entry_date)
            _log_blocked(record)
            del blocked["positions"][inst]
        else:
            rec["pos"] = asdict(bpos)
            pts = ((price - bpos.entry_price) if bpos.direction == "LONG"
                   else (bpos.entry_price - price))
            log.info("[BLOCKED-SHADOW][%s] HOLD %s  entry=%.1f  live=%.1f  unrealised INR %+.0f  blocked_by=%s",
                     inst, bpos.direction, bpos.entry_price, price, pts * bpos.lot_size, rec["blocked_by"])

    # ── PASS 2: entries -- real (slot-gated) or MiroFish shadow (not) ───────
    # Every instrument without an open real OR shadow position gets evaluated
    # regardless of whether another instrument holds the real slot -- this is
    # the one extra Kronos call/day the shadow tracking costs, spent so we
    # can see what MiroFish blocks even on days a real position sits elsewhere.
    for inst in instruments:
        if inst not in ctx:
            continue
        if inst in state.positions or inst in shadow_state.positions:
            continue

        real_slot_free = len(state.positions) == 0
        diag: dict = {}
        direction, confidence, risk_reward, price_target, miro_block = evaluate_signal(inst, ctx[inst], diag)
        price = px[inst]
        lot   = LOT_SIZES.get(inst, 1)
        try:    # instrumentation only -- must never break the real flow
            _log_daily_read({
                "date": today, "instrument": inst, "price": price,
                "adx": diag.get("adx"), "supertrend": diag.get("st"),
                "direction": diag.get("direction"), "confidence": diag.get("confidence"),
                "gate": "MIRO" if miro_block is not None else diag.get("gate"),
                "entered": direction != "NEUTRAL" and real_slot_free,
            })
        except Exception as e:
            log.warning("[%s] daily read log failed: %s", inst, e)

        if direction != "NEUTRAL":
            if not real_slot_free:
                log.info("[%s] Signal qualified but another position is already open "
                         "— one at a time, skipping real entry", inst)
                continue

            predicted_pts = ((price_target - price) if direction == "LONG"
                             else (price - price_target))
            predicted_profit_inr = predicted_pts * lot
            state.positions[inst] = Position(
                instrument=inst, direction=direction, entry_date=today,
                entry_price=price, lot_size=lot, hwm=price,
                trail_active=False, trail_stop=0.0,
                predicted_target=price_target, predicted_risk_reward=risk_reward,
                entry_confidence=confidence,
            )
            log.info("[%s] ENTRY %s @ %.1f  conf=%.0f%%  lot=%d  R:R=%.2f  "
                     "predicted_tgt=%.1f  predicted_profit=INR %+.0f  (same-day close)",
                     inst, direction, price, confidence * 100, lot,
                     risk_reward, price_target, predicted_profit_inr)
            _log_trade({
                "event":                 "ENTRY",
                "instrument":            inst,
                "direction":             direction,
                "entry_date":            today,
                "entry_price":           price,
                "confidence":            confidence,
                "lot_size":              lot,
                "predicted_risk_reward": risk_reward,
                "predicted_target":      price_target,
                "predicted_profit_inr":  predicted_profit_inr,
            })

        elif miro_block is not None:
            mb_direction = miro_block["direction"]
            predicted_pts = ((miro_block["price_target"] - price) if mb_direction == "LONG"
                             else (price - miro_block["price_target"]))
            predicted_profit_inr = predicted_pts * lot
            shadow_state.positions[inst] = Position(
                instrument=inst, direction=mb_direction, entry_date=today,
                entry_price=price, lot_size=lot, hwm=price,
                trail_active=False, trail_stop=0.0,
                predicted_target=miro_block["price_target"],
                predicted_risk_reward=miro_block["risk_reward"],
                entry_confidence=miro_block["confidence"],
            )
            log.info("[SHADOW][%s] ENTRY %s @ %.1f  conf=%.0f%%  lot=%d  "
                     "blocked_by=MiroFish(score=%.2f: %s)  predicted_profit=INR %+.0f",
                     inst, mb_direction, price, miro_block["confidence"] * 100, lot,
                     miro_block["mirofish_score"], "; ".join(miro_block["mirofish_reasons"][:2]),
                     predicted_profit_inr)
            _log_shadow_trade({
                "event":                 "ENTRY",
                "instrument":            inst,
                "direction":             mb_direction,
                "entry_date":            today,
                "entry_price":           price,
                "confidence":            miro_block["confidence"],
                "lot_size":              lot,
                "predicted_risk_reward": miro_block["risk_reward"],
                "predicted_target":      miro_block["price_target"],
                "predicted_profit_inr":  predicted_profit_inr,
                "mirofish_score":        miro_block["mirofish_score"],
                "mirofish_reasons":      miro_block["mirofish_reasons"],
            })

        else:
            # NEUTRAL and not a MiroFish block: if a confidence-clearing read was
            # stopped only by a trend gate (ADX / SuperTrend), track it forward.
            try:
                bb = _blocked_by(diag, INSTRUMENT_PARAMS.get(inst, _DEFAULT_PARAMS))
                if bb and inst not in blocked["positions"]:
                    bpos = Position(
                        instrument=inst, direction=diag["direction"], entry_date=today,
                        entry_price=price, lot_size=lot, hwm=price,
                        trail_active=False, trail_stop=0.0,
                        predicted_target=diag["price_target"], predicted_risk_reward=diag["risk_reward"],
                        entry_confidence=diag["confidence"],
                    )
                    blocked["positions"][inst] = {"pos": asdict(bpos), "blocked_by": bb}
                    log.info("[BLOCKED-SHADOW][%s] ENTRY %s @ %.1f  conf=%.0f%%  blocked_by=%s (adx=%.1f st=%s)",
                             inst, diag["direction"], price, diag["confidence"] * 100, bb,
                             diag["adx"], diag.get("st"))
                    _log_blocked({
                        "event": "BLOCKED_SHADOW_ENTRY", "instrument": inst,
                        "direction": diag["direction"], "entry_date": today, "entry_price": price,
                        "confidence": diag["confidence"], "blocked_by": bb,
                        "adx": diag["adx"], "supertrend": diag.get("st"), "lot_size": lot,
                    })
            except Exception as e:
                log.warning("[%s] blocked-read shadow entry failed: %s", inst, e)

    _save_state(state)
    _save_shadow_state(shadow_state)
    _save_decay_cf(decay_cf)
    _save_blocked(blocked)
    print_status(state)
    print_shadow_status(shadow_state)
    print_decay_cf_status(decay_cf)
    print_blocked_status(blocked)


def cmd_morning_scan():
    """
    Lightweight pre-market run (~09:20 IST, after a fresh MiroFish read at
    ~08:45 IST). Does NOT touch entries or price/technical exits — those run
    at the normal close-time cycle. Only closes a position early if fresh
    overnight news has flipped strongly against the held direction, closing
    the ~24h gap between one day's close-time read and the next.
    """
    state = _load_state()
    if not state.positions:
        log.info("No open positions — nothing for the morning scan to check.")
        return

    today = date.today().isoformat()
    for inst, pos in list(state.positions.items()):
        mf = get_mirofish(inst)
        if mf is None:
            log.info("[%s] No fresh MiroFish data — skipping morning scan", inst)
            continue

        score = float(mf["score"])
        triggered = (
            (pos.direction == "LONG"  and score <= MORNING_NEWS_EXIT_BEARISH) or
            (pos.direction == "SHORT" and score >= MORNING_NEWS_EXIT_BULLISH)
        )
        if not triggered:
            log.info("[%s] MiroFish score=%.2f (%s) does not warrant an early exit — holding %s",
                     inst, score, mf.get("lean"), pos.direction)
            continue

        try:
            ctx = get_context(inst, bars=60)
            price, src = get_entry_price(inst, ctx)
        except Exception as e:
            log.error("[%s] Could not fetch exit price for morning scan (%s) — skipping", inst, e)
            continue

        pts = ((price - pos.entry_price) if pos.direction == "LONG"
               else (pos.entry_price - price))
        pnl = pts * pos.lot_size
        state.realised_pnl += pnl
        state.trade_count  += 1
        log.info("[%s] NEWS_EXIT %s @ %.1f (%s)  MiroFish score=%.2f  reasons=%s  PnL INR %+.0f",
                 inst, pos.direction, price, src, score, mf.get("reasons"), pnl)
        _log_trade({
            "event":            "EXIT",
            "instrument":       inst,
            "direction":        pos.direction,
            "entry_date":       pos.entry_date,
            "entry_price":      pos.entry_price,
            "exit_date":        today,
            "exit_price":       price,
            "exit_reason":      "NEWS_EXIT",
            "pnl_pts":          pts,
            "pnl_inr":          pnl,
            "lot_size":         pos.lot_size,
            "mirofish_score":   score,
            "mirofish_reasons": mf.get("reasons"),
        })
        del state.positions[inst]

    _save_state(state)
    print_status(state)


def print_status(state: PaperState):
    sep = "=" * 60
    print(f"\n{sep}")
    print(f"  PAPER TRADER STATUS  |  Capital: INR {state.capital:,.0f}")
    print(f"  Realised P&L : INR {state.realised_pnl:+,.0f}  ({state.trade_count} trades closed)")
    print(sep)

    if state.positions:
        print("  OPEN POSITIONS:")
        for inst, pos in state.positions.items():
            print(f"    {inst:<10} {pos.direction}  entry={pos.entry_price:.1f}  "
                  f"date={pos.entry_date}  lot={pos.lot_size}")
    else:
        print("  No open positions.")
    print(sep)


def print_shadow_status(shadow_state: ShadowState):
    sep = "=" * 60
    print(f"\n{sep}")
    print(f"  MIROFISH SHADOW STATUS  (what if the veto hadn't blocked it)")
    print(f"  Shadow P&L   : INR {shadow_state.realised_pnl:+,.0f}  ({shadow_state.trade_count} trades closed)")
    print(sep)

    if shadow_state.positions:
        print("  OPEN SHADOW POSITIONS:")
        for inst, pos in shadow_state.positions.items():
            print(f"    {inst:<10} {pos.direction}  entry={pos.entry_price:.1f}  "
                  f"date={pos.entry_date}  lot={pos.lot_size}")
    else:
        print("  No open shadow positions.")
    print(sep)


def print_decay_cf_status(cf: dict):
    sep = "=" * 60
    print(f"\n{sep}")
    print("  DECAY COUNTERFACTUAL  (decay fired while red vs had it not)")
    print(sep)
    if cf["closed"]:
        print(f"  Closed pairs : {cf['closed']}   real INR {cf['sum_real']:+,.0f}   "
              f"not-fired INR {cf['sum_cf']:+,.0f}")
        print(f"  Value of firing decay: INR {cf['sum_real'] - cf['sum_cf']:+,.0f}   "
              f"(positive = firing helped)")
    else:
        print("  No closed pairs yet.")
    for inst, rec in cf["open"].items():
        p = rec["pos"]
        print(f"  TRACKING {inst:<10} {p['direction']}  entry={p['entry_price']:.1f}  "
              f"real exit {rec['real_exit_price']:.1f} on {rec['real_exit_date']} "
              f"(INR {rec['real_pnl_inr']:+,.0f})")
    two = cf.get("two_day", {"open": {}, "closed": 0, "sum_real": 0.0, "sum_arm": 0.0})
    print("  -- 2-DAY ARM: live rule vs 'decay only after 2 consecutive unsupported days' (all decay exits)")
    if two["closed"]:
        print(f"  Closed pairs : {two['closed']}   live INR {two['sum_real']:+,.0f}   "
              f"2-day INR {two['sum_arm']:+,.0f}")
        print(f"  Value of live rule vs 2-day: INR {two['sum_real'] - two['sum_arm']:+,.0f}   "
              f"(positive = live rule better)")
    else:
        print("  No closed pairs yet.")
    for inst, rec in two["open"].items():
        p = rec["pos"]
        print(f"  TRACKING {inst:<10} {p['direction']}  entry={p['entry_price']:.1f}  "
              f"live exit {rec['real_exit_price']:.1f} on {rec['real_exit_date']} "
              f"(INR {rec['real_pnl_inr']:+,.0f})  streak={p.get('unsupported_streak', 0)}")
    print(sep)


def print_blocked_status(b: dict):
    sep = "=" * 60
    print(f"\n{sep}")
    print("  BLOCKED-READ SHADOW  (confidence-clearing reads the ADX / SuperTrend gate stopped)")
    print("  Rule fixed in advance: no gate change before 30 closed trades per gate, and only")
    print("  if the result holds in BOTH chronological halves.")
    print(sep)
    stats = b["stats"]
    if stats:
        for key in sorted(stats):
            s = stats[key]
            print(f"  {key:<8} closed {s['n']:>3}  wins {s['wins']:>3}  P&L INR {s['pnl']:+,.0f}")
    else:
        print("  No closed blocked-read trades yet.")
    for inst, rec in b["positions"].items():
        p = rec["pos"]
        print(f"  OPEN {inst:<10} {p['direction']}  entry={p['entry_price']:.1f}  "
              f"date={p['entry_date']}  blocked_by={rec['blocked_by']}")
    print(sep)


def cmd_status():
    print_status(_load_state())
    print_shadow_status(_load_shadow_state())
    print_decay_cf_status(_load_decay_cf())
    print_blocked_status(_load_blocked())


def cmd_reset():
    for f in [STATE_FILE]:
        if Path(f).exists():
            Path(f).unlink()
    log.info("State reset. Trades log preserved at %s.", TRADES_LOG)
    _load_state()   # creates fresh state
    _save_state(_load_state())


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Kronos paper trader (same-day close entry)")
    parser.add_argument("--status",     action="store_true",
                        help="Print current positions and P&L")
    parser.add_argument("--reset",      action="store_true",
                        help="Wipe all state and start fresh")
    parser.add_argument("--sensex",     action="store_true",
                        help="Include SENSEX in active instruments")
    parser.add_argument("--morning-scan", action="store_true",
                        help="Pre-market news-exit scan for open positions only (~09:20 IST)")
    args = parser.parse_args()

    instruments = list(ACTIVE_INSTRUMENTS)
    if args.sensex:
        instruments.append("SENSEX")

    if args.reset:
        cmd_reset()
    elif args.status:
        cmd_status()
    elif args.morning_scan:
        cmd_morning_scan()
    else:
        cmd_daily_run(instruments)


if __name__ == "__main__":
    main()
