# -*- coding: utf-8 -*-
"""Validate PATH_SYNFUT before deployment.

Checks the entry gate fires sensibly, the deep-ITM strike maths is right, and
the ATR index exits behave. Backtested in INDEX POINTS across all local
sessions -- deep-ITM premium history does not exist, so rupee P&L is not
claimed. Index travel is what a delta~0.85 vehicle mostly captures anyway.
"""
import os, sys, glob, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.disable(logging.CRITICAL)

import statistics as st
import pandas as pd
import config
from options_bot import TradingBot
import synthetic_futures as SF
from deflated_sharpe import deflated_sharpe, sharpe

DATA = os.environ.get('FNO_DATA', r'C:\quant_trading\data')
DIRS = {'NIFTY': 'nifty_5min', 'BANKNIFTY': 'banknifty_5min', 'SENSEX': 'sensex_5min'}

P = F = 0
def ck(n, c, d=''):
    global P, F
    if c: P += 1; print(f'  PASS  {n}')
    else:  F += 1; print(f'  FAIL  {n}  {d}')

print('=' * 74); print('1. DEEP-ITM STRIKE MATHS'); print('=' * 74)
cases = [('NIFTY', 24300, 50, 'CALL'), ('NIFTY', 24300, 50, 'PUT'),
         ('BANKNIFTY', 57000, 100, 'CALL'), ('SENSEX', 77500, 100, 'PUT')]
for inst, px, gap, d in cases:
    k = SF._deep_itm_strike(px, d, gap)
    itm = (px - k) if d == 'CALL' else (k - px)
    pct = itm / px * 100
    print(f'  {inst:10s} {d:4s} spot {px:,} -> strike {k:,}  ITM {itm:,.0f} pts ({pct:.2f}%)')
    ck(f'{inst} {d} is genuinely ITM', itm > 0, f'itm={itm}')
    ck(f'{inst} {d} ITM ~1.2%', 0.8 <= pct <= 1.8, f'{pct:.2f}%')

print()
print('=' * 74); print('2. EXITS ARE INDEX-BASED, NOT PREMIUM %'); print('=' * 74)
import datetime as dt
now = dt.datetime(2026, 9, 2, 11, 0)
pos = dict(type='CALL', entry_index=57000.0, atr=80.0, peak_move=0.0)
ck('no exit on small move', SF._exit_reason(dict(pos), 57020.0, now) is None)
ck('stop at -1 ATR', 'Stop' in (SF._exit_reason(dict(pos), 57000 - 85, now) or ''))
ck('target at +2 ATR', 'Target' in (SF._exit_reason(dict(pos), 57000 + 165, now) or ''))
p2 = dict(pos); SF._exit_reason(p2, 57000 + 130, now)          # arm trail
ck('trail arms then fires', 'Trail' in (SF._exit_reason(p2, 57000 + 60, now) or ''),
   str(SF._exit_reason(p2, 57000 + 60, now)))
ck('force close respected',
   'Force-Close' in (SF._exit_reason(dict(pos), 57010.0,
                                     dt.datetime(2026, 9, 2, 14, 35)) or ''))

print()
print('=' * 74); print('3. BACKTEST — all local sessions, index points'); print('=' * 74)
rows = []
for inst, sub in DIRS.items():
    files = sorted(glob.glob(os.path.join(DATA, sub, '*.csv')))
    for fi in range(3, len(files)):
        day = os.path.basename(files[fi]).split('_')[-1].replace('.csv', '')
        try:
            dfs = [pd.read_csv(f, parse_dates=['ts'], index_col='ts')
                   for f in files[max(0, fi - 3):fi + 1]]
            da = pd.concat(dfs).sort_index()
            da = da[~da.index.duplicated(keep='first')]
            bot = TradingBot(inst); da = bot.add_indicators(da)
        except Exception:
            continue
        key = f'{day[:4]}-{day[4:6]}-{day[6:]}'
        dd = da[da.index.date.astype(str) == key]
        if len(dd) < 20:
            continue
        fired = False
        for i in range(6, len(dd)):
            t = dd.index[i]
            if t.strftime('%H:%M') > SF.SYNFUT_END or fired:
                break
            ifull = da.index.get_loc(t)
            # oc unavailable historically -> supply a neutral PCR so the ADX/DI
            # and MaxPain legs are exercised; PCR selectivity is NOT tested here.
            sig = SF._signal(da.iloc[:ifull + 1],
                             {'pcr': 0.90, 'max_pain': None}, None, t, None, inst)
            if not sig:
                continue
            fired = True
            atr = sig['atr']
            f = dd.iloc[i + 1:]
            f = f[[x.strftime('%H:%M') <= config.FORCE_CLOSE_TIME for x in f.index]]
            if len(f) == 0:
                break
            e = sig['price']; pk = 0.0; out = None
            for _, r in f.iterrows():
                hi, lo, cl = float(r['High']), float(r['Low']), float(r['Close'])
                adv = (e - lo) if sig['type'] == 'CALL' else (hi - e)
                fav = (hi - e) if sig['type'] == 'CALL' else (e - lo)
                if adv >= SF.SYNFUT_STOP_ATR * atr:
                    out = -SF.SYNFUT_STOP_ATR * atr; break
                if fav >= SF.SYNFUT_TARGET_ATR * atr:
                    out = SF.SYNFUT_TARGET_ATR * atr; break
                pk = max(pk, fav)
                cur = (cl - e) if sig['type'] == 'CALL' else (e - cl)
                if pk >= SF.SYNFUT_TRAIL_ACT * atr and cur < pk - SF.SYNFUT_TRAIL_ATR * atr:
                    out = cur; break
            if out is None:
                lc = float(f['Close'].iloc[-1])
                out = (lc - e) if sig['type'] == 'CALL' else (e - lc)
            rows.append(dict(day=day, inst=inst, dir=sig['type'], pts=out, atr=atr))
            break

print(f'  entries: {len(rows)}')
if rows:
    pts = [r['pts'] for r in rows]
    w = sum(1 for p in pts if p > 0)
    print(f'  {w}W/{len(pts)-w}L ({100*w/len(pts):.1f}%)  '
          f'mean {st.mean(pts):+.1f} pts  median {st.median(pts):+.1f} pts')
    print(f'  sum {sum(pts):+,.0f} index points   Sharpe {sharpe(pts):+.3f}')
    for inst in DIRS:
        v = [r['pts'] for r in rows if r['inst'] == inst]
        if v:
            ww = sum(1 for p in v if p > 0)
            print(f'    {inst:10s} n={len(v):3d} {ww}W/{len(v)-ww}L  '
                  f'mean {st.mean(v):+7.1f} pts  sum {sum(v):+8,.0f}')
    d = deflated_sharpe(pts, n_trials=1)
    print(f'\n  DSR as ONE pre-registered strategy: {d["dsr"]*100:.1f}%  -> {d["verdict"]}')
    print('  (N=1 is the honest count: parameters were fixed on structural')
    print('   grounds before this ran, not selected from a sweep.)')
    ck('backtest produced entries', len(rows) >= 20)

print()
print('=' * 74); print(f'RESULT: {P} passed, {F} failed'); print('=' * 74)
