import json,glob,os,statistics as st
from collections import defaultdict
L='/opt/trading_bot/live_bot/logs'
rows=[]
for f in sorted(glob.glob(f'{L}/counterfactual_*.jsonl')):
    for line in open(f,encoding='utf-8',errors='ignore'):
        line=line.strip()
        if not line: continue
        try: rows.append(json.loads(line))
        except Exception: pass
rows.sort(key=lambda d:(d['gate'], d['entry_time']))
print("="*104)
print("BLOCKED TRADES — what each gate refused, and what it would have become")
print("="*104)
if not rows:
    print("  no closed phantoms yet"); raise SystemExit
cur=None
for d in rows:
    if d['gate']!=cur:
        cur=d['gate']; print(f"\n─── {cur} " + "─"*(96-len(cur)))
        print(f"  {'date':>10s} {'inst':>9s} {'dir':>4s} {'in':>5s} {'out':>5s} {'held':>5s} "
              f"{'entry':>8s} {'exit':>8s} {'peak':>7s} {'P&L':>9s}  {'px':>4s}  why it closed")
    bs = d.get('px_src','?').upper().startswith('BS')
    flag = 'BS!' if bs else 'LTP'
    print(f"  {d['date']:>10s} {d['instrument']:>9s} {d['type']:>4s} "
          f"{d['entry_time'][11:16]:>5s} {d['exit_time'][11:16]:>5s} {d['held_min']:>4.0f}m "
          f"{d['entry_price']:>8.2f} {d['exit_price']:>8.2f} {d['peak_pct']:>6.1f}% "
          f"Rs{d['pnl_net']:>+8,.0f}  {flag:>4s}  {d['exit_reason'][:30]}")
print("\n" + "="*104)
print("PER-GATE VERDICT")
print("="*104)
print(f"  {'gate':12s} {'n':>3s} {'W/L':>7s} {'net':>10s} {'mean':>9s} {'reliable only':>26s}  verdict")
g=defaultdict(list)
for d in rows: g[d['gate']].append(d)
tot_all=tot_rel=0
for k,v in sorted(g.items(), key=lambda z:sum(x['pnl_net'] for x in z[1])):
    p=[x['pnl_net'] for x in v]; w=sum(1 for x in p if x>0)
    rel=[x['pnl_net'] for x in v if not x.get('px_src','').upper().startswith('BS')]
    tot_all+=sum(p); tot_rel+=sum(rel)
    relstr=(f"n={len(rel)} Rs{sum(rel):+,.0f}" if rel else "n=0 (all BS-priced)")
    verdict=('SAVED us money' if sum(rel)<0 else 'COST us money') if rel else 'unmeasured'
    print(f"  {k:12s} {len(p):3d} {w:3d}W/{len(p)-w:<3d} Rs{sum(p):+9,.0f} Rs{st.mean(p):+8,.0f} "
          f"{relstr:>26s}  {verdict}")
print(f"\n  TOTAL blocked: Rs{tot_all:+,.0f} across {len(rows)} phantoms")
print(f"  of which real-priced and therefore trustworthy: Rs{tot_rel:+,.0f}")
nbs=sum(1 for d in rows if d.get('px_src','').upper().startswith('BS'))
print(f"  BS-priced (unreliable, symbol bug Sep 24-28): {nbs}/{len(rows)}")
print("\n  READ: a NEGATIVE net means the gate blocked losers -- it earned its keep.")
print("        a POSITIVE net means the gate blocked winners -- it cost money.")
