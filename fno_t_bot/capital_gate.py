"""
capital_gate.py — Capital-threshold-based instrument gating.

Rules (config.py CAPITAL_GATE_* params):
  NIFTY     : always live — anchor instrument, highest WR (77%)
  BANKNIFTY : live only when combined NF+BNF capital >= CAPITAL_GATE_BNF_LIVE (Rs50k)
  SENSEX    : live only when combined capital >= CAPITAL_GATE_SENSEX_LIVE (Rs75k)

Combined capital = SENSEX_LIVE_START_CAPITAL + cumulative live NF+BNF P&L
                   since LIVE_SWITCH_DATE (same definition as capital_status.py).

Used by options_bot.py TradingBot.__init__() to override INSTRUMENT_STRATEGY
live_mode at startup.  The gate re-evaluates every time the bot (re)starts —
so after auth refresh at 08:45, the correct mode is applied for the day.
"""
from __future__ import annotations

import logging

import config
from capital_status import _load_trades, _stats

_log = logging.getLogger(__name__)


def get_combined_live_capital() -> float:
    """
    Read all NF + BNF live trade JSONL/log files and return:
        START_CAPITAL + cumulative_live_pnl

    This is the same figure shown by capital_status.py.
    Returns START_CAPITAL if no trades found (safe floor).
    """
    try:
        nf_trades  = _load_trades('NIFTY')
        bnf_trades = _load_trades('BANKNIFTY')
        ns = _stats(nf_trades)
        bs = _stats(bnf_trades)
        combined_pnl = ns['pnl'] + bs['pnl']
        return config.SENSEX_LIVE_START_CAPITAL + combined_pnl
    except Exception as exc:
        _log.warning(f'[CAPITAL-GATE] Could not read capital: {exc} — defaulting to START_CAPITAL')
        return float(config.SENSEX_LIVE_START_CAPITAL)


_PNL_CACHE = {}


def _closed_pnls(instrument: str, since: str | None = None) -> list:
    """Closed-trade net P&L for one instrument, oldest first.

    Reads the per-session trade logs the bot already writes, so the book
    survives restarts and is shared by the three bot processes with no new state
    to corrupt. BS-priced fills are excluded: they are modelled, not observed,
    and compounding on fiction would size real money off a guess.

    Cached for a minute -- it runs once per entry, not per tick.
    """
    import glob, json, os, re, time
    ld = getattr(config, 'LOG_DIRECTORY', 'logs')
    key = (instrument, since, ld)
    hit = _PNL_CACHE.get(key)
    if hit and (time.time() - hit[0]) < 60:
        return hit[1]
    out = []
    try:
        files = []
        for f in glob.glob(os.path.join(ld, f'*_{instrument}_trades_*.jsonl')):
            m = re.search(r'_trades_(\d{4}-\d{2}-\d{2})', os.path.basename(f))
            if m and not (since and m.group(1) < since):
                files.append((m.group(1), f))
        for _day, f in sorted(files):
            with open(f, encoding='utf-8', errors='ignore') as fh:
                for ln in fh:
                    try:
                        d = json.loads(ln)
                    except Exception:
                        continue
                    if d.get('pnl_net') is None or d.get('px_src') == 'BS':
                        continue
                    out.append(float(d['pnl_net']))
    except Exception:
        out = []
    _PNL_CACHE[key] = (time.time(), out)
    return out


def realised_pnl(instrument: str, since: str | None = None) -> float:
    """Sum of closed-trade net P&L for one instrument since a date."""
    return float(sum(_closed_pnls(instrument, since)))


def drawdown_state(instrument: str) -> tuple:
    """(drawdown fraction, risk multiplier) for one instrument's book.

    Drawdown is measured against the book's own running high-water mark, built
    from the same closed-trade stream the compounding book uses, starting from
    the configured capital:
        dd = (peak_equity - equity) / peak_equity

    The multiplier de-levers linearly as drawdown deepens:
        mult = clamp(1 - dd / DD_THROTTLE_FULL, DD_THROTTLE_MIN_MULT, 1)
    so with the defaults (full 25%, floor 0.25) a book 12.5% off its peak risks
    half the usual fraction, and one 25%+ off risks a quarter.

    This sits ON TOP of compounding, which already shrinks size as equity falls.
    Together they de-lever roughly quadratically in a drawdown. That is the
    intent: the cost of a drawdown is that recovery needs more than the loss, so
    risk should come down faster than equity does.
    """
    try:
        if not getattr(config, 'DD_THROTTLE_ENABLED', False):
            return 0.0, 1.0
        base = None
        per = (config.INSTRUMENT_STRATEGY.get(instrument, {}) or {}).get('capital')
        if per:
            base = float(per)
        if not base:
            return 0.0, 1.0
        eq = peak = base
        for p in _closed_pnls(instrument, getattr(config, 'COMPOUND_FROM_DATE', None)):
            eq += p
            peak = max(peak, eq)
        dd = max((peak - eq) / peak, 0.0) if peak > 0 else 0.0
        full = max(float(getattr(config, 'DD_THROTTLE_FULL', 0.25)), 1e-9)
        lo = float(getattr(config, 'DD_THROTTLE_MIN_MULT', 0.25))
        return dd, min(1.0, max(lo, 1.0 - dd / full))
    except Exception:
        return 0.0, 1.0



def get_sizing_book(instrument: str | None = None) -> float:
    """Equity used to SIZE a position: starting capital plus realised P&L.

    Separate from get_combined_live_capital() on purpose. That function returns
    SENSEX_LIVE_START_CAPITAL plus cumulative NIFTY/BANKNIFTY P&L -- a July 2026
    constant eroded by paper losses -- and resolve_live_mode() depends on it to
    decide live-vs-paper. Sizing off its own book keeps a capital change from
    silently flipping an instrument into live mode.

    History of this function:
      Oct 13 2026  TOTAL_CAPITAL was raised to Rs300,000 and sizing never saw it:
                   it read Rs37,230 and issued a Rs3,723 risk cap.
      Oct 21 2026  configured capital was read, but LOT_SIZING_MODE was 'fixed',
                   so the bot traded 1 lot at every account size from Rs1L to
                   Rs30L. Account size had no effect on anything.
      Oct 21 2026  this version: starting capital COMPOUNDS with realised P&L, so
                   the book grows after wins and shrinks after losses.

    With COMPOUND_ENABLED the book is
        start + realised P&L since COMPOUND_FROM_DATE,
    floored at COMPOUND_FLOOR_PCT x start so a bad run cannot size the book to
    zero. The floor is a safety net for the arithmetic, NOT a stop: below it the
    book simply stops shrinking.
    """
    try:
        base = None
        if instrument:
            per = (config.INSTRUMENT_STRATEGY.get(instrument, {}) or {}).get('capital')
            if per:
                base = float(per)
        if base is None:
            tot = getattr(config, 'TOTAL_CAPITAL', None)
            if tot:
                n = max(1, len([i for i in getattr(config, 'INSTRUMENTS', {})]))
                base = float(tot) / n
        if base is not None:
            if instrument and getattr(config, 'COMPOUND_ENABLED', False):
                pnl = realised_pnl(instrument,
                                   getattr(config, 'COMPOUND_FROM_DATE', None))
                floor = base * float(getattr(config, 'COMPOUND_FLOOR_PCT', 0.25))
                return max(base + pnl, floor)
            return base
    except Exception:
        pass
    return float(getattr(config, 'SENSEX_LIVE_START_CAPITAL', 50_000))



def get_risk_params(logger: logging.Logger | None = None,
                    instrument: str | None = None) -> tuple:
    """
    v1.7: capital-scaled per-trade risk sizing.

    Returns (risk_cap_rs, max_lots, book_rs):
      risk_cap = max(MAX_RISK_FLOOR, book × MAX_RISK_PCT_OF_BOOK)
      max_lots = highest DYN_MAX_LOTS_LADDER tier whose threshold ≤ book

    Rationale: the ₹5,000 static cap ≈ full-Kelly on the June live cohort at a
    ₹50k book (10%). Scaling with the book keeps risk at the same fraction as
    capital grows (₹75k → ₹7.5k cap / 3 lots; ₹1L → ₹10k / 4 lots) and shrinks
    it in drawdown (anti-martingale). Falls back to the static
    MAX_RISK_PER_TRADE / DYN_MAX_LOTS when the book is unreadable.

    Known limitation: book = SENSEX_LIVE_START_CAPITAL + NF/BNF JSONL P&L —
    SENSEX trades after Jul 8 2026 are not counted (drift is small; the
    baseline gets recalibrated against the Fyers balance periodically).
    """
    log = logger or _log
    try:
        book  = get_sizing_book(instrument)
        pct   = getattr(config, 'MAX_RISK_PCT_OF_BOOK', 0.10)
        floor = getattr(config, 'MAX_RISK_FLOOR', 2500)
        cap   = max(float(floor), book * pct)
        # ABSOLUTE ceiling only. The real constraint is the risk budget applied
        # to the ACTUAL premium, computed per entry in
        # TradingBot._affordable_lots(). DYN_MAX_LOTS_LADDER used to be the
        # binding ceiling and it stopped at 4 lots for any book >= Rs1L, so
        # Rs3L, Rs30L and Rs3cr all resolved to the same 4 -- a lookup table
        # that simply ran out. It is kept as a hard safety stop, nothing more.
        max_lots = int(getattr(config, 'ABSOLUTE_MAX_LOTS', 50))
        return cap, max_lots, book
    except Exception as exc:
        log.warning(f'[RISK-SCALE] book read failed ({exc}) — using static fallback')
        return (float(getattr(config, 'MAX_RISK_PER_TRADE', 5000)),
                int(getattr(config, 'DYN_MAX_LOTS', 2)),
                None)


def resolve_live_mode(instrument: str,
                      logger: logging.Logger | None = None) -> bool:
    """
    Return True (live) or False (paper) for the given instrument based on
    current combined capital vs configured thresholds.

    If CAPITAL_GATE_ENABLED is False, falls back to the manual live_mode
    flag in INSTRUMENT_STRATEGY (no change from old behaviour).

    Thresholds (config.py):
      CAPITAL_GATE_BNF_LIVE    = 50_000   BNF goes live at Rs50k combined
      CAPITAL_GATE_SENSEX_LIVE = 75_000   SENSEX goes live at Rs75k combined

    NIFTY is always live regardless of capital (anchor instrument).
    """
    log = logger or _log

    # Gate disabled → use manual config flag
    if not getattr(config, 'CAPITAL_GATE_ENABLED', False):
        manual = config.INSTRUMENT_STRATEGY.get(instrument, {}).get('live_mode', False)
        log.info(f'[CAPITAL-GATE] disabled — {instrument} using manual flag: '
                 f'{"LIVE" if manual else "PAPER"}')
        return manual

    capital = get_combined_live_capital()

    if instrument == 'NIFTY':
        log.info(f'[CAPITAL-GATE] {instrument}: always LIVE (anchor) | '
                 f'combined capital Rs{capital:,.0f}')
        return True

    elif instrument == 'BANKNIFTY':
        # Manual override: bypass capital gate when FORCE_BNF_LIVE is set
        if getattr(config, 'FORCE_BNF_LIVE', False):
            log.info(f'[CAPITAL-GATE] {instrument}: LIVE (FORCE_BNF_LIVE override) | '
                     f'capital Rs{capital:,.0f} — Rs{getattr(config, "CAPITAL_GATE_BNF_LIVE", 50_000):,.0f} '
                     f'threshold bypassed manually')
            return True
        thresh = getattr(config, 'CAPITAL_GATE_BNF_LIVE', 50_000)
        live   = capital >= thresh
        status = 'LIVE' if live else 'PAPER'
        gap    = thresh - capital
        msg    = (f'Rs{capital:,.0f} >= Rs{thresh:,.0f}'
                  if live else
                  f'Rs{capital:,.0f} < Rs{thresh:,.0f} (need Rs{gap:,.0f} more)')
        log.info(f'[CAPITAL-GATE] {instrument}: {status} | {msg}')
        return live

    elif instrument == 'SENSEX':
        # Manual override: bypass capital gate when FORCE_SENSEX_LIVE is set
        if getattr(config, 'FORCE_SENSEX_LIVE', False):
            log.info(f'[CAPITAL-GATE] {instrument}: LIVE (FORCE_SENSEX_LIVE override) | '
                     f'capital Rs{capital:,.0f} — Rs{getattr(config, "CAPITAL_GATE_SENSEX_LIVE", 75_000):,.0f} '
                     f'threshold bypassed manually')
            return True
        thresh = getattr(config, 'CAPITAL_GATE_SENSEX_LIVE', 75_000)
        live   = capital >= thresh
        status = 'LIVE' if live else 'PAPER'
        gap    = thresh - capital
        msg    = (f'Rs{capital:,.0f} >= Rs{thresh:,.0f}'
                  if live else
                  f'Rs{capital:,.0f} < Rs{thresh:,.0f} (need Rs{gap:,.0f} more)')
        log.info(f'[CAPITAL-GATE] {instrument}: {status} | {msg}')
        return live

    # Unknown instrument — default to paper (safe)
    log.warning(f'[CAPITAL-GATE] Unknown instrument {instrument} — defaulting PAPER')
    return False
