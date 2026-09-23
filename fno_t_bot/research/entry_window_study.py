import sys,os,glob,logging,random,statistics as st
sys.path.insert(0,r'C:\quant_trading\MyRepo\fno_t_bot')
sys.path.insert(0,r'C:\quant_trading\MyRepo\fno_t_bot\research')
logging.disable(logging.CRITICAL)
import pandas as pd, config
from options_bot import TradingBot
from deflated_sharpe import sharpe
from scipy import stats as sps
random.seed(53)
D=r'C:\quant_trading\data'
DIRS={'NIFTY':'nifty_5min','BANKNIFTY':'banknifty_5min','SENSEX':'sensex_5min'}
print("ENTRY-HOUR STUDY — is 09:45-11:00 really a danger zone?")
print("Replays PATH_REV + PATH_TREND with the time window REMOVED, across every")
print("merged session. Outcome = index points to 14:30, in ATR, stop 1 / target 2.\n")
rows=[]
for inst,sub in DIRS.items():
    files=sorted(glob.glob(os.path.join(D,sub,'*.csv')))
    for fi in range(3,len(files)):
        day=os.path.basename(files[fi]).split('_')[-1].replace('.csv','')
        try:
            da=pd.concat([pd.read_csv(f,parse_dates=['ts'],index_col='ts') for f in files[fi-3:fi+1]]).sort_index()
            da=da[~da.index.duplicated(keep='first')]
            bot=TradingBot(inst); da=bot.add_indicators(da)
        except Exception: continue
        k=f'{day[:4]}-{day[4:6]}-{day[6:]}'; dd=da[da.index.date.astype(str)==k]
        if len(dd)<25: continue
        bot._or_high=float(dd['High'].iloc[:6].max()); bot._or_low=float(dd['Low'].iloc[:6].min())
        bot._or_ready=True
        for a in ('_trend_qual_dir','_trend_anchor','_trend_leg_extreme','_trend_qual_time'): setattr(bot,a,None)
        bot._path_trend_fired=False
        fired=set()
        for i in range(6,len(dd)-3):
            t=dd.index[i]; hm=t.strftime('%H:%M')
            if hm<'09:45' or hm>'14:00': continue
            sd=da.iloc[:da.index.get_loc(t)+1]
            for nm,fn in (('REV',bot.get_path_rev_signal),('TREND',None)):
                if nm in fired: continue
                try:
                    if nm=='TREND':
                        bot._update_trend_qualification(sd,t)
                        sig=bot.get_path_trend_signal(sd,{},t)
                    else:
                        sig=fn(sd,{},t) if fn else None
                except Exception: sig=None
                if not sig: continue
                fired.add(nm)
                if nm=='TREND': bot._path_trend_fired=True
                e=float(sig['price']); atr=float(dd['ATR'].iloc[i] or 0)
                if atr<=0: continue
                f=dd.iloc[i+1:]; f=f[[x.strftime('%H:%M')<=config.FORCE_CLOSE_TIME for x in f.index]]
                if len(f)<2: continue
                out=None
                for _,r in f.iterrows():
                    hi,lo,cl=float(r['High']),float(r['Low']),float(r['Close'])
                    adv=(e-lo) if sig['type']=='CALL' else (hi-e)
                    fav=(hi-e) if sig['type']=='CALL' else (e-lo)
                    if adv>=atr: out=-1.0; break
                    if fav>=2*atr: out=2.0; break
                if out is None:
                    cl=float(f['Close'].iloc[-1]); out=((cl-e) if sig['type']=='CALL' else (e-cl))/atr
                d2=random.choice(['CALL','PUT'])
                co=None
                for _,r in f.iterrows():
                    hi,lo,cl=float(r['High']),float(r['Low']),float(r['Close'])
                    adv=(e-lo) if d2=='CALL' else (hi-e); fav=(hi-e) if d2=='CALL' else (e-lo)
                    if adv>=atr: co=-1.0; break
                    if fav>=2*atr: co=2.0; break
                if co is None:
                    cl=float(f['Close'].iloc[-1]); co=((cl-e) if d2=='CALL' else (e-cl))/atr
                rows.append(dict(inst=inst,day=day,path=nm,hm=hm,hour=hm[:2],pnl=out,ctl=co))
n=len(rows); print(f"signals (window removed): n={n}\n")
print(f"  {'hour':>6s} {'n':>5s} {'%right':>7s} {'mean ATR':>9s} {'control':>9s} {'edge':>8s} {'Sharpe':>8s}")
for h in ('09','10','11','12','13','14'):
    v=[r for r in rows if r['hour']==h]
    if len(v)<15: print(f"  {h}:xx {len(v):5d}   (too few)"); continue
    p=[x['pnl'] for x in v]; c=[x['ctl'] for x in v]
    print(f"  {h}:xx {len(p):5d} {100*sum(1 for z in p if z>0)/len(p):6.1f}% {st.mean(p):+9.3f} "
          f"{st.mean(c):+9.3f} {st.mean(p)-st.mean(c):+8.3f} {sharpe(p):+8.3f}")
print()
early=[r['pnl'] for r in rows if r['hm']<'11:00']
late =[r['pnl'] for r in rows if r['hm']>='11:00']
ec=[r['ctl'] for r in rows if r['hm']<'11:00']
lc=[r['ctl'] for r in rows if r['hm']>='11:00']
for lbl,p,c in (('09:45-10:59 (blocked today)',early,ec),('11:00-14:00 (current window)',late,lc)):
    if len(p)<15: continue
    print(f"  {lbl:30s} n={len(p):4d}  {100*sum(1 for z in p if z>0)/len(p):5.1f}% right  "
          f"mean {st.mean(p):+.3f} ATR  control {st.mean(c):+.3f}  edge {st.mean(p)-st.mean(c):+.3f}")
if len(early)>15 and len(late)>15:
    print(f"\n  early vs late: Mann-Whitney p={sps.mannwhitneyu(early,late,alternative='greater').pvalue:.4f}"
          f"  (one-sided, early > late)")
    print(f"  early vs its own control: p={sps.mannwhitneyu(early,ec,alternative='greater').pvalue:.4f}")
print("\n  by path, early window only:")
for pth in ('REV','TREND'):
    v=[r['pnl'] for r in rows if r['hm']<'11:00' and r['path']==pth]
    if len(v)>=15:
        print(f"    {pth:6s} n={len(v):4d}  {100*sum(1 for z in v if z>0)/len(v):5.1f}% right  mean {st.mean(v):+.3f} ATR")
