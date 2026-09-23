import glob,os,math,statistics as st
import numpy as np, pandas as pd
from scipy import stats as sps
print("CROSS-SECTIONAL MEAN REVERSION ON THE STOCK UNIVERSE")
print("The index finding (PATH_MR): 5-day move predicts the NEXT move, inverted.")
print("  index sample: 417 sessions, rho -0.072 pooled, no instrument significant alone.")
print("Same test on 1,735 NSE stocks -- orders of magnitude more observations.\n")
F=sorted(glob.glob(r'C:\quant_trading\swing_bot\.cache\prices\*.parquet'))
rows=[]
for f in F:
    try: d=pd.read_parquet(f)
    except Exception: continue
    if len(d)<120: continue
    cols={c.lower():c for c in d.columns}
    if not {'open','close'}<=set(cols): continue
    o=d[cols['open']].astype(float).values; c=d[cols['close']].astype(float).values
    if np.any(~np.isfinite(c)) or np.any(c<=0): continue
    sym=os.path.basename(f).replace('_NS.parquet','')
    for i in range(6,len(c)-1):
        ref=c[i-5]
        if ref<=0: continue
        mv5=(o[i]-ref)/ref*100                    # 5-day move into today's open
        fwd=(c[i]-o[i])/o[i]*100                  # today's open->close
        nxt=(c[i+1]-o[i+1])/o[i+1]*100            # tomorrow's open->close
        if abs(mv5)>40: continue                  # drop splits/garbage
        rows.append((mv5,fwd,nxt))
R=pd.DataFrame(rows,columns=['mv5','fwd','nxt'])
print(f"observations: {len(R):,}  (vs 417 index sessions)")
print(f"stocks used: {len(F):,}\n")
for tgt,lbl in (('fwd','same-day open->close'),('nxt','NEXT day open->close')):
    rho,p=sps.spearmanr(R['mv5'],R[tgt])
    print(f"  rho(5-day move, {lbl}) = {rho:+.4f}  p={p:.3e}")
print("\n  quintiles of the 5-day move (mean reversion => most-DOWN should do best):")
q=R.sort_values('mv5'); m=len(q)//5
print(f"    {'band':>16s} {'n':>7s} {'same-day':>10s} {'next-day':>10s}")
for i,nm in enumerate(('most DOWN','Q2','Q3','Q4','most UP')):
    b=q.iloc[i*m:(i+1)*m]
    print(f"    {nm:>16s} {len(b):7d} {b['fwd'].mean():+10.3f}% {b['nxt'].mean():+10.3f}%")
lo=q.iloc[:m]; hi=q.iloc[-m:]
print(f"\n  long most-DOWN / short most-UP, same day : {lo['fwd'].mean()-hi['fwd'].mean():+.3f}% per leg-pair")
print(f"  long most-DOWN / short most-UP, next day : {lo['nxt'].mean()-hi['nxt'].mean():+.3f}% per leg-pair")
u,pv=sps.mannwhitneyu(lo['nxt'],hi['nxt'],alternative='greater')
print(f"  Mann-Whitney (down beats up, next day) p={pv:.3e}")
print("\n  NOTE: daily bars only -- this is an overnight+intraday hold, not an")
print("  intraday strategy. Costs (~0.05%/round trip) are NOT deducted above.")
