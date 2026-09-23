import json,glob,os,statistics as st
L='/opt/trading_bot/live_bot/logs'
tr=[]
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
rv=[t for t in tr if t.get('rv_iv') is not None]
print(f"trades: {len(tr)}   with rv_iv logged: {len(rv)}\n")
v=[float(t['rv_iv']) for t in rv]
print("REALISED VOL / IMPLIED VOL at entry")
print(f"  median {st.median(v):.3f}   mean {st.mean(v):.3f}")
print(f"  below 1.0 (option priced ABOVE what the index actually moved): "
      f"{100*sum(1 for x in v if x<1)/len(v):.1f}% of entries")
for q in (.1,.25,.5,.75,.9):
    s=sorted(v); print(f"    p{int(q*100):02d}  {s[int(q*(len(s)-1))]:.3f}")
print("\n  a buyer needs realised > implied to be paid for the premium.")
print(f"  median shortfall: {(1-st.median(v))*100:.1f}% of the volatility paid for was never delivered.\n")
print("P&L SPLIT BY rv_iv")
for lo,hi,lbl in ((0,.6,'rv_iv < 0.60  (very overpriced)'),(.6,.85,'0.60-0.85'),
                  (.85,1.0,'0.85-1.00'),(1.0,9,'rv_iv > 1.00  (underpriced - buyer edge)')):
    s=[t for t in rv if lo<=float(t['rv_iv'])<hi]
    if len(s)<5: print(f"  {lbl:38s} n={len(s):3d}  (too few)"); continue
    p=[t['pnl_net'] for t in s]; w=sum(1 for x in p if x>0)
    print(f"  {lbl:38s} n={len(p):3d}  {100*w/len(p):5.1f}% win  mean Rs{st.mean(p):+8,.0f}  net Rs{sum(p):+9,.0f}")
try:
    from scipy import stats as sps
    p=[t['pnl_net'] for t in rv]
    rho,pv=sps.spearmanr(v,p)
    print(f"\n  Spearman rho(rv_iv, pnl) = {rho:+.3f}  p={pv:.4f}   n={len(v)}")
    hi=[t['pnl_net'] for t in rv if float(t['rv_iv'])>=st.median(v)]
    lo=[t['pnl_net'] for t in rv if float(t['rv_iv'])< st.median(v)]
    u,pu=sps.mannwhitneyu(hi,lo,alternative='greater')
    print(f"  above-median rv_iv (n={len(hi)}, mean Rs{st.mean(hi):+,.0f}) vs "
          f"below (n={len(lo)}, mean Rs{st.mean(lo):+,.0f})")
    print(f"  Mann-Whitney one-sided p = {pu:.4f}")
except Exception as ex: print("scipy:",ex)
