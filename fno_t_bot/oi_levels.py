# -*- coding: utf-8 -*-
"""Dynamic support/resistance from live option-chain OI and PCR.

MEASUREMENT FIRST. This computes levels and logs them. It is deliberately NOT
wired to any entry decision, and the reason matters:

On Oct 2 2026, eight mechanically-defined level types were tested across 17,300
touches on 1,227 sessions -- opening ranges (30 and 60 min), prior-day high /
low / close, round numbers, and the static prior-day OI walls. Every one of
them rejected price between 47.9% and 54.7% of the time. Twenty-four
instrument-by-level combinations, all clustered at ~51%. Repeated rejection
added nothing either: a level that had already turned price away three times
was no likelier to do it a fourth (52.1 / 51.5 / 51.3 / 52.6%).

So the bar for a new level definition is not "is it plausible" -- it is "does
it beat 51% on a sample that includes a chronological holdout". Until this one
clears that, wiring it to a trigger would be building on the same 51% coin flip
that twelve direction methods already died on.

WHAT IS DIFFERENT HERE
----------------------
The eight tested definitions were all STATIC: fixed at the open or inherited
from yesterday. This one is dynamic in two ways the others were not.

  1. It reads the LIVE chain every bar, so a wall that builds at 11:00 is seen
     at 11:00 rather than at tomorrow's EOD snapshot. The static OI walls in
     data/oi_zones are a 15:37 fetch -- by construction they can only ever
     describe yesterday.

  2. It tracks OI CHANGE, not just level. A strike with large but shrinking
     open interest is a wall being dismantled; one with smaller but rapidly
     building OI is a wall going up. The static tests could not see either,
     and that is the single most plausible reason they found nothing.

PCR enters as a directional discount rather than a level. Heavy put writing
(high PCR) means writers are defending downside: support is more credible and
resistance is likelier to give way. Low PCR inverts it. This only ever
reweights levels that OI already identified -- PCR never creates one.

Everything is best-effort. A failure logs nothing rather than a guess.
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
    """Resolve a name from options_bot at CALL time, not import time.

    options_bot imports these shadow modules at its line ~43, long before it
    defines round_trip_costs (line ~74) and IST (line ~65) -- so the
    module-level `from options_bot import ...` silently bound None in every
    one of them. Oct 6 2026 was the proof: phantom rows carried naive
    timestamps and `costs: 0.0`, which overstated every phantom P&L by the
    whole four-leg commission. Resolving on first use closes the cycle.
    """
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


def _score(oi: float, total: float, dist_pct: float, doi: float | None) -> float:
    """Wall strength: size, proximity, and whether it is building or unwinding.

    Proximity matters because a wall 4% away cannot plausibly act on an
    intraday move -- it is real, but not for today. The 1/(1+d) shape decays
    smoothly rather than imposing an arbitrary cutoff.

    The dOI term is the dynamic part. A strike adding open interest is being
    defended NOW; one shedding it is being abandoned, however large it still
    looks. Capped at +/-50% so a single noisy refresh cannot invent a wall.
    """
    if not total or total <= 0:
        return 0.0
    size = oi / total
    prox = 1.0 / (1.0 + abs(dist_pct))
    build = 1.0
    if doi is not None and oi > 0:
        build = 1.0 + max(-0.5, min(0.5, doi / oi))
    return size * prox * build


def compute(oc: dict, spot: float, instrument: str,
            prev: dict | None = None) -> dict | None:
    """Levels from the live chain. Returns None if the chain is unusable."""
    try:
        strikes = (oc or {}).get('strikes') or {}
        if not strikes or not spot or spot <= 0:
            return None
        pcr = oc.get('pcr')
        tot_c = sum((v or {}).get('call_oi') or 0 for v in strikes.values())
        tot_p = sum((v or {}).get('put_oi') or 0 for v in strikes.values())
        # Either side alone is still usable -- a partial chain fetch should
        # yield the levels it can rather than nothing. Only a chain with no
        # open interest at all is unusable.
        if tot_c <= 0 and tot_p <= 0:
            return None

        prev_s = (prev or {}).get('_raw') or {}
        res, sup = [], []
        for k, v in strikes.items():
            try:
                k = float(k)
            except Exception:
                continue
            v = v or {}
            d = (k - spot) / spot * 100.0
            pv = prev_s.get(str(k)) or prev_s.get(k) or {}
            if k > spot and tot_c > 0:
                oi = v.get('call_oi') or 0
                if oi > 0:
                    res.append(dict(strike=k, oi=oi, dist_pct=round(d, 3),
                                    d_oi=(oi - pv.get('call_oi')) if pv.get('call_oi') else None,
                                    score=_score(oi, tot_c, d,
                                                 (oi - pv.get('call_oi')) if pv.get('call_oi') else None)))
            elif k < spot and tot_p > 0:
                oi = v.get('put_oi') or 0
                if oi > 0:
                    sup.append(dict(strike=k, oi=oi, dist_pct=round(d, 3),
                                    d_oi=(oi - pv.get('put_oi')) if pv.get('put_oi') else None,
                                    score=_score(oi, tot_p, d,
                                                 (oi - pv.get('put_oi')) if pv.get('put_oi') else None)))
        if not res and not sup:
            return None

        # PCR as a directional discount. Heavy put writing defends the
        # downside, so support firms up and resistance is likelier to break.
        hi = float(getattr(config, 'OI_LEVEL_PCR_BULL', 1.15))
        lo = float(getattr(config, 'OI_LEVEL_PCR_BEAR', 0.85))
        adj = float(getattr(config, 'OI_LEVEL_PCR_ADJ', 0.25))
        rw = sw = 1.0
        bias = 'neutral'
        if pcr is not None:
            if pcr >= hi:
                rw, sw, bias = 1.0 - adj, 1.0 + adj, 'bullish'
            elif pcr <= lo:
                rw, sw, bias = 1.0 + adj, 1.0 - adj, 'bearish'
        for r in res:
            r['score'] = round(r['score'] * rw, 6)
        for s_ in sup:
            s_['score'] = round(s_['score'] * sw, 6)

        res.sort(key=lambda z: -z['score'])
        sup.sort(key=lambda z: -z['score'])
        return dict(
            spot=round(float(spot), 2), pcr=pcr, pcr_bias=bias,
            resistance=res[:3], support=sup[:3],
            nearest_res=res[0]['strike'] if res else None,
            nearest_sup=sup[0]['strike'] if sup else None,
            max_pain=oc.get('max_pain'),
            _raw={str(k): {'call_oi': (v or {}).get('call_oi'),
                           'put_oi': (v or {}).get('put_oi')}
                  for k, v in strikes.items()},
        )
    except Exception:
        return None


def update(bot, oc: dict, spot: float) -> dict | None:
    """Compute, remember for the next dOI, and log. Never raises."""
    try:
        if not getattr(config, 'OI_LEVELS_ENABLED', False):
            return None
        lv = compute(oc, spot, bot.instrument, getattr(bot, '_oi_lv_prev', None))
        if not lv:
            return None
        bot._oi_lv_prev = lv
        now = datetime.now(_late('IST')) if _late('IST') else datetime.now()
        every = int(getattr(config, 'OI_LEVELS_LOG_EVERY_MIN', 5))
        last = getattr(bot, '_oi_lv_logged', None)
        if last is None or (now - last).total_seconds() >= every * 60:
            bot._oi_lv_logged = now
            rec = {k: v for k, v in lv.items() if k != '_raw'}
            rec.update(instrument=bot.instrument, ts=now.isoformat())
            try:
                os.makedirs(_log_dir(), exist_ok=True)
                p = os.path.join(_log_dir(),
                                 f'oi_levels_{bot.instrument}_'
                                 f'{now.strftime("%Y-%m-%d")}.jsonl')
                with open(p, 'a', encoding='utf-8') as fh:
                    fh.write(json.dumps(rec, default=str) + '\n')
            except Exception:
                pass
        return lv
    except Exception:
        try:
            bot.logger.debug('  [OI-LEVELS] update failed')
        except Exception:
            pass
        return None


def reset_day(bot) -> None:
    bot._oi_lv_prev = None
    bot._oi_lv_logged = None
