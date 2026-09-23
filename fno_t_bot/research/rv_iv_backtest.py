import sys,os,glob,json,logging,statistics as st
sys.path.insert(0,'/opt/trading_bot/live_bot')
logging.disable(logging.CRITICAL)
import pandas as pd
from options_bot import TradingBot

IV={}
for u,inst in (('nifty','NIFTY'),('banknifty','BANKNIFTY'),('sensex','SENSEX')):
    try:
        for line in open(f'/tmp/iv_{u}.txt',encoding='utf-8',errors='ignore'):
            p=line.split()
            if len(p)==2:
                try: IV[(inst,p[0])]=float(p[1])
                except Exception: pass
    except Exception: pass

L='/opt/trading_bot/live_bot/logs'; tr=[]
for f in sorted(glob.glob(f'{L}/FnO_T_Bot_*_trades_*.jsonl')):
    if '.bak' in f or 'challenger' in f or 'EARLY' in f: continue
    day=os.path.basename(f).replace('.jsonl','').split('_')[-1]
    for line in open(f,encoding='utf-8',errors='ignore'):
        line=line.strip()
        if not line: continue
        try: d=json.loads(line)
        except Exception: continue
        if d.get('pnl_net') is None: continue
        d['_day']=day; tr.append(d)
tr.sort(key=lambda z:(z['_day'],z['entry_time']))
print(f"closed trades on disk: {len(tr)}")

# --- HV at each trade's entry bar, replayed from index CSVs ---------------
DIRS={'NIFTY':'nifty_5min','BANKNIFTY':'banknifty_5min','SENSEX':'sensex_5min'}
HV={}
days=sorted({(t['instrument'],t['_day']) for t in tr})
for inst,day in days:
    ymd=day.replace('-','')
    files=sorted(glob.glob(f'/opt/trading_bot/data/{DIRS[inst]}/*.csv'))
    idx=[i for i,f in enumerate(files) if ymd in f]
    if not idx or idx[0]<3: continue
    fi=idx[0]
    try:
        da=pd.concat([pd.read_csv(f,parse_dates=['ts'],index_col='ts') for f in files[fi-3:fi+1]]).sort_index()
        da=da[~da.index.duplicated(keep='first')]
        da=TradingBot(inst).add_indicators(da)
    except Exception: continue
    dd=da[da.index.date.astype(str)==day]
    for ts,row in dd.iterrows():
        v=row.get('HV')
        if v==v and v: HV[(inst,ts.strftime('%Y-%m-%dT%H:%M'))]=float(v)

def hv_at(inst,stamp):
    """most recent completed bar at or before this minute"""
    d,hm=stamp.split('T'); h,m=int(hm[:2]),int(hm[3:5])
    for back in range(0,10):
        mm=m-back
        hh=h
        if mm<0: mm+=60; hh-=1
        k=(inst,f'{d}T{hh:02d}:{mm:02d}')
        if k in HV: return HV[k]
    return None

rec=0
for t in tr:
    stamp=t['entry_time'][:16]
    inst=t['instrument']
    iv=IV.get((inst,stamp))
    if iv is None:
        d,hm=stamp.split('T'); h,m=int(hm[:2]),int(hm[3:5])
        for off in (-1,1,-2,2):
            mm=(m+off)%60; hh=h+((m+off)//60)
            iv=IV.get((inst,f'{d}T{hh:02d}:{mm:02d}'))
            if iv: break
    hv=hv_at(inst,stamp)
    t['_iv']=iv; t['_hv']=hv
    t['_rv_recon']=(round(hv*100.0/iv,3) if (iv and hv and iv>0) else None)
    if t['_rv_recon'] is not None: rec+=1
print(f"reconstructed rv_iv for: {rec}\n")

# --- VALIDATION -----------------------------------------------------------
both=[t for t in tr if t.get('rv_iv') is not None and t.get('_rv_recon') is not None]
print("="*78)
print(f"VALIDATION — logged vs reconstructed, on the {len(both)} trades that have both")
print("="*78)
if both:
    a=[float(t['rv_iv']) for t in both]; b=[t['_rv_recon'] for t in both]
    diff=[abs(x-y) for x,y in zip(a,b)]
    rel=[abs(x-y)/x for x,y in zip(a,b) if x>0]
    print(f"  median |logged - reconstructed| = {st.median(diff):.4f}")
    print(f"  median relative error          = {100*st.median(rel):.1f}%")
    print(f"  within 10%: {100*sum(1 for r in rel if r<=.10)/len(rel):.0f}%   "
          f"within 20%: {100*sum(1 for r in rel if r<=.20)/len(rel):.0f}%")
    try:
        from scipy import stats as sps
        print(f"  Pearson r = {sps.pearsonr(a,b)[0]:.4f}   Spearman = {sps.spearmanr(a,b)[0]:.4f}")
        agree=sum(1 for x,y in zip(a,b) if (x<0.70)==(y<0.70))
        print(f"  SAME side of the 0.70 gate: {agree}/{len(both)} = {100*agree/len(both):.0f}%")
    except Exception as ex: print(" ",ex)

# --- COMBINED SAMPLE: logged where present, reconstructed otherwise -------
print("\n"+"="*78)
print("COMBINED BACKTEST — chain-sourced trades only (the gate's scope)")
print("="*78)
comb=[]
for t in tr:
    src=t.get('rv_iv_src')
    val=None; how=None
    if t.get('rv_iv') is not None and src=='chain':
        val=float(t['rv_iv']); how='logged'
    elif t.get('_rv_recon') is not None and t['instrument'] in ('NIFTY','BANKNIFTY'):
        val=t['_rv_recon']; how='reconstructed'
    if val is not None:
        comb.append(dict(day=t['_day'],inst=t['instrument'],path=t.get('path'),
                         rv=val,pnl=t['pnl_net'],how=how))
comb.sort(key=lambda z:z['day'])
n=len(comb)
nl=sum(1 for c in comb if c['how']=='logged')
print(f"n={n}  ({nl} logged, {n-nl} reconstructed)  "
      f"span {comb[0]['day']}..{comb[-1]['day']}")
byi={}
for c in comb: byi[c['inst']]=byi.get(c['inst'],0)+1
print(f"  by instrument: {byi}\n")
print(f"  {'thr':>5s} {'kept':>5s} {'win%':>6s} {'mean':>9s} {'net':>10s}   "
      f"{'blocked':>7s} {'win%':>6s} {'mean':>9s} {'net':>10s}")
for thr in (.55,.60,.65,.70,.75,.80,.85,.90):
    kp=[c['pnl'] for c in comb if c['rv']<thr]
    bl=[c['pnl'] for c in comb if c['rv']>=thr]
    if len(kp)<5 or len(bl)<5: continue
    print(f"  {thr:5.2f} {len(kp):5d} {100*sum(1 for x in kp if x>0)/len(kp):5.1f}% "
          f"Rs{st.mean(kp):+8,.0f} Rs{sum(kp):+9,.0f}   "
          f"{len(bl):7d} {100*sum(1 for x in bl if x>0)/len(bl):5.1f}% "
          f"Rs{st.mean(bl):+8,.0f} Rs{sum(bl):+9,.0f}")
tot=sum(c['pnl'] for c in comb)
kp=[c['pnl'] for c in comb if c['rv']<0.70]
print(f"\n  book as traded          : Rs{tot:+,.0f} over {n} trades")
print(f"  book with gate at 0.70  : Rs{sum(kp):+,.0f} over {len(kp)} trades")
print(f"  improvement             : Rs{sum(kp)-tot:+,.0f}")
h=comb[n//2:]
print(f"\n  HOLDOUT (second half, n={len(h)}, {h[0]['day']}..{h[-1]['day']}):")
for thr in (.65,.70,.75):
    kp2=[c['pnl'] for c in h if c['rv']<thr]; bl2=[c['pnl'] for c in h if c['rv']>=thr]
    if len(kp2)<4 or len(bl2)<4: continue
    print(f"    thr {thr:.2f}  kept n={len(kp2):2d} Rs{st.mean(kp2):+8,.0f}   "
          f"blocked n={len(bl2):2d} Rs{st.mean(bl2):+8,.0f}")
print("\n  by path, gate at 0.70:")
for p in ('REV','TREND','B','A_HELD'):
    v=[c for c in comb if c['path']==p]
    if len(v)<5: continue
    k=[c['pnl'] for c in v if c['rv']<0.70]; b=[c['pnl'] for c in v if c['rv']>=0.70]
    print(f"    {p:8s} n={len(v):3d}  kept {len(k):2d} Rs{st.mean(k):+8,.0f}" if k else
          f"    {p:8s} n={len(v):3d}  kept 0", end='')
    print(f"   blocked {len(b):2d} Rs{st.mean(b):+8,.0f}" if b else "   blocked 0")
try:
    from scipy import stats as sps
    kp=[c['pnl'] for c in comb if c['rv']<0.70]; bl=[c['pnl'] for c in comb if c['rv']>=0.70]
    u,p=sps.mannwhitneyu(kp,bl,alternative='greater')
    print(f"\n  Mann-Whitney (kept > blocked) p = {p:.4f}   n={len(kp)}/{len(bl)}")
    rho,pr=sps.spearmanr([c['rv'] for c in comb],[c['pnl'] for c in comb])
    print(f"  Spearman rho(rv_iv, pnl) = {rho:+.3f}  p={pr:.4f}")
except Exception as ex: print(" ",ex)
