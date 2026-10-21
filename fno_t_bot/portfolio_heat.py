# -*- coding: utf-8 -*-
"""Portfolio heat: the three index bots are one correlated bet, so size them as one.

NIFTY, BANKNIFTY and SENSEX each run as a separate process and each sized as if
the other two did not exist. They are not independent. On Oct 21 2026 all three
REV entries fired within 34 seconds of each other (10:35:26, 10:35:57, 10:36:00)
on the same setup, and the indices moved 1.15-1.61% together. With each bot
risking its own 3% of its own book, a correlated stop-out costs about three
times that of the TOTAL book in one morning, and nothing in the system knew it.

"Heat" is the rupee amount currently at risk across all open positions:
    sum over open positions of  lots x lot_size x entry_price x stop fraction
i.e. what the book loses if every open position is stopped out at its stop.

Design:
  * Each bot writes ONLY its own file (logs/.heat_<INST>.json), so there is no
    shared writer and no lock to get wrong. Replacement is atomic.
  * A bot sizing a new entry reads the OTHER bots' files.
  * Files from a previous session, or older than HEAT_MAX_AGE_SEC, are ignored,
    so a bot that crashed holding a position cannot starve the others forever.
  * Every failure path returns "no information" and sizing carries on. A heat cap
    that can break entries is worse than no heat cap: an entry is never blocked
    by this module, only sized down, and never below one lot.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime

import config

HEAT_MAX_AGE_SEC = 12 * 3600


def _dir() -> str:
    return getattr(config, 'LOG_DIRECTORY', 'logs')


def _path(instrument: str) -> str:
    return os.path.join(_dir(), f'.heat_{instrument}.json')


def position_risk(pos: dict, lot_size: int) -> float:
    """Rupees lost if this position is stopped out. Prefers the value recorded
    at entry; falls back to recomputing so resumed positions still count."""
    try:
        r = pos.get('risk_inr')
        if r is not None:
            return max(float(r), 0.0)
        lots = int(pos.get('lots') or 1)
        px = float(pos.get('entry_price') or 0)
        stop = float(pos.get('stop_pct') or getattr(config, 'STOP_LOSS', 0.25))
        return max(lots * lot_size * px * stop, 0.0)
    except Exception:
        return 0.0


def publish(instrument: str, positions: list, lot_size: int) -> None:
    """Write this instrument's current open risk. Never raises."""
    try:
        risk = sum(position_risk(p, lot_size) for p in (positions or []))
        body = dict(instrument=instrument, risk=round(risk, 2),
                    n=len(positions or []), ts=time.time(),
                    date=datetime.now().strftime('%Y-%m-%d'))
        os.makedirs(_dir(), exist_ok=True)
        tmp = _path(instrument) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(body, fh)
        os.replace(tmp, _path(instrument))
    except Exception:
        pass


def others_open_risk(instrument: str, instruments=None) -> tuple[float, int]:
    """(rupees at risk across the OTHER instruments, how many files counted)."""
    total, seen = 0.0, 0
    today = datetime.now().strftime('%Y-%m-%d')
    names = list(instruments if instruments is not None
                 else getattr(config, 'INSTRUMENTS', {}).keys())
    for other in names:
        if other == instrument:
            continue
        try:
            p = _path(other)
            if not os.path.exists(p):
                continue
            if time.time() - os.path.getmtime(p) > HEAT_MAX_AGE_SEC:
                continue
            with open(p, encoding='utf-8') as fh:
                d = json.load(fh)
            if d.get('date') != today:
                continue
            total += max(float(d.get('risk') or 0), 0.0)
            seen += 1
        except Exception:
            continue
    return total, seen


def headroom(instrument: str, total_book: float):
    """Rupees of NEW risk this instrument may add before total heat hits the cap.

    Returns None when the cap is disabled or unreadable, meaning 'no limit'.
    """
    try:
        if not getattr(config, 'PORTFOLIO_HEAT_ENABLED', False) or total_book <= 0:
            return None
        cap = float(getattr(config, 'PORTFOLIO_HEAT_PCT', 0.06)) * float(total_book)
        used, _ = others_open_risk(instrument)
        return max(cap - used, 0.0)
    except Exception:
        return None
