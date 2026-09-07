import sys,os,glob,logging,random,statistics as st
sys.path.insert(0,'/opt/trading_bot/live_bot')
sys.path.insert(0,'/opt/trading_bot/repo/fno_t_bot/research')
logging.disable(logging.CRITICAL)
import pandas as pd
from options_bot import TradingBot
from deflated_sharpe import deflated_sharpe, sharpe
from scipy import stats as sps
random.seed(31)
print("PATH_MR — multi-day mean-reversion directional rule")
print("  at 11:00: 5-day trend in top tertile -> PUT; bottom tertile -> CALL; else no trade")
print("  exits in INDEX POINTS: stop 1 ATR / target 2 ATR / force-close 14:30")
print("  (index points, so no option-pricing model contaminates the read)\n")
DIRS={'NIFTY':'nifty_5min','BANKNIFTY':'banknifty_5min','SENSEX':'sensex_5min'}
raw=[]
for inst,sub in DIRS.items():
    files=sorted(glob.glob(f'/opt/trading_bot/data/{sub}/*.csv'))
    hist=[]
    for fi in range(len(files)):
        day=os.path.basename(files[fi]).split('_')[-1].replace('.csv','')
        try: d0=pd.read_csv(files[fi],parse_dates=['ts'],index_col='ts')
        except Exception: continue
        if len(d0)<25: continue
        rec=dict(day=day,o=float(d0['Open'].iloc[0]),c=float(d0['Close'].iloc[-1]))
        if len(hist)>=5:
            t5=(rec['o']-hist[-5]['c'])/hist[-5]['c']*100
            try:
                da=pd.concat([pd.read_csv(f,parse_dates=['ts'],index_col='ts') for f in files[max(0,fi-3):fi+1]]).sort_index()
                da=da[~da.index.duplicated(keep='first')]; da=TradingBot(inst).add_indicators(da)
                k=f'{day[:4]}-{day[4:6]}-{day[6:]}'; dd=da[da.index.date.astype(str)==k]
                idx=[i for i,t in enumerate(dd.index) if t.strftime('%H:%M')=='11:00']
                if idx:
                    i=idx[0]; S=float(dd['Close'].iloc[i]); atr=float(dd['ATR'].iloc[i] or 0)
                    fwd=dd.iloc[i+1:]; fwd=fwd[[x.strftime('%H:%M')<='14:30' for x in fwd.index]]
                    if atr>0 and len(fwd)>6:
                        raw.append(dict(inst=inst,day=day,t5=t5,S=S,atr=atr,
                                        path=list(zip(fwd['High'].astype(float),
                                                      fwd['Low'].astype(float),
                                                      fwd['Close'].astype(float)))))
            except Exception: pass
        hist.append(rec)
raw.sort(key=lambda z:z['day'])
def walk(r,d):
    S,atr=r['S'],r['atr']
    for hi,lo,cl in r['path']:
        adv=(S-lo) if d=='CALL' else (hi-S)
        fav=(hi-S) if d=='CALL' else (S-lo)
        if adv>=1.0*atr: return -1.0
        if fav>=2.0*atr: return  2.0
    cl=r['path'][-1][2]
    return ((cl-S) if d=='CALL' else (S-cl))/atr
ts=sorted(x['t5'] for x in raw); lo_c=ts[len(ts)//3]; hi_c=ts[2*len(ts)//3]
print(f"  tertile cuts: 5-day trend  <= {lo_c:+.2f}%  (CALL)   >= {hi_c:+.2f}%  (PUT)\n")
sig=[];ctl=[]
for r in raw:
    if r['t5']>=hi_c: d='PUT'
    elif r['t5']<=lo_c: d='CALL'
    else: continue
    sig.append(dict(day=r['day'],inst=r['inst'],pnl=walk(r,d)))
    ctl.append(walk(r,random.choice(['CALL','PUT'])))
p=[x['pnl'] for x in sig]; n=len(p)
w=sum(1 for x in p if x>0)
print(f"SIGNAL   n={n}  {w}W/{n-w}L ({100*w/n:.1f}%)  mean {st.mean(p):+.3f} ATR  "
      f"total {sum(p):+.1f} ATR  Sharpe {sharpe(p):+.3f}")
cw=sum(1 for x in ctl if x>0)
print(f"CONTROL  n={len(ctl)}  {cw}W/{len(ctl)-cw}L ({100*cw/len(ctl):.1f}%)  "
      f"mean {st.mean(ctl):+.3f} ATR  total {sum(ctl):+.1f} ATR")
print(f"  edge over coin-flip: {st.mean(p)-st.mean(ctl):+.3f} ATR/trade  "
      f"Mann-Whitney p={sps.mannwhitneyu(p,ctl,alternative='greater').pvalue:.4f}")
d=deflated_sharpe(p,n_trials=4)
print(f"\n  DSR (n_trials=4: lookbacks 3/5 x this entry time) = {d['dsr']*100:.1f}%")
print(f"  {d['verdict']}")
print("\nBY PERIOD (the regime question):")
h=n//2
for lbl,sl in (('first half',sig[:h]),('second half',sig[h:])):
    v=[x['pnl'] for x in sl]
    print(f"  {lbl:12s} {sl[0]['day']}..{sl[-1]['day']}  n={len(v):3d}  "
          f"{100*sum(1 for z in v if z>0)/len(v):5.1f}% win  mean {st.mean(v):+.3f} ATR")
print("\nBY INSTRUMENT:")
for inst in DIRS:
    v=[x['pnl'] for x in sig if x['inst']==inst]
    if len(v)<15: continue
    print(f"  {inst:10s} n={len(v):3d}  {100*sum(1 for z in v if z>0)/len(v):5.1f}% win  "
          f"mean {st.mean(v):+.3f} ATR")
print("\n2026 ONLY (where the effect is live):")
v=[x['pnl'] for x in sig if x['day']>='20260101']
print(f"  n={len(v)}  {100*sum(1 for z in v if z>0)/len(v):.1f}% win  mean {st.mean(v):+.3f} ATR "
      f" Sharpe {sharpe(v):+.3f}")

print("\n"+"="*72)
print("DOSE-RESPONSE — does a STRONGER 5-day move produce a better fade?")
print("Noise does not usually produce a monotone dose-response curve.")
print("="*72)
allr=[]
for r in raw:
    d='PUT' if r['t5']>0 else 'CALL'
    allr.append(dict(day=r['day'],inst=r['inst'],mag=abs(r['t5']),pnl=walk(r,d),
                     ctl=walk(r,random.choice(['CALL','PUT']))))
allr.sort(key=lambda z:z['mag']); N=len(allr); q=N//5
print(f"  n={N}  (every session, direction = fade the 5-day move)")
print(f"  {'|5-day move|':>16s} {'n':>4s} {'win%':>7s} {'mean ATR':>10s} {'control':>9s} {'edge':>8s}")
for i in range(5):
    b=allr[i*q:(i+1)*q]
    v=[x['pnl'] for x in b]; c=[x['ctl'] for x in b]
    print(f"  {b[0]['mag']:5.2f}-{b[-1]['mag']:5.2f}%   {len(v):4d} "
          f"{100*sum(1 for z in v if z>0)/len(v):6.1f}% {st.mean(v):+10.3f} "
          f"{st.mean(c):+9.3f} {st.mean(v)-st.mean(c):+8.3f}")
rho,p=sps.spearmanr([x['mag'] for x in allr],[x['pnl'] for x in allr])
print(f"\n  rho(|5-day move|, fade outcome) = {rho:+.4f}  p={p:.4f}")
print("\n  TOP DECILE ONLY (strongest 10% of multi-day moves):")
dd=allr[-N//10:]
v=[x['pnl'] for x in dd]; c=[x['ctl'] for x in dd]
print(f"    n={len(v)}  {100*sum(1 for z in v if z>0)/len(v):.1f}% win  mean {st.mean(v):+.3f} ATR"
      f"   control {st.mean(c):+.3f}   Sharpe {sharpe(v):+.3f}")
print(f"    Mann-Whitney vs control p={sps.mannwhitneyu(v,c,alternative='greater').pvalue:.4f}")
d2=deflated_sharpe(v,n_trials=6)
print(f"    DSR (n_trials=6) = {d2['dsr']*100:.1f}%  {d2['verdict'][:44]}")
v26=[x['pnl'] for x in dd if x['day']>='20260101']
if len(v26)>15:
    print(f"    2026 only: n={len(v26)}  {100*sum(1 for z in v26 if z>0)/len(v26):.1f}% win  "
          f"mean {st.mean(v26):+.3f} ATR")
