# -*- coding: utf-8 -*-
"""BANKNIFTY breakout strategy: pure decision logic.

THIS FILE DOES NO I/O. No network, no files, no clock. Every function takes its
inputs as arguments and every parameter comes from bnf_config. Two consequences:

  * the live runner and the historical replay execute the SAME decisions, so a
    replay result is evidence about live behaviour rather than about a copy;
  * a bad result can be attributed. Change bnf_config.py and the strategy is
    unchanged; change this file and the config is unchanged.

THE THESIS. Price coils against a key level (previous-day high/low, opening
range, day high/low) on above-normal participation, in a volatile regime. The
break is anticipated in the direction of the level, BEFORE it happens.

THE EXITS. Three independent reasons to leave, because "the price hit a number"
and "the idea is wrong" are different events:

  risk       the premium falls HARD_STOP_PREMIUM_PCT, or reaches the target;
  thesis     a close back through the coil (broken), or the break fails to carry
             far enough within a set number of bars (stale);
  protection a trailing exit scaled to volatility (ATR), checked on closes, plus
             a forced close at the end of the day.

HONEST STATUS. Pre-registered research on 420 sessions (research/) found that no
setup of this family beats a random entry after costs. See bnf_config.py.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

import bnf_config as cfg_default

LEVEL_NAMES = ('PDH', 'PDL', 'ORH', 'ORL', 'DAYH', 'DAYL')


# ── small helpers ───────────────────────────────────────────────────────────
def _hhmm_add(hhmm: str, minutes: int) -> str:
    h, m = int(hhmm[:2]), int(hhmm[3:5])
    t = h * 60 + m + minutes
    return f'{t // 60:02d}:{t % 60:02d}'


def bar_close_time(open_hhmm: str, cfg=cfg_default) -> str:
    """A bar stamped with its OPEN time closes BAR_MINUTES later."""
    return _hhmm_add(open_hhmm, cfg.BAR_MINUTES)


def atr(H, L, i: int, n: int = 14) -> float:
    """Mean high-low range of the last n bars ending at i (needs i >= n-1)."""
    lo = max(0, i - n + 1)
    return float(np.mean(np.asarray(H[lo:i + 1], float) - np.asarray(L[lo:i + 1], float)))


def compression_ratio(H, L, i: int, coil_bars: int):
    """range of the last coil_bars over what recent per-bar ranges predict for
    that many bars. Below 1 is tighter than usual. None until enough history."""
    if i < 15 or i + 1 < coil_bars:
        return None
    a = atr(H, L, i, 14)
    if a <= 0:
        return None
    rng = float(np.max(H[i - coil_bars + 1:i + 1]) - np.min(L[i - coil_bars + 1:i + 1]))
    return rng / (a * math.sqrt(coil_bars))


# ── context built from PRIOR sessions only ──────────────────────────────────
def session_rv(C) -> float:
    r = np.diff(np.log(np.asarray(C, float)))
    return float(np.std(r)) if len(r) > 1 else 0.0


def hot_regime(prev_rvs: list, cfg=cfg_default):
    """Was the most recent session's realised vol above the median of the window
    before it? None when there is not enough history to say."""
    if len(prev_rvs) < 21:
        return None
    window = prev_rvs[max(0, len(prev_rvs) - 1 - cfg.HOT_REGIME_WINDOW):-1]
    if len(window) < 20:
        return None
    return bool(prev_rvs[-1] > float(np.median(window)))


def volume_baseline(prev_sessions: list, n_bars: int, cfg=cfg_default) -> np.ndarray:
    """Median volume at each bar-of-day over the previous N sessions that carry
    volume. NaN where there is not enough history."""
    hist = [np.asarray(s['V'], float) for s in prev_sessions[-cfg.VOL_BASELINE_SESSIONS:]
            if float(np.sum(s['V'])) > 0]
    base = np.full(n_bars, np.nan)
    if len(hist) < 10:
        return base
    for b in range(n_bars):
        xs = [h[b] for h in hist if len(h) > b and h[b] > 0]
        if len(xs) >= 8:
            base[b] = float(np.median(xs))
    return base


def build_day_context(prev_sessions: list, n_bars: int = 75, cfg=cfg_default) -> dict:
    """Everything the day needs from history. prev_sessions are dicts with
    H, L, C, V arrays, oldest first, NOT including today."""
    if not prev_sessions:
        return dict(pdh=None, pdl=None, hot=None, vbase=np.full(n_bars, np.nan))
    last = prev_sessions[-1]
    rvs = [session_rv(s['C']) for s in prev_sessions]
    return dict(pdh=float(np.max(last['H'])), pdl=float(np.min(last['L'])),
                hot=hot_regime(rvs, cfg), vbase=volume_baseline(prev_sessions, n_bars, cfg))


# ── key levels ──────────────────────────────────────────────────────────────
def key_levels(day: dict, H, L, i: int, cfg=cfg_default) -> dict:
    out = {}
    if 'PDH' in cfg.LEVELS and day.get('pdh') is not None:
        out['PDH'] = float(day['pdh'])
    if 'PDL' in cfg.LEVELS and day.get('pdl') is not None:
        out['PDL'] = float(day['pdl'])
    if i >= 5:
        if 'ORH' in cfg.LEVELS:
            out['ORH'] = float(np.max(H[:6]))
        if 'ORL' in cfg.LEVELS:
            out['ORL'] = float(np.min(L[:6]))
    if 'DAYH' in cfg.LEVELS:
        out['DAYH'] = float(np.max(H[:i + 1]))
    if 'DAYL' in cfg.LEVELS:
        out['DAYL'] = float(np.min(L[:i + 1]))
    return out


def nearest_levels(levels: dict, px: float):
    """(nearest level above price, name), (nearest below, name). A level EQUAL to
    price counts as neither, so a close exactly on the day high does not 'press'
    the day high."""
    above = [(v, k) for k, v in levels.items() if v > px]
    below = [(v, k) for k, v in levels.items() if v < px]
    return (min(above) if above else (None, None)), (max(below) if below else (None, None))


# ── entry ───────────────────────────────────────────────────────────────────
@dataclass
class EntryDecision:
    enter: bool
    direction: int = 0                       # +1 CALL, -1 PUT
    thesis: dict = field(default_factory=dict)
    gates: dict = field(default_factory=dict)  # name -> (passed, detail)

    def blocked_by(self) -> list:
        return [k for k, (ok, _) in self.gates.items() if not ok]


def evaluate_entry(bars: dict, i: int, day: dict, state: dict, cfg=cfg_default) -> EntryDecision:
    """Decide whether to open a position at the CLOSE of bar i.

    bars:  H, L, C, V arrays and T (list of 'HH:MM' bar-open times), all CLOSED
           bars of today, length >= i+1.
    state: trades_today, open_positions, last_loss_bar (or None).

    Every gate is evaluated, none short-circuits, so a run of 'no trade' can be
    attributed to the gate that is actually doing the blocking. That is what makes
    a config problem distinguishable from a strategy problem.
    """
    H, L, C, V, T = bars['H'], bars['L'], bars['C'], bars['V'], bars['T']
    px = float(C[i])
    g = {}

    ct = bar_close_time(T[i], cfg)
    g['window'] = (cfg.ENTRY_START <= ct <= cfg.ENTRY_END, ct)

    cap_ok = (state.get('trades_today', 0) < cfg.MAX_TRADES_PER_DAY
              and state.get('open_positions', 0) < cfg.MAX_CONCURRENT_POSITIONS)
    lb = state.get('last_loss_bar')
    cool_ok = lb is None or (i - lb) > cfg.COOLDOWN_BARS_AFTER_LOSS
    g['capacity'] = (cap_ok and cool_ok,
                     f"trades={state.get('trades_today', 0)} open={state.get('open_positions', 0)}"
                     f" cooldown={'ok' if cool_ok else 'active'}")

    hot = day.get('hot')
    g['regime'] = ((hot is True) if cfg.REQUIRE_HOT_REGIME else True,
                   'hot' if hot else ('cold' if hot is False else 'unknown'))

    levels = key_levels(day, H, L, i, cfg)
    (up, up_n), (dn, dn_n) = nearest_levels(levels, px)
    du = (up - px) / px * 100 if up else 9e9
    dd = (px - dn) / px * 100 if dn else 9e9
    near = min(du, dd)
    toward = 1 if du <= dd else -1
    g['level'] = (near <= cfg.LEVEL_PROX_PCT,
                  f"{up_n or '-'}@{du:.3f}% / {dn_n or '-'}@{dd:.3f}%")
    lvl_name, lvl_val = (up_n, up) if toward > 0 else (dn_n, dn)

    cr = compression_ratio(H, L, i, cfg.COIL_BARS)
    g['coil'] = (cr is not None and cr <= cfg.COIL_RATIO_MAX,
                 'n/a' if cr is None else f'{cr:.3f}<={cfg.COIL_RATIO_MAX}')

    if cfg.VOLUME_MODE == 'off':
        g['volume'] = (True, 'off')
    else:
        vb = np.asarray(day['vbase'][max(0, i - 2):i + 1], float)
        vv = np.asarray(V[max(0, i - 2):i + 1], float)
        if (len(vb) < 3 or not np.all(np.isfinite(vb)) or np.any(vb <= 0)
                or float(np.sum(V)) <= 0):
            g['volume'] = (False, 'no volume data')
        else:
            rel = float(np.mean(vv / vb))
            ok = rel >= cfg.VOL_REL_MIN if cfg.VOLUME_MODE == 'participate' else rel <= cfg.VOL_REL_MAX_DRYUP
            g['volume'] = (ok, f'{rel:.2f}x')

    ok_all = all(ok for ok, _ in g.values())
    if not ok_all:
        return EntryDecision(False, 0, {}, g)

    direction = toward if cfg.DIRECTION == 'toward' else -toward
    lo = i - cfg.COIL_BARS + 1
    coil_hi, coil_lo = float(np.max(H[lo:i + 1])), float(np.min(L[lo:i + 1]))
    buf = cfg.THESIS_BREAK_BUFFER_PCT / 100
    if cfg.DIRECTION == 'toward':
        inv = coil_lo * (1 - buf) if direction > 0 else coil_hi * (1 + buf)
    else:                                   # fading: the level must HOLD
        inv = lvl_val * (1 - buf) if direction > 0 else lvl_val * (1 + buf)
    thesis = dict(level=lvl_name, level_value=lvl_val, direction=direction,
                  coil_hi=coil_hi, coil_lo=coil_lo, invalidation=float(inv),
                  atr=atr(H, L, i, 14), entry_bar=i, entry_px=px,
                  mode=cfg.DIRECTION)
    return EntryDecision(True, direction, thesis, g)


# ── exits ───────────────────────────────────────────────────────────────────
def stop_index_pct(cfg=cfg_default) -> float:
    return cfg.HARD_STOP_PREMIUM_PCT * 100 / cfg.PREMIUM_PCT_PER_INDEX_PCT


def target_index_pct(cfg=cfg_default) -> float:
    return cfg.TARGET_PREMIUM_PCT * 100 / cfg.PREMIUM_PCT_PER_INDEX_PCT


def new_position(decision: EntryDecision, premium: float, lots: int) -> dict:
    t = decision.thesis
    return dict(dir=decision.direction, entry_bar=t['entry_bar'], entry_px=t['entry_px'],
                entry_premium=float(premium), lots=int(lots), thesis=dict(t),
                best_fav=0.0, worst_adv=0.0, best_px=t['entry_px'], confirmed=False)


def check_premium(pos: dict, premium_pct: float, cfg=cfg_default):
    """Live premium poll (between bars). Hard risk limits only."""
    if premium_pct <= -cfg.HARD_STOP_PREMIUM_PCT:
        return dict(kind='stop', reason=f'premium {premium_pct * 100:+.1f}% <= -{cfg.HARD_STOP_PREMIUM_PCT * 100:.0f}%')
    if premium_pct >= cfg.TARGET_PREMIUM_PCT:
        return dict(kind='target', reason=f'premium {premium_pct * 100:+.1f}% >= +{cfg.TARGET_PREMIUM_PCT * 100:.0f}%')
    return None


def update_excursions(pos: dict, h: float, l: float) -> None:
    e, d = pos['entry_px'], pos['dir']
    fav = (h - e) / e * 100 if d > 0 else (e - l) / e * 100
    adv = (e - l) / e * 100 if d > 0 else (h - e) / e * 100
    if fav > pos['best_fav']:
        pos['best_fav'] = fav
        pos['best_px'] = h if d > 0 else l
    pos['worst_adv'] = max(pos['worst_adv'], adv)


def on_bar_exit(pos: dict, bars: dict, i: int, cfg=cfg_default, replay: bool = False):
    """Evaluate a CLOSED bar against an open position. Returns a decision dict or
    None, and mutates pos with the new excursions.

    replay=True additionally applies the hard stop/target from the bar's high and
    low, converted through PREMIUM_PCT_PER_INDEX_PCT, because a replay has no real
    premium to poll. Live passes replay=False: the poll already covers them.
    Within one bar the STOP is assumed to fill first -- the conservative choice.
    """
    H, L, C, T = bars['H'], bars['L'], bars['C'], bars['T']
    e, d = pos['entry_px'], pos['dir']
    h, l, c = float(H[i]), float(L[i]), float(C[i])
    fav_i = (h - e) / e * 100 if d > 0 else (e - l) / e * 100
    adv_i = (e - l) / e * 100 if d > 0 else (h - e) / e * 100

    if replay:
        if adv_i >= stop_index_pct(cfg):
            return dict(kind='stop', reason='hard stop (replay)',
                        exit_pct=-stop_index_pct(cfg))
        if fav_i >= target_index_pct(cfg):
            return dict(kind='target', reason='target (replay)',
                        exit_pct=target_index_pct(cfg))

    update_excursions(pos, h, l)
    th = pos['thesis']
    cl_pct = (c - e) / e * 100 * d

    if bar_close_time(T[i], cfg) >= cfg.FORCE_CLOSE:
        return dict(kind='force_close', reason=f'end of day {cfg.FORCE_CLOSE}', exit_pct=cl_pct)

    if (d > 0 and c < th['invalidation']) or (d < 0 and c > th['invalidation']):
        return dict(kind='thesis_broken',
                    reason=f"close {c:.0f} through invalidation {th['invalidation']:.0f}",
                    exit_pct=cl_pct)

    if pos['best_fav'] >= cfg.THESIS_CONFIRM_PCT:
        pos['confirmed'] = True
    held = i - pos['entry_bar']
    if not pos['confirmed'] and held >= cfg.THESIS_CONFIRM_BARS:
        return dict(kind='thesis_stale',
                    reason=f"no {cfg.THESIS_CONFIRM_PCT}% follow-through in {held} bars "
                           f"(best {pos['best_fav']:.3f}%)", exit_pct=cl_pct)

    if pos['best_fav'] >= cfg.TRAIL_ARM_INDEX_PCT:
        trail = pos['best_px'] - d * cfg.TRAIL_ATR_MULT * th['atr']
        if (d > 0 and c <= trail) or (d < 0 and c >= trail):
            return dict(kind='trail',
                        reason=f"close {c:.0f} through trail {trail:.0f} "
                               f"({cfg.TRAIL_ATR_MULT} ATR from best {pos['best_px']:.0f})",
                        exit_pct=cl_pct)
    return None


# ── sizing & costs ──────────────────────────────────────────────────────────
def book_state(pnls: list, cfg=cfg_default) -> dict:
    """Book, running peak, drawdown and the risk multiplier, from the closed-trade
    P&L stream. The book compounds; the multiplier de-levers in a drawdown."""
    start = float(cfg.STARTING_CAPITAL)
    eq = peak = start
    for p in (pnls if cfg.COMPOUND_ENABLED else []):
        eq += float(p)
        peak = max(peak, eq)
    book = max(eq, start * cfg.COMPOUND_FLOOR_PCT)
    dd = max((peak - eq) / peak, 0.0) if peak > 0 else 0.0
    mult = 1.0
    if cfg.DD_THROTTLE_ENABLED:
        mult = min(1.0, max(cfg.DD_THROTTLE_MIN_MULT, 1.0 - dd / max(cfg.DD_THROTTLE_FULL, 1e-9)))
    return dict(book=book, peak=peak, drawdown=dd, risk_mult=mult)


def size_lots(premium: float, bs: dict, cfg=cfg_default) -> dict:
    """Lots for one entry. Floors, never rounds, so the stated risk is a ceiling.
    Returns lots=0 only when even one lot cannot be afforded."""
    if premium is None or premium <= 0:
        return dict(lots=0, why='no premium')
    one_cost = premium * cfg.LOT_SIZE
    one_risk = one_cost * cfg.HARD_STOP_PREMIUM_PCT
    book = bs['book']
    if one_cost > book:
        return dict(lots=0, why=f'one lot costs {one_cost:,.0f} > book {book:,.0f}', one_risk=one_risk)
    budget = book * cfg.RISK_PCT_OF_BOOK * bs['risk_mult']
    by_risk = int(budget // one_risk) if one_risk > 0 else 0
    by_cash = int((book * cfg.MAX_DEPLOY_PCT) // one_cost)
    lots = max(cfg.MIN_LOTS, min(by_risk, by_cash, cfg.MAX_LOTS))
    if lots * one_cost > book:
        lots = max(1, int(book // one_cost))
    return dict(lots=lots, budget=budget, one_risk=one_risk, one_cost=one_cost,
                by_risk=by_risk, by_cash=by_cash,
                why=f'book={book:,.0f} dd={bs["drawdown"] * 100:.1f}% x{bs["risk_mult"]:.2f} '
                    f'budget={budget:,.0f} risk/lot={one_risk:,.0f}')


def round_trip_cost(buy_premium: float, sell_premium: float, lots: int, cfg=cfg_default) -> float:
    q = lots * cfg.LOT_SIZE
    buy_v, sell_v = buy_premium * q, sell_premium * q
    exch = cfg.EXCH_TXN_PCT * (buy_v + sell_v)
    return (2 * cfg.BROKERAGE_PER_ORDER + cfg.STT_SELL_PCT * sell_v + exch
            + cfg.GST_PCT * (2 * cfg.BROKERAGE_PER_ORDER + exch) + cfg.STAMP_BUY_PCT * buy_v)
