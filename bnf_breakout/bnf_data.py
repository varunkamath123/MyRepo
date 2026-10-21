# -*- coding: utf-8 -*-
"""Data layer: turns history files and live API candles into the same shape.

The strategy only ever sees dicts of numpy arrays (H, L, C, V) plus a list of
bar-open times (T). This module is the one place that knows about CSV layouts and
the Fyers candle format, so the live runner and the replay feed the strategy
identical structures.
"""
from __future__ import annotations

import glob
import os
from datetime import datetime, timedelta, timezone

import numpy as np

IST = timezone(timedelta(hours=5, minutes=30))
OPEN_HHMM, LAST_BAR_HHMM = '09:15', '15:25'


def _session(day: str, rows: list) -> dict | None:
    """rows: (hhmm, o, h, l, c, v) in time order -> a session dict, or None."""
    rows = [r for r in rows if OPEN_HHMM <= r[0] <= LAST_BAR_HHMM]
    if len(rows) < 60:
        return None
    return dict(day=day,
                T=[r[0] for r in rows],
                O=np.array([r[1] for r in rows], float),
                H=np.array([r[2] for r in rows], float),
                L=np.array([r[3] for r in rows], float),
                C=np.array([r[4] for r in rows], float),
                V=np.array([r[5] for r in rows], float))


def load_history(history_dir: str, before: str | None = None, last_n: int | None = None) -> list:
    """Complete sessions from the nightly CSVs, oldest first.

    `before` (YYYY-MM-DD) excludes that day and later, so today's partial file can
    never leak into the baselines. last_n keeps only the most recent sessions.
    """
    out = []
    for f in sorted(glob.glob(os.path.join(history_dir, '*.csv'))):
        try:
            with open(f, encoding='utf-8', errors='ignore') as fh:
                head = [h.strip().lower() for h in fh.readline().strip().split(',')]
                ix = {k: head.index(k) for k in ('ts', 'open', 'high', 'low', 'close', 'volume')}
                rows, day = [], None
                for ln in fh:
                    p = ln.rstrip('\n').split(',')
                    if len(p) < len(head):
                        continue
                    ts = p[ix['ts']]
                    day = day or ts[:10]
                    rows.append((ts[11:16], float(p[ix['open']]), float(p[ix['high']]),
                                 float(p[ix['low']]), float(p[ix['close']]),
                                 float(p[ix['volume']] or 0)))
        except Exception:
            continue
        if day is None or (before and day >= before):
            continue
        s = _session(day, rows)
        if s:
            out.append(s)
    return out[-last_n:] if last_n else out


def candles_to_bars(candles: list, now: datetime | None = None, bar_minutes: int = 5) -> dict:
    """Fyers history candles -> a bars dict of CLOSED bars only.

    A candle is [epoch_seconds, open, high, low, close, volume]. The last candle
    from the API is usually the bar still forming; acting on it would mean
    deciding on a half-built bar, so any bar whose close time is in the future
    (or within a couple of seconds of now) is dropped.
    """
    now = now or datetime.now(IST)
    rows = []
    for c in candles or []:
        try:
            t = datetime.fromtimestamp(float(c[0]), IST)
        except Exception:
            continue
        if t + timedelta(minutes=bar_minutes, seconds=2) > now:
            continue                                        # still forming
        hhmm = t.strftime('%H:%M')
        if OPEN_HHMM <= hhmm <= LAST_BAR_HHMM:
            rows.append((hhmm, float(c[1]), float(c[2]), float(c[3]), float(c[4]),
                         float(c[5]) if len(c) > 5 and c[5] is not None else 0.0))
    rows.sort(key=lambda r: r[0])
    return dict(T=[r[0] for r in rows],
                O=np.array([r[1] for r in rows], float),
                H=np.array([r[2] for r in rows], float),
                L=np.array([r[3] for r in rows], float),
                C=np.array([r[4] for r in rows], float),
                V=np.array([r[5] for r in rows], float))


def bars_are_contiguous(T: list, bar_minutes: int = 5) -> bool:
    """True when there are no holes from the open to the last bar. A hole means a
    dropped candle, and ATR/compression computed across it would be quietly wrong."""
    if not T:
        return False
    exp = []
    h, m = 9, 15
    for _ in range(len(T)):
        exp.append(f'{h:02d}:{m:02d}')
        m += bar_minutes
        h, m = h + m // 60, m % 60
    return list(T) == exp
