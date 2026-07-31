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

import json
import logging
import os
from datetime import datetime, time as dtime
from typing import Optional

import pandas as pd
import pytz

from reversal_guard import compute_reversal_risk

IST = pytz.timezone('Asia/Kolkata')

# ── MiroFish (news/sentiment) — context only, never a gate ──────────────────
# NIFTY/BANKNIFTY only (no SENSEX read exists). mirofish_swarm.py writes to
# MIROFISH_OUT_FILE = /opt/trading_bot/live_bot/mirofish_scores.json on EC2
# (repo/ is root/ec2-user-owned, tradingbot can't write there — live_bot/ is).
# Repo-relative guess kept as a fallback for local/dev runs.
_MIRO_PATHS = [
    '/opt/trading_bot/live_bot/mirofish_scores.json',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mirofish_scores.json'),
]
MIRO_MAX_AGE_HOURS = 8   # stale data is treated as absent, never blocks/ages badly


def _read_mirofish(instrument: str) -> Optional[dict]:
    """Return {'lean':.., 'score':.., 'age_h':..} for NIFTY/BANKNIFTY, or None.

    Graceful on every failure mode (missing file, stale, wrong instrument,
    malformed JSON) — mirrors paper_trader.py's precedent: missing/stale
    MiroFish data never blocks anything, it's just absent context.
    """
    if instrument not in ('NIFTY', 'BANKNIFTY'):
        return None
    for path in _MIRO_PATHS:
        if os.path.exists(path):
            break
    else:
        return None
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        if instrument not in data or 'score' not in data[instrument]:
            return None
        gen = datetime.fromisoformat(data['generated_at'])
        age_h = (datetime.now(IST) - gen).total_seconds() / 3600
        if age_h > MIRO_MAX_AGE_HOURS:
            return None
        return {'lean': data[instrument].get('lean', 'neutral'),
                'score': float(data[instrument]['score']), 'age_h': round(age_h, 1)}
    except Exception:
        return None

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
MAX_CHASE      = 0.55            # HARD anticipation guard (Jul 22 replay fix): the
                                 # entry must sit in the lower-mid of the day range
                                 # in its own direction. Without this, VWAP-reject on
                                 # a trending day fires near the day extreme (chase
                                 # 0.85-0.92) — re-creating the chase problem. A true
                                 # anticipation entry is low-chase by definition.
FORCE_CLOSE    = dtime(14, 30)

# ── Exhaustion gate (Jul 25 2026) ────────────────────────────────────────────
# "Is today a trend day?" was tested empirically (day_classifier's eff/range
# framework, time-scaled multiple ways) and DOES NOT predict the full-day
# TREND label reliably by 11:00-12:00 — recall and false-positive rate track
# each other 1:1 across a full grid search (11-34% recall). Not a tuning
# problem: the day-level forecast just isn't callable this early. Discarded.
#
# The tractable question is narrower: is THIS SPECIFIC move — the one
# anticipation is about to fade — actually exhausted right now, or still
# accelerating? reversal_guard.compute_reversal_risk() already answers this,
# and is ALREADY LIVE on the confirmatory side (options_bot.py ~line 4918):
# a CONFIRMATORY entry gets capped/skipped when exhaustion risk in its OWN
# direction is high (>=30 caps lots, >=50 skips — proven thresholds, real
# money). Anticipation needs the SAME check with INVERTED polarity: a CALL
# here is a bet that a DOWN-move is exhausted, so it must show HIGH
# exhaustion risk if scored as a hypothetical PUT (and vice versa). This is
# the Jul22-replay fix: that day's anticipation CALLs lost because they faded
# a still-accelerating downtrend — compute_reversal_risk(df, i, 'PUT') would
# have read LOW (fresh move, not exhausted) and blocked exactly those trades.
#
# RETIRED AS A GATE (Jul 30 2026) — kept only as a logged diagnostic field.
# See the "Exhaustion score — LOGGED ONLY" block in evaluate_bar() for the
# ablation that retired it. History of the mistake, kept deliberately:
# it was set to 50 by borrowing a constant from a filter with a different
# job, recalibrated to 35 against 257 reversals, and only a proper ablation
# on the FULL 1,517-signal pool revealed it adds ~nothing either way. The
# earlier calibration was measuring the wrong thing: how many real reversals
# it caught, never whether catching them that way made money.
# THRESHOLD (historical, no longer enforced) — was 50, then 35.
# 50 was borrowed from reversal_guard's 'skip' tier, which exists to BLOCK
# breakout entries. That was a guess, and it was wrong for ENTERING reversals.
# Measured against 257 genuine intraday reversals (a turn followed by a >=0.25%
# sustained move) across ~40 days x 3 instruments, scored at the turn bar in
# the direction anticipation would fade, with 339 non-turn bars as a control:
#
#   thresh | catches real reversals | fires on non-turns (false pos)
#      50  |        19.5%           |   8.3%     <- old: blocked 4 of 5 real turns
#      40  |        36.6%           |  13.9%
#      35  |        46.7%           |  18.9%     <- CHOSEN
#      30  |        61.1%           |  26.8%
#      20  |        82.9%           |  49.0%     <- no longer a signal, ~coin flip
#
# 35 more than doubles real-reversal capture vs 50 while holding false
# positives under 19% and preserving ~2.5:1 discrimination (turn mean 35.0 /
# non-turn mean 22.3). Below 30 the ratio collapses toward noise. Evidence for
# 50 being too strict was also live: Jul 27-30 produced 4 straight zero-trade
# days with every candidate scoring 0-21.
# NOTE: this loosens an ENTRY filter, but anticipation_scout remains SHADOW —
# no real orders. It buys us a real fill rate so the shadow can finally produce
# the evidence needed to judge the engine at all.
EXHAUSTION_MIN_SCORE = 35

# ── Per-instrument shadow state (each bot = its own process) ─────────────────
_open:    dict[str, Optional[dict]] = {}
_count:   dict[str, int]            = {}
_pnl:     dict[str, float]          = {}
_last_ts: dict[str, object]         = {}
# Separate counter for LIVE signals — the shadow tracker and the live path
# must not share a per-day budget, or one would starve the other.
_live_count: dict[str, int]         = {}


def daily_reset(instrument: str, logger: logging.Logger | None = None) -> None:
    prev = _pnl.get(instrument, 0.0)
    n    = _count.get(instrument, 0)
    if logger and n:
        logger.info(f"  [ANTICIP] {instrument}: yesterday {n} shadow setup(s), "
                    f"P&L=Rs{prev:+,.0f}")
    _open[instrument]  = None
    _count[instrument] = 0
    _pnl[instrument]   = 0.0
    _live_count[instrument] = 0


def _levels(instrument, price, or_high, or_low, oi_zones, pdh, pdl):
    """Return (supports_below, resistances_above) — sorted by proximity to price."""
    sup, res = [], []
    def add(container, val, name):
        try:
            v = float(val)
            if v > 0:
                container.append((v, name))
        except (TypeError, ValueError):
            pass
    # structural levels.
    # VWAP REMOVED as an entry level (Jul 30 2026) — measured on 92 signals
    # over 60 days: VWAP entries won 37.5% and lost money on BOTH sides
    # (CALL 33.3% / -0.008%, PUT 40.0% / -0.066%), while OR boundaries won
    # 63.5% (+0.107%). Mechanism: an OR boundary is FIXED at 09:40 and
    # accumulates real order-flow memory all day; VWAP is a MOVING line that
    # drifts toward price, so "price holding VWAP" is frequently just price
    # and VWAP converging — a coincidence read as support. VWAP stays in use
    # elsewhere (exit scoring, reversal_guard stretch, confirmatory filters);
    # it is only unfit as an anticipation ENTRY level.
    if or_low and price > or_low:   add(sup, or_low,  'OR_low')
    if or_high and price < or_high: add(res, or_high, 'OR_high')
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


def _detect_setup(instrument, df, oi_zones, now, or_high=None, or_low=None):
    """Shared setup detection for BOTH the shadow tracker and the live signal.

    Single source of truth on purpose: this codebase has been bitten before by
    duplicated logic drifting apart. Returns (setup | None, meta) where meta
    carries chase / adx / exhaustion / price for the caller to log or gate on.
    """
    if df is None or len(df) < TOUCH_BARS + 1:
        return None, {}
    row   = df.iloc[-1]
    price = float(row['Close'])
    pdh   = float(row.get('PDH', 0) or 0)
    pdl   = float(row.get('PDL', 0) or 0)
    dip   = float(row.get('DI_plus', 0) or 0)
    dim   = float(row.get('DI_minus', 0) or 0)
    adx   = float(row.get('ADX', 0) or 0)
    prev_close = float(df['Close'].iloc[-2])
    win_lo = float(df['Low'].iloc[-TOUCH_BARS:].min())
    win_hi = float(df['High'].iloc[-TOUCH_BARS:].max())

    sup, res = _levels(instrument, price, or_high, or_low, oi_zones, pdh, pdl)

    setup = None
    if sup:
        lvl, name = sup[0]
        if ((price - lvl) / price <= PROX_PCT
                and (win_lo - lvl) / lvl <= TOUCH_PCT
                and win_lo >= lvl * (1 - STOP_BEYOND)
                and price > lvl and price >= prev_close
                and dip >= dim - DI_MARGIN):
            stop  = lvl * (1 - STOP_BEYOND)
            setup = dict(dir='CALL', level=lvl, level_name=name, stop=stop,
                         target=price + RR * (price - stop))
    if setup is None and res:
        lvl, name = res[0]
        if ((lvl - price) / price <= PROX_PCT
                and (lvl - win_hi) / lvl <= TOUCH_PCT
                and win_hi <= lvl * (1 + STOP_BEYOND)
                and price < lvl and price <= prev_close
                and dim >= dip - DI_MARGIN):
            stop  = lvl * (1 + STOP_BEYOND)
            setup = dict(dir='PUT', level=lvl, level_name=name, stop=stop,
                         target=price - RR * (stop - price))
    if setup is None:
        return None, {}

    day = df[df.index.date == now.date()]
    d_hi, d_lo = float(day['High'].max()), float(day['Low'].min())
    rpos  = (price - d_lo) / (d_hi - d_lo) if d_hi > d_lo else 0.5
    chase = rpos if setup['dir'] == 'CALL' else 1.0 - rpos
    opp   = 'PUT' if setup['dir'] == 'CALL' else 'CALL'
    try:
        exh = compute_reversal_risk(df, len(df) - 1, opp)['score']
    except Exception:
        exh = None
    return setup, dict(chase=chase, adx=adx, exhaustion=exh, price=price)


def get_live_signal(instrument, df, oi_zones, logger, now,
                    or_high=None, or_low=None):
    """LIVE entry signal for options_bot's pipeline — returns a signal dict or None.

    Applies ONLY this engine's own entry conditions (time window, per-day cap,
    chase guard). Everything downstream is deliberately left to options_bot's
    existing, proven machinery: risk-cap sizing ladder, pre-order funds check,
    regime/quality lot caps, the live chase gate, exchange SL-M placement,
    trailing stop, never-progressed exit, checkpoint logic and JSONL logging.
    No parallel order path is created.

    NOTE ON EXITS: the shadow tracker simulates a level-based stop and a 2:1
    target on the UNDERLYING. A live trade instead inherits the standard option
    exit stack (25% premium stop, IV-scaled target, trailing, never-progressed).
    Those are not equivalent -- the option stop is generally the wider of the
    two -- so live results will diverge from the shadow P&L. Compare with that
    in mind; do not expect the shadow numbers to reproduce.
    """
    if not (WINDOW_START <= now.time() <= WINDOW_END):
        return None
    if _live_count.get(instrument, 0) >= MAX_PER_DAY:
        return None

    setup, meta = _detect_setup(instrument, df, oi_zones, now, or_high, or_low)
    if setup is None:
        return None

    if meta['chase'] > MAX_CHASE:
        logger.info(
            f"  [ANTICIP-LIVE] skip {setup['dir']} {instrument} "
            f"{setup['level_name']}@{setup['level']:.0f}: chase_pos="
            f"{meta['chase']:.2f} > {MAX_CHASE} — not anticipatory"
        )
        return None

    _miro = _read_mirofish(instrument)
    _mnote = 'n/a'
    if _miro:
        bull = setup['dir'] == 'CALL'
        if (bull and _miro['lean'] == 'bullish') or (not bull and _miro['lean'] == 'bearish'):
            _mnote = f"AGREES({_miro['lean']} {_miro['score']:.2f})"
        elif (bull and _miro['lean'] == 'bearish') or (not bull and _miro['lean'] == 'bullish'):
            _mnote = f"CONFLICTS({_miro['lean']} {_miro['score']:.2f})"
        else:
            _mnote = f"neutral({_miro['score']:.2f})"

    _live_count[instrument] = _live_count.get(instrument, 0) + 1
    logger.info(
        f"  [ANTICIP-LIVE] ⚡ SIGNAL {setup['dir']} {instrument} | "
        f"hold {setup['level_name']}@{setup['level']:.0f} | idx {meta['price']:.0f} | "
        f"ADX={meta['adx']:.0f} chase={meta['chase']:.2f} "
        f"exh={meta['exhaustion']} miro={_mnote} | "
        f"level-stop {setup['stop']:.0f} (reference only — live uses option stop)"
    )
    return {
        'type'      : setup['dir'],
        'price'     : meta['price'],
        'adx'       : meta['adx'],
        'strength'  : 1,
        'lots'      : 1,          # conservative: never request >1 on a new engine
        'path'      : 'ANTICIP',
        'chase_pos' : round(meta['chase'], 3),
        'anticip_level'      : setup['level'],
        'anticip_level_name' : setup['level_name'],
        'anticip_exhaustion' : meta['exhaustion'],
        'anticip_miro'       : _mnote,
    }


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
                f"~Rs{pnl:+,.0f} | held {int((now - pos['t0']).total_seconds()/60)}m | "
                f"[entry: chase={pos.get('chase_pos','?')} "
                f"exhaustion={pos.get('exhaustion_score','?')}/100 miro={pos.get('miro','?')}]"
            )
            _open[instrument] = None
        return

    # ── look for a new setup ─────────────────────────────────────────────────
    if not (WINDOW_START <= now.time() <= WINDOW_END):
        return
    if _count.get(instrument, 0) >= MAX_PER_DAY:
        return

    dip  = float(row.get('DI_plus', 0) or 0)
    dim  = float(row.get('DI_minus', 0) or 0)
    adx  = float(row.get('ADX', 0) or 0)
    prev_close = float(df['Close'].iloc[-2])
    win_lo = float(df['Low'].iloc[-TOUCH_BARS:].min())
    win_hi = float(df['High'].iloc[-TOUCH_BARS:].max())

    sup, res = _levels(instrument, price, or_high, or_low, oi_zones, pdh, pdl)

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

    # Anticipation guard: reject setups that are already at the extreme — those
    # aren't anticipatory, they're chases wearing a level's name.
    if chase > MAX_CHASE:
        logger.info(
            f"  [ANTICIP-SHADOW] skip {setup['dir']} {instrument} "
            f"{setup['level_name']}@{setup['level']:.0f}: chase_pos={chase:.2f} "
            f"> {MAX_CHASE} — not anticipatory (already at the extreme)"
        )
        return

    # ── Exhaustion score — LOGGED ONLY, no longer a gate (Jul 30 2026) ──────
    # It was a gate (>=50, then >=35). Ablation on 1,517 unfiltered signals
    # showed it contributes ~nothing as a filter (+Rs3/trade vs baseline,
    # 51.6% win vs 53.6% baseline) while destroying volume: stacked with
    # chase+DI it cut trades 419 -> 46 and TOTAL P&L from ~Rs322k to ~Rs55k.
    #
    # Root cause of the design error: compute_reversal_risk() awards points
    # for DECLINING ADX, and ADX can only decline from a high level -- so the
    # gate systematically selected strong-trend conditions, the worst regime
    # in which to fade a move. Median ADX at our entries was 42 (55% above 40).
    # Kept as a logged field because it is still useful post-hoc evidence.
    _opp = 'PUT' if setup['dir'] == 'CALL' else 'CALL'
    try:
        _rev_score = compute_reversal_risk(df, len(df) - 1, _opp)['score']
    except Exception:
        _rev_score = None   # non-fatal: it no longer gates anything

    # ── MiroFish — directional CONTEXT only, never a gate (user directive) ──
    _miro = _read_mirofish(instrument)
    _miro_note = 'n/a'
    if _miro:
        _dir_bullish = (setup['dir'] == 'CALL')
        _lean_bullish = _miro['lean'] == 'bullish'
        _lean_bearish = _miro['lean'] == 'bearish'
        if (_dir_bullish and _lean_bullish) or (not _dir_bullish and _lean_bearish):
            _miro_note = f"AGREES (lean={_miro['lean']} score={_miro['score']:.2f}, {_miro['age_h']}h old)"
        elif (_dir_bullish and _lean_bearish) or (not _dir_bullish and _lean_bullish):
            _miro_note = f"CONFLICTS (lean={_miro['lean']} score={_miro['score']:.2f}, {_miro['age_h']}h old)"
        else:
            _miro_note = f"neutral (score={_miro['score']:.2f}, {_miro['age_h']}h old)"

    setup.update(entry=price, t0=now, chase_pos=round(chase, 2),
                 exhaustion_score=_rev_score, miro=_miro_note)
    _open[instrument]  = setup
    _count[instrument] = _count.get(instrument, 0) + 1
    logger.info(
        f"  [ANTICIP-SHADOW] ⚡ ENTRY {setup['dir']} {instrument} | "
        f"hold {setup['level_name']}@{setup['level']:.0f} | idx {price:.0f} | "
        f"stop {setup['stop']:.0f} target {setup['target']:.0f} | "
        f"ADX={adx:.0f} chase_pos={chase:.2f} exhaustion={_rev_score}/100 "
        f"miro={_miro_note} | anticipatory entry (before breakout)"
    )
