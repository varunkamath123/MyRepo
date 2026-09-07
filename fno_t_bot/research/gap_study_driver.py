import sys,os,statistics as st
sys.path.insert(0,r'C:\quant_trading\MyRepo\fno_t_bot\research')
from gap_study import sessions, _perm_diff_median
D=r'C:\quant_trading\data'
for inst,sub in (('NIFTY','nifty_5min'),('BANKNIFTY','banknifty_5min'),('SENSEX','sensex_5min')):
    rows=sessions(D,sub)
    if not rows: print(f"{inst}: no rows"); continue
    print(f"\n{'='*70}\n{inst}  sessions={len(rows)}   (ORIGINAL methodology: close=15:30, median, permutation)")
    for thresh in (0.15,0.25,0.35):
        up=[r for r in rows if r['gap_pct']>=thresh]
        dn=[r for r in rows if r['gap_pct']<=-thresh]
        if len(up)<5 or len(dn)<5:
            print(f"  thr {thresh}: too few (up {len(up)} dn {len(dn)})"); continue
        du=[r['day_pct'] for r in up]; dd=[r['day_pct'] for r in dn]
        p=_perm_diff_median(du,dd)
        spread=st.median(dd)-st.median(du)
        print(f"  thr ±{thresh:.2f}%  up n={len(up):3d} med {st.median(du):+7.3f}%   "
              f"dn n={len(dn):3d} med {st.median(dd):+7.3f}%   "
              f"fade-spread {spread:+.3f}pp   perm p={p:.4f}"
              f"{'  ** SIG **' if p<0.05 else ''}")
