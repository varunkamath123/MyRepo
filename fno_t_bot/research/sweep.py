import sys,os,glob,logging,statistics as st
sys.path.insert(0,'/opt/trading_bot/live_bot')
logging.disable(logging.CRITICAL)
import pandas as pd, config
from options_bot import TradingBot
DIRS={'NIFTY':'nifty_5min','BANKNIFTY':'banknifty_5min','SENSEX':'sensex_5min'}
rows=[]
for inst,sub in DIRS.items():
    files=sorted(glob.glob(f'/opt/trading_bot/data/{sub}/*.csv'))
    for fi in range(3,len(files)):
        day=os.path.basename(files[fi]).split('_')[-1].replace('.csv','')
        try:
            da=pd.concat([pd.read_csv(f,parse_dates=['ts'],index_col='ts') for f in files[fi-3:fi+1]]).sort_index()
            da=da[~da.index.duplicated(keep='first')]
            bot=TradingBot(inst); da=bot.add_indicators(da)
        except Exception: continue
        k=f'{day[:4]}-{day[4:6]}-{day[6:]}'; dd=da[da.index.date.astype(str)==k]
        if len(dd)<20: continue
        bot._or_high=float(dd['High'].iloc[:6].max()); bot._or_low=float(dd['Low'].iloc[:6].min())
        bot._or_ready=True
        for a in ('_trend_qual_dir','_trend_anchor','_trend_leg_extreme','_trend_qual_time'):
            setattr(bot,a,None)
        bot._path_trend_fired=False
        for i in range(6,len(dd)):
            t=dd.index[i]
            if t.strftime('%H:%M')>config.PATH_TREND_END: break
            sd=da.iloc[:da.index.get_loc(t)+1]
            try:
                bot._update_trend_qualification(sd,t); sig=bot.get_path_trend_signal(sd,{},t)
            except Exception: sig=None
            if not sig: continue
            bot._path_trend_fired=True
            e=float(sig['price']); atr=float(dd['ATR'].iloc[i] or 0)
            if atr<=0: break
            f=dd.iloc[i+1:]; f=f[[x.strftime('%H:%M')<=config.FORCE_CLOSE_TIME for x in f.index]]
            if len(f)<2: break
            cl=float(f['Close'].iloc[-1]); res=((cl-e) if sig['type']=='CALL' else (e-cl))/atr
            rows.append(dict(day=day,leg=sig.get('trend_leg_atr') or 0,res=res))
            break
rows.sort(key=lambda r:r['day']); n=len(rows)
print(f"n={n}\n")
print("FLOOR SWEEP — keep only signals with leg >= X   (is 1.5 cherry-picked?)")
print(f"  {'floor':>6s} {'kept':>5s} {'%right':>7s} {'mean ATR':>9s}   {'dropped':>7s} {'%right':>7s} {'mean ATR':>9s}")
for thr in (1.0,1.1,1.2,1.3,1.4,1.5,1.6,1.8,2.0):
    kp=[r['res'] for r in rows if r['leg']>=thr]
    dr=[r['res'] for r in rows if r['leg']<thr]
    if len(kp)<10 or len(dr)<10: continue
    print(f"  {thr:6.1f} {len(kp):5d} {100*sum(1 for x in kp if x>0)/len(kp):6.1f}% "
          f"{st.mean(kp):+9.2f}   {len(dr):7d} {100*sum(1 for x in dr if x>0)/len(dr):6.1f}% {st.mean(dr):+9.2f}")
print("\nSAME SWEEP, second half only (holdout)")
h=rows[n//2:]
for thr in (1.0,1.2,1.3,1.5,1.8,2.0):
    kp=[r['res'] for r in h if r['leg']>=thr]; dr=[r['res'] for r in h if r['leg']<thr]
    if len(kp)<8 or len(dr)<8: continue
    print(f"  {thr:6.1f} {len(kp):5d} {100*sum(1 for x in kp if x>0)/len(kp):6.1f}% "
          f"{st.mean(kp):+9.2f}   {len(dr):7d} {100*sum(1 for x in dr if x>0)/len(dr):6.1f}% {st.mean(dr):+9.2f}")
