import sys,os,glob,logging,math
sys.path.insert(0,'/opt/trading_bot/live_bot')
logging.disable(logging.CRITICAL)
import numpy as np, pandas as pd
from scipy import stats as sps
from options_bot import TradingBot
print("IF LOCALITY IS THE MECHANISM, WINDOW LENGTH SHOULD MATTER")
print("HAR (one value per day) scored rho +0.009. The bot's 30-bar HV scored")
print("-0.183. So sweep the realised-vol window and find where the signal peaks.\n")
IV={}
for u,inst in (('nifty','NIFTY'),('banknifty','BANKNIFTY')):
    for line in open(f'/tmp/iv_{u}.txt',encoding='utf-8',errors='ignore'):
        p=line.split()
        if len(p)==2:
            try: IV[(inst,p[0])]=float(p[1])
            except Exception: pass
WINS=[6,12,20,30,50,78]
rows=[]
for inst,sub in (('NIFTY','nifty_5min'),('BANKNIFTY','banknifty_5min')):
    files=sorted(glob.glob(f'/opt/trading_bot/data/{sub}/*.csv'))
    for fi in range(3,len(files)):
        day=os.path.basename(files[fi]).split('_')[-1].replace('.csv','')
        if day<'20260428': continue
        try:
            da=pd.concat([pd.read_csv(f,parse_dates=['ts'],index_col='ts') for f in files[fi-3:fi+1]]).sort_index()
            da=da[~da.index.duplicated(keep='first')]; da=TradingBot(inst).add_indicators(da)
        except Exception: continue
        c=da['Close'].astype(float)
        lr=np.log(c).diff()
        hvw={w: lr.rolling(w).std()*math.sqrt(252*75) for w in WINS}   # 75 bars/day
        k=f'{day[:4]}-{day[4:6]}-{day[6:]}'
        mask=da.index.date.astype(str)==k
        dd=da[mask]
        if len(dd)<30: continue
        for i in range(6,len(dd)-12):
            t=dd.index[i]; hm=t.strftime('%H:%M')
            if not ('09:45'<=hm<='13:00'): continue
            iv=IV.get((inst,t.strftime('%Y-%m-%dT%H:%M')))
            if not iv or iv<=0: continue
            atr=dd['ATR'].iloc[i]
            if not atr or atr<=0: continue
            S=float(dd['Close'].iloc[i]); nxt=dd.iloc[i+1:i+13]
            fwd=max(abs(float(nxt['High'].max())-S),abs(S-float(nxt['Low'].min())))/float(atr)
            r=dict(fwd=fwd)
            ok=True
            for w in WINS:
                v=hvw[w].get(t, np.nan)
                if v!=v or v<=0: ok=False; break
                r[f'w{w}']=float(v)*100/iv
            if ok: rows.append(r)
R=pd.DataFrame(rows)
print(f"observations: {len(R):,}\n")
if len(R)>300:
    print(f"  {'HV window':>12s} {'rho':>9s} {'p':>12s}  {'q1 med':>7s} {'q5 med':>7s} {'spread':>8s}")
    best=None
    for w in WINS:
        col=f'w{w}'
        s=R[(R[col]>0.05)&(R[col]<5)]
        if len(s)<300: continue
        rho,p=sps.spearmanr(s[col],s['fwd'])
        q=s.sort_values(col); m=len(q)//5
        q1=q.iloc[:m]['fwd'].median(); q5=q.iloc[-m:]['fwd'].median()
        tag=' <- production' if w==30 else ''
        print(f"  {w:9d} bar {rho:+9.4f} {p:12.2e}  {q1:7.2f} {q5:7.2f} {q1-q5:+8.2f}{tag}")
        if best is None or abs(rho)>abs(best[1]): best=(w,rho,p,q1-q5)
    print(f"\n  strongest: {best[0]}-bar window, rho {best[1]:+.4f}, spread {best[3]:+.2f} ATR")
    print(f"  production is 30-bar. {'Worth changing.' if best[0]!=30 else 'Already optimal.'}")
    print("\n  NOTE: this is a 6-point sweep on one dataset -- a candidate, not a")
    print("  conclusion. Any change needs a holdout before it goes near the live gate.")
