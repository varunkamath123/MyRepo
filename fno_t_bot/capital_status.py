"""capital_status.py — Track cumulative live P&L toward SENSEX live-trading threshold.

Usage:
    python capital_status.py              # summary view (JSONL-based)
    python capital_status.py --detail     # show individual trades
    python capital_status.py --month 2026-04   # filter to one month
    python capital_status.py --fyers      # fetch live balance from Fyers API (ground truth)
    python capital_status.py --fyers --detail  # Fyers balance + trade breakdown

What it does:
    Reads FnO_T_Bot_{NIFTY|BANKNIFTY}_*.log files from the logs directory
    and parses EXIT lines (excluding PATH-D PAPER and CHALLENGER trades)
    to compute cumulative live P&L.  Also reads dated JSONL files if present.

    --fyers mode additionally fetches the live Fyers account balance via API,
    which is the definitive ground truth (handles paper/live mode splits that
    the JSONL cannot distinguish).

SENSEX goes live when:
    NIFTY live P&L + BANKNIFTY live P&L  >=  Rs 23,000
    (i.e. combined capital Rs 52k -> Rs 75k)
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

# ── Locate logs directory ──────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))

try:
    import config as _cfg
    LOG_DIR          = os.path.join(_HERE, _cfg.LOG_DIRECTORY)
    THRESHOLD        = _cfg.SENSEX_LIVE_THRESHOLD      # 75_000
    START_CAPITAL    = _cfg.SENSEX_LIVE_START_CAPITAL  # 52_000
    NEEDED_PNL       = THRESHOLD - START_CAPITAL        # 23_000
    BOT_NAME         = _cfg.BOT_NAME
    LIVE_SWITCH_DATE = getattr(_cfg, 'LIVE_SWITCH_DATE', '2026-04-06')
except (ImportError, AttributeError):
    LOG_DIR          = os.path.join(_HERE, 'logs')
    THRESHOLD        = 75_000
    START_CAPITAL    = 52_000
    NEEDED_PNL       = 23_000
    BOT_NAME         = 'FnO_T_Bot'
    LIVE_SWITCH_DATE = '2026-04-06'

# ── CLI flags (only meaningful when run as a script) ──────────────────────────
# When imported as a module (by capital_gate.py etc.) these stay at their
# neutral defaults so none of the filter logic fires inside the functions.
DETAIL      = False
MONTH_F     = None
USE_FYERS   = False
if __name__ == '__main__':
    DETAIL     = '--detail' in sys.argv
    USE_FYERS  = '--fyers' in sys.argv
    for _i, _a in enumerate(sys.argv[1:], 1):
        if _a.startswith('--month'):
            if '=' in _a:
                MONTH_F = _a.split('=', 1)[1]
            elif _i < len(sys.argv) - 1:
                MONTH_F = sys.argv[_i + 1]


# ── Fyers live balance fetch ───────────────────────────────────────────────────

def _fetch_fyers_balance() -> dict:
    """
    Connect to Fyers API using the saved token and return fund details.

    Returns dict with keys:
      ok            : bool — False if connection failed
      total_balance : float
      available     : float
      utilized      : float
      realised_pnl  : float  (intraday, resets each day)
      limit_at_start: float  (SOD balance — yesterday's closing equity)
      error         : str | None
    """
    result = {
        'ok': False, 'total_balance': 0.0, 'available': 0.0,
        'utilized': 0.0, 'realised_pnl': 0.0, 'limit_at_start': 0.0,
        'error': None,
    }
    try:
        from fyers_apiv3 import fyersModel
        from fyers_auth import FyersAuth

        token_file = os.path.join(LOG_DIR, 'token.txt')
        if not os.path.exists(token_file):
            result['error'] = 'token.txt not found'
            return result

        with open(token_file) as f:
            lines = f.read().strip().split('\n')
        if not lines:
            result['error'] = 'token.txt is empty'
            return result

        auth = FyersAuth()
        auth.access_token = lines[0]

        fyers = fyersModel.FyersModel(
            client_id=auth.app_id,
            token=auth.access_token,
            log_path='/tmp/',
        )

        resp = fyers.funds()
        if resp.get('s') != 'ok':
            result['error'] = f"Fyers API error: {resp.get('message', resp)}"
            return result

        fund_limit = {item['title']: item['equityAmount']
                      for item in resp.get('fund_limit', [])}

        result['ok']             = True
        result['total_balance']  = fund_limit.get('Total Balance',         0.0)
        result['available']      = fund_limit.get('Available Balance',     0.0)
        result['utilized']       = fund_limit.get('Utilized Amount',       0.0)
        result['realised_pnl']   = fund_limit.get('Realized Profit and Loss', 0.0)
        result['limit_at_start'] = fund_limit.get('Limit at start of the day', 0.0)

    except Exception as e:
        result['error'] = str(e)

    return result

# ── Parser: extract live EXIT lines from .log files ───────────────────────────
# Live EXIT line format (not PAPER, not CHALLENGER):
#   2026-03-13 14:34:47,607 [INFO] ❌ EXIT  CALL | EOD Force-Close (14:30) | P&L: Rs-1,097.11 (-12.5%) | Capital: Rs 48,902.89
_EXIT_RE = re.compile(
    r'(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}.*'
    r'\[INFO\].*EXIT\s+(CALL|PUT)\s+\|'
    r'.*P&L:\s*[^\d+-]*([+-]?[\d,]+\.?\d*)\s*\('
)

def _parse_log_file(fp: str) -> list[dict]:
    """Extract live trade exit records from a daily .log file."""
    trades = []
    try:
        with open(fp, encoding='utf-8', errors='replace') as f:
            for line in f:
                # Exclude PATH-D PAPER and CHALLENGER lines
                if '[PATH-D PAPER]' in line or '[CHALLENGER]' in line:
                    continue
                # Must contain EXIT
                if ' EXIT ' not in line:
                    continue
                # Must be [INFO] level
                if '[INFO]' not in line:
                    continue
                m = _EXIT_RE.search(line)
                if not m:
                    continue
                date_str = m.group(1)
                # Month filter
                if MONTH_F and not date_str.startswith(MONTH_F):
                    continue
                direction = m.group(2)
                pnl_str   = m.group(3).replace(',', '').replace(' ', '')
                try:
                    pnl = float(pnl_str)
                except ValueError:
                    continue
                trades.append({
                    'date'     : date_str,
                    'type'     : direction,
                    'pnl_net'  : pnl,
                    'mode'     : 'live',
                    'source'   : os.path.basename(fp),
                })
    except OSError:
        pass
    return trades


def _load_from_logs(instrument: str) -> list[dict]:
    """Load all live trades for an instrument from daily .log files."""
    pattern = os.path.join(LOG_DIR, f'{BOT_NAME}_{instrument}_*.log')
    files   = sorted(glob.glob(pattern))
    trades  = []
    seen    = set()   # deduplicate by (date, pnl) in case restarts cause double-logging
    for fp in files:
        for t in _parse_log_file(fp):
            key = (t['date'], t['pnl_net'])
            if key not in seen:
                seen.add(key)
                trades.append(t)
    return trades


def _load_from_jsonl(instrument: str) -> list[dict]:
    """Load live trades from dated JSONL files (new format, post-fix)."""
    pattern = os.path.join(LOG_DIR, f'{BOT_NAME}_{instrument}_trades_*.jsonl')
    files   = sorted(glob.glob(pattern))
    trades  = []
    for fp in files:
        if MONTH_F and MONTH_F not in os.path.basename(fp):
            continue
        try:
            with open(fp, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        t = json.loads(line)
                        if t.get('mode') == 'live':
                            trades.append(t)
                    except json.JSONDecodeError:
                        pass
        except OSError:
            pass
    return trades


def _load_trades(instrument: str) -> list[dict]:
    """Load live trades, filtered to >= LIVE_SWITCH_DATE.

    Merges log-file and JSONL records so nothing is missed.
    JSONL is the authoritative (richer) source; log-file records fill any gaps
    (e.g. legacy services whose logs predate the JSONL format).
    Dedup by (date, type, rounded pnl_net) to avoid double-counting when both
    sources record the same trade.
    """
    log_trades   = _load_from_logs(instrument)
    jsonl_trades = _load_from_jsonl(instrument)

    # Start with JSONL records (richer fields).
    # Dedup WITHIN the JSONL list too: the bot's shutdown handler used to
    # re-append the last trade on every restart, so files can contain exact
    # duplicate lines. Key on (entry_time, type, strike) — same key the bot
    # itself uses in _load_today_trades().
    merged      = []
    seen_keys: set = set()
    _seen_jsonl: set = set()
    for t in jsonl_trades:
        jk = (t.get('entry_time', ''), t.get('type', ''), str(t.get('strike', '')))
        if jk in _seen_jsonl:
            continue
        _seen_jsonl.add(jk)
        merged.append(t)
        dt = t.get('date', t.get('exit_time', ''))[:10]
        seen_keys.add((dt, t.get('type', ''), round(t.get('pnl_net', 0))))

    # Add any log-file records not already covered by JSONL
    for t in log_trades:
        dt = t.get('date', '')[:10]
        k  = (dt, t.get('type', ''), round(t.get('pnl_net', 0)))
        if k not in seen_keys:
            merged.append(t)
            seen_keys.add(k)

    # Filter to live trades on or after the live switch date
    return [t for t in merged
            if t.get('date', t.get('exit_time', ''))[:10] >= LIVE_SWITCH_DATE]


# ── Aggregate ─────────────────────────────────────────────────────────────────
def _stats(trades: list[dict]) -> dict:
    if not trades:
        return {'n': 0, 'wins': 0, 'pnl': 0.0, 'best': 0.0, 'worst': 0.0}
    pnls = [t.get('pnl_net', 0.0) for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    return {
        'n'    : len(trades),
        'wins' : wins,
        'pnl'  : sum(pnls),
        'best' : max(pnls),
        'worst': min(pnls),
    }


def _bar(pct: float, width: int = 30) -> str:
    """Progress bar helper."""
    filled = int(min(max(pct, 0), 100) / 100 * width)
    return '[' + '#' * filled + '-' * (width - filled) + ']'


def _wr(s: dict) -> str:
    return f'{s["wins"]}/{s["n"]}' if s['n'] else '0/0'


# ── Script entry-point ────────────────────────────────────────────────────────
# All print / computation code is guarded here so the module can be imported
# cleanly by capital_gate.py without any side-effects.
if __name__ == '__main__':
    nifty_live = _load_trades('NIFTY')
    bnf_live   = _load_trades('BANKNIFTY')

    ns = _stats(nifty_live)
    bs = _stats(bnf_live)

    combined_pnl      = ns['pnl'] + bs['pnl']
    combined_capital  = START_CAPITAL + combined_pnl   # JSONL-derived

    # ── Fyers live balance (ground truth) ─────────────────────────────────────
    fyers_data        = None
    fyers_capital     = None   # set if --fyers succeeds
    if USE_FYERS:
        print('\n  Fetching live balance from Fyers API...', end='', flush=True)
        fyers_data = _fetch_fyers_balance()
        if fyers_data['ok']:
            fyers_capital = fyers_data['total_balance']
            print(f' ✓  Rs {fyers_capital:,.2f}')
        else:
            print(f' ✗  {fyers_data["error"]}')

    # Capital to use for gate decisions: Fyers if available, else JSONL estimate
    gate_capital      = fyers_capital if fyers_capital is not None else combined_capital
    gate_pnl          = gate_capital - START_CAPITAL
    pct_done          = gate_pnl / NEEDED_PNL * 100 if NEEDED_PNL else 100.0

    month_note = f'  (filtered to {MONTH_F})' if MONTH_F else ''
    print()
    print('=' * 66)
    print('  Capital Gate Tracker' + month_note)
    print('=' * 66)
    print()
    print(f'  Live trading start                  : {LIVE_SWITCH_DATE}')
    print(f'  Start capital (NF + BNF combined)  : Rs {START_CAPITAL:>10,.0f}')
    print(f'  SENSEX threshold                    : Rs {THRESHOLD:>10,.0f}')
    print(f'  Required net P&L for SENSEX         : Rs {NEEDED_PNL:>10,.0f}')
    print()
    print('  -- Live P&L by instrument (JSONL)' + '-' * 29)
    print(f'  NIFTY     {ns["n"]:3d} trades  WR {_wr(ns):5s}  '
          f'Net {"+" if ns["pnl"]>=0 else ""}Rs {ns["pnl"]:>10,.0f}  '
          f'(best Rs {ns["best"]:+,.0f} / worst Rs {ns["worst"]:+,.0f})')
    print(f'  BANKNIFTY {bs["n"]:3d} trades  WR {_wr(bs):5s}  '
          f'Net {"+" if bs["pnl"]>=0 else ""}Rs {bs["pnl"]:>10,.0f}  '
          f'(best Rs {bs["best"]:+,.0f} / worst Rs {bs["worst"]:+,.0f})')
    print('  ' + '-' * 58)
    print(f'  JSONL combined P&L                  : '
          f'{"+" if combined_pnl>=0 else ""}Rs {combined_pnl:>10,.0f}')
    print(f'  JSONL capital estimate              : Rs {combined_capital:>10,.0f}')

    # Fyers section (only printed when --fyers was requested)
    if fyers_data is not None:
        print()
        if fyers_data['ok']:
            gap      = fyers_capital - combined_capital
            gap_sign = '+' if gap >= 0 else ''
            print(f'  -- Fyers live account' + '-' * 41)
            print(f'  Total balance (Fyers)               : Rs {fyers_capital:>10,.2f}  ← ground truth')
            print(f'  Available balance                   : Rs {fyers_data["available"]:>10,.2f}')
            print(f'  Utilized (open margin)              : Rs {fyers_data["utilized"]:>10,.2f}')
            print(f'  Intraday realised P&L               : Rs {fyers_data["realised_pnl"]:>10,.2f}')
            print(f'  SOD limit (prev close equity)       : Rs {fyers_data["limit_at_start"]:>10,.2f}')
            print(f'  Gap vs JSONL estimate               : {gap_sign}Rs {gap:>9,.2f}')
            if abs(gap) > 500:
                print(f'  NOTE: gap > Rs 500 — likely paper-mode trades in JSONL')
            fyers_pnl = fyers_capital - START_CAPITAL
            print(f'  True P&L since {LIVE_SWITCH_DATE}          : '
                  f'{"+" if fyers_pnl>=0 else ""}Rs {fyers_pnl:>10,.2f}')
        else:
            print(f'  Fyers fetch failed: {fyers_data["error"]}')
            print(f'  Using JSONL estimate as fallback.')

    print()
    print(f'  Progress to SENSEX: {_bar(pct_done)}  {pct_done:.1f}%')

    # ── Capital gate status (uses Fyers balance when available) ───────────────
    try:
        import config as _cfg2
        _bnf_thresh    = getattr(_cfg2, 'CAPITAL_GATE_BNF_LIVE',   50_000)
        _sensex_thresh = getattr(_cfg2, 'CAPITAL_GATE_SENSEX_LIVE', 75_000)
        _force_bnf     = getattr(_cfg2, 'FORCE_BNF_LIVE',    False)
        _force_sensex  = getattr(_cfg2, 'FORCE_SENSEX_LIVE', False)
        src_label      = '← Fyers' if fyers_capital is not None else '← JSONL estimate'
        print()
        print('  -- Capital gate status --')
        print(f'  Capital used                        : Rs {gate_capital:>10,.0f}  {src_label}')
        print(f'  NIFTY     : always LIVE')
        if _force_bnf:
            _bnf_mode  = 'LIVE (FORCE override)'
        else:
            _bnf_mode  = 'LIVE' if gate_capital >= _bnf_thresh else f'PAPER  (need Rs {_bnf_thresh:,.0f}, have Rs {gate_capital:,.0f})'
        print(f'  BANKNIFTY : {_bnf_mode}')
        if _force_sensex:
            _sx_mode   = 'LIVE (FORCE override)'
        else:
            _sx_mode   = 'LIVE' if gate_capital >= _sensex_thresh else f'PAPER  (need Rs {_sensex_thresh:,.0f})'
        print(f'  SENSEX    : {_sx_mode}')
    except Exception:
        pass

    if gate_pnl >= NEEDED_PNL:
        print()
        print('  STATUS: SENSEX THRESHOLD MET -- SENSEX IS READY TO GO LIVE')
    else:
        still_needed = NEEDED_PNL - gate_pnl
        print()
        print(f'  STATUS: Still needs Rs {still_needed:,.0f} more P&L for SENSEX')
        if ns['n'] + bs['n'] > 0:
            avg_per_trade = combined_pnl / (ns['n'] + bs['n'])
            if avg_per_trade > 0:
                trades_needed = int(still_needed / avg_per_trade) + 1
                print(f'         At current avg Rs {avg_per_trade:,.0f}/trade '
                      f'~ {trades_needed} more trades needed')
            else:
                print(f'         Current avg P&L/trade is negative')

    print()

    # ── Detail view ───────────────────────────────────────────────────────────
    if DETAIL:
        print('-' * 66)
        print('  Individual live trades (JSONL)')
        print('-' * 66)
        all_live = sorted(nifty_live + bnf_live,
                          key=lambda t: (t.get('date', ''), t.get('exit_time', '')))
        cum = 0.0
        for t in all_live:
            pnl  = t.get('pnl_net', 0.0)
            cum += pnl
            icon = '+' if pnl >= 0 else '-'
            date = t.get('exit_time', t.get('date', '?'))[:10]
            reason = t.get('exit_reason', t.get('source', '?'))
            mode   = t.get('mode', 'live')
            flag   = ' [PAPER?]' if mode != 'live' else ''
            print(f'  {date}  {t.get("instrument", "?"):10s}  '
                  f'{t.get("type","?"):4s}  '
                  f'{icon}Rs {abs(pnl):>8,.0f}  '
                  f'[{reason[:20]:20s}]  '
                  f'cum Rs {cum:>+10,.0f}{flag}')
        print()
