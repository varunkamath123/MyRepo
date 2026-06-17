"""
shared_state.py — Consolidated daily P&L tracker for FnO_T_Bot.

All bot processes (paper_bot + early_bot, 3 instruments each = up to 6 processes)
write and read a single JSON file to enforce a CONSOLIDATED daily loss cap.

Design:
  - File: /opt/trading_bot/live_bot/logs/daily_state.json  (Linux EC2)
          C:/quant_trading/live_bot/logs/daily_state.json  (Windows dev)
  - Lock: .lock file alongside state file (fcntl on Linux, fallback on Windows)
  - Auto-reset: every read checks today's date and resets if stale

Thread/process safety: file lock ensures single-writer access.
"""

from __future__ import annotations

import json
import os
import time
import logging
from datetime import date, datetime
from pathlib import Path

import pytz
import config

IST = pytz.timezone('Asia/Kolkata')

# ─── Locate state file ────────────────────────────────────────────────────────

_STATE_FILE = Path(os.path.join(os.path.dirname(__file__),
                                config.LOG_DIRECTORY, 'daily_state.json'))
_LOCK_FILE  = _STATE_FILE.with_suffix('.lock')

# ─── Internal helpers ─────────────────────────────────────────────────────────

def _today_str() -> str:
    return datetime.now(IST).strftime('%Y-%m-%d')


def _acquire_lock(timeout: float = 5.0) -> bool:
    """
    Best-effort cross-platform file lock.
    Returns True if acquired, False on timeout.
    On Linux: uses fcntl.flock (atomic).
    On Windows: simple flag-file spin-lock (good enough for seconds-scale latency).
    """
    deadline = time.monotonic() + timeout
    try:
        import fcntl
        fd = open(_LOCK_FILE, 'w')
        while time.monotonic() < deadline:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd          # caller must close to release
            except BlockingIOError:
                time.sleep(0.05)
        fd.close()
        return False
    except ImportError:
        # Windows — spin on flag file
        while time.monotonic() < deadline:
            try:
                fd = open(_LOCK_FILE, 'x')   # exclusive create
                return fd
            except FileExistsError:
                time.sleep(0.05)
        return False


def _release_lock(fd) -> None:
    if fd is False or fd is None:
        return
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
    except (ImportError, OSError):
        pass
    try:
        fd.close()
    except OSError:
        pass
    try:
        _LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _read_raw() -> dict:
    """Read state file; return empty dict if missing/corrupt."""
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_raw(state: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_FILE.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2)
    tmp.replace(_STATE_FILE)


def _fresh_state() -> dict:
    return {
        'date'        : _today_str(),
        'instruments' : {},   # instrument -> {bots: {bot_id: pnl}, total: pnl,
                              #                open_positions: {bot_id: {type, registered_at}}}
        'grand_total' : 0.0,
        'sgx'         : {},   # {price, change_pct, direction, wide_gap, strong_gap, fetched_at}
    }


def _ensure_today(state: dict) -> dict:
    """Return state reset to today if the date has rolled over."""
    if state.get('date') != _today_str():
        return _fresh_state()
    return state

# ─── Public API ───────────────────────────────────────────────────────────────

def update_pnl(instrument: str, bot_id: str, delta_pnl: float,
               logger: logging.Logger | None = None) -> float:
    """
    Add delta_pnl (negative for a loss) for the given instrument + bot_id.
    Returns the new consolidated grand_total for today.

    bot_id examples: 'MAIN_NIFTY', 'EARLY_NIFTY', 'MAIN_BANKNIFTY', ...
    """
    fd = _acquire_lock()
    if fd is False:
        if logger:
            logger.warning('[SHARED_STATE] Could not acquire lock — skipping P&L update')
        return 0.0
    try:
        state = _ensure_today(_read_raw())
        inst_data = state['instruments'].setdefault(
            instrument, {'bots': {}, 'total': 0.0}
        )
        old  = inst_data['bots'].get(bot_id, 0.0)
        inst_data['bots'][bot_id] = old + delta_pnl
        inst_data['total'] = sum(inst_data['bots'].values())

        state['grand_total'] = sum(
            v['total'] for v in state['instruments'].values()
        )
        _write_raw(state)
        return state['grand_total']
    finally:
        _release_lock(fd)


def get_consolidated_pnl() -> float:
    """Return today's consolidated P&L across all instruments and bots."""
    state = _ensure_today(_read_raw())
    return state.get('grand_total', 0.0)


def get_instrument_pnl(instrument: str) -> float:
    """Return today's P&L for one instrument across all bots."""
    state = _ensure_today(_read_raw())
    return state.get('instruments', {}).get(instrument, {}).get('total', 0.0)


def is_consolidated_loss_exceeded(
        cap: float | None = None,
        logger: logging.Logger | None = None) -> bool:
    """
    Return True if today's consolidated loss has breached the cap.
    Uses config.CONSOLIDATED_DAILY_LOSS_CAP when cap is None.
    Losses are negative — breach when grand_total <= -cap.
    """
    limit = cap if cap is not None else config.CONSOLIDATED_DAILY_LOSS_CAP
    grand = get_consolidated_pnl()
    breached = grand <= -abs(limit)
    if breached and logger:
        logger.warning(
            f'[SHARED_STATE] ⛔ Consolidated daily loss cap hit: '
            f'₹{grand:,.2f} (limit -₹{limit:,.0f}). No new entries today.'
        )
    return breached


def register_position(instrument: str, bot_id: str, opt_type: str,
                      logger: logging.Logger | None = None) -> None:
    """
    Register an open position so other bots (e.g. paper_bot) can see it.
    Called by early_bot on entry. Cleared by clear_position() on close.

    Stored under instruments[instrument]['open_positions'][bot_id].
    """
    fd = _acquire_lock()
    if fd is False:
        if logger:
            logger.warning('[SHARED_STATE] Could not acquire lock — skipping position register')
        return
    try:
        state = _ensure_today(_read_raw())
        inst  = state['instruments'].setdefault(instrument, {'bots': {}, 'total': 0.0})
        inst.setdefault('open_positions', {})[bot_id] = {
            'type'         : opt_type,
            'registered_at': datetime.now(IST).strftime('%H:%M:%S'),
        }
        _write_raw(state)
        if logger:
            logger.info(f'[SHARED_STATE] Registered open {opt_type} position: {instrument}/{bot_id}')
    finally:
        _release_lock(fd)


def clear_position(instrument: str, bot_id: str,
                   logger: logging.Logger | None = None) -> None:
    """
    Clear a registered open position on close.
    Called by early_bot check_exits() when a position is closed.
    """
    fd = _acquire_lock()
    if fd is False:
        return
    try:
        state = _ensure_today(_read_raw())
        inst  = state['instruments'].get(instrument, {})
        inst.get('open_positions', {}).pop(bot_id, None)
        _write_raw(state)
        if logger:
            logger.info(f'[SHARED_STATE] Cleared position: {instrument}/{bot_id}')
    finally:
        _release_lock(fd)


def get_open_positions(instrument: str) -> dict:
    """
    Return {bot_id: {type, registered_at}} for all bots holding a position
    on this instrument. Empty dict = no open positions.
    """
    state = _ensure_today(_read_raw())
    return (state.get('instruments', {})
                 .get(instrument, {})
                 .get('open_positions', {}))


def has_open_position(instrument: str,
                      exclude_bot: str | None = None) -> bool:
    """
    Return True if any bot (other than exclude_bot) has an open position
    on this instrument. Used by paper_bot to block entry when early_bot is in.
    """
    positions = get_open_positions(instrument)
    if exclude_bot:
        positions = {k: v for k, v in positions.items() if k != exclude_bot}
    return bool(positions)


def set_sgx_context(ctx: dict, logger: logging.Logger | None = None) -> None:
    """
    Store today's pre-market GIFT/SGX Nifty gap context.
    Called once at startup by the NIFTY bot; other bots read via get_sgx_context().
    ctx: dict from sgx_nifty.fetch_and_analyze()
    """
    fd = _acquire_lock()
    if fd is False:
        if logger:
            logger.warning('[SHARED_STATE] Could not acquire lock — skipping SGX context write')
        return
    try:
        state = _ensure_today(_read_raw())
        state['sgx'] = ctx
        _write_raw(state)
    finally:
        _release_lock(fd)


def get_sgx_context() -> dict:
    """
    Return today's SGX gap context dict, or {} if not yet set.
    Keys: price, change_pct, direction, wide_gap, strong_gap, fetched_at
    """
    state = _ensure_today(_read_raw())
    return state.get('sgx', {})


def snapshot(logger: logging.Logger | None = None) -> str:
    """Return a human-readable one-line status for logging."""
    state = _ensure_today(_read_raw())
    gt    = state.get('grand_total', 0.0)
    parts = []
    for inst, d in sorted(state.get('instruments', {}).items()):
        parts.append(f'{inst}:₹{d["total"]:+,.0f}')
    detail = ' | '.join(parts) if parts else 'no data'
    sign   = '+' if gt >= 0 else ''
    return f'[Consolidated] Total={sign}₹{gt:,.0f} | {detail}'
