import json,glob,os,re,statistics as st
from collections import defaultdict
L='/opt/trading_bot/live_bot/logs'
# --- trades ---
tr=[]
for f in sorted(glob.glob(f'{L}/FnO_T_Bot_*_trades_*.jsonl')):
    if '.bak' in f or 'challenger' in f or 'EARLY' in f: continue
    for line in open(f,encoding='utf-8',errors='ignore'):
        line=line.strip()
        if not line: continue
        try: d=json.loads(line)
        except Exception: continue
        if d.get('pnl_net') is None: continue
        tr.append(d)
# --- zone decisions keyed by (inst, YYYY-MM-DDTHH:MM) ---
Z={}
pat=re.compile(r'^(\d{4}-\d\d-\d\d)T(\d\d:\d\d):\d\d\+\d+.*\[OI-ZONE\] (\w+) (CALL|PUT) . (\w+) \(score=(-?\d+)\)(.*)$')
for line in open('/tmp/oiz.txt',encoding='utf-8',errors='ignore'):
    m=pat.match(line.strip())
    if not m: continue
    d,hm,inst,typ,act,sc,rest=m.groups()
    Z[(inst,f'{d}T{hm}')]=dict(act=act,score=int(sc),
        wall=('Approaching' in rest), broke=('Just broke through' in rest),
        nozones=('No OI zones loaded' in rest))
matched=[]
for t in tr:
    k=(t['instrument'], t['entry_time'][:16])
    z=Z.get(k)
    if z is None:                       # try +/- 1 minute
        hh,mm=int(t['entry_time'][11:13]),int(t['entry_time'][14:16])
        for off in (-1,1):
            k2=(t['instrument'], t['entry_time'][:11]+f'{hh:02d}:{(mm+off)%60:02d}')
            z=Z.get(k2)
            if z: break
    if z: matched.append((t,z))
print(f"closed trades: {len(tr)}   with an OI-ZONE decision at entry: {len(matched)}\n")
def blk(name,rows):
    if len(rows)<5: print(f"  {name:34s} n={len(rows):3d}  (too few)"); return None
    p=[t['pnl_net'] for t,_ in rows]; w=sum(1 for x in p if x>0)
    print(f"  {name:34s} n={len(p):3d}  {100*w/len(p):5.1f}% win  "
          f"mean Rs{st.mean(p):+8,.0f}  median Rs{st.median(p):+7,.0f}  net Rs{sum(p):+9,.0f}")
    return p
print("BY ZONE ACTION (all paths)")
byact=defaultdict(list)
for t,z in matched: byact[z['act']].append((t,z))
pools={}
for a in ('BOOST','TAKE','REDUCE','SKIP'):
    r=blk(a,byact.get(a,[]))
    if r: pools[a]=r
print("\nBY ZONE ACTION — excluding 'No OI zones loaded' (no real signal)")
real=[(t,z) for t,z in matched if not z['nozones']]
byact2=defaultdict(list)
for t,z in real: byact2[z['act']].append((t,z))
pools2={}
for a in ('BOOST','TAKE','REDUCE','SKIP'):
    r=blk(a,byact2.get(a,[]))
    if r: pools2[a]=r
print(f"\n  (dropped {len(matched)-len(real)} trades where no OI zones were loaded)")
print("\nWALL AHEAD vs CLEAR (real-zone trades only)")
blk('approaching a wall',[x for x in real if x[1]['wall']])
blk('no wall ahead',[x for x in real if not x[1]['wall']])
print("\nJUST BROKE THROUGH a wall")
blk('broke through',[x for x in real if x[1]['broke']])
blk('did not break through',[x for x in real if not x[1]['broke']])
print("\nBY PATH x ZONE (real-zone trades)")
for path in ('REV','TREND'):
    for a in ('TAKE','REDUCE','SKIP','BOOST'):
        v=[x for x in real if x[0].get('path')==path and x[1]['act']==a]
        if len(v)>=4:
            p=[t['pnl_net'] for t,_ in v]; w=sum(1 for y in p if y>0)
            print(f"  {path:6s} {a:7s} n={len(p):3d}  {100*w/len(p):5.1f}% win  mean Rs{st.mean(p):+8,.0f}")
try:
    from scipy import stats as sps
    neg=pools2.get('REDUCE',[])+pools2.get('SKIP',[])
    pos=pools2.get('TAKE',[])+pools2.get('BOOST',[])
    if len(neg)>=5 and len(pos)>=5:
        u,p=sps.mannwhitneyu(pos,neg,alternative='greater')
        print(f"\n  TAKE/BOOST (n={len(pos)}, mean Rs{st.mean(pos):+,.0f}) vs "
              f"REDUCE/SKIP (n={len(neg)}, mean Rs{st.mean(neg):+,.0f})")
        print(f"  Mann-Whitney one-sided p = {p:.4f}")
        wpos=sum(1 for x in pos if x>0); wneg=sum(1 for x in neg if x>0)
        odds,pf=sps.fisher_exact([[wpos,len(pos)-wpos],[wneg,len(neg)-wneg]])
        print(f"  win-rate {100*wpos/len(pos):.1f}% vs {100*wneg/len(neg):.1f}%  Fisher p = {pf:.4f}")
except Exception as ex: print("scipy:",ex)
