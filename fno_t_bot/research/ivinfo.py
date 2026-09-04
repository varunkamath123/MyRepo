import sys,os,glob,logging,statistics as st
sys.path.insert(0,'/opt/trading_bot/live_bot')
logging.disable(logging.CRITICAL)
import pandas as pd
from options_bot import TradingBot
print("CONTROL: does IV add information BEYOND trailing HV alone?")
print("Does IV carry information trailing realised vol does not?")
print("H: low rv_iv (chain prices MORE move than has occurred) precedes LARGER forward moves.")
print("Directionless, so no strategy assumptions. Overlapping windows -> p optimistic.\n")
IV={}
for u,inst in (('nifty','NIFTY'),('banknifty','BANKNIFTY')):
    for line in open(f'/tmp/iv_{u}.txt',encoding='utf-8',errors='ignore'):
        p=line.split()
        if len(p)==2:
            try: IV[(inst,p[0])]=float(p[1])
            except Exception: pass
print(f"ATM-IV stamps loaded: {len(IV):,}")
rows=[]
for inst,sub in (('NIFTY','nifty_5min'),('BANKNIFTY','banknifty_5min')):
    files=sorted(glob.glob(f'/opt/trading_bot/data/{sub}/*.csv'))
    for fi in range(3,len(files)):
        day=os.path.basename(files[fi]).split('_')[-1].replace('.csv','')
        if day<'20260428': continue
        try:
            da=pd.concat([pd.read_csv(f,parse_dates=['ts'],index_col='ts') for f in files[fi-3:fi+1]]).sort_index()
            da=da[~da.index.duplicated(keep='first')]
            da=TradingBot(inst).add_indicators(da)
        except Exception: continue
        k=f'{day[:4]}-{day[4:6]}-{day[6:]}'; dd=da[da.index.date.astype(str)==k]
        if len(dd)<30 or 'HV' not in dd.columns: continue
        for i in range(6,len(dd)-12):
            t=dd.index[i]
            iv=IV.get((inst,t.strftime('%Y-%m-%dT%H:%M')))
            if not iv or iv<=0: continue
            hv=dd['HV'].iloc[i]; atr=dd['ATR'].iloc[i]
            if not hv or hv!=hv or not atr or atr<=0: continue
            rv_iv=float(hv)*100.0/iv
            if not (0.05<rv_iv<5): continue
            e=float(dd['Close'].iloc[i]); nxt=dd.iloc[i+1:i+13]   # next 60 min
            mv=max(abs(float(nxt['High'].max())-e),abs(e-float(nxt['Low'].min())))
            rows.append((rv_iv, mv/float(atr)))
n=len(rows); print(f"observations (5-min spaced): {n:,}\n")
if n>200:
    rows.sort()
    print(f"  {'rv_iv band':>18s} {'n':>6s} {'median fwd 60m move':>22s}")
    qs=[rows[i*n//5:(i+1)*n//5] for i in range(5)]
    for b in qs:
        lo,hi=b[0][0],b[-1][0]
        print(f"  {lo:6.2f} - {hi:5.2f}   {len(b):6d} {st.median(x[1] for x in b):18.2f} ATR")
    try:
        from scipy import stats as sps
        a=[x[0] for x in rows]; c=[x[1] for x in rows]
        rho,p=sps.spearmanr(a,c)
        print(f"\n  Spearman rho(rv_iv, forward move) = {rho:+.4f}  p={p:.2e}  n={n:,}")
        lo=[x[1] for x in rows if x[0]<0.70]; hi=[x[1] for x in rows if x[0]>=0.70]
        u,pu=sps.mannwhitneyu(lo,hi,alternative='greater')
        print(f"  rv_iv<0.70 (n={len(lo):,}) median {st.median(lo):.2f} ATR  vs  "
              f">=0.70 (n={len(hi):,}) median {st.median(hi):.2f} ATR")
        print(f"  Mann-Whitney one-sided (low moves MORE) p = {pu:.2e}")
    except Exception as ex: print("scipy:",ex)

# ---- decisive control: stratify by HV, test rv_iv WITHIN each stratum ----
print("\n"+"="*78)
print("CONTROL — is this just vol mean-reversion (low HV -> bigger moves)?")
print("Stratify by HV, then test rv_iv inside each stratum. If IV carries no")
print("information of its own, rv_iv should stop predicting once HV is held fixed.")
print("="*78)
rows2=[]
for inst,sub in (('NIFTY','nifty_5min'),('BANKNIFTY','banknifty_5min')):
    files=sorted(glob.glob(f'/opt/trading_bot/data/{sub}/*.csv'))
    for fi in range(3,len(files)):
        day=os.path.basename(files[fi]).split('_')[-1].replace('.csv','')
        if day<'20260428': continue
        try:
            da=pd.concat([pd.read_csv(f,parse_dates=['ts'],index_col='ts') for f in files[fi-3:fi+1]]).sort_index()
            da=da[~da.index.duplicated(keep='first')]; da=TradingBot(inst).add_indicators(da)
        except Exception: continue
        k=f'{day[:4]}-{day[4:6]}-{day[6:]}'; dd=da[da.index.date.astype(str)==k]
        if len(dd)<30 or 'HV' not in dd.columns: continue
        for i in range(6,len(dd)-12):
            t=dd.index[i]
            iv=IV.get((inst,t.strftime('%Y-%m-%dT%H:%M')))
            if not iv or iv<=0: continue
            hv=dd['HV'].iloc[i]; atr=dd['ATR'].iloc[i]
            if not hv or hv!=hv or not atr or atr<=0: continue
            r=float(hv)*100.0/iv
            if not (0.05<r<5): continue
            e=float(dd['Close'].iloc[i]); nxt=dd.iloc[i+1:i+13]
            mv=max(abs(float(nxt['High'].max())-e),abs(e-float(nxt['Low'].min())))
            rows2.append((float(hv), iv, r, mv/float(atr)))
try:
    from scipy import stats as sps
    hvs=[x[0] for x in rows2]; ivs=[x[1] for x in rows2]
    fwd=[x[3] for x in rows2]; rr=[x[2] for x in rows2]
    print(f"\n  n={len(rows2):,}")
    print(f"  rho(HV,    forward move) = {sps.spearmanr(hvs,fwd)[0]:+.4f}  p={sps.spearmanr(hvs,fwd)[1]:.2e}")
    print(f"  rho(IV,    forward move) = {sps.spearmanr(ivs,fwd)[0]:+.4f}  p={sps.spearmanr(ivs,fwd)[1]:.2e}")
    print(f"  rho(rv_iv, forward move) = {sps.spearmanr(rr,fwd)[0]:+.4f}  p={sps.spearmanr(rr,fwd)[1]:.2e}")
    idx=sorted(range(len(rows2)), key=lambda i: rows2[i][0])
    m=len(idx)//4
    print("\n  WITHIN HV quartiles — does rv_iv still predict?")
    for q in range(4):
        sl=[rows2[i] for i in idx[q*m:(q+1)*m]]
        a=[x[2] for x in sl]; c=[x[3] for x in sl]
        rho,p=sps.spearmanr(a,c)
        print(f"    HV q{q+1}  (HV {sl[0][0]*100:5.1f}%-{sl[-1][0]*100:5.1f}%)  n={len(sl):5d}  "
              f"rho {rho:+.4f}  p={p:.2e}")
except Exception as ex: print("scipy:",ex)
