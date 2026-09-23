# -*- coding: utf-8 -*-
"""
Multi-strategy paper harness — ten pre-registered books in parallel
==================================================================

Why
---
The week of Aug 17-21 tested ~3,072 configuration variants SEQUENTIALLY: change
one thing, look at five trades, change another. Deflated Sharpe put the whole
exercise at DSR 0.0% — indistinguishable from the luckiest of that many random
tries. The failure was structural, not a matter of insufficient care: sequential
tuning on tiny samples cannot produce a trustworthy answer.

This runs ten PRE-REGISTERED strategies on the same bars with isolated books and
no mid-flight edits, turning "search until something looks good" into a
controlled experiment with a fixed decision date.

The controls matter more than the candidates
--------------------------------------------
  random      the null. If it wins, nothing else here is real.
  inverse     current signals with direction flipped. The random-entry baseline
              measured 45.9% right-side vs random's 49.9%; if inverting wins,
              the logic is backwards rather than absent.
  fixed_time  show up at 10:30 with a trivial rule. Isolates whether our TIMING
              adds anything over merely participating.

Discipline (the part that failed last time)
-------------------------------------------
* rules frozen at first run; no edits while live
* every book reported always — losers are never quietly dropped
* a winner must clear the DSR bar for N=10, not merely beat the others
* decision date set in advance, not "when it looks conclusive"

Safety: paper only. Separate books, separate JSONL, no Fyers orders, no contact
with the live position. An exception in one book cannot affect another.
"""
from __future__ import annotations

import json
import os
import random as _random
from contextlib import contextmanager
from datetime import time as dtime

import config

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
MULTI_CAPITAL    = 26_000.0        # per strategy per instrument, mirrors live
EXPERIMENT_START = '2026-08-25'
DECISION_DATE    = '2026-11-24'    # ~3 months, fixed in advance on purpose

_books: dict = {}
_rng = _random.Random(20260825)    # fixed seed: the random control is reproducible


def _book(inst: str, strat: str) -> dict:
    k = (inst, strat)
    if k not in _books:
        _books[k] = dict(position=None, trades=0, pnl=0.0,
                         capital=MULTI_CAPITAL, day=None,
                         rev_fired=False, trend_fired=False, signals_today=0,
                         morning_dir=None, morning_adx_peak=0.0,
                         morning_di_peak=0.0, trend_qual_dir=None,
                         trend_qual_time=None, trend_leg_extreme=None,
                         trend_anchor=None)
    return _books[k]


def _reset_day(b: dict, day: str) -> None:
    b.update(day=day, rev_fired=False, trend_fired=False, signals_today=0,
             morning_dir=None, morning_adx_peak=0.0, morning_di_peak=0.0,
             trend_qual_dir=None, trend_qual_time=None,
             trend_leg_extreme=None, trend_anchor=None)


@contextmanager
def _engine_state(bot, b: dict):
    """Swap a book's own engine state onto the shared bot for one call.

    The engine methods are stateful — one-shot flags, morning peaks,
    qualification tracking. Reusing the live bot's state would let the live
    bot's own firing suppress every book here. Swapping keeps each book
    independent while guaranteeing the SAME code path as live.
    """
    keys = ('_path_rev_fired', '_path_trend_fired', '_morning_dir',
            '_morning_adx_peak', '_morning_di_peak', '_trend_qual_dir',
            '_trend_qual_time', '_trend_leg_extreme', '_trend_anchor')
    saved = {k: getattr(bot, k, None) for k in keys}
    bot._path_rev_fired    = b['rev_fired']
    bot._path_trend_fired  = b['trend_fired']
    bot._morning_dir       = b['morning_dir']
    bot._morning_adx_peak  = b['morning_adx_peak']
    bot._morning_di_peak   = b['morning_di_peak']
    bot._trend_qual_dir    = b['trend_qual_dir']
    bot._trend_qual_time   = b['trend_qual_time']
    bot._trend_leg_extreme = b['trend_leg_extreme']
    bot._trend_anchor      = b['trend_anchor']
    try:
        yield
    finally:
        b['rev_fired']         = bot._path_rev_fired
        b['trend_fired']       = bot._path_trend_fired
        b['morning_dir']       = bot._morning_dir
        b['morning_adx_peak']  = bot._morning_adx_peak
        b['morning_di_peak']   = bot._morning_di_peak
        b['trend_qual_dir']    = bot._trend_qual_dir
        b['trend_qual_time']   = bot._trend_qual_time
        b['trend_leg_extreme'] = bot._trend_leg_extreme
        b['trend_anchor']      = bot._trend_anchor
        for k, v in saved.items():
            setattr(bot, k, v)


def _engines(bot, b, df, oc, now, want_rev=True, want_trend=True):
    sig = None
    with _engine_state(bot, b):
        try:
            bot._update_trend_qualification(df, now)
        except Exception:
            pass
        if want_rev and not b['rev_fired']:
            try:
                sig = bot.get_path_rev_signal(df, oc, now)
            except Exception:
                sig = None
            if sig:
                bot._path_rev_fired = True
        if sig is None and want_trend and not b['trend_fired']:
            try:
                sig = bot.get_path_trend_signal(df, oc, now)
            except Exception:
                sig = None
            if sig:
                bot._path_trend_fired = True
    return sig


def _chase(df, price, direction) -> float:
    td = df[df.index.date == df.index[-1].date()]
    hi = float(td['High'].max()); lo = float(td['Low'].min())
    if hi <= lo:
        return 0.5
    rp = (price - lo) / (hi - lo)
    return rp if direction == 'CALL' else 1.0 - rp


def _mk(direction, price, adx, path):
    return dict(type=direction, price=float(price), adx=float(adx or 0),
                strength=1, lots=1, path=path, otm_strikes=0,
                otm_reason=f'{path} paper', gap_type=None, dynamic_or=False)


# ── the ten. Frozen at first run. ───────────────────────────────────────────

def s_current(bot, b, df, oc, now, ctx):
    return _engines(bot, b, df, oc, now)


def s_rev_only(bot, b, df, oc, now, ctx):
    return _engines(bot, b, df, oc, now, want_trend=False)


def s_trend_only(bot, b, df, oc, now, ctx):
    return _engines(bot, b, df, oc, now, want_rev=False)


def s_inverse(bot, b, df, oc, now, ctx):
    s = _engines(bot, b, df, oc, now)
    if not s:
        return None
    s = dict(s)
    s['type'] = 'PUT' if s['type'] == 'CALL' else 'CALL'
    s['path'] = 'INV'
    return s


def s_random(bot, b, df, oc, now, ctx):
    """Coin flip, once per day, at a randomly chosen eligible bar."""
    if b['signals_today'] or not (dtime(9, 45) <= now.time() <= dtime(13, 0)):
        return None
    if _rng.random() > 0.02:
        return None
    row = df.iloc[-1]
    return _mk(_rng.choice(('CALL', 'PUT')), row['Close'], row.get('ADX'), 'RANDOM')


def s_fixed_time(bot, b, df, oc, now, ctx):
    """Show up at 10:30 with a trivial rule: which side of the open we're on."""
    if b['signals_today'] or now.strftime('%H:%M') < '10:30':
        return None
    td = df[df.index.date == df.index[-1].date()]
    if len(td) < 2:
        return None
    row = df.iloc[-1]
    d = 'CALL' if float(row['Close']) >= float(td['Open'].iloc[0]) else 'PUT'
    return _mk(d, row['Close'], row.get('ADX'), 'FIXED')


def s_gap_fade(bot, b, df, oc, now, ctx):
    """BANKNIFTY only. Gaps fade: gap up -> PUT, gap down -> CALL.

    Spread +0.271 pct-pts to 14:30, p=0.0020; significant at 5 of 6 thresholds
    and present in both chronological halves. The 0.20% cutoff tested strongest
    and is FIXED here rather than tuned further.
    """
    if ctx.get('inst') != 'BANKNIFTY' or b['signals_today']:
        return None
    if now.strftime('%H:%M') < '09:45':
        return None
    g = ctx.get('gap_pct')
    if g is None or abs(g) < 0.20:
        return None
    row = df.iloc[-1]
    return _mk('PUT' if g > 0 else 'CALL', row['Close'], row.get('ADX'), 'GAPFADE')


def s_gap_rev(bot, b, df, oc, now, ctx):
    s = s_gap_fade(bot, b, df, oc, now, ctx)
    if s:
        return s
    return _engines(bot, b, df, oc, now, want_trend=False)


def s_skip_first(bot, b, df, oc, now, ctx):
    """Live record: trade #1 of the day ran 26.8% WR / -Rs785 mean vs 45-55%
    for #2-#4. Takes the SECOND signal of the day instead of the first."""
    s = _engines(bot, b, df, oc, now)
    if not s:
        return None
    b['signals_today'] += 1
    if b['signals_today'] < 2:
        return None
    return s


def s_poker(bot, b, df, oc, now, ctx):
    """Same entries as current; the difference is the fold rule (see _exit_check).

    NOTE ON THE FOLD TIME: an earlier draft used a flat 20 minutes because it
    won a 17-variant sweep (+1.19% vs +0.91%). That was cherry-picking — the
    same sweep showed the fold-time effect was NOT monotonic (15min +0.89,
    20 +1.19, 30 +1.16, 45 +0.91, 60 +1.07), i.e. noise plus best-of-17
    inflation. Replaced with a runway-proportional rule, which is an actual
    poker concept ("fold when the hand can no longer develop in the time left")
    rather than a fitted constant.
    """
    return _engines(bot, b, df, oc, now)


STRATEGIES = {
    'random':     dict(fn=s_random,     role='control'),
    'inverse':    dict(fn=s_inverse,    role='control'),
    'fixed_time': dict(fn=s_fixed_time, role='control'),
    'current':    dict(fn=s_current,    role='incumbent'),
    'rev_only':   dict(fn=s_rev_only,   role='candidate'),
    'trend_only': dict(fn=s_trend_only, role='candidate'),
    'gap_fade':   dict(fn=s_gap_fade,   role='candidate'),
    'gap_rev':    dict(fn=s_gap_rev,    role='candidate'),
    'skip_first': dict(fn=s_skip_first, role='candidate'),
    'poker':      dict(fn=s_poker,      role='candidate',
                       exits=dict(fold_runway_frac=1/3.)),
}


def _exit_check(pos, opt_px, now, spec):
    """Mirrors the live exit stack. Poker overrides the fold rule only."""
    if opt_px is None or pos['entry_price'] <= 0:
        return None
    pnl = (opt_px - pos['entry_price']) / pos['entry_price']
    pos['peak'] = max(pos.get('peak', 0.0), pnl)
    mins = (now - pos['entry_time']).total_seconds() / 60.0
    ex = spec.get('exits') or {}

    if pnl <= -getattr(config, 'STOP_LOSS', 0.25):
        return f"Stop-Loss ({getattr(config, 'STOP_LOSS', 0.25) * 100:.0f}%)"
    if pnl >= pos.get('target', getattr(config, 'BASE_TARGET', 0.55)):
        return f"Target ({pos.get('target', 0.55) * 100:.0f}%)"
    act = getattr(config, 'TRAILING_ACTIVATION', 0.12)
    dist = getattr(config, 'TRAILING_DISTANCE', 0.10)
    if pos['peak'] > act and pnl < pos['peak'] - dist:
        return 'Trailing Stop'

    # fold rule
    frac = ex.get('fold_runway_frac')
    if frac is not None:
        fold_after = max(pos.get('runway_min', 45.0) * frac, 10.0)
        label = f'Fold ({fold_after:.0f}m = {frac:.0%} of runway'
    else:
        fold_after = getattr(config, 'NEVER_PROGRESS_MINUTES', 45)
        label = f'Never-Progressed ({fold_after:.0f}m'
    if (getattr(config, 'NEVER_PROGRESS_ENABLED', True) and pnl < 0
            and pos['peak'] < getattr(config, 'NEVER_PROGRESS_MIN_PEAK', 0.05)
            and mins >= fold_after):
        return f'{label}, peak {pos["peak"] * 100:+.1f}%)'

    if now.strftime('%H:%M') >= getattr(config, 'FORCE_CLOSE_TIME', '14:30'):
        return f"EOD Force-Close ({getattr(config, 'FORCE_CLOSE_TIME', '14:30')})"
    return None


def _runway_min(now) -> float:
    fc = getattr(config, 'FORCE_CLOSE_TIME', '14:30')
    h, m = (int(x) for x in fc.split(':'))
    end = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return max((end - now).total_seconds() / 60.0, 1.0)


def _write(inst: str, strat: str, rec: dict) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    p = os.path.join(LOG_DIR, f'multi_{strat}_{inst}_{rec["entry_time"][:10]}.jsonl')
    try:
        with open(p, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec) + '\n')
    except Exception:
        pass


def evaluate_bar(bot, instrument, df, oc, now, gap_pct=None, logger=None):
    """Call once per scan tick from the live bot. Never raises."""
    if not getattr(config, 'MULTI_STRATEGY_ENABLED', False):
        return
    try:
        from fyers_orders import build_option_symbol, get_next_expiry, get_ltp
    except Exception:
        return

    day = now.strftime('%Y-%m-%d')
    ctx = dict(inst=instrument, gap_pct=gap_pct)

    for name, spec in STRATEGIES.items():
        try:
            b = _book(instrument, name)
            if b['day'] != day:
                _reset_day(b, day)

            pos = b['position']
            if pos:
                ltp = get_ltp(bot.fyers, pos['symbol']) if bot.fyers else None
                if ltp is None:
                    continue
                why = _exit_check(pos, float(ltp), now, spec)
                if why:
                    px = float(ltp)
                    net = (px - pos['entry_price']) * pos['lot'] - 70.0
                    b['pnl'] += net
                    b['capital'] += net
                    b['trades'] += 1
                    b['position'] = None
                    _write(instrument, name, dict(
                        strategy=name, role=spec['role'], instrument=instrument,
                        type=pos['type'], path=pos['path'], strike=pos['strike'],
                        entry_time=pos['entry_time'].isoformat(),
                        exit_time=now.isoformat(),
                        entry_price=round(pos['entry_price'], 2),
                        exit_price=round(px, 2), lot_size=pos['lot'],
                        pnl_pct=round((px - pos['entry_price']) / pos['entry_price'] * 100, 2),
                        max_pnl_pct=round(pos['peak'] * 100, 2),
                        pnl_net=round(net, 2), exit_reason=why,
                        chase_pos=pos.get('chase'),
                        runway_min=round(pos.get('runway_min', 0), 1),
                        capital=round(b['capital'], 2)))
                    if logger:
                        logger.info(f"  [MULTI:{name}] {instrument} EXIT "
                                    f"{pos['type']} {why} | Rs{net:+,.0f} | "
                                    f"book Rs{b['capital']:,.0f}")
                continue

            if b['trades'] >= 3 or now.strftime('%H:%M') >= getattr(
                    config, 'FORCE_CLOSE_TIME', '14:30'):
                continue

            sig = spec['fn'](bot, b, df, oc, now, ctx)
            if not sig:
                continue

            gap = int(bot.inst_cfg.get('strike_gap', 50))
            strike = int(round(float(sig['price']) / gap) * gap)
            sym = build_option_symbol(instrument, strike, sig['type'],
                                      get_next_expiry(instrument))
            ltp = get_ltp(bot.fyers, sym) if bot.fyers else None
            if not ltp or float(ltp) < getattr(config, 'MIN_OPTION_PRICE', 20):
                continue

            b['position'] = dict(
                symbol=sym, strike=strike, type=sig['type'], path=sig['path'],
                entry_price=float(ltp), entry_time=now, peak=0.0,
                lot=int(bot.inst_cfg.get('lot_size', 1)),
                target=getattr(config, 'BASE_TARGET', 0.55),
                runway_min=_runway_min(now),
                chase=round(_chase(df, float(sig['price']), sig['type']), 3))
            b['signals_today'] = max(b['signals_today'], 1)
            if logger:
                logger.info(f"  [MULTI:{name}] {instrument} ENTRY {sig['type']} "
                            f"{strike} @ Rs{float(ltp):.2f} ({sig['path']})")
        except Exception as e:
            if logger:
                logger.debug(f"  [MULTI:{name}] {instrument} error: {e}")
            continue
