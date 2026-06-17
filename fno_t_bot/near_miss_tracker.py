"""
near_miss_tracker.py — append vX near-miss events to daily JSONL files.

A "near-miss" is a bar where the bot was in-window and capable of entering
but the vX signal didn't fire because of one of two filter failures:

  ADX_LOW    — ADX was within 5pts of the required threshold (DI alignment
               was correct, so a higher ADX would have fired).

  STALE_CROSS — ADX was sufficient, but the last DI+/DI- crossover happened
                4–10 bars ago (just outside the 3-bar fresh-cross window).

Data accumulates across trading days in:
    live_bot/logs/near_miss_{INSTRUMENT}_{YYYY-MM-DD}.jsonl

Run near_miss_analyzer.py after 30+ trading days to surface patterns
(e.g. "ADX gap median is 2.1pts — threshold appears correctly calibrated").
"""

from __future__ import annotations

import json
import os

_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')


def record(
    *,
    date: str,
    time: str,
    instrument: str,
    reason: str,
    adx_actual: float,
    adx_threshold: float,
    adx_gap: float,
    direction: str,
    px: float,
    di_plus: float,
    di_minus: float,
    di_spread_pct: float,
    vwap: float | None,
    vwap_ok: bool,
    htf_bull: bool,
    htf_bear: bool,
    cross_bars_ago: int | None,
) -> None:
    """Append one near-miss event to the instrument's daily JSONL file.

    Parameters
    ----------
    date            : 'YYYY-MM-DD' — trading date
    time            : 'HH:MM'     — bar time (IST)
    instrument      : 'NIFTY' | 'BANKNIFTY' | 'SENSEX'
    reason          : 'ADX_LOW' | 'STALE_CROSS'
    adx_actual      : ADX value at this bar
    adx_threshold   : the per-direction ADX minimum required (call_adx_min or put_adx_min)
    adx_gap         : adx_threshold - adx_actual  (positive = ADX is short)
    direction       : 'CALL' | 'PUT'  (inferred from DI+/DI- alignment)
    px              : Close price at this bar
    di_plus         : +DI value (positive directional indicator)
    di_minus        : -DI value (negative directional indicator)
    di_spread_pct   : abs(di_plus - di_minus) / max(di_plus, di_minus)  (DI separation)
    vwap            : VWAP value, or None if unavailable
    vwap_ok         : True if price is on the correct side of VWAP for direction
    htf_bull        : True if 15m SuperTrend is +1 (BULL)
    htf_bear        : True if 15m SuperTrend is -1 (BEAR)
    cross_bars_ago  : bars since last DI+/DI- crossover (None if no cross found in scan window)

    This function is intentionally silent on all errors — a write failure
    must never crash or slow down the live trading loop.
    """
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        path = os.path.join(_LOG_DIR, f'near_miss_{instrument}_{date}.jsonl')
        row = {
            'record_type'    : 'near_miss',
            'date'           : date,
            'time'           : time,
            'instrument'     : instrument,
            'reason'         : reason,
            'adx_actual'     : round(float(adx_actual), 1),
            'adx_threshold'  : float(adx_threshold),
            'adx_gap'        : round(float(adx_gap), 1),
            'direction'      : direction,
            'px'             : round(float(px), 2),
            'di_plus'        : round(float(di_plus), 2),
            'di_minus'       : round(float(di_minus), 2),
            'di_spread_pct'  : round(float(di_spread_pct), 5),
            'vwap'           : round(float(vwap), 2) if vwap is not None else None,
            'vwap_ok'        : bool(vwap_ok),
            'htf_bull'       : bool(htf_bull),
            'htf_bear'       : bool(htf_bear),
            'cross_bars_ago' : int(cross_bars_ago) if cross_bars_ago is not None else None,
        }
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(row) + '\n')
    except Exception:
        pass   # never propagate — bot loop must not be affected


def record_outcome(
    *,
    date: str,
    time: str,
    instrument: str,
    reason: str,
    direction: str,
    px: float,
    outcome_at: str,
    px_now: float,
) -> None:
    """Append a price-outcome row for a previously recorded near-miss event.

    Called by paper_bot.py's bar loop at +6 bars (30 min), +12 bars (60 min),
    and +18 bars (90 min) after each near-miss to record what price actually did.

    Parameters
    ----------
    date, time, instrument, reason, direction
        Key fields — must match the original near_miss record exactly so the
        analyzer can join them.
    px          : Close price at the near-miss bar (stored in the near_miss record)
    outcome_at  : '30m' | '60m' | '90m'
    px_now      : Current Close price (N minutes after the near-miss)

    Result labelling (0.10% noise floor):
        CONTINUED — price moved with the signal direction (would have profited)
        REVERSED  — price moved against the signal direction (would have lost)
        FLAT      — price barely moved (within noise floor)
    """
    try:
        move_pts  = round(float(px_now) - float(px), 2)
        noise_thr = float(px) * 0.001          # 0.10% of index price

        if direction == 'CALL':
            if move_pts >= noise_thr:
                result = 'CONTINUED'
            elif move_pts <= -noise_thr:
                result = 'REVERSED'
            else:
                result = 'FLAT'
        else:  # PUT direction — profitable if price falls
            if move_pts <= -noise_thr:
                result = 'CONTINUED'
            elif move_pts >= noise_thr:
                result = 'REVERSED'
            else:
                result = 'FLAT'

        path = os.path.join(_LOG_DIR, f'near_miss_{instrument}_{date}.jsonl')
        row = {
            'record_type' : 'outcome',
            'date'        : date,
            'time'        : time,
            'instrument'  : instrument,
            'reason'      : reason,
            'direction'   : direction,
            'px_at_miss'  : round(float(px), 2),
            'outcome_at'  : outcome_at,
            'px_now'      : round(float(px_now), 2),
            'move_pts'    : move_pts,
            'move_pct'    : round(move_pts / float(px) * 100, 3),
            'result'      : result,
        }
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(row) + '\n')
    except Exception:
        pass   # never propagate — bot loop must not be affected
