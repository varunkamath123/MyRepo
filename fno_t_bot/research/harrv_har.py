import sys,os,glob,logging,math,statistics as st
sys.path.insert(0,'/opt/trading_bot/live_bot')
logging.disable(logging.CRITICAL)
import numpy as np, pandas as pd
from scipy import stats as sps
print("HAR-RV vs the current rv_iv ratio")
print("Corsi (2009): RV_t+1 = b0 + b_d*RV_d + b_w*RV_w + b_m*RV_m, plain OLS.")
print("Forecasts are OUT-OF-SAMPLE: each day is predicted from an expanding")
print("window of days STRICTLY BEFORE it. No peeking.\n")
DIRS={'NIFTY':'nifty_5min','BANKNIFTY':'banknifty_5min','SENSEX':'sensex_5min'}
IV={}
for u,inst in (('nifty','NIFTY'),('banknifty','BANKNIFTY')):
    try:
        for line in open(f'/tmp/iv_{u}.txt',encoding='utf-8',errors='ignore'):
            p=line.split()
            if len(p)==2:
                try: IV[(inst,p[0])]=float(p[1])
                except Exception: pass
    except Exception: pass
print(f"ATM-IV stamps available: {len(IV):,}\n")

out={}
for inst,sub in DIRS.items():
    files=sorted(glob.glob(f'/opt/trading_bot/data/{sub}/*.csv'))
    rows=[]
    for f in files:
        day=os.path.basename(f).split('_')[-1].replace('.csv','')
        try: d=pd.read_csv(f,parse_dates=['ts'],index_col='ts')
        except Exception: continue
        if len(d)<20: continue
        c=d['Close'].astype(float)
        r=np.diff(np.log(c.values))
        if len(r)<10: continue
        rv=float(np.sqrt(np.sum(r**2))*math.sqrt(252))      # annualised realised vol
        # trailing HV exactly as the bot computes it (close-to-close, 30 bars)
        rows.append(dict(day=day, rv=rv, close=float(c.iloc[-1]),
                         o=float(d['Open'].iloc[0]),
                         hi=float(d['High'].max()), lo=float(d['Low'].min())))
    if len(rows)<60: continue
    df=pd.DataFrame(rows).sort_values('day').reset_index(drop=True)
    df['rv_w']=df['rv'].rolling(5).mean()
    df['rv_m']=df['rv'].rolling(22).mean()
    # expanding-window out-of-sample HAR forecast
    fc=[np.nan]*len(df)
    for i in range(40,len(df)):
        tr=df.iloc[:i].dropna(subset=['rv','rv_w','rv_m'])
        if len(tr)<30: continue
        X=np.column_stack([np.ones(len(tr)-1), tr['rv'].values[:-1],
                           tr['rv_w'].values[:-1], tr['rv_m'].values[:-1]])
        y=tr['rv'].values[1:]                       # predict NEXT day's RV
        try: b,_,_,_=np.linalg.lstsq(X,y,rcond=None)
        except Exception: continue
        prev=df.iloc[i-1]
        if pd.isna(prev['rv_w']) or pd.isna(prev['rv_m']): continue
        fc[i]=float(b[0]+b[1]*prev['rv']+b[2]*prev['rv_w']+b[3]*prev['rv_m'])
    df['har']=fc
    df['hv_naive']=df['rv'].shift(1)                # yesterday's RV = the crude proxy
    out[inst]=df

print("1. DOES HAR ACTUALLY FORECAST RV BETTER THAN 'YESTERDAY'S RV'?")
print(f"   {'inst':10s} {'n':>5s} {'HAR RMSE':>10s} {'naive RMSE':>11s} {'HAR corr':>9s} {'naive':>8s}")
for inst,df in out.items():
    v=df.dropna(subset=['har','hv_naive','rv'])
    if len(v)<40: continue
    eh=np.sqrt(np.mean((v['har']-v['rv'])**2)); en=np.sqrt(np.mean((v['hv_naive']-v['rv'])**2))
    ch=np.corrcoef(v['har'],v['rv'])[0,1]; cn=np.corrcoef(v['hv_naive'],v['rv'])[0,1]
    print(f"   {inst:10s} {len(v):5d} {eh:10.4f} {en:11.4f} {ch:9.3f} {cn:8.3f}"
          f"   {'HAR better' if eh<en else 'naive better'}")

print("\n2. AS A PREMIUM-RICHNESS SIGNAL: har/IV vs the current hv/IV")
print("   Question: which ratio better predicts the NEXT DAY's realised move?")
rows=[]
for inst,df in out.items():
    if inst=='SENSEX': continue                     # no chain IV
    for i in range(len(df)-1):
        r=df.iloc[i]
        if pd.isna(r['har']) or pd.isna(r['hv_naive']): continue
        ivs=[v for (k,d),v in IV.items() if k==inst and d[:10]==
             f"{r['day'][:4]}-{r['day'][4:6]}-{r['day'][6:]}"]
        if not ivs: continue
        iv=float(np.median(ivs))
        if iv<=0: continue
        nxt=df.iloc[i+1]
        realised=abs(nxt['close']-nxt['o'])/nxt['o']*100
        rng=(nxt['hi']-nxt['lo'])/nxt['o']*100
        rows.append(dict(inst=inst, har_iv=r['har']*100/iv, hv_iv=r['hv_naive']*100/iv,
                         realised=realised, rng=rng))
print(f"   n={len(rows)} day pairs with both a forecast and an IV")
if len(rows)>40:
    R=pd.DataFrame(rows)
    for tgt,lbl in (('realised','next-day |open->close| move'),('rng','next-day range')):
        a=sps.spearmanr(R['har_iv'],R[tgt]); b=sps.spearmanr(R['hv_iv'],R[tgt])
        print(f"   vs {lbl}:")
        print(f"      HAR/IV  rho {a[0]:+.4f}  p={a[1]:.4f}")
        print(f"      hv/IV   rho {b[0]:+.4f}  p={b[1]:.4f}   (what the bot uses today)")
        print(f"      -> {'HAR is the better signal' if abs(a[0])>abs(b[0]) else 'no improvement from HAR'}")
