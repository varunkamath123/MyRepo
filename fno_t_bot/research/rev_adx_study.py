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
        if d.get('pnl_net') is None or day<'2026-08-18': continue
        d['_day']=day; tr.append(d)
rev=[t for t in tr if t.get('path')=='REV' and t.get('entry_adx')]
print("IS PATH_REV BROKEN? Its exhaustion test is RELATIVE (ADX < 0.85 x peak),")
print("never ABSOLUTE. An ADX of 46 that fell from 55 counts as 'waning' -- but 46")
print("is a raging trend. Hypothesis: REV loses when absolute ADX is still high.\n")
print(f"REV trades, real-premium era: n={len(rev)}\n")
print(f"  {'entry ADX band':>18s} {'n':>3s} {'W/L':>7s} {'net':>10s} {'mean':>9s}")
for lo,hi,lbl in ((0,25,'< 25  quiet'),(25,35,'25-35 moderate'),
                  (35,45,'35-45 strong'),(45,99,'> 45  raging')):
    v=[t for t in rev if lo<=t['entry_adx']<hi]
    if not v: continue
    p=[t['pnl_net'] for t in v]; w=sum(1 for x in p if x>0)
    print(f"  {lbl:>18s} {len(p):3d} {w:3d}W/{len(p)-w:<3d} Rs{sum(p):+9,.0f} Rs{st.mean(p):+8,.0f}")
print()
lo=[t['pnl_net'] for t in rev if t['entry_adx']<35]
hi=[t['pnl_net'] for t in rev if t['entry_adx']>=35]
if lo and hi:
    print(f"  ADX < 35 : n={len(lo):2d}  net Rs{sum(lo):+8,.0f}  mean Rs{st.mean(lo):+7,.0f}")
    print(f"  ADX >= 35: n={len(hi):2d}  net Rs{sum(hi):+8,.0f}  mean Rs{st.mean(hi):+7,.0f}")
    try:
        from scipy import stats as sps
        u,p=sps.mannwhitneyu(lo,hi,alternative='greater')
        print(f"  low-ADX beats high-ADX?  Mann-Whitney p={p:.4f}")
        rho,pr=sps.spearmanr([t['entry_adx'] for t in rev],[t['pnl_net'] for t in rev])
        print(f"  rho(entry ADX, pnl) = {rho:+.3f}  p={pr:.4f}   n={len(rev)}")
    except Exception as e: print(e)
print("\n  every REV trade, worst first:")
for t in sorted(rev,key=lambda z:z['pnl_net'])[:8]:
    print(f"    {t['_day']} {t['instrument']:10s} {t['type']:4s} ADX {t['entry_adx']:5.1f}  "
          f"ratio {t.get('rev_adx_ratio')}  Rs{t['pnl_net']:+8,.0f}")
