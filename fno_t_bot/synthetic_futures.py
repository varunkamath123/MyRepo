# -*- coding: utf-8 -*-
"""
PATH_SYNFUT — deep-ITM trend following, run as a synthetic intraday future
==========================================================================

Spec (user, 2026-09-01)
-----------------------
  * entry decided by OI levels + PCR + ADX, all three required
  * trend continuation only, no reversal/fade logic
  * buy DEEP ITM so the position behaves like an intraday future, not an option

Runs as an isolated paper book alongside the live strategy. It never touches the
live position, places no orders, and cannot raise into the live path.

Why the exits are in INDEX POINTS, not premium %
------------------------------------------------
This is the design consequence of the deep-ITM instruction, and it is not
optional. A deep-ITM contract is mostly intrinsic value with delta ~0.85, so
premium moves roughly 1:1 with the index -- that is the entire point of choosing
it. But the live exit stack is calibrated for ATM contracts: 25% stop, 55%
target, trail arming at 12%. On a delta-0.85 contract priced near intrinsic, a
25% premium loss needs an index move several times larger than a normal
session's range, and the 55% target would essentially never fire. Transplanting
those percentages would produce a position with no working stop.

So exits are ATR-scaled index levels. ATR self-scales across NIFTY / BANKNIFTY /
SENSEX, which avoids inventing three sets of hand-picked point thresholds --
i.e. avoids adding to the ~3,072 variant search that Deflated Sharpe already
showed was worthless.

Parameters are PRE-REGISTERED and frozen. They were chosen on structural
grounds (1 ATR = one bar's typical range; 2:1 reward:risk), NOT by sweeping.
Any later tuning must be recorded as a new trial for DSR purposes.

Known risk, stated up front
---------------------------
Deep-ITM strikes are less liquid than ATM. Wide spreads could make real fills
materially worse than the quoted LTP this book records. Entry LTP, and the
implied delta actually achieved, are logged on every trade so that cost can be
measured rather than assumed.
"""
from __future__ import annotations

import json
import os
from datetime import time as dtime

import config

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')

# ── pre-registered parameters (frozen 2026-09-01) ───────────────────────────
SYNFUT_START        = '09:45'
SYNFUT_END          = '13:00'   # leave >=90 min runway to force-close
SYNFUT_ADX_MIN      = 25.0      # trend must exist
SYNFUT_ADX_RISE     = 1.5       # ...and be building, over N bars
SYNFUT_ADX_BARS     = 3
SYNFUT_DI_SPREAD    = 12.0      # directional conviction
SYNFUT_PCR_CALL_MAX = 0.95      # call-heavy book supports a CALL
SYNFUT_PCR_PUT_MIN  = 1.05      # put-heavy book supports a PUT
SYNFUT_ITM_PCT      = 0.012     # ~1.2% in the money -> delta ~0.80-0.90
SYNFUT_STOP_ATR     = 1.0       # index-point stop
SYNFUT_TARGET_ATR   = 2.0       # 2:1 reward:risk
SYNFUT_TRAIL_ACT    = 1.5       # arm trail after 1.5 ATR in favour
SYNFUT_TRAIL_ATR    = 0.75      # give back 0.75 ATR from the peak
SYNFUT_CAPITAL      = 26_000.0

_book: dict = {}


def _b(inst: str) -> dict:
    if inst not in _book:
        _book[inst] = dict(position=None, day=None, trades=0,
                           capital=SYNFUT_CAPITAL, fired=False)
    return _book[inst]


def _write(inst: str, rec: dict) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    p = os.path.join(LOG_DIR, f'synfut_{inst}_{rec["entry_time"][:10]}.jsonl')
    try:
        with open(p, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec) + '\n')
    except Exception:
        pass


def _signal(df, oc, oi_zones, now, logger, inst):
    """All three inputs must agree: ADX (trend), PCR (positioning), OI (room)."""
    if not (dtime(*map(int, SYNFUT_START.split(':')))
            <= now.time() <= dtime(*map(int, SYNFUT_END.split(':')))):
        return None
    if len(df) < SYNFUT_ADX_BARS + 2:
        return None

    row = df.iloc[-1]
    adx = float(row.get('ADX', 0) or 0)
    dip = float(row.get('DI_plus', 0) or 0)
    dim = float(row.get('DI_minus', 0) or 0)
    atr = float(row.get('ATR', 0) or 0)
    px  = float(row['Close'])
    if atr <= 0:
        return None

    # ── 1. ADX: trend exists and is building ────────────────────────────────
    adx_prev = float(df['ADX'].iloc[-(SYNFUT_ADX_BARS + 1)] or 0)
    if adx < SYNFUT_ADX_MIN or (adx - adx_prev) < SYNFUT_ADX_RISE:
        return None
    spread = abs(dip - dim)
    if spread < SYNFUT_DI_SPREAD:
        return None
    direction = 'CALL' if dip > dim else 'PUT'

    # ── 2. PCR: the options book must agree with the direction ──────────────
    pcr = (oc or {}).get('pcr')
    if pcr is None:
        return None                      # PCR is REQUIRED by spec, not optional
    if direction == 'CALL' and pcr > SYNFUT_PCR_CALL_MAX:
        return None
    if direction == 'PUT' and pcr < SYNFUT_PCR_PUT_MIN:
        return None

    # ── 3. OI levels: is there room to the next opposing wall? ──────────────
    zone_action = None
    try:
        from oi_zones import get_zone_signal
        if oi_zones:
            z = get_zone_signal(px, direction, oi_zones,
                                dte=getattr(config, 'DAYS_TO_EXPIRY', 3))
            zone_action = z.get('action')
            if zone_action in ('SKIP', 'REDUCE'):
                return None              # pinned against a wall -> no room
    except Exception:
        pass
    if zone_action is None:
        # No OI zones (e.g. SENSEX before the EOD fetch) -> fall back to MaxPain
        mp = (oc or {}).get('max_pain')
        if not mp:
            return None                  # spec requires OI input; without it, no trade
        if direction == 'CALL' and px >= mp:
            return None                  # already above the magnet, little room up
        if direction == 'PUT' and px <= mp:
            return None
        zone_action = 'MAXPAIN'

    return dict(type=direction, price=px, adx=adx, atr=atr, pcr=pcr,
                di_spread=spread, adx_prev=adx_prev, zone=zone_action)


def _deep_itm_strike(px: float, direction: str, gap: int) -> int:
    """Strike ~SYNFUT_ITM_PCT in the money -> delta ~0.80-0.90."""
    offset = max(round((px * SYNFUT_ITM_PCT) / gap), 1) * gap
    return int(round((px - offset if direction == 'CALL' else px + offset) / gap) * gap)


def _exit_reason(pos, index_px, now):
    """Index-point exits. The option is only the vehicle."""
    atr = pos['atr']
    move = (index_px - pos['entry_index']) if pos['type'] == 'CALL' \
        else (pos['entry_index'] - index_px)
    pos['peak_move'] = max(pos.get('peak_move', 0.0), move)

    if move <= -SYNFUT_STOP_ATR * atr:
        return f'Stop ({SYNFUT_STOP_ATR:.1f}xATR = {SYNFUT_STOP_ATR*atr:.0f}pts)'
    if move >= SYNFUT_TARGET_ATR * atr:
        return f'Target ({SYNFUT_TARGET_ATR:.1f}xATR = {SYNFUT_TARGET_ATR*atr:.0f}pts)'
    if (pos['peak_move'] >= SYNFUT_TRAIL_ACT * atr
            and move < pos['peak_move'] - SYNFUT_TRAIL_ATR * atr):
        return f'Trail (peak {pos["peak_move"]:.0f}pts, gave {SYNFUT_TRAIL_ATR:.2f}xATR)'
    if now.strftime('%H:%M') >= getattr(config, 'FORCE_CLOSE_TIME', '14:30'):
        return f"EOD Force-Close ({getattr(config, 'FORCE_CLOSE_TIME', '14:30')})"
    return None


def evaluate_bar(bot, instrument, df, oc, now, logger=None):
    """Call once per scan tick. Never raises into the caller."""
    if not getattr(config, 'SYNFUT_ENABLED', False):
        return
    try:
        from fyers_orders import build_option_symbol, get_next_expiry, get_ltp
    except Exception:
        return

    try:
        b = _b(instrument)
        day = now.strftime('%Y-%m-%d')
        if b['day'] != day:
            b.update(day=day, fired=False, position=None)

        index_px = float(df['Close'].iloc[-1])
        pos = b['position']

        # ── manage an open position ─────────────────────────────────────────
        if pos:
            why = _exit_reason(pos, index_px, now)
            if not why:
                return
            ltp = get_ltp(bot.fyers, pos['symbol']) if bot.fyers else None
            exit_px = float(ltp) if ltp else pos['entry_price']
            net = (exit_px - pos['entry_price']) * pos['lot'] - 70.0
            b['capital'] += net
            b['trades'] += 1
            b['position'] = None
            realised = ((exit_px - pos['entry_price']) /
                        max(index_px - pos['entry_index'], 1e-9)) \
                if pos['type'] == 'CALL' else \
                       ((exit_px - pos['entry_price']) /
                        max(pos['entry_index'] - index_px, 1e-9))
            _write(instrument, dict(
                strategy='synfut', instrument=instrument, type=pos['type'],
                strike=pos['strike'], symbol=pos['symbol'],
                entry_time=pos['entry_time'].isoformat(),
                exit_time=now.isoformat(),
                entry_index=round(pos['entry_index'], 2),
                exit_index=round(index_px, 2),
                index_move=round(index_px - pos['entry_index'], 2),
                entry_price=round(pos['entry_price'], 2),
                exit_price=round(exit_px, 2), lot_size=pos['lot'],
                atr_at_entry=round(pos['atr'], 2),
                peak_move_pts=round(pos.get('peak_move', 0), 1),
                pnl_pct=round((exit_px - pos['entry_price']) /
                              pos['entry_price'] * 100, 2),
                pnl_net=round(net, 2), exit_reason=why,
                adx=round(pos['adx'], 1), pcr=pos['pcr'], zone=pos['zone'],
                itm_pts=round(pos['itm_pts'], 1),
                realised_delta=round(realised, 3),
                capital=round(b['capital'], 2)))
            if logger:
                logger.info(f"  [SYNFUT] {instrument} EXIT {pos['type']} {why} | "
                            f"idx {pos['entry_index']:.0f}->{index_px:.0f} | "
                            f"Rs{net:+,.0f} | book Rs{b['capital']:,.0f}")
            return

        # ── look for a new entry ────────────────────────────────────────────
        if b['fired'] or now.strftime('%H:%M') >= getattr(
                config, 'FORCE_CLOSE_TIME', '14:30'):
            return

        sig = _signal(df, oc, getattr(bot, '_oi_zones', None), now, logger, instrument)
        if not sig:
            return

        gap = int(bot.inst_cfg.get('strike_gap', 50))
        strike = _deep_itm_strike(sig['price'], sig['type'], gap)
        sym = build_option_symbol(instrument, strike, sig['type'],
                                  get_next_expiry(instrument))
        ltp = get_ltp(bot.fyers, sym) if bot.fyers else None
        if not ltp or float(ltp) <= 0:
            if logger:
                logger.info(f"  [SYNFUT] {instrument}: no quote for deep-ITM "
                            f"{sym} — skipped (liquidity)")
            return

        itm_pts = abs(sig['price'] - strike)
        intrinsic = itm_pts
        time_value = float(ltp) - intrinsic
        b['position'] = dict(
            symbol=sym, strike=strike, type=sig['type'], entry_price=float(ltp),
            entry_index=sig['price'], entry_time=now, atr=sig['atr'],
            peak_move=0.0, lot=int(bot.inst_cfg.get('lot_size', 1)),
            adx=sig['adx'], pcr=sig['pcr'], zone=sig['zone'], itm_pts=itm_pts)
        b['fired'] = True
        if logger:
            logger.info(
                f"  [SYNFUT] {instrument} ENTRY {sig['type']} {strike} "
                f"(deep ITM {itm_pts:.0f}pts) @ Rs{float(ltp):.2f} "
                f"| intrinsic {intrinsic:.0f} + time {time_value:.1f} "
                f"| ADX {sig['adx_prev']:.1f}->{sig['adx']:.1f} "
                f"DI-spread {sig['di_spread']:.1f} PCR {sig['pcr']:.2f} "
                f"zone={sig['zone']} | stop {SYNFUT_STOP_ATR*sig['atr']:.0f}pts "
                f"target {SYNFUT_TARGET_ATR*sig['atr']:.0f}pts")
    except Exception as e:
        if logger:
            logger.debug(f"  [SYNFUT] {instrument} error: {e}")
