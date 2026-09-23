import sys,os,glob,logging,random,statistics as st
sys.path.insert(0,r'C:\quant_trading\MyRepo\fno_t_bot')
sys.path.insert(0,r'C:\quant_trading\MyRepo\fno_t_bot\research')
logging.disable(logging.CRITICAL)
import pandas as pd
from options_bot import TradingBot
from deflated_sharpe import deflated_sharpe, sharpe
from scipy import stats as sps
random.seed(31)
D=r'C:\quant_trading\data'
DIRS={'NIFTY':'nifty_5min','BANKNIFTY':'banknifty_5min','SENSEX':'sensex_5min'}
print("PATH_MR RE-TEST ON FULL LOCAL DATA (was 20% of NIFTY/BNF on EC2)\n")
sess=[]
for inst,sub in DIRS.items():
    files=sorted(glob.glob(os.path.join(D,sub,'*.csv')))
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
                        sess.append(dict(inst=inst,day=day,t5=t5,S=S,atr=atr,
                            ret=(float(fwd['Close'].iloc[-1])-S)/atr,
                            path=list(zip(fwd['High'].astype(float),fwd['Low'].astype(float),fwd['Close'].astype(float)))))
            except Exception: pass
        hist.append(rec)
sess.sort(key=lambda z:z['day']); n=len(sess)
cnt={}
for s in sess: cnt[s['inst']]=cnt.get(s['inst'],0)+1
print(f"sessions: {n}   {cnt}   span {sess[0]['day']}..{sess[-1]['day']}\n")
rho,p=sps.spearmanr([s['t5'] for s in sess],[s['ret'] for s in sess])
print(f"POOLED  rho(5-day trend, 11:00->14:30) = {rho:+.4f}  p={p:.5f}")
for inst in DIRS:
    v=[s for s in sess if s['inst']==inst]
    r,pp=sps.spearmanr([s['t5'] for s in v],[s['ret'] for s in v])
    print(f"  {inst:10s} n={len(v):3d}  rho={r:+.4f}  p={pp:.4f}")
print("\nREGIME CHECK — was the 'shift' an artifact of the EC2 data gap?")
h=n//2
for lbl,sl in (('first half',sess[:h]),('second half',sess[h:])):
    c={}
    for s in sl: c[s['inst']]=c.get(s['inst'],0)+1
    r,pp=sps.spearmanr([s['t5'] for s in sl],[s['ret'] for s in sl])
    print(f"  {lbl:12s} {sl[0]['day']}..{sl[-1]['day']} n={len(sl):3d} rho={r:+.4f} p={pp:.4f}  {c}")
t=n//3
for i,l in enumerate(('T1','T2','T3')):
    sl=sess[i*t:(i+1)*t]
    r,pp=sps.spearmanr([s['t5'] for s in sl],[s['ret'] for s in sl])
    print(f"  {l} {sl[0]['day']}..{sl[-1]['day']} n={len(sl):3d}  rho={r:+.4f}  p={pp:.4f}")
def walk(r,d):
    S,atr=r['S'],r['atr']
    for hi,lo,cl in r['path']:
        adv=(S-lo) if d=='CALL' else (hi-S)
        fav=(hi-S) if d=='CALL' else (S-lo)
        if adv>=1.0*atr: return -1.0
        if fav>=2.0*atr: return 2.0
    cl=r['path'][-1][2]
    return ((cl-S) if d=='CALL' else (S-cl))/atr
ts=sorted(abs(x['t5']) for x in sess); dec=ts[int(.9*len(ts))]
print(f"\nTRADEABLE RULE — top decile |5-day move| >= {dec:.2f}%")
sig=[];ctl=[]
for r in sess:
    if abs(r['t5'])<dec: continue
    sig.append(walk(r,'PUT' if r['t5']>0 else 'CALL')); ctl.append(walk(r,random.choice(['CALL','PUT'])))
w=sum(1 for x in sig if x>0)
print(f"  SIGNAL  n={len(sig)}  {100*w/len(sig):.1f}% win  mean {st.mean(sig):+.3f} ATR  Sharpe {sharpe(sig):+.3f}")
print(f"  CONTROL n={len(ctl)}  mean {st.mean(ctl):+.3f} ATR")
print(f"  edge {st.mean(sig)-st.mean(ctl):+.3f} ATR/trade   "
      f"Mann-Whitney p={sps.mannwhitneyu(sig,ctl,alternative='greater').pvalue:.4f}")
d=deflated_sharpe(sig,n_trials=6)
print(f"  DSR (n_trials=6) = {d['dsr']*100:.1f}%   {d['verdict'][:46]}")
