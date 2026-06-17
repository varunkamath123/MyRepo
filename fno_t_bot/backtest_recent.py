"""
backtest_recent.py — Run vX backtest on the last N trading days
Usage:  python backtest_recent.py [N]   (default N=10)

Loads data from existing bot.py infrastructure but filters to
the most recent N trading days across all 3 instruments.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import date, timedelta

N_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 10

# ── Load data (reuse bot.py loader) ──────────────────────────────────────────
BASE = Path(r'C:\quant_trading\data')
INSTRUMENT_DIRS = {
    'NIFTY'    : BASE / 'nifty_5min',
    'BANKNIFTY': BASE / 'banknifty_5min',
    'SENSEX'   : BASE / 'sensex_5min',
}

def load_recent(folder: Path, n_days: int) -> pd.DataFrame:
    files = sorted(folder.glob('*.csv'))
    # Take last n_days+2 files (extra buffer for prev_close)
    recent_files = files[-(n_days + 2):]
    frames = []
    for f in recent_files:
        try:
            df = pd.read_csv(f, index_col=0, parse_dates=True)
            frames.append(df)
        except Exception as e:
            print(f"  skip {f.name}: {e}")
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames).sort_index()
    df = df[~df.index.duplicated(keep='first')]
    return df

# ── Import and run bot backtest ───────────────────────────────────────────────
# Patch sys.argv so bot.py doesn't try to use our args
sys.argv = ['bot.py', 'vX']

# Import bot.py's core functions
import importlib.util, types

spec = importlib.util.spec_from_file_location(
    'bot', os.path.join(os.path.dirname(__file__), 'bot.py')
)
bot_mod = importlib.util.util = importlib.util.module_from_spec(spec)

# We don't exec the full module (it auto-runs). Instead, use subprocess.
import subprocess

# Build a mini driver script that imports bot and filters dates
DRIVER = r"""
import sys, os
sys.path.insert(0, r'C:\quant_trading\live_bot')
os.chdir(r'C:\quant_trading\live_bot')
sys.argv = ['bot.py', 'vX']

import pandas as pd
import numpy as np
from pathlib import Path

N_DAYS = {n_days}

BASE = Path(r'C:\quant_trading\data')
INSTRUMENT_DIRS = {{
    'NIFTY'    : BASE / 'nifty_5min',
    'BANKNIFTY': BASE / 'banknifty_5min',
    'SENSEX'   : BASE / 'sensex_5min',
}}

def load_recent(folder, n_days):
    files = sorted(folder.glob('*.csv'))
    recent = files[-(n_days + 2):]
    frames = []
    for f in recent:
        try:
            df = pd.read_csv(f, index_col=0, parse_dates=True)
            frames.append(df)
        except:
            pass
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames).sort_index()
    return df[~df.index.duplicated(keep='first')]

# Load bot module pieces we need
import importlib.util
spec = importlib.util.spec_from_file_location(
    'bot', r'C:\quant_trading\live_bot\bot.py'
)
# Run bot module in restricted mode: import but suppress __main__ block
import ast, types

src = open(r'C:\quant_trading\live_bot\bot.py').read()

# Replace the __main__ guard so it doesn't auto-run
src_patched = src.replace(
    "if __name__ == '__main__':",
    "if False and __name__ == '__main__':"
)

bot_ns = {{'__name__': 'bot', '__file__': r'C:\quant_trading\live_bot\bot.py'}}
exec(compile(src_patched, 'bot.py', 'exec'), bot_ns)

run_backtest = bot_ns['run_backtest']
import config as config_mod

print(f'Running vX backtest on last {{N_DAYS}} trading days')
print('='*65)

all_trades = []
total_pnl  = 0
total_wins = 0
total_n    = 0

for instr, folder in INSTRUMENT_DIRS.items():
    df = load_recent(folder, N_DAYS)
    if df.empty:
        print(f'  {{instr}}: no data')
        continue

    # Filter to last N_DAYS unique trading dates
    df.index = pd.to_datetime(df.index, utc=False)
    if df.index.tz is not None:
        df.index = df.index.tz_convert('Asia/Kolkata').tz_localize(None)

    trading_dates = sorted(df.index.normalize().unique())[-N_DAYS:]
    cutoff        = pd.Timestamp(trading_dates[0])
    df_recent     = df[df.index >= cutoff]

    print(f'  {{instr}}: {{len(trading_dates)}} days '
          f'({{trading_dates[0].date()}} -> {{trading_dates[-1].date()}})')

    try:
        trades, pnl, stats = run_backtest(df_recent, instr, variant='vX')
        n = len(trades)
        w = sum(1 for t in trades if t.get('pnl', 0) > 0) if trades else 0
        all_trades.extend(trades)
        total_pnl  += pnl
        total_wins += w
        total_n    += n
        wr = 100*w/n if n else 0
        print(f'    Trades: {{n}}  WR: {{w}}/{{n}} ({{wr:.0f}}%)  Net P&L: Rs {{pnl:,.0f}}')
        if trades:
            for t in trades:
                d = t.get('date') or t.get('entry_time', '')
                tp = t.get('type', '')
                path = t.get('path', '')
                tpnl = t.get('pnl', 0)
                exit_r = t.get('exit_reason', '')
                gap  = t.get('gap_type', '')
                print(f'      {{d}}  {{tp:<5}} {{path:<10}} {{gap:<18}} '
                      f'P&L=Rs {{tpnl:>7,.0f}}  {{exit_r}}')
    except Exception as e:
        print(f'    ERROR: {{e}}')
        import traceback; traceback.print_exc()

print()
print('='*65)
print(f'COMBINED  Trades: {{total_n}}  '
      f'WR: {{total_wins}}/{{total_n}} ({{100*total_wins/total_n:.0f}}%)  '
      f'Net P&L: Rs {{total_pnl:,.0f}}')
print('='*65)
""".format(n_days=N_DAYS)

driver_path = Path(r'C:\quant_trading\live_bot\_recent_driver.py')
driver_path.write_text(DRIVER, encoding='utf-8')

result = subprocess.run(
    [sys.executable, str(driver_path)],
    capture_output=False, text=True
)
