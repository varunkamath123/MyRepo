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
