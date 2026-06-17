# -*- coding: utf-8 -*-
"""
MaxPain Trap — Paper Strategy (Variant A: Opening MaxPain Displacement)
=======================================================================
On expiry days (DTE ≤ 2), MaxPain gravity is at its peak: option writers
have a strong ₹ incentive to pin the index at MaxPain by expiry.  When
the market opens significantly displaced from MaxPain, writers defend
aggressively — every rally above MaxPain gets sold, every dip below gets
bought.  This strategy exploits that pinning force.

Entry conditions (all must pass):
  1. DTE ≤ config.MP_TRAP_DTE_MAX (2)
  2. |spot − MaxPain| / MaxPain ≥ per-instrument gap threshold
       NIFTY     : 0.50%  (monthly expiry — rarer, larger moves)
       BANKNIFTY : 0.35%  (weekly expiry — more frequent, smaller displacement)
       SENSEX    : 0.50%  (monthly expiry; BSE — no MaxPain data, so never fires)
  3. PCR confirms direction (per-instrument PCR thresholds)
  4. Within entry window (09:15–10:00 IST)
  5. At most 1 trade per instrument per day

Exit:
  - Target : spot within 0.1% of MaxPain
  - Stop   : option premium falls 25% below entry
  - Force  : 14:30 IST

Capital (paper, per instrument):
  - Pool       : config.MP_TRAP_PAPER_CAPITAL  (₹10,000)
  - Per trade  : ~₹3,000 (scaled lots, max 3)
  - Daily halt : P&L ≤ -config.MP_TRAP_DAILY_LOSS_LIMIT  → no more entries today

Trade logging:
  - Each closed position appended to logs/mp_trap_learnings.jsonl
  - Read by weekly_analyzer.py for per-instrument threshold calibration
  - Set path via set_log_path() at bot startup

Integration (options_bot.py):
  - Called once per 5-min bar, independent of ORB limits
  - daily_reset() called at start of each trading day
  - set_log_path() called once at startup
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, time as dtime
from typing import Optional

import pytz
from scipy.stats import norm

IST = pytz.timezone('Asia/Kolkata')

# ── Module-level defaults (overridden by config.py dicts) ─────────────────────
_DEFAULT_GAP_PCT          = 0.005
_DEFAULT_PCR_PUT_CONFIRM  = 0.85
_DEFAULT_PCR_CALL_CONFIRM = 1.15

MP_ENTRY_START     = dtime(9, 15)
MP_ENTRY_END       = dtime(10, 0)
MP_TARGET_BUFFER   = 0.001    # within 0.1% of MaxPain = target
MP_FORCE_CLOSE     = dtime(14, 30)
MP_MAX_LOTS        = 3
MP_TARGET_SPEND    = 3_000.0

# ── JSONL trade log ───────────────────────────────────────────────────────────
_MP_TRAP_JSONL: Optional[str] = None


def set_log_path(path: str) -> None:
    """Set the JSONL file path for trade logging (call once at bot startup)."""
    global _MP_TRAP_JSONL
    _MP_TRAP_JSONL = path


def _log_trade(pos: dict, exit_reason: str, exit_spot: float,
               cur_px: float, pnl: float, gain_pct: float,
               now: datetime) -> None:
    """Append a closed trade record to mp_trap_learnings.jsonl."""
    if not _MP_TRAP_JSONL:
        return
    record = {
        'date'         : now.strftime('%Y-%m-%d'),
        'instrument'   : pos['instrument'],
        'signal_type'  : pos['signal_type'],
        'gap_pct'      : round((pos['entry_spot'] - pos['max_pain'])
                               / pos['max_pain'] * 100, 3),
        'pcr_at_entry' : pos.get('pcr_at_entry'),
        'max_pain'     : pos['max_pain'],
        'entry_spot'   : round(pos['entry_spot'], 1),
        'entry_premium': round(pos['entry_premium'], 2),
        'entry_time'   : pos['entry_time'],
        'exit_spot'    : round(exit_spot, 1),
        'exit_time'    : now.strftime('%H:%M'),
        'exit_reason'  : exit_reason,
        'gain_pct'     : round(gain_pct * 100, 2),
        'pnl'          : round(pnl, 2),
        'lots'         : pos['lots'],
        'lot_size'     : pos['lot_size'],
        'dte'          : pos['dte'],
        'win'          : pnl > 0,
    }
    try:
        with open(_MP_TRAP_JSONL, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record) + '\n')
    except Exception:
        pass   # non-fatal


# ── In-memory state (per instrument) ─────────────────────────────────────────
_sim:          dict[str, Optional[dict]] = {}
_capital:      dict[str, float]          = {}
_daily_pnl:    dict[str, float]          = {}
_total_pnl:    dict[str, float]          = {}
_trades:       dict[str, int]            = {}
_fired_today:  dict[str, bool]           = {}
_last_eval_ts: dict[str, object]         = {}


# ── Config helpers — read per-instrument thresholds ───────────────────────────

def _gap_pct(instrument: str) -> float:
    try:
        import config as _cfg
        v = _cfg.MP_TRAP_GAP_PCT
        return v.get(instrument, _DEFAULT_GAP_PCT) if isinstance(v, dict) else float(v)
    except Exception:
        return _DEFAULT_GAP_PCT


def _pcr_put_confirm(instrument: str) -> float:
    try:
        import config as _cfg
        v = _cfg.MP_TRAP_PCR_PUT_CONFIRM
        return v.get(instrument, _DEFAULT_PCR_PUT_CONFIRM) if isinstance(v, dict) else float(v)
    except Exception:
        return _DEFAULT_PCR_PUT_CONFIRM


def _pcr_call_confirm(instrument: str) -> float:
    try:
        import config as _cfg
        v = _cfg.MP_TRAP_PCR_CALL_CONFIRM
        return v.get(instrument, _DEFAULT_PCR_CALL_CONFIRM) if isinstance(v, dict) else float(v)
    except Exception:
        return _DEFAULT_PCR_CALL_CONFIRM


def _paper_capital() -> float:
    try:
        import config as _cfg
        return float(_cfg.MP_TRAP_PAPER_CAPITAL)
    except Exception:
        return 10_000.0


def _daily_loss_limit() -> float:
    try:
        import config as _cfg
        return float(_cfg.MP_TRAP_DAILY_LOSS_LIMIT)
    except Exception:
        return 3_000.0


def _dte_max() -> int:
    try:
        import config as _cfg
        return int(_cfg.MP_TRAP_DTE_MAX)
    except Exception:
        return 2


# ── Black-Scholes pricer ──────────────────────────────────────────────────────

def _bs(opt_type: str, S: float, K: float, T: float,
        iv: float, r: float = 0.065) -> float:
    """Black-Scholes European option price. Returns intrinsic on error."""
    try:
        if T <= 0 or iv <= 0 or S <= 0 or K <= 0:
            return max(0.0, S - K) if opt_type == 'CALL' else max(0.0, K - S)
        d1 = (math.log(S / K) + (r + 0.5 * iv ** 2) * T) / (iv * math.sqrt(T))
        d2 = d1 - iv * math.sqrt(T)
        if opt_type == 'CALL':
            return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
    except Exception:
        return max(0.1, S * iv * math.sqrt(T) * 0.4)


# ── Daily reset ───────────────────────────────────────────────────────────────

def daily_reset(instrument: str, logger: Optional[logging.Logger] = None) -> None:
    """Call at start of each trading day."""
    if _sim.get(instrument):
        if logger:
            logger.warning(
                f"  [MP-TRAP] {instrument}: overnight position found — clearing"
            )
        _sim[instrument] = None

    prev = _daily_pnl.get(instrument, 0.0)
    if logger and prev != 0.0:
        logger.info(
            f"  [MP-TRAP] {instrument}: yesterday P&L=₹{prev:+,.0f} | "
            f"Capital=₹{_capital.get(instrument, _paper_capital()):,.0f} | "
            f"Total=₹{_total_pnl.get(instrument, 0.0):+,.0f} | "
            f"Trades so far={_trades.get(instrument, 0)}"
        )
    _daily_pnl[instrument]  = 0.0
    _fired_today[instrument] = False
    _last_eval_ts.pop(instrument, None)


# ── Position monitoring ───────────────────────────────────────────────────────

def _update_position(instrument: str, spot: float,
                     logger: logging.Logger, now: datetime) -> None:
    """Re-price open paper position and check exit conditions."""
    pos = _sim.get(instrument)
    if not pos:
        return

    t      = now.time()
    T      = max(pos['dte'] / 365, 0.0001)   # fixed T for intraday (decay minor vs spot move)
    cur_px = _bs(pos['signal_type'], spot, pos['atm_strike'], T, pos['iv'])

    entry_px = pos['entry_premium']
    gain_pct = (cur_px - entry_px) / entry_px if entry_px > 0 else 0.0

    max_pain   = pos['max_pain']
    mp_dist    = abs(spot - max_pain) / max(spot, 1)
    target_hit = mp_dist <= MP_TARGET_BUFFER

    exit_reason: Optional[str] = None
    if t >= MP_FORCE_CLOSE:
        exit_reason = 'Force-Close (14:30)'
    elif target_hit:
        exit_reason = f'MaxPain {max_pain:,.0f} reached ✓'
    elif gain_pct <= -0.25:
        exit_reason = 'Stop (-25%)'

    if exit_reason:
        units = pos['lots'] * pos['lot_size']
        pnl   = (cur_px - entry_px) * units
        cap   = _paper_capital()
        _capital[instrument]   = _capital.get(instrument, cap) + pnl
        _daily_pnl[instrument] = _daily_pnl.get(instrument, 0.0) + pnl
        _total_pnl[instrument] = _total_pnl.get(instrument, 0.0) + pnl
        logger.info(
            f"  [MP-TRAP PAPER] EXIT {pos['signal_type']} {instrument} | "
            f"{exit_reason} | "
            f"Entry@{pos['entry_time']} ₹{entry_px:.2f} → ₹{cur_px:.2f} "
            f"({gain_pct*100:+.1f}%) | "
            f"Spot {pos['entry_spot']:,.0f}→{spot:,.0f} | MaxPain={max_pain:,.0f} | "
            f"P&L ₹{pnl:+,.0f} | "
            f"DailyP&L=₹{_daily_pnl[instrument]:+,.0f} | "
            f"Capital=₹{_capital[instrument]:,.0f}"
        )
        _log_trade(pos, exit_reason, spot, cur_px, pnl, gain_pct, now)
        _sim[instrument] = None
    else:
        logger.info(
            f"  [MP-TRAP PAPER] OPEN {pos['signal_type']} {instrument} | "
            f"MaxPain={max_pain:,.0f} | Spot={spot:,.0f} | "
            f"Dist={abs(spot - max_pain):.0f}pts ({mp_dist*100:.2f}% from pin) | "
            f"≈{gain_pct*100:+.1f}%"
        )


# ── Main entry point ──────────────────────────────────────────────────────────

def evaluate_bar(
    instrument : str,
    df,
    oc         : dict,
    dte        : int,
    now        : datetime,
    lot_size   : int,
    strike_gap : int,
    logger     : logging.Logger,
) -> None:
    """
    Called once per 5-min bar from options_bot.py — independent of ORB limits.

    1. Always: update (and possibly close) any open paper position.
    2. If in entry window and no trade today: evaluate entry conditions.
    """
    spot     = float(df.iloc[-1]['Close'])
    max_pain = (oc or {}).get('max_pain')
    pcr      = (oc or {}).get('pcr')
    atm_iv   = (oc or {}).get('atm_iv') or 15.0

    # ── 1. Update open position (always, every bar) ───────────────────────────
    if _sim.get(instrument):
        try:
            _update_position(instrument, spot, logger, now)
        except Exception as exc:
            logger.warning(f"  [MP-TRAP] _update_position error: {exc}")

    # ── 2. Entry gate checks ──────────────────────────────────────────────────
    if _fired_today.get(instrument):
        return

    if dte > _dte_max():
        return

    if not max_pain:
        return   # NSE data unavailable (SENSEX/BSE or NSE down)

    t = now.time()
    if not (MP_ENTRY_START <= t <= MP_ENTRY_END):
        return

    # Per-bar dedup
    bar_ts = df.index[-1]
    if _last_eval_ts.get(instrument) == bar_ts:
        return
    _last_eval_ts[instrument] = bar_ts

    # Daily loss halt
    if _daily_pnl.get(instrument, 0.0) <= -_daily_loss_limit():
        return

    # ── 3. Per-instrument thresholds ─────────────────────────────────────────
    gap_threshold  = _gap_pct(instrument)
    pcr_put_thr    = _pcr_put_confirm(instrument)
    pcr_call_thr   = _pcr_call_confirm(instrument)

    gap = (spot - max_pain) / max_pain   # + = above MaxPain, - = below

    signal_type: Optional[str] = None

    if gap >= gap_threshold:
        # Spot above MaxPain → gravity pulls DOWN → PUT
        if pcr is not None and pcr >= pcr_put_thr:
            logger.info(
                f"  [MP-TRAP] {instrument}: PUT candidate — "
                f"gap={gap*100:+.2f}% (≥{gap_threshold*100:.2f}%) ✓ "
                f"but PCR={pcr:.2f} ≥ {pcr_put_thr:.2f} (not bearish) — skip"
            )
            return
        signal_type = 'PUT'

    elif gap <= -gap_threshold:
        # Spot below MaxPain → gravity pulls UP → CALL
        if pcr is not None and pcr <= pcr_call_thr:
            logger.info(
                f"  [MP-TRAP] {instrument}: CALL candidate — "
                f"gap={gap*100:+.2f}% (≤-{gap_threshold*100:.2f}%) ✓ "
                f"but PCR={pcr:.2f} ≤ {pcr_call_thr:.2f} (not bullish) — skip"
            )
            return
        signal_type = 'CALL'

    else:
        # Gap < threshold — log first bar of window only (avoid noise)
        if t == MP_ENTRY_START or (df.index[-1] == df.index[-1]):
            logger.info(
                f"  [MP-TRAP] {instrument}: gap {gap*100:+.2f}% "
                f"< ±{gap_threshold*100:.2f}% threshold "
                f"(MaxPain={max_pain:,.0f} | DTE={dte}) — no displacement"
            )
        return

    if signal_type is None:
        return

    # ── 4. Option pricing (Black-Scholes ATM) ────────────────────────────────
    atm_strike = int(round(spot / strike_gap) * strike_gap)
    T          = max(dte / 365, 0.0001)
    iv_dec     = atm_iv / 100
    entry_px   = _bs(signal_type, spot, atm_strike, T, iv_dec)

    if entry_px < 0.50:
        logger.info(
            f"  [MP-TRAP] {instrument}: entry premium ₹{entry_px:.2f} too low "
            f"(IV={atm_iv:.1f}%, DTE={dte}) — skip"
        )
        return

    # ── 5. Lot sizing ─────────────────────────────────────────────────────────
    cap          = _paper_capital()
    avail        = _capital.get(instrument, cap)
    cost_per_lot = entry_px * lot_size
    if cost_per_lot <= 0:
        return
    lots       = max(1, min(MP_MAX_LOTS, int(MP_TARGET_SPEND / cost_per_lot)))
    total_cost = lots * cost_per_lot

    if total_cost > avail:
        lots       = max(1, int(avail / cost_per_lot))
        total_cost = lots * cost_per_lot

    if lots < 1 or total_cost > avail:
        logger.info(
            f"  [MP-TRAP] {instrument}: insufficient capital "
            f"₹{avail:.0f} < ₹{cost_per_lot:.0f}/lot"
        )
        return

    # ── 6. Enter ──────────────────────────────────────────────────────────────
    _capital[instrument]     = avail - total_cost
    _trades[instrument]      = _trades.get(instrument, 0) + 1
    _fired_today[instrument] = True

    pcr_str = f'{pcr:.2f}' if pcr is not None else 'N/A'
    logger.info(
        f"  [MP-TRAP PAPER] ⚡ ENTER {signal_type} {instrument} | "
        f"MaxPain={max_pain:,.0f} | Spot={spot:,.0f} | "
        f"Gap={gap*100:+.2f}% ({'above' if gap > 0 else 'below'} pin) | "
        f"PCR={pcr_str} (thr={'<' if signal_type=='PUT' else '>'}"
        f"{pcr_put_thr if signal_type=='PUT' else pcr_call_thr:.2f}) | "
        f"Gap-thr={gap_threshold*100:.2f}% | ATM-IV={atm_iv:.1f}% | DTE={dte} | "
        f"Strike={atm_strike:,} | Est.Prem=₹{entry_px:.2f} | "
        f"{lots}×{lot_size}u=₹{total_cost:.0f} | "
        f"Target: spot→{max_pain:,.0f} | Capital=₹{_capital[instrument]:,.0f}"
    )

    _sim[instrument] = {
        'signal_type'  : signal_type,
        'instrument'   : instrument,
        'entry_spot'   : spot,
        'max_pain'     : max_pain,
        'atm_strike'   : atm_strike,
        'entry_premium': entry_px,
        'iv'           : iv_dec,
        'dte'          : dte,
        'lots'         : lots,
        'lot_size'     : lot_size,
        'entry_time'   : now.strftime('%H:%M'),
        'pcr_at_entry' : pcr,
    }


# ── Status helper ─────────────────────────────────────────────────────────────

def status_summary() -> dict:
    """Return per-instrument status dict for morning briefing."""
    result = {}
    for inst, pos in _sim.items():
        result[inst] = {
            'open_position': pos is not None,
            'capital'      : _capital.get(inst, _paper_capital()),
            'daily_pnl'    : _daily_pnl.get(inst, 0.0),
            'total_pnl'    : _total_pnl.get(inst, 0.0),
            'trades'       : _trades.get(inst, 0),
            'gap_threshold': _gap_pct(inst),
        }
    return result
