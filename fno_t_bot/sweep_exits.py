# -*- coding: utf-8 -*-
"""
sweep_exits.py - Grid search over stop / target / trail_dist for the vX strategy.

Method: patch config.py in-place, run bot.py vX sequentially, restore.
MPLBACKEND=Agg prevents matplotlib GUI hang. Takes ~25-35 min for 42 combos.

Grid:
  stop_pct   : 0.25 0.30 0.35 0.40 0.45 0.50
  target_pct : 0.60 0.80 1.00 1.20 1.30 1.50 1.80 2.00
  trail_dist : fixed 0.20 (default) or 0.15/0.20/0.25 with --full
  trail_act  = max(stop + 0.10, target * 0.42)

Prerequisite: vX branch in bot.py must read config.STOP_LOSS / config.BASE_TARGET.

Usage:
  cd C:\\quant_trading\\live_bot
  python sweep_exits.py           # 48 combos (6 stops x 8 targets)
  python sweep_exits.py --full    # 144 combos (3 trail_dist values)
"""

from __future__ import annotations
import os, re, subprocess, sys, time
from itertools import product

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

LIVE_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_PATH     = os.path.join(LIVE_BOT_DIR, 'bot.py')
CONFIG_PATH  = os.path.join(LIVE_BOT_DIR, 'config.py')
PYTHON       = sys.executable

STOPS        = [0.25, 0.30, 0.35, 0.40, 0.45, 0.50]
TARGETS      = [0.60, 0.80, 1.00, 1.20, 1.30, 1.50, 1.80, 2.00]
TRAIL_DISTS  = [0.15, 0.20, 0.25] if '--full' in sys.argv else [0.20]

# ─── Config patch / restore ───────────────────────────────────────────────────
with open(CONFIG_PATH, encoding='utf-8') as _f:
    _ORIG_CONFIG = _f.read()

def _patch(stop: float, target: float, trail_act: float, trail_dist: float):
    t = _ORIG_CONFIG
    t = re.sub(r'(STOP_LOSS\s*=\s*)[\d.]+',          rf'\g<1>{stop}',       t)
    t = re.sub(r'(BASE_TARGET\s*=\s*)[\d.]+',         rf'\g<1>{target}',     t)
    t = re.sub(r'(TRAILING_ACTIVATION\s*=\s*)[\d.]+', rf'\g<1>{trail_act}',  t)
    t = re.sub(r'(TRAILING_DISTANCE\s*=\s*)[\d.]+',   rf'\g<1>{trail_dist}', t)
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        f.write(t)

def _restore():
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        f.write(_ORIG_CONFIG)

# ─── Parser ───────────────────────────────────────────────────────────────────
def _num(output: str, pattern: str, default: float = 0.0) -> float:
    m = re.search(pattern, output)
    if not m:
        return default
    nums = re.findall(r'-?[\d,]+\.?\d*', m.group(1))
    return float(nums[0].replace(',', '')) if nums else default

def _parse(output: str) -> dict:
    trades = int(_num(output, r'TRADES\s*:(.*)',         0))
    wr     = _num(output, r'Win Rate\s*:(.*)')
    net    = _num(output, r'Net P&L\s*:(.*)')
    dd     = _num(output, r'Max Drawdown\s*:(.*)')
    sharpe = _num(output, r'Sharpe\s*:(.*)')
    pf     = _num(output, r'Profit Factor\s*:(.*)')
    wl     = _num(output, r'Win/Loss Ratio\s*:(.*)')
    if re.search(r'Net P&L.*?LOSS', output):
        net = -abs(net)
    # Count exit types from EXIT BREAKDOWN
    eod_m    = re.search(r'EOD Close.*?:\s*(\d+)', output)
    target_m = re.search(r'Target.*?:\s*(\d+)', output)
    trail_m  = re.search(r'Trailing Stop.*?:\s*(\d+)', output)
    stop_m   = re.search(r'Stop \(.*?:\s*(\d+)', output)
    return {
        'trades': trades, 'wr': wr, 'net': net, 'dd': dd,
        'sharpe': sharpe, 'pf': pf, 'wl': wl,
        'n_eod':    int(eod_m.group(1))    if eod_m    else 0,
        'n_target': int(target_m.group(1)) if target_m else 0,
        'n_trail':  int(trail_m.group(1))  if trail_m  else 0,
        'n_stop':   int(stop_m.group(1))   if stop_m   else 0,
    }

# ─── Run one combo ────────────────────────────────────────────────────────────
def run_combo(stop: float, target: float,
              trail_act: float, trail_dist: float) -> dict:
    _patch(stop, target, trail_act, trail_dist)
    env = os.environ.copy()
    env['PYTHONUTF8'] = '1'
    env['MPLBACKEND'] = 'Agg'   # prevent chart-save hang
    try:
        result = subprocess.run(
            [PYTHON, BOT_PATH, 'vX'],
            capture_output=True, text=True,
            encoding='utf-8', errors='replace',
            timeout=120, cwd=LIVE_BOT_DIR, env=env,
        )
        out = result.stdout + result.stderr
        r = _parse(out)
        r.update({'stop': stop, 'target': target,
                  'trail_act': trail_act, 'trail_dist': trail_dist})
        return r
    except subprocess.TimeoutExpired:
        return {'stop': stop, 'target': target, 'trail_act': trail_act,
                'trail_dist': trail_dist, 'trades': 0, 'error': 'timeout'}
    except Exception as e:
        return {'stop': stop, 'target': target, 'trail_act': trail_act,
                'trail_dist': trail_dist, 'trades': 0, 'error': str(e)}
    finally:
        _restore()

# ─── Table printer ────────────────────────────────────────────────────────────
def _table(rows: list, title: str, key: str, reverse: bool = True, n: int = 15):
    valid = [r for r in rows if r.get('trades', 0) > 0]
    print(f"\n{'─'*80}")
    print(f"  TOP {n} by {title}")
    print(f"  {'SL':>5}  {'TGT':>5}  {'Tact':>5}  {'Tdist':>5}  "
          f"{'Tr':>4}  {'WR%':>6}  {'Net P&L':>11}  {'DD%':>7}  "
          f"{'Sharpe':>6}  {'PF':>5}  {'Tgt%':>5}  {'Trail%':>6}  {'EOD%':>5}")
    print(f"  {'─'*76}")
    for r in sorted(valid, key=lambda x: x.get(key, -999), reverse=reverse)[:n]:
        tr = r['trades']
        pct = lambda k: f"{r[k]/tr*100:.0f}%" if tr else '-'
        sign = '+' if r['net'] >= 0 else ''
        print(f"  {r['stop']:>4.0%}  {r['target']:>4.0%}  {r['trail_act']:>4.0%}  "
              f"{r['trail_dist']:>4.0%}  {tr:>4}  {r['wr']:>6.1f}  "
              f"{sign}Rs.{abs(r['net']):>8,.0f}  {r['dd']:>7.2f}  "
              f"{r['sharpe']:>6.2f}  {r['pf']:>5.2f}  "
              f"{pct('n_target'):>5}  {pct('n_trail'):>6}  {pct('n_eod'):>5}")

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    combos = []
    for stop, target, trail_dist in product(STOPS, TARGETS, TRAIL_DISTS):
        trail_act = round(max(stop + 0.10, target * 0.42), 2)
        if trail_act >= target - 0.05:
            trail_act = round(target - 0.08, 2)
        if trail_act <= stop:
            continue
        combos.append((stop, target, trail_act, trail_dist))

    print(f"\n{'='*80}")
    print(f"  EXIT SWEEP — vX  (NIFTY + BANKNIFTY + SENSEX combined)")
    print(f"  {len(combos)} combos  |  sequential  |  MPLBACKEND=Agg")
    print(f"  trail_act = max(stop+10%, target*42%)  |  trail_dist fixed at 0.20")
    print(f"  Current baseline: SL=40%  TGT=130%  trail_act=55%  trail_dist=20%")
    print(f"{'='*80}")

    results = []
    t0 = time.time()

    for idx, (stop, target, trail_act, trail_dist) in enumerate(combos, 1):
        elapsed = time.time() - t0
        eta = (elapsed / idx) * (len(combos) - idx) if idx > 1 else 0
        print(f"  [{idx:>3}/{len(combos)}] SL={stop:.0%}  TGT={target:.0%}  "
              f"ta={trail_act:.0%}  td={trail_dist:.0%}  ETA {eta/60:.1f}min",
              end='  ', flush=True)

        r = run_combo(stop, target, trail_act, trail_dist)
        if r.get('trades', 0) == 0:
            print(f"-> {r.get('error', 'no trades')}")
            continue
        results.append(r)
        tr = r['trades']
        sign = '+' if r['net'] >= 0 else ''
        print(f"-> {tr}tr  WR {r['wr']:.1f}%  "
              f"Net {sign}Rs.{abs(r['net']):,.0f}  "
              f"DD {r['dd']:.1f}%  Sharpe {r['sharpe']:.2f}  PF {r['pf']:.2f}  "
              f"[Tgt:{r['n_target']} Trail:{r['n_trail']} Stop:{r['n_stop']} EOD:{r['n_eod']}]")

    _restore()

    if not results:
        print("\n  No results.")
        return

    _table(results, "NET P&L",    'net',    reverse=True)
    _table(results, "SHARPE",     'sharpe', reverse=True)
    _table(results, "WIN RATE",   'wr',     reverse=True)
    _table(results, "PROFIT FACTOR", 'pf', reverse=True)

    cur = next((r for r in results
                if abs(r['stop']-0.40) < 0.01 and abs(r['target']-1.30) < 0.01
                and abs(r['trail_dist']-0.20) < 0.01), None)
    if cur:
        print(f"\n{'─'*80}")
        print(f"  CURRENT CONFIG (SL=40%  TGT=130%  ta=55%  td=20%):")
        print(f"    {cur['trades']}tr  WR {cur['wr']:.1f}%  "
              f"Net {'+' if cur['net']>=0 else ''}Rs.{abs(cur['net']):,.0f}  "
              f"DD {cur['dd']:.1f}%  Sharpe {cur['sharpe']:.2f}  PF {cur['pf']:.2f}")
        tr = cur['trades']
        print(f"    Exit mix: Target {cur['n_target']/tr*100:.0f}%  "
              f"Trail {cur['n_trail']/tr*100:.0f}%  "
              f"Stop {cur['n_stop']/tr*100:.0f}%  "
              f"EOD {cur['n_eod']/tr*100:.0f}%")

    best_net    = max(results, key=lambda x: x['net'])
    best_sharpe = max(results, key=lambda x: x['sharpe'])
    best_pf     = max(results, key=lambda x: x['pf'])
    print(f"\n  >> BEST NET P&L : SL={best_net['stop']:.0%} TGT={best_net['target']:.0%}"
          f" ta={best_net['trail_act']:.0%} td={best_net['trail_dist']:.0%}"
          f"  -> {best_net['trades']}tr  WR {best_net['wr']:.1f}%"
          f"  Net +Rs.{best_net['net']:,.0f}"
          f"  DD {best_net['dd']:.1f}%  Sharpe {best_net['sharpe']:.2f}")
    print(f"  >> BEST SHARPE  : SL={best_sharpe['stop']:.0%} TGT={best_sharpe['target']:.0%}"
          f" ta={best_sharpe['trail_act']:.0%} td={best_sharpe['trail_dist']:.0%}"
          f"  -> {best_sharpe['trades']}tr  WR {best_sharpe['wr']:.1f}%"
          f"  Net +Rs.{best_sharpe['net']:,.0f}"
          f"  DD {best_sharpe['dd']:.1f}%  Sharpe {best_sharpe['sharpe']:.2f}")
    print(f"  >> BEST PF      : SL={best_pf['stop']:.0%} TGT={best_pf['target']:.0%}"
          f" ta={best_pf['trail_act']:.0%} td={best_pf['trail_dist']:.0%}"
          f"  -> {best_pf['trades']}tr  WR {best_pf['wr']:.1f}%"
          f"  Net +Rs.{best_pf['net']:,.0f}"
          f"  PF {best_pf['pf']:.2f}")

    total = time.time() - t0
    print(f"\n  Done: {len(results)}/{len(combos)} valid combos in {total/60:.1f} min")
    print(f"{'='*80}\n")


if __name__ == '__main__':
    main()
