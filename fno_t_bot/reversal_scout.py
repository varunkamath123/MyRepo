# -*- coding: utf-8 -*-
"""
FnO_T_Bot — Path D: Reversal Scout  (Phase 2: Paper Trading)
=============================================================
Identifies HIGH-CONVICTION reversal setups where a far-OTM option
would offer extreme asymmetric reward — the "lottery ticket" trade.

Philosophy
----------
  Paths A / B / C all buy ATM options targeting 40–130% gains.
  Path D targets options 2 strikes OTM:

    ATM  ₹150  → +87% if index moves 300 pts
    OTM  ₹20   → +500% on the same 300-pt move  (option goes near-ITM)

  The OTM premium is ⅛ the ATM cost, so the maximum loss is small and
  fixed.  But when multiple signals converge the move is likely to be
  LARGE — exactly the environment where OTM options pay off most.

Scoring (max 13 points)
-----------------------
  1. PCR extreme    0–2 pts   (< 0.65 or > 1.35 → crowd is wrong, squeeze risk)
  2. HTF alignment  0–2 pts   (15m SuperTrend + EMA: partial credit for transition)
  3. OI wall bounce 0–2 pts   (price AT the wall it should BOUNCE OFF:
                               CALL = near PUT-wall/support below price;
                               PUT  = near CALL-wall/resistance above price)
  4. ADX strength   0–1 pt    (ADX ≥ 35 → momentum confirmed)
  5. IV Skew        0–1 pt    (skew > 7% in the trade direction)
  6. ST flip        0–3 pts   (15m SuperTrend flipped direction in last ~20 min —
                               PRIMARY reversal trigger; valid for 4 bars after flip)
  7. MaxPain gravity 0–2 pts  (price ≥ 0.8% away from MaxPain →
                               gravity pull toward MaxPain at expiry;
                               CALL when price below MaxPain, PUT when above)

  Score ≥ 5 → open paper OTM position, track and close at target/stop/force-close.

Phase 2 (now active)
  Paper capital: ₹10,000 per instrument (independent budgets).
  Target spend : ~₹3,000 per trade (scaled lots, max 3).
  Stop         : 80% of premium (option near-zero).
  Target       : 300% gain (option 4× entry price).
  Force-close  : 14:30 IST.
  Max daily loss: ₹3,000 → stop trading Path D for rest of day.
  Max 1 open position per instrument at a time.

Usage (from paper_bot.py)
--------------------------
  import reversal_scout
  # called once per bar in the main loop:
  reversal_scout.evaluate_bar(
      instrument    = self.instrument,
      df            = df,
      htf           = htf,
      oc            = oc,
      oi_zones      = self._oi_zones,
      inst_cfg      = self.inst_cfg,
      hv            = hv,
      logger        = self.logger,
      now           = now,
      in_window     = _in_window,
      days_to_expiry= config.DAYS_TO_EXPIRY,
  )
"""

from __future__ import annotations

import logging
from datetime import datetime, time as dtime
from math import log, sqrt, exp
from typing import Optional

import pytz
from scipy.stats import norm

IST = pytz.timezone('Asia/Kolkata')

# ── Phase ─────────────────────────────────────────────────────────────────────
PHASE = 2   # 1 = log + simulate only | 2 = paper trade (active)

# ── Paper capital (Phase 2) ────────────────────────────────────────────────────
PATH_D_CAPITAL       = 10_000.0   # ₹10,000 starting paper capital per instrument
PATH_D_TARGET_SPEND  =  3_000.0   # target ₹3,000 deployment per trade (scaled lots)
PATH_D_MAX_LOTS      =      3     # cap at 3 lots regardless of premium
PATH_D_MAX_DAILY_LOSS=  3_000.0   # halt Path D trading if daily loss exceeds this

# ── Convergence thresholds ─────────────────────────────────────────────────────
PCR_BULL_EXTREME  = 0.65    # PCR below this → extreme CALL squeeze risk
PCR_BULL_MODERATE = 0.82    # PCR below this → moderate CALL setup
PCR_BEAR_EXTREME  = 1.35    # PCR above this → extreme PUT squeeze risk
PCR_BEAR_MODERATE = 1.18    # PCR above this → moderate PUT setup

ADX_HIGH          = 35      # ADX ≥ this → strong momentum
IVSKEW_HIGH       = 7.0     # IVSkew % magnitude → directional premium
WALL_CLOSE_PCT    = 0.002   # within 0.2% of wall → very close  (score +2)
WALL_NEAR_PCT     = 0.005   # within 0.5% of wall → approaching (score +1)

ST_FLIP_BARS      = 4       # 15m-ST flip signal stays active for this many 5-min bars (~20 min)
ST_FLIP_PTS       = 3       # points awarded for a recent 15m-ST direction flip

MAXPAIN_FAR_PCT   = 0.015   # price >1.5% from MaxPain → strong gravity  (+2)
MAXPAIN_NEAR_PCT  = 0.008   # price >0.8% from MaxPain → moderate gravity (+1)

MIN_SCORE         = 5       # minimum to fire a sim log entry
MAX_SCORE         = 13      # 11 + 2 for MaxPain gravity component

# ── OTM / position settings ────────────────────────────────────────────────────
OTM_STRIKES       = 2       # number of strike_gap steps away from ATM
PAPER_TARGET_PCT  = 3.00    # 300% gain → close (option went to 4× entry price)
PAPER_STOP_PCT    = 0.80    # 80% loss  → close (avoid holding near-zero)
PAPER_FORCE_CLOSE = dtime(14, 30)   # force-close all positions at 14:30

# ── In-memory paper position store (one per instrument) ───────────────────────
# Structure: { 'NIFTY': {…position dict…} | None, … }
_sim: dict[str, Optional[dict]] = {}

# Capital and P&L tracking (per instrument, per process)
_capital:   dict[str, float] = {}   # remaining paper capital
_daily_pnl: dict[str, float] = {}   # today's Path D P&L
_total_pnl: dict[str, float] = {}   # cumulative Path D P&L
_trades:    dict[str, int]   = {}   # total trades fired (for counting)

# Track last bar timestamp per instrument to avoid re-evaluating same bar
_last_eval_ts: dict[str, object] = {}

# 15m SuperTrend flip tracking (for component #6)
# _prev_st15      : last observed ST value per instrument (+1/-1)
# _st_flip_recent : active flip signal per instrument → (direction, bars_remaining)
_prev_st15:      dict[str, Optional[int]]         = {}
_st_flip_recent: dict[str, tuple[str, int]]       = {}


# ── Black-Scholes ──────────────────────────────────────────────────────────────

def _bs(opt_type: str, S: float, K: float, T: float, hv: float,
        r: float = 0.065) -> float:
    """European option price via Black-Scholes. Returns 0.0 on any error."""
    try:
        if T <= 0 or hv <= 0 or S <= 0 or K <= 0:
            return max(0.0, S - K) if opt_type == 'CALL' else max(0.0, K - S)
        d1 = (log(S / K) + (r + 0.5 * hv ** 2) * T) / (hv * sqrt(T))
        d2 = d1 - hv * sqrt(T)
        if opt_type == 'CALL':
            return S * norm.cdf(d1) - K * exp(-r * T) * norm.cdf(d2)
        return K * exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
    except Exception:
        return 0.0


# ── Scoring helpers ────────────────────────────────────────────────────────────

def _score_one_direction(
    signal_type: str,
    price      : float,
    adx        : float,
    htf        : dict,
    oc         : dict,
    oi_zones   : Optional[dict],
) -> tuple[int, dict]:
    """
    Score a single direction (CALL or PUT) across 5 components.
    Returns (total_score, components_dict).
    components_dict = { name: (points, description_string) }
    """
    C: dict[str, tuple[int, str]] = {}

    # ── 1. PCR extreme (max +2) ────────────────────────────────────────────────
    pcr = (oc or {}).get('pcr')
    pcr_pts, pcr_note = 0, ''
    if pcr is not None:
        if signal_type == 'CALL':
            if pcr < PCR_BULL_EXTREME:
                pcr_pts, pcr_note = 2, f'PCR={pcr:.2f} EXTREME-low → squeeze risk ⚠'
            elif pcr < PCR_BULL_MODERATE:
                pcr_pts, pcr_note = 1, f'PCR={pcr:.2f} low'
        else:  # PUT
            if pcr > PCR_BEAR_EXTREME:
                pcr_pts, pcr_note = 2, f'PCR={pcr:.2f} EXTREME-high → capitulation risk ⚠'
            elif pcr > PCR_BEAR_MODERATE:
                pcr_pts, pcr_note = 1, f'PCR={pcr:.2f} high'
    C['pcr'] = (pcr_pts, pcr_note)

    # ── 2. Higher-timeframe alignment (max +2) ─────────────────────────────────
    # SuperTrend only — EMA dropped from HTF (lags more on 15m bars than ST).
    # ST is ATR-based, adapts to volatility, always definitive (+1/-1).
    st15 = (htf or {}).get('supertrend_15m')   # +1=bull, -1=bear
    htf_pts, htf_note = 0, ''
    if signal_type == 'CALL':
        if st15 == 1:
            htf_pts, htf_note = 2, '15m ST=BULL'
    else:
        if st15 == -1:
            htf_pts, htf_note = 2, '15m ST=BEAR'
    C['htf'] = (htf_pts, htf_note)

    # ── 3. OI wall bounce (max +2) — REVERSAL direction ───────────────────────
    # Path D trades REVERSALS, so we score the wall the price should BOUNCE OFF:
    #   CALL reversal: price is sitting AT/near a PUT-wall (heavy put OI = support
    #                  below) → market makers defend → bounce UP → CALL
    #   PUT  reversal: price is sitting AT/near a CALL-wall (heavy call OI =
    #                  resistance above) → market makers defend → bounce DOWN → PUT
    # This is the OPPOSITE of a breakout path (which would score resistance for CALL).
    wall_pts, wall_note = 0, ''
    if oi_zones:
        if signal_type == 'CALL':
            # Bounce up from PUT-wall (support) BELOW price
            raw = oi_zones.get('support', [])
            candidates = sorted(
                [w['strike'] for w in raw if isinstance(w, dict)
                 and w.get('strike', 0) < price],
                reverse=True,   # nearest support below price first
            )
            label = 'PUT-wall/support'
        else:
            # Bounce down from CALL-wall (resistance) ABOVE price
            raw = oi_zones.get('resistance', [])
            candidates = sorted(
                [w['strike'] for w in raw if isinstance(w, dict)
                 and w.get('strike', 0) > price],
            )           # nearest resistance above price first
            label = 'CALL-wall/resistance'
        if candidates:
            nearest  = candidates[0]
            dist_pct = abs(nearest - price) / price
            if dist_pct <= WALL_CLOSE_PCT:
                wall_pts  = 2
                wall_note = (f'{label} {nearest:,.0f} AT price '
                             f'({dist_pct*100:.2f}% away) — bounce zone ⚠')
            elif dist_pct <= WALL_NEAR_PCT:
                wall_pts  = 1
                wall_note = (f'{label} {nearest:,.0f} near '
                             f'({dist_pct*100:.2f}% away)')
    C['oi_wall'] = (wall_pts, wall_note)

    # ── 4. ADX strength (max +1) ───────────────────────────────────────────────
    adx_pts = 1 if adx >= ADX_HIGH else 0
    C['adx'] = (adx_pts, f'ADX={adx:.1f}{"✓" if adx_pts else f" <{ADX_HIGH}"}')

    # ── 5. IV Skew confirmation (max +1) ───────────────────────────────────────
    # Positive iv_skew = put_iv > call_iv (market fears downside = PUT confirming)
    iv_skew    = (oc or {}).get('iv_skew')
    skew_pts, skew_note = 0, ''
    if iv_skew is not None:
        if signal_type == 'PUT' and iv_skew > IVSKEW_HIGH:
            skew_pts  = 1
            skew_note = f'IVSkew={iv_skew:+.1f}% (fear premium → confirms PUT)'
        elif signal_type == 'CALL' and iv_skew < -IVSKEW_HIGH:
            skew_pts  = 1
            skew_note = f'IVSkew={iv_skew:+.1f}% (call premium → confirms CALL)'
    C['iv_skew'] = (skew_pts, skew_note)

    # ── 6. 15m SuperTrend flip (max +3) — primary reversal trigger ─────────────
    # When 15m-ST has JUST flipped direction (within last ~20 min), this is the
    # core reversal confirmation.  The flip direction is injected into htf by
    # evaluate_bar() BEFORE this function is called.
    # Positive: it overrides the trend-following bias of component #2.
    st_flip_dir = (htf or {}).get('st_flip_dir')   # 'CALL', 'PUT', or None
    flip_pts, flip_note = 0, ''
    if st_flip_dir == signal_type:
        flip_pts  = ST_FLIP_PTS
        flip_note = f'15m ST flip→{st_flip_dir} ✅ REVERSAL confirmed'
    C['st_flip'] = (flip_pts, flip_note)

    # ── 7. MaxPain gravity (max +2) ────────────────────────────────────────────
    # At expiry, market makers' hedging flows pull the index toward the strike
    # where the total option premium is minimised (MaxPain).
    # If price is significantly BELOW MaxPain → CALL (gravity pulls up).
    # If price is significantly ABOVE MaxPain → PUT  (gravity pulls down).
    # SENSEX has no MaxPain (BSE, not NSE) — oc['max_pain'] will be None.
    max_pain = (oc or {}).get('max_pain')
    mp_pts, mp_note = 0, ''
    if max_pain and price > 0:
        mp_dist      = price - max_pain          # + = price above MaxPain
        mp_dist_pct  = mp_dist / price
        if signal_type == 'CALL' and mp_dist_pct < -MAXPAIN_FAR_PCT:
            mp_pts  = 2
            mp_note = (f'MaxPain={max_pain:,.0f} | price {abs(mp_dist_pct)*100:.1f}% '
                       f'BELOW → strong CALL gravity ⚠')
        elif signal_type == 'CALL' and mp_dist_pct < -MAXPAIN_NEAR_PCT:
            mp_pts  = 1
            mp_note = (f'MaxPain={max_pain:,.0f} | price {abs(mp_dist_pct)*100:.1f}% '
                       f'below → CALL gravity')
        elif signal_type == 'PUT' and mp_dist_pct > MAXPAIN_FAR_PCT:
            mp_pts  = 2
            mp_note = (f'MaxPain={max_pain:,.0f} | price {mp_dist_pct*100:.1f}% '
                       f'ABOVE → strong PUT gravity ⚠')
        elif signal_type == 'PUT' and mp_dist_pct > MAXPAIN_NEAR_PCT:
            mp_pts  = 1
            mp_note = (f'MaxPain={max_pain:,.0f} | price {mp_dist_pct*100:.1f}% '
                       f'above → PUT gravity')
    C['max_pain'] = (mp_pts, mp_note)

    total = sum(v[0] for v in C.values())
    return total, C


# ── Formatting ─────────────────────────────────────────────────────────────────

def _fmt(
    signal_type : str,
    instrument  : str,
    score       : int,
    components  : dict,
    price       : float,
    atm_strike  : int,
    otm_strike  : int,
    otm_premium : float,
    lot_size    : int,
    lots        : int,
    opened_sim  : bool,
) -> str:
    bar  = '█' * score + '░' * (MAX_SCORE - score)
    dist = abs(otm_strike - atm_strike)
    cost = otm_premium * lot_size

    lines = [
        f"  [PATH-D] {signal_type:4s} {instrument} | "
        f"Score={score}/{MAX_SCORE} [{bar}]",

        f"  Underlying={price:,.0f} | "
        f"ATM={atm_strike:,} → OTM={otm_strike:,} ({dist}pts away)",

        f"  Entry=₹{otm_premium:.2f} | "
        f"{lots}×{lot_size}units=₹{cost:.0f} deployed | "
        f"Target=₹{otm_premium*(1+PAPER_TARGET_PCT):.2f} (+{PAPER_TARGET_PCT*100:.0f}%) | "
        f"Stop=₹{otm_premium*(1-PAPER_STOP_PCT):.2f} (-{PAPER_STOP_PCT*100:.0f}%)",
    ]

    # Component detail line
    parts = []
    for name, (pts, note) in components.items():
        if note:
            parts.append(f"{'✓' if pts > 0 else '·'} {note}")
    if parts:
        lines.append('  ' + ' | '.join(parts))

    if opened_sim:
        lines.append(
            f"  ⚡ [PATH-D PAPER] ENTERED (score {score}/{MAX_SCORE} ≥ {MIN_SCORE})"
        )
    return '\n'.join(lines)


# ── Sim position management ────────────────────────────────────────────────────

def _update_sim(
    instrument     : str,
    df,
    hv             : float,
    now            : datetime,
    logger         : logging.Logger,
    days_to_expiry : int,
) -> None:
    """
    Check the open Path D paper position for this instrument each bar.
    Close on target (300%), stop (80%), or force-close (14:30).
    Updates capital and P&L tracking on close.
    """
    pos = _sim.get(instrument)
    if not pos:
        return

    t         = now.time()
    price     = float(df.iloc[-1]['Close'])
    T         = max(days_to_expiry / 365, 0.001)
    opt_price = _bs(pos['signal_type'], price, pos['otm_strike'], T, hv)

    if pos['entry_premium'] <= 0:
        return  # guard against division-by-zero

    effective_units = pos['lots'] * pos['lot_size']
    gain_pct        = (opt_price - pos['entry_premium']) / pos['entry_premium']
    pnl             = (opt_price - pos['entry_premium']) * effective_units

    exit_reason = None
    if t >= PAPER_FORCE_CLOSE:
        exit_reason = 'Force-Close (14:30)'
    elif gain_pct >= PAPER_TARGET_PCT:
        exit_reason = f'Target (+{PAPER_TARGET_PCT*100:.0f}%)'
    elif gain_pct <= -PAPER_STOP_PCT:
        exit_reason = f'Stop (-{PAPER_STOP_PCT*100:.0f}%)'

    if exit_reason:
        # Update capital and P&L
        _capital[instrument]   = _capital.get(instrument, PATH_D_CAPITAL) + pnl
        _daily_pnl[instrument] = _daily_pnl.get(instrument, 0.0) + pnl
        _total_pnl[instrument] = _total_pnl.get(instrument, 0.0) + pnl

        logger.info(
            f"  [PATH-D PAPER] EXIT {pos['signal_type']:4s} {instrument} | "
            f"{exit_reason} | Score={pos['score']}/8 | "
            f"Strike={pos['otm_strike']:,} | "
            f"Entry@{pos['entry_time']} ₹{pos['entry_premium']:.2f} → ₹{opt_price:.2f} | "
            f"P&L=₹{pnl:+,.2f} ({gain_pct*100:+.1f}%) | "
            f"{pos['lots']}lots×{pos['lot_size']}units | "
            f"DailyD=₹{_daily_pnl[instrument]:+,.0f} | "
            f"Capital=₹{_capital[instrument]:,.0f}"
        )
        _sim[instrument] = None
    else:
        logger.info(
            f"  [PATH-D PAPER] OPEN {pos['signal_type']:4s} {instrument} | "
            f"Strike={pos['otm_strike']:,} | "
            f"₹{pos['entry_premium']:.2f} → ₹{opt_price:.2f} "
            f"({gain_pct*100:+.1f}% | P&L ₹{pnl:+,.0f})"
        )


# ── Public API ─────────────────────────────────────────────────────────────────

def daily_reset(instrument: str, logger: logging.Logger | None = None) -> None:
    """
    Call at the start of each new trading day (from paper_bot.py daily reset).
    Resets daily P&L counter; leaves capital and total P&L intact.
    Force-closes any overnight position (shouldn't happen, but safe guard).
    """
    if _sim.get(instrument):
        if logger:
            logger.warning(
                f"  [PATH-D] {instrument}: open position at daily reset — clearing"
            )
        _sim[instrument] = None

    prev_daily = _daily_pnl.get(instrument, 0.0)
    if logger and prev_daily != 0.0:
        logger.info(
            f"  [PATH-D] {instrument}: yesterday P&L=₹{prev_daily:+,.0f} | "
            f"Capital=₹{_capital.get(instrument, PATH_D_CAPITAL):,.0f} | "
            f"Total P&L=₹{_total_pnl.get(instrument, 0.0):+,.0f} | "
            f"Trades so far={_trades.get(instrument, 0)}"
        )
    _daily_pnl[instrument] = 0.0
    _last_eval_ts.pop(instrument, None)
    _st_flip_recent.pop(instrument, None)   # don't carry flip signal across days


def evaluate_bar(
    instrument     : str,
    df,
    htf            : dict,
    oc             : dict,
    oi_zones       : Optional[dict],
    inst_cfg       : dict,
    hv             : float,
    logger         : logging.Logger,
    now            : datetime,
    in_window      : bool,
    days_to_expiry : int = 2,
) -> None:
    """
    Called once per 5-min bar from paper_bot.py's main loop.

    1. Always: update (and possibly close) any open sim position.
    2. If in_window and no sim position open: score CALL and PUT,
       log the better one if score ≥ MIN_SCORE, open sim position.
    """
    # ── 1. Update open sim position (always, regardless of window) ─────────────
    try:
        _update_sim(instrument, df, hv, now, logger, days_to_expiry)
    except Exception as exc:
        logger.warning(f"  [PATH-D] _update_sim error: {exc}")

    # ── 1b. Track 15m-ST flip on EVERY bar (even outside window) ───────────────
    # Must happen before the early-return so flips outside the window are not
    # missed — a flip at 10:55 is still a valid reversal signal at 11:00.
    try:
        cur_st15  = (htf or {}).get('supertrend_15m')   # +1 or -1 or None
        prev_st15 = _prev_st15.get(instrument)
        if cur_st15 is not None:
            if prev_st15 is not None and cur_st15 != prev_st15:
                # New flip — arm the recent-flip signal for ST_FLIP_BARS bars
                flip_dir = 'CALL' if cur_st15 == 1 else 'PUT'
                _st_flip_recent[instrument] = (flip_dir, ST_FLIP_BARS)
                logger.info(
                    f"  [PATH-D] {instrument}: 15m-ST flipped "
                    f"{prev_st15:+d}→{cur_st15:+d} ({flip_dir}) at {now.strftime('%H:%M')} "
                    f"— reversal signal active for {ST_FLIP_BARS} bars"
                )
            elif instrument in _st_flip_recent:
                # Decrement existing flip counter
                fd, bl = _st_flip_recent[instrument]
                if bl > 1:
                    _st_flip_recent[instrument] = (fd, bl - 1)
                else:
                    _st_flip_recent.pop(instrument, None)
            _prev_st15[instrument] = cur_st15
    except Exception:
        pass

    # ── 2. Only score during entry window ──────────────────────────────────────
    if not in_window:
        return

    # ── 3. Deduplicate: one evaluation per 5-min bar per instrument ────────────
    bar_ts = df.index[-1]
    if _last_eval_ts.get(instrument) == bar_ts:
        return
    _last_eval_ts[instrument] = bar_ts

    # ── 4. Skip if sim position already open ───────────────────────────────────
    if _sim.get(instrument):
        return

    # ── 5. Score both directions, pick the best ────────────────────────────────
    try:
        row   = df.iloc[-1]
        price = float(row['Close'])
        adx   = float(row.get('ADX', 0) or 0)

        # Inject active ST flip direction into htf for component #6 scoring
        flip_info = _st_flip_recent.get(instrument)
        htf_aug   = dict(htf or {})
        htf_aug['st_flip_dir'] = flip_info[0] if flip_info else None

        best_score, best_type, best_comps = -1, None, {}
        for stype in ('CALL', 'PUT'):
            sc, comps = _score_one_direction(
                stype, price, adx, htf_aug, oc, oi_zones
            )
            if sc > best_score:
                best_score, best_type, best_comps = sc, stype, comps

        if best_score < MIN_SCORE or best_type is None:
            # Log near-miss scores (≥ 3/11) so we can monitor how close we are
            # without waiting for a fire. Silent below 3 to avoid noise.
            if best_score >= 3 and best_type is not None:
                _best_comps_str = ' | '.join(
                    f"{n}={v[0]}pt" for n, v in best_comps.items() if v[0] > 0
                )
                logger.info(
                    f"  [PATH-D] {best_type:4s} {instrument} | "
                    f"Score={best_score}/{MAX_SCORE} — "
                    f"{MIN_SCORE - best_score}pt below threshold | "
                    f"Px={price:,.0f} | {_best_comps_str if _best_comps_str else 'no components met'}"
                )
            return

        # ── 6. Build OTM strike ────────────────────────────────────────────────
        gap        = int(inst_cfg.get('strike_gap', 50))
        lot_size   = int(inst_cfg.get('lot_size', 1))
        atm_strike = int(round(price / gap) * gap)
        otm_strike = (atm_strike + OTM_STRIKES * gap if best_type == 'CALL'
                      else atm_strike - OTM_STRIKES * gap)

        T           = max(days_to_expiry / 365, 0.001)
        otm_premium = _bs(best_type, price, otm_strike, T, hv)

        # Don't enter if option is essentially worthless (too far OTM)
        if otm_premium < 0.50:
            return

        # ── 7. Capital guard (Phase 2) ─────────────────────────────────────────
        avail_capital = _capital.get(instrument, PATH_D_CAPITAL)
        daily_loss    = _daily_pnl.get(instrument, 0.0)

        if daily_loss <= -PATH_D_MAX_DAILY_LOSS:
            logger.info(
                f"  [PATH-D] {instrument}: daily loss ₹{daily_loss:,.0f} reached "
                f"limit ₹{PATH_D_MAX_DAILY_LOSS:,.0f} — Path D halted for today"
            )
            return

        # ── 8. Lot sizing — deploy ~₹3,000 per trade, max 3 lots ──────────────
        cost_per_lot = otm_premium * lot_size
        lots = max(1, min(PATH_D_MAX_LOTS,
                          int(PATH_D_TARGET_SPEND / cost_per_lot)))
        total_cost = lots * cost_per_lot

        if total_cost > avail_capital:
            lots = max(1, int(avail_capital / cost_per_lot))
            total_cost = lots * cost_per_lot

        if lots < 1 or total_cost > avail_capital:
            logger.info(
                f"  [PATH-D] {instrument}: insufficient capital "
                f"(need ₹{cost_per_lot:.0f}, have ₹{avail_capital:.0f}) — skip"
            )
            return

        logger.info(_fmt(
            signal_type = best_type,
            instrument  = instrument,
            score       = best_score,
            components  = best_comps,
            price       = price,
            atm_strike  = atm_strike,
            otm_strike  = otm_strike,
            otm_premium = otm_premium,
            lot_size    = lot_size,
            lots        = lots,
            opened_sim  = True,
        ))

        # Deduct cost from paper capital
        _capital[instrument]  = avail_capital - total_cost
        _trades[instrument]   = _trades.get(instrument, 0) + 1

        _sim[instrument] = {
            'signal_type'   : best_type,
            'instrument'    : instrument,
            'otm_strike'    : otm_strike,
            'entry_premium' : otm_premium,
            'entry_price'   : price,
            'lot_size'      : lot_size,
            'lots'          : lots,
            'score'         : best_score,
            'hv'            : hv,
            'entry_time'    : now.strftime('%H:%M'),
        }

    except Exception as exc:
        logger.warning(f"  [PATH-D] evaluate_bar error: {exc}")
