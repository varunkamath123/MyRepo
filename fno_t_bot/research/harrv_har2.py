import sys,os,glob,logging,math,statistics as st
sys.path.insert(0,'/opt/trading_bot/live_bot')
logging.disable(logging.CRITICAL)
import numpy as np, pandas as pd
from scipy import stats as sps
from options_bot import TradingBot
print("MATCHED-HORIZON TEST — the horizon the gate actually uses")
print("rv_iv is evaluated AT ENTRY and the claim is about the forward 60-min move.")
print("So compare, at each intraday bar: HAR-forecast/IV vs bot-HV/IV.\n")
IV={}
for u,inst in (('nifty','NIFTY'),('banknifty','BANKNIFTY')):
    for line in open(f'/tmp/iv_{u}.txt',encoding='utf-8',errors='ignore'):
        p=line.split()
        if len(p)==2:
            try: IV[(inst,p[0])]=float(p[1])
            except Exception: pass
DIRS={'NIFTY':'nifty_5min','BANKNIFTY':'banknifty_5min'}
# --- daily RV series + out-of-sample HAR forecast per day --------------------
HAR={}
for inst,sub in DIRS.items():
    files=sorted(glob.glob(f'/opt/trading_bot/data/{sub}/*.csv'))
    rec=[]
    for f in files:
        day=os.path.basename(f).split('_')[-1].replace('.csv','')
        try: d=pd.read_csv(f,parse_dates=['ts'],index_col='ts')
        except Exception: continue
        if len(d)<20: continue
        r=np.diff(np.log(d['Close'].astype(float).values))
        if len(r)<10: continue
        rec.append((day, float(np.sqrt(np.sum(r**2))*math.sqrt(252))))
    df=pd.DataFrame(rec,columns=['day','rv']).sort_values('day').reset_index(drop=True)
    df['rv_w']=df['rv'].rolling(5).mean(); df['rv_m']=df['rv'].rolling(22).mean()
    for i in range(40,len(df)):
        tr=df.iloc[:i].dropna(subset=['rv','rv_w','rv_m'])
        if len(tr)<30: continue
        X=np.column_stack([np.ones(len(tr)-1),tr['rv'].values[:-1],
                           tr['rv_w'].values[:-1],tr['rv_m'].values[:-1]])
        try: b,_,_,_=np.linalg.lstsq(X,df['rv'].values[1:i][-(len(tr)-1):],rcond=None)
        except Exception: continue
        p=df.iloc[i-1]
        if pd.isna(p['rv_w']) or pd.isna(p['rv_m']): continue
        HAR[(inst,df.iloc[i]['day'])]=float(b[0]+b[1]*p['rv']+b[2]*p['rv_w']+b[3]*p['rv_m'])
print(f"HAR day-forecasts built: {len(HAR)}")
rows=[]
for inst,sub in DIRS.items():
    files=sorted(glob.glob(f'/opt/trading_bot/data/{sub}/*.csv'))
    for fi in range(3,len(files)):
        day=os.path.basename(files[fi]).split('_')[-1].replace('.csv','')
        if day<'20260428' or (inst,day) not in HAR: continue
        try:
            da=pd.concat([pd.read_csv(f,parse_dates=['ts'],index_col='ts') for f in files[fi-3:fi+1]]).sort_index()
            da=da[~da.index.duplicated(keep='first')]; da=TradingBot(inst).add_indicators(da)
        except Exception: continue
        k=f'{day[:4]}-{day[4:6]}-{day[6:]}'; dd=da[da.index.date.astype(str)==k]
        if len(dd)<30 or 'HV' not in dd.columns: continue
        har=HAR[(inst,day)]
        for i in range(6,len(dd)-12):
            t=dd.index[i]; hm=t.strftime('%H:%M')
            if not ('09:45'<=hm<='13:00'): continue
            iv=IV.get((inst,t.strftime('%Y-%m-%dT%H:%M')))
            if not iv or iv<=0: continue
            hv=dd['HV'].iloc[i]; atr=dd['ATR'].iloc[i]
            if not hv or hv!=hv or not atr or atr<=0: continue
            S=float(dd['Close'].iloc[i]); nxt=dd.iloc[i+1:i+13]
            mv=max(abs(float(nxt['High'].max())-S),abs(S-float(nxt['Low'].min())))/float(atr)
            rows.append(dict(hv_iv=float(hv)*100/iv, har_iv=har*100/iv, fwd=mv))
n=len(rows); print(f"intraday observations: {n:,}\n")
if n>200:
    R=pd.DataFrame(rows)
    R=R[(R.hv_iv>0.05)&(R.hv_iv<5)&(R.har_iv>0.05)&(R.har_iv<5)]
    print(f"after sanity filter: n={len(R):,}")
    a=sps.spearmanr(R['har_iv'],R['fwd']); b=sps.spearmanr(R['hv_iv'],R['fwd'])
    print(f"\n  rho(HAR/IV, forward 60-min move) = {a[0]:+.4f}  p={a[1]:.3e}")
    print(f"  rho(hv /IV, forward 60-min move) = {b[0]:+.4f}  p={b[1]:.3e}   <- in production")
    print(f"\n  -> {'HAR/IV is the STRONGER signal' if abs(a[0])>abs(b[0]) else 'the crude hv/IV remains stronger'}")
    print("\n  quintiles (lower ratio should precede LARGER moves):")
    for lbl,col in (('HAR/IV','har_iv'),('hv/IV','hv_iv')):
        q=R.sort_values(col); m=len(q)//5
        vals=[q.iloc[i*m:(i+1)*m]['fwd'].median() for i in range(5)]
        print(f"    {lbl:8s} " + "  ".join(f"{v:.2f}" for v in vals) +
              f"   spread {vals[0]-vals[-1]:+.2f} ATR")
