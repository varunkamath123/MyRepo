# -*- coding: utf-8 -*-
"""Can the SIZE of the winners be raised, and what does it do to the losers? (replay, index level)

Run:  python quantum_exits.py <history_dir>

Written after the owner's point that the size of the wins matters more than how often they come. Every
variant uses the strategy's own entries; only the exits change. Leverage is set to a realistic 30% premium per
1% index for the monthly option (the replay's shipped 71.4 belongs to week-dated options and made the hard stop
0.35% of spot, which cut winners short). Columns: win rate, average win, average loss, payoff ratio, expectancy
with a bootstrap CI, profit factor, mean of the best 5% of trades, share of trades above +0.5%.
"""
import os
import sys
import math
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import bnf_replay as RP, bnf_data, bnf_config as cfg
bnf_config_dir = cfg.HISTORY_DIR
rng=np.random.default_rng(4)
sess=bnf_data.load_history(sys.argv[1] if len(sys.argv)>1 else bnf_config_dir)
sess=[s for s in sess if len(s['T'])==75]
def mk(**kw):
    c=RP._ReplayCfg(); c.VOLUME_MODE='off'
    for k,v in kw.items(): setattr(c,k,v)
    return c
BIG=1e6
variants=[
 ('AS SHIPPED (71.4 leverage in the replay)', dict()),
 ('realistic leverage (30%/1%): stop 0.83%, target 1.83%', dict(PREMIUM_PCT_PER_INDEX_PCT=30.0)),
 ('  + no target (let it run)', dict(PREMIUM_PCT_PER_INDEX_PCT=30.0, TARGET_PREMIUM_PCT=99.0)),
 ('  + trail 2.5 ATR', dict(PREMIUM_PCT_PER_INDEX_PCT=30.0, TARGET_PREMIUM_PCT=99.0, TRAIL_ATR_MULT=2.5)),
 ('  + trail 4 ATR', dict(PREMIUM_PCT_PER_INDEX_PCT=30.0, TARGET_PREMIUM_PCT=99.0, TRAIL_ATR_MULT=4.0)),
 ('  + no stale rule', dict(PREMIUM_PCT_PER_INDEX_PCT=30.0, TARGET_PREMIUM_PCT=99.0, THESIS_CONFIRM_BARS=10**6)),
 ('  + no stale, trail 2.5 ATR', dict(PREMIUM_PCT_PER_INDEX_PCT=30.0, TARGET_PREMIUM_PCT=99.0, THESIS_CONFIRM_BARS=10**6, TRAIL_ATR_MULT=2.5)),
 ('  + no stale, no thesis-broken, trail 2.5', dict(PREMIUM_PCT_PER_INDEX_PCT=30.0, TARGET_PREMIUM_PCT=99.0, THESIS_CONFIRM_BARS=10**6, TRAIL_ATR_MULT=2.5, THESIS_BREAK_BUFFER_PCT=BIG)),
 ('  + hold to the close (no exits but the stop)', dict(PREMIUM_PCT_PER_INDEX_PCT=30.0, TARGET_PREMIUM_PCT=99.0, THESIS_CONFIRM_BARS=10**6, TRAIL_ARM_INDEX_PCT=1e9, THESIS_BREAK_BUFFER_PCT=BIG)),
]
print('%-56s %4s %5s %7s %7s %6s %9s %7s %8s %7s'%('variant','n','win%','avgwin','avgloss','payoff','expect','PF','top5%avg','>+0.5%'))
for name,kw in variants:
    tr,_=RP.run(sess,'strategy',c=mk(**kw))
    x=np.array([t['pct'] for t in tr])
    w=x[x>0]; l=x[x<=0]
    pr=w.mean()/abs(l.mean()); pf=w.sum()/abs(l.sum())
    srt=np.sort(x)[::-1]; n5=max(1,int(len(x)*0.05))
    bs=[rng.choice(x,len(x)).mean() for _ in range(2000)]
    h=len(x)//2
    print('%-56s %4d %4.0f%% %+6.2f%% %+6.2f%% %6.2f %+8.3f%% %6.2f %+7.2f%% %6.1f%%   CI[%+.3f,%+.3f] halves %+.3f/%+.3f'%(name,len(x),len(w)/len(x)*100,w.mean(),l.mean(),pr,x.mean(),pf,srt[:n5].mean(),(x>0.5).mean()*100,np.percentile(bs,2.5),np.percentile(bs,97.5),x[:h].mean(),x[h:].mean()))
