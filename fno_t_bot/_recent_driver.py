"""
Direct driver: run vX backtest on last N trading days.
Called by backtest_recent.py or directly: python _recent_driver.py [N]
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
from pathlib import Path

N_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 10

BASE = Path(r'C:\quant_trading\data')
INSTRUMENT_DIRS = {
    'NIFTY'    : BASE / 'nifty_5min',
    'BANKNIFTY': BASE / 'banknifty_5min',
    'SENSEX'   : BASE / 'sensex_5min',
}

def load_recent(folder, n_days):
    files = sorted(folder.glob('*.csv'))
    # extra 2 files as buffer for prev_close lookup on day 1
    recent = files[-(n_days + 2):]
    frames = []
    for f in recent:
        try:
            df = pd.read_csv(f, index_col=0, parse_dates=True)
            frames.append(df)
        except Exception as e:
            print(f"    skip {f.name}: {e}")
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames).sort_index()
    return df[~df.index.duplicated(keep='first')]

# ── Patch sys.argv so bot.py's __main__ block does nothing ───────────────────
_orig_argv = sys.argv[:]
sys.argv   = ['bot.py', 'vX']

# Exec bot.py with __main__ guard neutralised
_bot_src = open(r'C:\quant_trading\live_bot\bot.py', encoding='utf-8').read()
_bot_src = _bot_src.replace(
    "if __name__ == '__main__':",
    "if False:  # suppressed by _recent_driver"
)
_bot_ns = {'__name__': '__bot__', '__file__': r'C:\quant_trading\live_bot\bot.py'}
exec(compile(_bot_src, 'bot.py', 'exec'), _bot_ns)

run_backtest = _bot_ns['run_backtest']
sys.argv = _orig_argv

# ── Run ───────────────────────────────────────────────────────────────────────
print(f'vX backtest — last {N_DAYS} trading days')
print(f'Data: NIFTY / BANKNIFTY / SENSEX   (Fyers 5-min)')
print('='*68)

all_trades = []
total_pnl  = 0
total_wins = 0
total_n    = 0
grand_best = 0
grand_worst = 0

for instr, folder in INSTRUMENT_DIRS.items():
    full_df = load_recent(folder, N_DAYS)
    if full_df.empty:
        print(f'  {instr}: no data found in {folder}')
        continue

    # Normalise index to tz-naive IST
    if full_df.index.tz is not None:
        full_df.index = full_df.index.tz_convert('Asia/Kolkata').tz_localize(None)

    # Find the N_DAYS most recent trading dates
    trading_dates = sorted(full_df.index.normalize().unique())
    backtest_dates = trading_dates[-N_DAYS:]
    cutoff = pd.Timestamp(backtest_dates[0])

    # Keep data from cutoff (gives run_backtest the prev_close context too if available)
    df = full_df[full_df.index >= cutoff]

    d0 = backtest_dates[0].date()
    d1 = backtest_dates[-1].date()
    print(f'\n  {instr}  ({d0} → {d1},  {len(backtest_dates)} days)')
    print(f'  {"─"*62}')

    try:
        trades, pnl, stats = run_backtest(df, instr, variant='vX')
    except Exception as e:
        import traceback
        print(f'  ERROR: {e}')
        traceback.print_exc()
        continue

    n = len(trades)
    w = sum(1 for t in trades if t.get('pnl', 0) > 0) if trades else 0
    wr = 100 * w / n if n else 0
    best  = max((t.get('pnl', 0) for t in trades), default=0)
    worst = min((t.get('pnl', 0) for t in trades), default=0)

    all_trades.extend(trades)
    total_pnl  += pnl
    total_wins += w
    total_n    += n
    grand_best  = max(grand_best,  best)
    grand_worst = min(grand_worst, worst)

    wr_sym = '✓' if wr >= 60 else ('△' if wr >= 40 else '✗')
    print(f'  Trades: {n}  WR: {w}/{n} ({wr:.0f}%)  Net: Rs {pnl:+,.0f}  '
          f'Best: Rs {best:+,.0f}  Worst: Rs {worst:+,.0f}  {wr_sym}')

    if trades:
        print(f'  {"Date/Time":<18} {"Type":<5} {"Path":<10} {"Gap":<18} '
              f'{"P&L":>9}  {"Exit"}')
        print(f'  {"─"*75}')
        for t in trades:
            dt   = str(t.get('date', t.get('entry_time', '')))[:16]
            tp   = t.get('type', '')
            path = t.get('path', '')
            gap  = t.get('gap_type', 'N/A')[:17]
            tpnl = t.get('pnl', 0)
            exit_r = t.get('exit_reason', '')[:20]
            sym  = '+' if tpnl > 0 else '-'
            print(f'  {dt:<18} {tp:<5} {path:<10} {gap:<18} '
                  f'Rs {tpnl:>+8,.0f}  {exit_r}')
    else:
        print('  (no trades fired)')

# ── Summary ───────────────────────────────────────────────────────────────────
print()
print('='*68)
overall_wr = 100 * total_wins / total_n if total_n else 0
print(f'  COMBINED  Trades: {total_n}  '
      f'WR: {total_wins}/{total_n} ({overall_wr:.0f}%)  '
      f'Net P&L: Rs {total_pnl:+,.0f}')
print(f'  Best single trade: Rs {grand_best:+,.0f}'
      f'   Worst: Rs {grand_worst:+,.0f}')
print('='*68)
