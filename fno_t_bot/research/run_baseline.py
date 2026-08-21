# -*- coding: utf-8 -*-
"""Driver: replay the CURRENT strategy, then test it against random entries.

Runs entirely on local index CSVs -- no EC2, no option pricing model.
"""
import os, sys, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import config
from options_bot import TradingBot
from random_baseline import run, report, forward_excursion

DATA = os.environ.get('FNO_DATA', r'C:\quant_trading\data')
DIRS = {'NIFTY':'nifty_5min','BANKNIFTY':'banknifty_5min','SENSEX':'sensex_5min'}
FORCE = config.FORCE_CLOSE_TIME

def chase_of(day_df, i, direction):
    td = day_df.iloc[:i+1]
    hi = float(td['High'].max()); lo = float(td['Low'].min())
    if hi <= lo: return 0.5
    px = float(td['Close'].iloc[-1]); rp = (px-lo)/(hi-lo)
    return rp if direction == 'CALL' else 1.0-rp

def replay(max_days_per_inst=None):
    """Replay current entry logic. oc={} -> REV loses its MaxPain component,
    so REV signals here are UNDER-counted vs live. Flagged, not hidden."""
    entries = []
    for inst, d in DIRS.items():
        files = sorted(glob.glob(os.path.join(DATA, d, '*.csv')))
        if max_days_per_inst: files = files[-max_days_per_inst:]
        for fi in range(3, len(files)):
            day = os.path.basename(files[fi]).split('_')[-1].replace('.csv','')
            try:
                dfs = [pd.read_csv(f, parse_dates=['ts'], index_col='ts')
                       for f in files[max(0,fi-3):fi+1]]
                da = pd.concat(dfs).sort_index()
                da = da[~da.index.duplicated(keep='first')]
                b = TradingBot(inst); da = b.add_indicators(da)
            except Exception:
                continue
            key = f"{day[:4]}-{day[4:6]}-{day[6:]}"
            dd = da[da.index.date.astype(str) == key]
            if len(dd) < 20: continue
            b._or_high = float(dd['High'].iloc[:6].max())
            b._or_low  = float(dd['Low'].iloc[:6].min()); b._or_ready = True
            b._morning_dir=None; b._morning_adx_peak=0.0; b._morning_di_peak=0.0
            b._trend_qual_dir=None; b._path_trend_fired=False; b._path_rev_fired=False
            for i in range(6, len(dd)):
                t = dd.index[i]; hm = t.strftime('%H:%M')
                if hm > FORCE: break
                ifull = da.index.get_loc(t); sub = da.iloc[:ifull+1]
                if hm < '12:00': b._update_morning_trend(dd.iloc[:i+1])
                b._update_trend_qualification(sub, t)
                sig = None
                if not b._path_rev_fired:
                    try: sig = b.get_path_rev_signal(sub, {}, t)
                    except Exception: sig = None
                    if sig: b._path_rev_fired = True
                if sig is None and not b._path_trend_fired:
                    try: sig = b.get_path_trend_signal(sub, {}, t)
                    except Exception: sig = None
                    if sig: b._path_trend_fired = True
                if not sig: continue
                ch = chase_of(dd, i, sig['type'])
                if (config.CHASE_GATE_MODE=='active' and ch > config.CHASE_GATE_MAX
                        and sig['path'] not in config.CHASE_GATE_EXEMPT_PATHS):
                    continue
                ex = forward_excursion(dd, i, sig['type'], FORCE)
                if not ex: continue
                entries.append(dict(day=day, inst=inst, hm=hm, dir=sig['type'],
                                    path=sig['path'], chase=round(ch,3), **ex))
                break   # MAX_CONCURRENT_POSITIONS = 1
    return entries

if __name__ == '__main__':
    import logging; logging.disable(logging.CRITICAL)
    print("replaying current strategy on local data ...", flush=True)
    E = replay()
    print(f"  {len(E)} entries across {len({e['day'] for e in E})} sessions\n")
    if not E:
        sys.exit("no entries")
    res = run(E, DATA, DIRS, force_close=FORCE, draws_per_entry=40)
    report(res)

    df = pd.DataFrame(E)
    print()
    print("="*78); print("BY PATH — strategy only"); print("="*78)
    for p, g in df.groupby('path'):
        print(f"  {p:7s} n={len(g):4d}  right-side {100*(g['close']>0).mean():5.1f}%  "
              f"med close {g['close'].median():+7.1f}  MFE/MAE "
              f"{g.mfe.median()/max(g.mae.median(),.01):.2f}")
    df.to_csv(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'replay_entries.csv'), index=False)
    print("\n  wrote replay_entries.csv")
