import sys,os,glob,logging,random,statistics as st
sys.path.insert(0,r'C:\quant_trading\MyRepo\fno_t_bot')
sys.path.insert(0,r'C:\quant_trading\MyRepo\fno_t_bot\research')
logging.disable(logging.CRITICAL)
import pandas as pd, config
from options_bot import TradingBot
from deflated_sharpe import deflated_sharpe, sharpe
from scipy import stats as sps
random.seed(41)
D=r'C:\quant_trading\data'
DIRS={'NIFTY':'nifty_5min','BANKNIFTY':'banknifty_5min','SENSEX':'sensex_5min'}
FC=config.FORCE_CLOSE_TIME

def frames():
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
            yield inst,day,bot,da,dd

print("="*72); print("RE-TEST 1 — PATH_TREND leg floor (this change is LIVE at 1.5)"); print("="*72)
rows=[]
for inst,day,bot,da,dd in frames():
    bot._or_high=float(dd['High'].iloc[:6].max()); bot._or_low=float(dd['Low'].iloc[:6].min())
    bot._or_ready=True
    for a in ('_trend_qual_dir','_trend_anchor','_trend_leg_extreme','_trend_qual_time'): setattr(bot,a,None)
    bot._path_trend_fired=False
    for i in range(6,len(dd)):
        t=dd.index[i]
        if t.strftime('%H:%M')>config.PATH_TREND_END: break
        sd=da.iloc[:da.index.get_loc(t)+1]
        try:
            bot._update_trend_qualification(sd,t)
            _sv=config.PATH_TREND_MIN_LEG_ATR; config.PATH_TREND_MIN_LEG_ATR=0
            sig=bot.get_path_trend_signal(sd,{},t); config.PATH_TREND_MIN_LEG_ATR=_sv
        except Exception:
            config.PATH_TREND_MIN_LEG_ATR=_sv; sig=None
        if not sig: continue
        bot._path_trend_fired=True
        e=float(sig['price']); atr=float(dd['ATR'].iloc[i] or 0)
        if atr<=0: break
        f=dd.iloc[i+1:]; f=f[[x.strftime('%H:%M')<=FC for x in f.index]]
        if len(f)<2: break
        cl=float(f['Close'].iloc[-1]); res=((cl-e) if sig['type']=='CALL' else (e-cl))/atr
        rows.append(dict(inst=inst,day=day,leg=sig.get('trend_leg_atr') or 0,res=res))
        break
n=len(rows); print(f"PATH_TREND signals on FULL data: n={n}  (EC2 gave 197)")
c={}
for r in rows: c[r['inst']]=c.get(r['inst'],0)+1
print(f"  by instrument: {c}\n")
print(f"  {'floor':>6s} {'kept':>5s} {'%right':>7s} {'mean ATR':>9s}   {'dropped':>7s} {'%right':>7s} {'mean ATR':>9s}")
for thr in (1.0,1.2,1.3,1.5,1.8,2.0):
    kp=[r['res'] for r in rows if r['leg']>=thr]; dr=[r['res'] for r in rows if r['leg']<thr]
    if len(kp)<15 or len(dr)<15: continue
    print(f"  {thr:6.1f} {len(kp):5d} {100*sum(1 for x in kp if x>0)/len(kp):6.1f}% {st.mean(kp):+9.3f}   "
          f"{len(dr):7d} {100*sum(1 for x in dr if x>0)/len(dr):6.1f}% {st.mean(dr):+9.3f}")
lo=[r['res'] for r in rows if r['leg']<1.5]; hi=[r['res'] for r in rows if r['leg']>=1.5]
if len(lo)>15 and len(hi)>15:
    print(f"\n  is the 1.5 floor justified?  Mann-Whitney (>=1.5 beats <1.5) "
          f"p={sps.mannwhitneyu(hi,lo,alternative='greater').pvalue:.4f}")
    rho,p=sps.spearmanr([r['leg'] for r in rows],[r['res'] for r in rows])
    print(f"  rho(leg, outcome) = {rho:+.4f}  p={p:.4f}")

print("\n"+"="*72); print("RE-TEST 2 — SYNFUT ADX/DI core (EC2 said coin flip on n=350)"); print("="*72)
import synthetic_futures as SF
SF._pcr_thresholds=lambda i,nw:(9.99,-9.99)
sig=[];ctl=[]
def ov(f,e,d,atr):
    for _,r in f.iterrows():
        hi,lo,cl=float(r['High']),float(r['Low']),float(r['Close'])
        adv=(e-lo) if d=='CALL' else (hi-e); fav=(hi-e) if d=='CALL' else (e-lo)
        if adv>=SF.SYNFUT_STOP_ATR*atr: return -SF.SYNFUT_STOP_ATR*atr
        if fav>=SF.SYNFUT_TARGET_ATR*atr: return SF.SYNFUT_TARGET_ATR*atr
    lc=float(f['Close'].iloc[-1]); return (lc-e) if d=='CALL' else (e-lc)
for inst,day,bot,da,dd in frames():
    for i in range(6,len(dd)):
        t=dd.index[i]
        if t.strftime('%H:%M')>SF.SYNFUT_END: break
        sd=da.iloc[:da.index.get_loc(t)+1]; px=float(sd['Close'].iloc[-1])
        s=SF._signal(sd,{'pcr':0.90,'max_pain':px*1.05},None,t,None,inst) or \
          SF._signal(sd,{'pcr':0.90,'max_pain':px*0.95},None,t,None,inst)
        if not s: continue
        f=dd.iloc[i+1:]; f=f[[x.strftime('%H:%M')<=FC for x in f.index]]
        if len(f)<2: break
        e,atr=float(s['price']),s['atr']
        sig.append(ov(f,e,s['type'],atr)/atr); ctl.append(ov(f,e,random.choice(['CALL','PUT']),atr)/atr)
        break
if sig:
    w=sum(1 for x in sig if x>0)
    print(f"  SYNFUT  n={len(sig)}  {100*w/len(sig):.1f}% right  mean {st.mean(sig):+.3f} ATR  Sharpe {sharpe(sig):+.3f}")
    print(f"  CONTROL n={len(ctl)}  {100*sum(1 for x in ctl if x>0)/len(ctl):.1f}% right  mean {st.mean(ctl):+.3f} ATR")
    print(f"  edge {st.mean(sig)-st.mean(ctl):+.3f} ATR   p={sps.mannwhitneyu(sig,ctl,alternative='greater').pvalue:.4f}")
    d=deflated_sharpe(sig,n_trials=1)
    print(f"  DSR (as ONE pre-registered strategy) = {d['dsr']*100:.1f}%  {d['verdict'][:44]}")
