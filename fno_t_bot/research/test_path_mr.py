import sys,os,logging,glob
sys.path.insert(0,r'C:\quant_trading\MyRepo\fno_t_bot')
logging.disable(logging.CRITICAL)
import pandas as pd, config, path_mr
from datetime import datetime
from options_bot import TradingBot
P=F=0
def ck(n,c,d=''):
    global P,F
    if c: P+=1; print(f'  PASS  {n}')
    else: F+=1; print(f'  FAIL  {n}  {d}')
print(f"PATH_MR shadow — enabled={config.PATH_MR_SHADOW_ENABLED}")
files=sorted(glob.glob(r'C:\quant_trading\data\nifty_5min\*.csv'))
da=pd.concat([pd.read_csv(f,parse_dates=['ts'],index_col='ts') for f in files[-7:]]).sort_index()
da=da[~da.index.duplicated(keep='first')]
bot=TradingBot('NIFTY'); da=bot.add_indicators(da)
last=sorted({d.date() for d in da.index})[-1]
now=datetime.combine(last, datetime.strptime('11:05','%H:%M').time())
path_mr.LOG_DIR=os.path.join(os.environ.get('TEMP','.'),'mrtest')
path_mr._fired.clear()
r=path_mr.evaluate(bot=bot,instrument='NIFTY',df=da,now=now,logger=None)
ck('returns a dict or None', r is None or isinstance(r,dict))
ck('opened NO position', len(bot.positions)==0, f'{len(bot.positions)} positions')
w=glob.glob(os.path.join(path_mr.LOG_DIR,'*.jsonl'))
ck('wrote a shadow log line', len(w)==1, str(w))
if r:
    print(f"    signal: {r['direction']} band={r['band']} {r['lookback_days']}d move {r['trend_pct']:+.2f}%")
    ck('direction fades the move', (r['direction']=='PUT')==(r['trend_pct']>0))
    ck('band matches magnitude',
       r['band']==('STRONG' if r['magnitude']>=path_mr.MR_DECILE_PCT else 'NORMAL'))
else:
    print("    NEUTRAL (5-day move inside the tertile cut) — valid outcome")
path_mr._fired.clear()
n2=datetime.combine(last, datetime.strptime('10:00','%H:%M').time())
ck('silent before 11:00', path_mr.evaluate(bot=bot,instrument='NIFTY',df=da,now=n2,logger=None) is None)
path_mr._fired.clear()
path_mr.evaluate(bot=bot,instrument='NIFTY',df=da,now=now,logger=None)
before=sum(1 for _ in open(w[0],encoding='utf-8')) if w else 0
path_mr.evaluate(bot=bot,instrument='NIFTY',df=da,now=now,logger=None)
after=sum(1 for _ in open(w[0],encoding='utf-8')) if w else 0
ck('fires at most once per session', after==before, f'{before}->{after}')
cfg=config.PATH_MR_SHADOW_ENABLED
config.PATH_MR_SHADOW_ENABLED=False; path_mr._fired.clear()
ck('respects the disable flag', path_mr.evaluate(bot=bot,instrument='NIFTY',df=da,now=now,logger=None) is None)
config.PATH_MR_SHADOW_ENABLED=cfg

# --- REGRESSION (Sep 21 2026) -------------------------------------------
# The live frame carries ~5 trading days but this engine needs 6. It used to
# return None SILENTLY in that case, so it looked idle rather than broken and
# went unnoticed from Sep 12 to Sep 21. The reference close must now come from
# the daily CSV store whenever the frame is short.
_fs = sorted(glob.glob(r'C:\quant_trading\data\nifty_5min\*.csv'))
if len(_fs) >= 8:
    _short = pd.concat([pd.read_csv(f, parse_dates=['ts'], index_col='ts')
                        for f in _fs[-5:]]).sort_index()
    _short = _short[~_short.index.duplicated(keep='first')]
    _short = bot.add_indicators(_short)
    _days = sorted({d.date() for d in _short.index})
    ck('frame really is short (the production shape)',
       len(_days) < path_mr.MR_LOOKBACK + 1, '%d days' % len(_days))
    _ref = path_mr._ref_close_from_store('NIFTY', _days[-1])
    ck('store fallback resolves the reference close', bool(_ref and _ref > 0), str(_ref))
    path_mr._fired.clear()
    _r = path_mr.evaluate(bot=bot, instrument='NIFTY', df=_short,
                          now=datetime.combine(_days[-1], datetime.strptime('11:05','%H:%M').time()),
                          logger=None)
    ck('short frame no longer bails silently (a verdict is reached)',
       ('NIFTY', _days[-1].strftime('%Y-%m-%d')) in path_mr._fired)

import shutil; shutil.rmtree(path_mr.LOG_DIR,ignore_errors=True)
print(f"\nRESULT: {P} passed, {F} failed")
sys.exit(1 if F else 0)
