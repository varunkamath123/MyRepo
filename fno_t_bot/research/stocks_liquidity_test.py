import glob,os,math,statistics as st
import numpy as np, pandas as pd
from scipy import stats as sps
print("LIQUIDITY-RESTRICTED, COST-CHARGED, HOLDOUT-TESTED")
print("Daily portfolio: long the most-DOWN quintile by 5-day move, short the")
print("most-UP, held open->close, rebalanced daily. Market-neutral by construction.\n")
F=sorted(glob.glob(r'C:\quant_trading\swing_bot\.cache\prices\*.parquet'))
data={}
turn={}
for f in F:
    try: d=pd.read_parquet(f)
    except Exception: continue
    if len(d)<120: continue
    c={k.lower():k for k in d.columns}
    if not {'open','close','volume'}<=set(c): continue
    o=d[c['open']].astype(float); cl=d[c['close']].astype(float); v=d[c['volume']].astype(float)
    if (cl<=0).any() or not np.isfinite(cl).all(): continue
    sym=os.path.basename(f).replace('_NS.parquet','')
    df=pd.DataFrame({'o':o.values,'c':cl.values,'v':v.values}, index=pd.to_datetime(d.index))
    df['turnover']=df['c']*df['v']
    t=float(df['turnover'].median())
    if not np.isfinite(t) or t<=0: continue
    data[sym]=df; turn[sym]=t
print(f"stocks loaded: {len(data):,}")
rank=sorted(turn.items(), key=lambda z:-z[1])
tiers={'top 100':[s for s,_ in rank[:100]],
       'top 200':[s for s,_ in rank[:200]],
       'top 500':[s for s,_ in rank[:500]],
       'all':[s for s,_ in rank]}
print(f"median daily turnover: top100 Rs{turn[rank[99][0]]/1e7:.1f}cr  "
      f"top200 Rs{turn[rank[199][0]]/1e7:.1f}cr  top500 Rs{turn[rank[499][0]]/1e7:.1f}cr\n")

def backtest(syms, cost_leg):
    """cost_leg = all-in round-trip cost per leg, as a fraction."""
    byday={}
    for s in syms:
        df=data[s]; o=df['o'].values; c=df['c'].values; idx=df.index
        for i in range(6,len(c)):
            ref=c[i-5]
            if ref<=0 or o[i]<=0: continue
            mv=(o[i]-ref)/ref*100
            if abs(mv)>40: continue
            byday.setdefault(idx[i],[]).append((mv,(c[i]-o[i])/o[i]*100))
    rets=[]
    for d in sorted(byday):
        v=byday[d]
        if len(v)<40: continue
        v.sort(key=lambda z:z[0]); m=max(len(v)//5,1)
        lo=np.mean([x[1] for x in v[:m]]); hi=np.mean([x[1] for x in v[-m:]])
        rets.append((d, lo-hi-2*cost_leg*100))
    return rets

print(f"  {'universe':>10s} {'cost/leg':>9s} {'days':>5s} {'mean/day':>9s} {'ann':>8s} "
      f"{'Sharpe':>8s} {'win%':>6s}")
best=None
for name,syms in tiers.items():
    for cl_ in (0.0005, 0.0010, 0.0015):
        r=backtest(syms, cl_)
        if len(r)<60: continue
        v=[x[1] for x in r]
        mu=st.mean(v); sd=st.pstdev(v) or 1e-9
        shp=mu/sd*math.sqrt(252)
        print(f"  {name:>10s} {cl_*100:8.2f}% {len(v):5d} {mu:+8.3f}% {mu*252:+7.0f}% "
              f"{shp:+8.2f} {100*sum(1 for x in v if x>0)/len(v):5.1f}%")
        if cl_==0.0010 and (best is None or mu>best[1]): best=(name,mu,r)
print()
if best:
    name,mu,r=best
    r.sort(key=lambda z:z[0]); h=len(r)//2
    print(f"  CHRONOLOGICAL HOLDOUT ({name}, 0.10%/leg):")
    for lbl,sl in (('first half',r[:h]),('HOLDOUT second half',r[h:])):
        v=[x[1] for x in sl]
        mu2=st.mean(v); sd2=st.pstdev(v) or 1e-9
        print(f"    {lbl:22s} {sl[0][0].date()}..{sl[-1][0].date()}  n={len(v):4d}  "
              f"mean {mu2:+.3f}%/day  Sharpe {mu2/sd2*math.sqrt(252):+.2f}  "
              f"win {100*sum(1 for x in v if x>0)/len(v):.1f}%")
    v=[x[1] for x in r]
    t_,pv=sps.ttest_1samp(v,0)
    print(f"\n    mean != 0 ?  t={t_:+.2f}  p={pv:.3e}")
