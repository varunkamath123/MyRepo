# -*- coding: utf-8 -*-
"""Backtest all ten pre-registered strategies over every session we have.

Measured in INDEX POINTS. No option pricing, so nothing inherits the 15.7%
BS/IV error. Comparison between books is the point; absolute rupees are not
claimed.
"""
import os, sys, glob, logging
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.CRITICAL)

import statistics as st
import pandas as pd
import config
from options_bot import TradingBot
import multi_strategy as M
from deflated_sharpe import deflated_sharpe, sharpe

DATA = os.environ.get('FNO_DATA', r'C:\quant_trading\data')
DIRS = {'NIFTY': 'nifty_5min', 'BANKNIFTY': 'banknifty_5min', 'SENSEX': 'sensex_5min'}
FORCE = config.FORCE_CLOSE_TIME


def fwd(day_df, i, direction):
    f = day_df.iloc[i + 1:]
    f = f[[t.strftime('%H:%M') <= FORCE for t in f.index]]
    if len(f) == 0:
        return None
    e = float(day_df['Close'].iloc[i])
    if direction == 'PUT':
        return dict(mfe=e - float(f['Low'].min()), mae=float(f['High'].max()) - e,
                    close=e - float(f['Close'].iloc[-1]))
    return dict(mfe=float(f['High'].max()) - e, mae=e - float(f['Low'].min()),
                close=float(f['Close'].iloc[-1]) - e)


def run():
    out = {k: [] for k in M.STRATEGIES}
    for inst, sub in DIRS.items():
        files = sorted(glob.glob(os.path.join(DATA, sub, '*.csv')))
        prev_close, prev_day = None, None
        for fi in range(3, len(files)):
            day = os.path.basename(files[fi]).split('_')[-1].replace('.csv', '')
            try:
                dfs = [pd.read_csv(f, parse_dates=['ts'], index_col='ts')
                       for f in files[max(0, fi - 3):fi + 1]]
                da = pd.concat(dfs).sort_index()
                da = da[~da.index.duplicated(keep='first')]
                bot = TradingBot(inst)
                da = bot.add_indicators(da)
            except Exception:
                continue
            key = f'{day[:4]}-{day[4:6]}-{day[6:]}'
            dd = da[da.index.date.astype(str) == key]
            if len(dd) < 20:
                continue

            gap_pct = None
            if prev_close:
                import datetime as _dt
                d0 = _dt.datetime.strptime(prev_day, '%Y%m%d').date()
                d1 = _dt.datetime.strptime(day, '%Y%m%d').date()
                if (d1 - d0).days <= 4:          # skip data discontinuities
                    gap_pct = (float(dd['Open'].iloc[0]) - prev_close) / prev_close * 100
            prev_close, prev_day = float(dd['Close'].iloc[-1]), day

            bot._or_high = float(dd['High'].iloc[:6].max())
            bot._or_low = float(dd['Low'].iloc[:6].min())
            bot._or_ready = True
            books = {n: M._book(f'{inst}_bt', n) for n in M.STRATEGIES}
            for n in books:
                M._reset_day(books[n], key)
            taken = {n: False for n in M.STRATEGIES}
            ctx = dict(inst=inst, gap_pct=gap_pct)

            for i in range(6, len(dd)):
                t = dd.index[i]
                if t.strftime('%H:%M') > FORCE:
                    break
                ifull = da.index.get_loc(t)
                sub_df = da.iloc[:ifull + 1]
                if t.strftime('%H:%M') < '12:00':
                    try:
                        bot._update_morning_trend(dd.iloc[:i + 1])
                    except Exception:
                        pass
                for n, spec in M.STRATEGIES.items():
                    if taken[n]:
                        continue
                    b = books[n]
                    b['morning_dir'] = bot._morning_dir
                    b['morning_adx_peak'] = bot._morning_adx_peak
                    b['morning_di_peak'] = bot._morning_di_peak
                    try:
                        sig = spec['fn'](bot, b, sub_df, {}, t, ctx)
                    except Exception:
                        sig = None
                    if not sig:
                        continue
                    ch = M._chase(sub_df, float(sig['price']), sig['type'])
                    if (config.CHASE_GATE_MODE == 'active'
                            and ch > config.CHASE_GATE_MAX
                            and sig['path'] not in config.CHASE_GATE_EXEMPT_PATHS
                            and sig['path'] not in ('RANDOM', 'FIXED', 'GAPFADE', 'INV')):
                        continue
                    r = fwd(dd, i, sig['type'])
                    if not r:
                        continue
                    out[n].append(dict(day=day, inst=inst, dir=sig['type'],
                                       path=sig['path'], hm=t.strftime('%H:%M'),
                                       chase=round(ch, 3), **r))
                    taken[n] = True
    return out


if __name__ == '__main__':
    print('backtesting 10 strategies over all local sessions ...', flush=True)
    res = run()
    rows = []
    for n, spec in M.STRATEGIES.items():
        v = res[n]
        if not v:
            rows.append((n, spec['role'], 0, 0, 0, 0, 0, 0)); continue
        cl = [x['close'] for x in v]
        rows.append((n, spec['role'], len(v),
                     100 * sum(1 for c in cl if c > 0) / len(cl),
                     st.median(cl), st.mean(cl),
                     st.median([x['mfe'] for x in v]) /
                     max(st.median([x['mae'] for x in v]), .01),
                     sharpe(cl)))
    print()
    print('=' * 96)
    print('TEN STRATEGIES — index points, all sessions')
    print('=' * 96)
    print(f"{'strategy':12s} {'role':10s} {'n':>5s} {'right%':>8s} "
          f"{'medClose':>9s} {'meanClose':>10s} {'MFE/MAE':>8s} {'Sharpe':>8s}")
    print('-' * 96)
    for n, role, cnt, right, med, mean, ratio, shp in sorted(
            rows, key=lambda r: -r[5]):
        print(f'{n:12s} {role:10s} {cnt:5d} {right:7.1f}% {med:9.1f} '
              f'{mean:10.1f} {ratio:8.2f} {shp:+8.3f}')

    ctrl = {n: r for n, r, c, ri, m, mn, ra, s in
            [(x[0], x[5], 0, 0, 0, 0, 0, 0) for x in rows]}
    rnd = next((x for x in rows if x[0] == 'random'), None)
    print()
    if rnd:
        print(f"  random control mean close: {rnd[5]:+.1f} pts   "
              f"right-side {rnd[3]:.1f}%")
        beat = [x[0] for x in rows if x[5] > rnd[5] and x[1] != 'control']
        print(f"  candidates beating random : {len(beat)}/7"
              + (f"  -> {', '.join(beat)}" if beat else ''))
    print()
    print('  DSR against N=10 (are any distinguishable from best-of-ten luck?)')
    for n, role, cnt, right, med, mean, ratio, shp in rows:
        v = [x['close'] for x in res[n]]
        if len(v) < 10:
            continue
        d = deflated_sharpe(v, n_trials=10)
        print(f"    {n:12s} Sharpe {d['sharpe']:+.3f}  bar {d['sr0']:+.3f}  "
              f"DSR {d['dsr']*100:5.1f}%  {d['verdict'][:44]}")
