import sys,os,glob,json,logging,math,statistics as st
sys.path.insert(0,'/opt/trading_bot/live_bot')
logging.disable(logging.CRITICAL)
import pandas as pd, config
from options_bot import TradingBot

print("="*88)
print("PART 1 — rv_iv threshold sweep on the 43 live trades that logged it")
print("="*88)
L='/opt/trading_bot/live_bot/logs'; tr=[]
for f in sorted(glob.glob(f'{L}/FnO_T_Bot_*_trades_*.jsonl')):
    if '.bak' in f or 'challenger' in f or 'EARLY' in f: continue
    day=os.path.basename(f).replace('.jsonl','').split('_')[-1]
    for line in open(f,encoding='utf-8',errors='ignore'):
        line=line.strip()
        if not line: continue
        try: d=json.loads(line)
        except Exception: continue
        if d.get('pnl_net') is None or d.get('rv_iv') is None: continue
        d['_day']=day; tr.append(d)
tr.sort(key=lambda z:z['_day'])
print(f"n={len(tr)}\n  {'thr':>5s} {'kept':>5s} {'win%':>6s} {'mean':>9s}   {'blocked':>7s} {'win%':>6s} {'mean':>9s}")
for thr in (.60,.65,.70,.75,.80,.85,.90,.95):
    kp=[t['pnl_net'] for t in tr if float(t['rv_iv'])<thr]
    bl=[t['pnl_net'] for t in tr if float(t['rv_iv'])>=thr]
    if len(kp)<6 or len(bl)<6: continue
    print(f"  {thr:5.2f} {len(kp):5d} {100*sum(1 for x in kp if x>0)/len(kp):5.1f}% Rs{st.mean(kp):+8,.0f}   "
          f"{len(bl):7d} {100*sum(1 for x in bl if x>0)/len(bl):5.1f}% Rs{st.mean(bl):+8,.0f}")
h=tr[len(tr)//2:]
print(f"\n  holdout (second half, n={len(h)}):")
for thr in (.70,.85):
    kp=[t['pnl_net'] for t in h if float(t['rv_iv'])<thr]; bl=[t['pnl_net'] for t in h if float(t['rv_iv'])>=thr]
    if len(kp)<4 or len(bl)<4: print(f"    thr {thr}: too few"); continue
    print(f"    thr {thr:.2f}  kept n={len(kp)} mean Rs{st.mean(kp):+,.0f}   blocked n={len(bl)} mean Rs{st.mean(bl):+,.0f}")

print("\n"+"="*88)
print("PART 2 — MECHANISM test, no IV needed: has volatility ALREADY expanded?")
print("  vol_exp = stdev(last 6 bar returns) / stdev(last 36 bar returns)")
print("  high = the move already happened.  Predicts: high -> worse forward outcome.")
print("="*88)
DIRS={'NIFTY':'nifty_5min','BANKNIFTY':'banknifty_5min','SENSEX':'sensex_5min'}
rows=[]
for inst,sub in DIRS.items():
    files=sorted(glob.glob(f'/opt/trading_bot/data/{sub}/*.csv'))
    for fi in range(3,len(files)):
        day=os.path.basename(files[fi]).split('_')[-1].replace('.csv','')
        try:
            da=pd.concat([pd.read_csv(f,parse_dates=['ts'],index_col='ts') for f in files[fi-3:fi+1]]).sort_index()
            da=da[~da.index.duplicated(keep='first')]
            bot=TradingBot(inst); da=bot.add_indicators(da)
        except Exception: continue
        k=f'{day[:4]}-{day[4:6]}-{day[6:]}'; dd=da[da.index.date.astype(str)==k]
        if len(dd)<20: continue
        bot._or_high=float(dd['High'].iloc[:6].max()); bot._or_low=float(dd['Low'].iloc[:6].min())
        bot._or_ready=True
        for a in ('_trend_qual_dir','_trend_anchor','_trend_leg_extreme','_trend_qual_time'):
            setattr(bot,a,None)
        bot._path_trend_fired=False
        for i in range(6,len(dd)):
            t=dd.index[i]
            if t.strftime('%H:%M')>config.PATH_TREND_END: break
            j=da.index.get_loc(t); sd=da.iloc[:j+1]
            try:
                bot._update_trend_qualification(sd,t); sig=bot.get_path_trend_signal(sd,{},t)
            except Exception: sig=None
            if not sig: continue
            bot._path_trend_fired=True
            c=sd['Close'].astype(float)
            if len(c)<40: break
            r=c.pct_change().dropna()
            s6,s36=r.iloc[-6:].std(),r.iloc[-36:].std()
            if not s36 or s36<=0: break
            e=float(sig['price']); atr=float(dd['ATR'].iloc[i] or 0)
            if atr<=0: break
            f=dd.iloc[i+1:]; f=f[[x.strftime('%H:%M')<=config.FORCE_CLOSE_TIME for x in f.index]]
            if len(f)<2: break
            clv=float(f['Close'].iloc[-1]); res=((clv-e) if sig['type']=='CALL' else (e-clv))/atr
            rows.append(dict(day=day,ve=float(s6/s36),leg=sig.get('trend_leg_atr') or 0,res=res))
            break
rows.sort(key=lambda z:z['day']); n=len(rows)
print(f"n={n} signals")
q=sorted(x['ve'] for x in rows)
print(f"vol_exp: p25 {q[n//4]:.2f}  median {q[n//2]:.2f}  p75 {q[3*n//4]:.2f}\n")
print(f"  {'band':>16s} {'n':>4s} {'%right':>7s} {'mean ATR':>9s}")
for a,b,lbl in ((0,.8,'< 0.8 quiet'),(.8,1.1,'0.8-1.1'),(1.1,1.5,'1.1-1.5'),(1.5,99,'> 1.5 expanded')):
    v=[x['res'] for x in rows if a<=x['ve']<b]
    if len(v)<10: print(f"  {lbl:>16s} {len(v):4d}   (too few)"); continue
    print(f"  {lbl:>16s} {len(v):4d} {100*sum(1 for x in v if x>0)/len(v):6.1f}% {st.mean(v):+9.2f}")
try:
    from scipy import stats as sps
    ve=[x['ve'] for x in rows]; rs=[x['res'] for x in rows]
    rho,p=sps.spearmanr(ve,rs); print(f"\n  Spearman rho(vol_exp, outcome) = {rho:+.3f}  p={p:.4f}  n={n}")
    hf=rows[n//2:]
    ve2=[x['ve'] for x in hf]; rs2=[x['res'] for x in hf]
    rho2,p2=sps.spearmanr(ve2,rs2); print(f"  HOLDOUT second half             = {rho2:+.3f}  p={p2:.4f}  n={len(hf)}")
    print("\n  combined with the new leg floor (leg>=1.5), does vol_exp still add?")
    sub=[x for x in rows if x['leg']>=1.5]
    if len(sub)>25:
        r3,p3=sps.spearmanr([x['ve'] for x in sub],[x['res'] for x in sub])
        print(f"    within leg>=1.5 (n={len(sub)}): rho {r3:+.3f}  p={p3:.4f}")
except Exception as ex: print("scipy:",ex)
