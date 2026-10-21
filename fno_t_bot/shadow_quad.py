# -*- coding: utf-8 -*-
"""Shadow engine: ADX + VIX + OI + PCR. Logs only, never trades.

WHAT THIS IS
------------
A directional signal built from exactly four inputs, evaluated every bar and
written to disk. It places no orders, opens no phantom positions and makes no
API calls of its own -- the premium archive already records ATM +/- 4 every
five minutes, so the evaluator prices these signals offline by joining the two
files. Nothing here can affect the live book.

HONEST STARTING POSITION
------------------------
Each of these four inputs has already been tested and come back null:

    OI drift / PCR drift      642 obs   47.4% right   -0.025% captured
    MaxPain pull              666 obs   49.7% right   +0.010%
    OI walls as S/R levels    n=10-63   ~51%, NIFTY and BANKNIFTY opposite signs
    ADX -> retracement size   1,664 obs  rho -0.058
    ADX as an entry gate      it is what REV/TREND already use: p=0.663, p=0.412
    VIX level                 IV *level* predicts nothing (p=0.116); the ratio does

And the one conjunction tested so far (MACD + OI drift + price, "all three
agree") was the WORST performer of the group -- 45.5% against a 50.8% base
rate, both halves negative. Agreement between correlated reads concentrates
error rather than signal.

So the prior is poor. What has NOT been tested is these four as a joint rule,
and shadow mode costs nothing, so the question is worth asking properly rather
than asserting the answer.

THE DESIGN CHOICE THAT MATTERS
------------------------------
Every evaluation is logged with all four component votes AND the raw inputs
behind them -- not merely the final decision, and not only the bars where it
fires.

That means one dataset answers many questions offline: any fire threshold
(2, 3 or 4 of 4), any subset of the components, any input cutoff. Nothing
needs a live re-run, which is how 3,072 variants got swept last time and cost
nine permanently unreachable confidence points. The evaluator carries the
multiple-testing count explicitly.

PRE-REGISTERED, BEFORE ANY DATA EXISTS
--------------------------------------
Primary hypothesis: with FIRE_THRESHOLD = 3 (three of four components
agreeing), the signalled direction is right more often than that instrument's
own base rate, by a margin that holds in BOTH chronological halves.

Kill criterion: if after 30 sessions the hit rate is inside the base rate
+/- 3 percentage points, or flips sign between halves, this engine is dead and
that gets written into REBUILD_PLAN.md rather than re-tuned. A threshold sweep
afterwards does not rescue it -- the sweep is the failure mode, not the remedy.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

import config

try:
    from options_bot import IST
except Exception:
    IST = None


def _late(name):
    """Resolve from options_bot at call time -- it imports this module before
    it defines IST, so a module-level import binds None (see the Oct 6 2026
    circular-import bug that silently zeroed every phantom cost)."""
    g = globals()
    if g.get(name) is None:
        try:
            import options_bot as _ob
            g[name] = getattr(_ob, name, None)
        except Exception:
            pass
    return g.get(name)


def _log_dir() -> str:
    return getattr(config, 'LOG_DIRECTORY', 'logs')


# ── the four components. each returns (vote, detail) with vote in {-1,0,+1} ──

def _adx_vote(adx, di_p, di_m):
    """Trend strength plus DI direction."""
    floor = float(getattr(config, 'SHADOW_QUAD_ADX_FLOOR', 20))
    need = float(getattr(config, 'SHADOW_QUAD_DI_SPREAD', 8))
    if adx is None or di_p is None or di_m is None:
        return 0, None
    if adx < floor:
        return 0, f'ADX {adx:.1f} < {floor:.0f}'
    spread = di_p - di_m
    if spread >= need:
        return +1, f'ADX {adx:.1f}, DI+ leads by {spread:.1f}'
    if -spread >= need:
        return -1, f'ADX {adx:.1f}, DI- leads by {-spread:.1f}'
    return 0, f'ADX {adx:.1f}, DI spread {spread:+.1f} < {need:.0f}'


def _pcr_vote(pcr):
    """Heavy put writing (high PCR) means writers are defending the downside,
    which reads bullish; heavy call writing reads bearish. This is the same
    convention oi_levels uses for its PCR discount, kept identical on purpose
    so the two cannot disagree about what PCR means."""
    if pcr is None:
        return 0, None
    bull = float(getattr(config, 'SHADOW_QUAD_PCR_BULL', 1.10))
    bear = float(getattr(config, 'SHADOW_QUAD_PCR_BEAR', 0.90))
    if pcr >= bull:
        return +1, f'PCR {pcr:.2f} >= {bull}'
    if pcr <= bear:
        return -1, f'PCR {pcr:.2f} <= {bear}'
    return 0, f'PCR {pcr:.2f} neutral'


def _maxpain_vote(spot, max_pain):
    """MaxPain treated as an ATTRACTOR, not a wall. The Oct 2 2026 test
    supports that reading: price rejected off max_pain only 40-45% of the time,
    BELOW the ~51% baseline for ordinary levels -- i.e. it passes through and
    settles near it rather than bouncing. So price below MaxPain reads up."""
    if not spot or not max_pain:
        return 0, None
    d = (max_pain - spot) / spot * 100.0
    need = float(getattr(config, 'SHADOW_QUAD_MP_MIN_DIST', 0.20))
    if d >= need:
        return +1, f'MaxPain {d:+.2f}% above spot'
    if -d >= need:
        return -1, f'MaxPain {d:+.2f}% below spot'
    return 0, f'MaxPain {d:+.2f}% (within {need}%)'


def _wall_vote(spot, strikes):
    """Whichever side's open-interest wall sits CLOSER is the nearer barrier.
    A put wall just below is support; a call wall just above is resistance."""
    if not spot or not strikes:
        return 0, None
    best_c = best_p = None
    for k, v in strikes.items():
        try:
            k = float(k)
        except Exception:
            continue
        v = v or {}
        if k > spot and (v.get('call_oi') or 0) > 0:
            if best_c is None or (v['call_oi'] > (strikes.get(best_c) or {}).get('call_oi', 0)):
                best_c = k
        elif k < spot and (v.get('put_oi') or 0) > 0:
            if best_p is None or (v['put_oi'] > (strikes.get(best_p) or {}).get('put_oi', 0)):
                best_p = k
    if best_c is None or best_p is None:
        return 0, None
    dc = (best_c - spot) / spot * 100.0
    dp = (spot - best_p) / spot * 100.0
    if dp < dc:
        return +1, f'put wall {best_p:.0f} ({dp:.2f}%) nearer than call wall {best_c:.0f} ({dc:.2f}%)'
    if dc < dp:
        return -1, f'call wall {best_c:.0f} ({dc:.2f}%) nearer than put wall {best_p:.0f} ({dp:.2f}%)'
    return 0, 'walls equidistant'


def evaluate(bot, df, oc, spot) -> dict | None:
    """Score the four components. Returns the record, or None if unusable."""
    try:
        row = df.iloc[-1]

        def _f(col):
            """Per-field coercion. One unusable column must NOT discard the
            whole record: if a missing ADX returned None from evaluate(), only
            bars where every input happened to be present would be logged, and
            the base rate would be computed over a self-selected sample that
            flatters the hit rate. Abstain on the field, keep the bar."""
            if col not in df.columns:
                return None
            try:
                v = float(row[col])
            except (TypeError, ValueError):
                return None
            return None if v != v else v          # drop NaN

        adx = _f('ADX')
        di_p = _f('DI_plus')
        di_m = _f('DI_minus')
        vix = getattr(bot, '_last_vix', None)
        if vix is None:
            vix = _f('VIX')
        pcr = (oc or {}).get('pcr')
        mp = (oc or {}).get('max_pain')
        strikes = (oc or {}).get('strikes') or {}

        a_v, a_d = _adx_vote(adx, di_p, di_m)
        p_v, p_d = _pcr_vote(pcr)
        m_v, m_d = _maxpain_vote(spot, mp)
        w_v, w_d = _wall_vote(spot, strikes)
        score = a_v + p_v + m_v + w_v

        lo = float(getattr(config, 'SHADOW_QUAD_VIX_MIN', 11))
        hi = float(getattr(config, 'SHADOW_QUAD_VIX_MAX', 22))
        vix_ok = (vix is not None and lo <= vix <= hi)

        thr = int(getattr(config, 'SHADOW_QUAD_FIRE_THRESHOLD', 3))
        direction = None
        if vix_ok and abs(score) >= thr:
            direction = 'CALL' if score > 0 else 'PUT'

        # ── Magnitude context, added Oct 16 2026 ───────────────────────
        # The quad votes on DIRECTION only, and direction has no measured edge
        # in this book. What DOES separate is whether the day can deliver a
        # move big enough to pay for the option at all: split by trailing
        # realised vol at entry, the share of 240min windows clearing an
        # IV-adjusted break-even runs 23.4% in the bottom quintile against
        # 50.0% in the top -- a +26.6 point gap that is monotone across all
        # five and holds in both chronological halves (+26.1 / +30.4).
        #
        # Splitting by IV instead collapses to +12 and is non-monotone, which
        # is the tell: IV rises only 1.34x across those quintiles while the
        # actual excursion rises 2.3x, so implied under-responds to realised.
        # That gap is the whole opportunity, and a fitted GARCH does NOT
        # capture it better -- it came last in a walk-forward contest against
        # this same plain trailing estimator (n=901).
        #
        # Logged, not acted on. It makes the pairing measurable: does a quad
        # fire on a high-vol day behave differently from one on a dead day?
        # Nothing can answer that until both fields sit on the same record.
        _hv = _impl = _ratio = None
        try:
            _h = df['HV'].iloc[-1] if 'HV' in df.columns else None
            _hv = round(float(_h), 4) if _h == _h and _h is not None else None
        except Exception:
            pass
        try:
            # UNITS, which the first version of this got wrong on Oct 20 2026.
            # df['HV'] is annualised vol as a FRACTION (0.1982 = 19.82%) while
            # atm_iv is annualised vol as a PERCENT (14.86 = 14.86%). The first
            # version divided the fraction by an implied ONE-DAY percent, which
            # compared three different units at once and came out ~6x low --
            # 0.219 where the real ratio was 1.39. Logging-only, so no trade was
            # affected, but it would have corrupted the direction-x-magnitude
            # pairing this field exists to make measurable.
            #
            # Use the convention already in options_bot (rv_iv = hv*100/iv), so
            # this number is directly comparable to the rv_iv on every trade
            # record rather than being a second, subtly different quantity.
            _iv = float((oc or {}).get('atm_iv') or 0) or None
            if _iv:
                _impl = round(_iv / (252 ** 0.5), 4)   # implied 1-session move %
                if _hv:
                    _ratio = round(_hv * 100.0 / _iv, 3)  # realised/implied
        except Exception:
            pass

        return dict(
            instrument=bot.instrument, spot=round(float(spot), 2),
            score=score, direction=direction, fired=bool(direction),
            vix=vix, vix_ok=vix_ok,
            # date was only ever recoverable from the FILENAME, which made
            # every evaluator reconstruct it with a regex. Put it on the row.
            # _late('IST') not the module-level IST: this module is
            # imported by options_bot BEFORE options_bot defines IST, so
            # the module-level name is bound to None and a naive
            # datetime would stamp the host's timezone, not IST.
            date=(datetime.now(_late('IST')) if _late('IST')
                  else datetime.now()).strftime('%Y-%m-%d'),
            # hv_pct alongside hv so the record is self-describing: one is the
            # raw fraction the frame carries, the other the percent everything
            # else in this project quotes vol in.
            hv=_hv, hv_pct=(round(_hv * 100.0, 3) if _hv else None),
            atm_iv=_iv, impl_daily=_impl, rv_iv_fwd=_ratio,
            votes=dict(adx=a_v, pcr=p_v, maxpain=m_v, wall=w_v),
            why=dict(adx=a_d, pcr=p_d, maxpain=m_d, wall=w_d),
            raw=dict(adx=adx, di_plus=di_p, di_minus=di_m,
                     pcr=pcr, max_pain=mp, n_strikes=len(strikes)),
        )
    except Exception:
        return None


def observe(bot, df, oc, spot) -> None:
    """Evaluate and log. Throttled. Never raises, never trades."""
    try:
        if not getattr(config, 'SHADOW_QUAD_ENABLED', False):
            return
        now = datetime.now(_late('IST')) if _late('IST') else datetime.now()
        every = int(getattr(config, 'SHADOW_QUAD_LOG_EVERY_MIN', 5))
        last = getattr(bot, '_shq_last', None)
        if last is not None and (now - last).total_seconds() < every * 60:
            return
        rec = evaluate(bot, df, oc, spot)
        if rec is None:
            return
        bot._shq_last = now
        rec['ts'] = now.isoformat()
        try:
            os.makedirs(_log_dir(), exist_ok=True)
            p = os.path.join(_log_dir(),
                             f'shadow_quad_{bot.instrument}_'
                             f'{now.strftime("%Y-%m-%d")}.jsonl')
            with open(p, 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(rec, default=str) + chr(10))
        except Exception:
            pass
        if rec['fired']:
            v = rec['votes']
            bot.logger.info(
                f"  [SHADOW-QUAD] {bot.instrument} {rec['direction']} "
                f"score={rec['score']:+d} "
                f"(adx{v['adx']:+d} pcr{v['pcr']:+d} mp{v['maxpain']:+d} "
                f"wall{v['wall']:+d}) VIX={rec['vix']} — logged, not traded"
            )
    except Exception:
        try:
            bot.logger.debug('  [SHADOW-QUAD] observe failed')
        except Exception:
            pass


def reset_day(bot) -> None:
    bot._shq_last = None
