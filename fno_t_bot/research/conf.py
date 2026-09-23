import json,glob,os,statistics as st
from collections import defaultdict
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
print(f"n={len(tr)}\n")
print("CONFOUND 1 — is rv_iv just measuring the INSTRUMENT?")
for inst in ('NIFTY','BANKNIFTY','SENSEX'):
    v=[t for t in tr if t['instrument']==inst]
    if not v: continue
    r=[float(t['rv_iv']) for t in v]; p=[t['pnl_net'] for t in v]
    srcs=defaultdict(int)
    for t in v: srcs[t.get('rv_iv_src','?')]+=1
    print(f"  {inst:10s} n={len(v):3d}  median rv_iv {st.median(r):.3f}  "
          f"mean pnl Rs{st.mean(p):+7,.0f}  src={dict(srcs)}")
print("\nCONFOUND 2 — is rv_iv just measuring the PATH?")
for path in ('REV','TREND','A_HELD','B'):
    v=[t for t in tr if t.get('path')==path]
    if len(v)<4: continue
    r=[float(t['rv_iv']) for t in v]; p=[t['pnl_net'] for t in v]
    print(f"  {path:8s} n={len(v):3d}  median rv_iv {st.median(r):.3f}  mean pnl Rs{st.mean(p):+7,.0f}")
print("\nDOES THE EFFECT SURVIVE *WITHIN* EACH SLICE?  (thr 0.70)")
def within(label, rows):
    kp=[t['pnl_net'] for t in rows if float(t['rv_iv'])<0.70]
    bl=[t['pnl_net'] for t in rows if float(t['rv_iv'])>=0.70]
    if len(kp)<4 or len(bl)<4:
        print(f"  {label:28s} n={len(rows):3d}  kept {len(kp)} / blocked {len(bl)} — too few"); return
    print(f"  {label:28s} kept n={len(kp):3d} Rs{st.mean(kp):+8,.0f}   "
          f"blocked n={len(bl):3d} Rs{st.mean(bl):+8,.0f}   gap Rs{st.mean(kp)-st.mean(bl):+8,.0f}")
for inst in ('NIFTY','BANKNIFTY','SENSEX'):
    within(f"within {inst}", [t for t in tr if t['instrument']==inst])
for path in ('REV','TREND'):
    within(f"within {path}", [t for t in tr if t.get('path')==path])
within("src=chain only", [t for t in tr if t.get('rv_iv_src')=='chain'])
print("\nCONFOUND 3 — is rv_iv just a proxy for VIX / the day itself?")
byday=defaultdict(list)
for t in tr: byday[t['_day']].append(t)
multi=[d for d,v in byday.items() if len(v)>1]
print(f"  {len(byday)} distinct days, {len(multi)} with >1 trade")
same=[]
for d in multi:
    v=byday[d]; r=[float(t['rv_iv']) for t in v]
    if max(r)-min(r)>0.15:
        lo=min(v,key=lambda t:float(t['rv_iv'])); hi=max(v,key=lambda t:float(t['rv_iv']))
        same.append(lo['pnl_net']-hi['pnl_net'])
if same:
    print(f"  SAME-DAY pairs where rv_iv differs >0.15: n={len(same)}")
    print(f"    lower-rv_iv trade minus higher-rv_iv trade: mean Rs{st.mean(same):+,.0f}  "
          f"median Rs{st.median(same):+,.0f}  positive in {sum(1 for x in same if x>0)}/{len(same)}")
    print("    (this controls for the day entirely — same session, same regime)")
