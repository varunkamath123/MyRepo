# -*- coding: utf-8 -*-
"""Backfill 5-min bars for the NIFTY top-10 by weight (~60% of the index).

Research-only: writes to data/stocks_5min/, touches no production code.
data_collector.HEAVYWEIGHT_STOCKS only ever listed three names and had never
been run, so no constituent data existed. Created Oct 2 2026 to test whether
breadth confirms index level-breaks (it does not -- rho -0.012 p=0.79 on
NIFTY, -0.037 p=0.44 on BANKNIFTY), but the dataset is kept because it is the
only constituent history we have and any future breadth, rotation or
sector-divergence work needs it.

Run as the bot user so it can write under /opt/trading_bot/data:
    sudo -u tradingbot /opt/trading_bot/venv/bin/python stocks_backfill_top10.py

Yields ~272 sessions per ticker, 2,720 files.
"""
import os, sys, time, datetime as dt
sys.path.insert(0, '/opt/trading_bot/live_bot')
import pandas as pd
from data_collector import _get_fyers_client

TOP10 = {'HDFCBANK':'NSE:HDFCBANK-EQ','RELIANCE':'NSE:RELIANCE-EQ',
         'ICICIBANK':'NSE:ICICIBANK-EQ','INFY':'NSE:INFY-EQ',
         'BHARTIARTL':'NSE:BHARTIARTL-EQ','TCS':'NSE:TCS-EQ','ITC':'NSE:ITC-EQ',
         'LT':'NSE:LT-EQ','AXISBANK':'NSE:AXISBANK-EQ','SBIN':'NSE:SBIN-EQ'}
OUT = '/opt/trading_bot/data/stocks_5min'
os.makedirs(OUT, exist_ok=True)
fy = _get_fyers_client()
if not fy:
    sys.exit('no client')
end = dt.date(2026, 10, 1)
start = end - dt.timedelta(days=400)
for tk, sym in TOP10.items():
    saved = 0
    cur = start
    while cur < end:
        chunk_end = min(cur + dt.timedelta(days=89), end)
        try:
            r = fy.history({'symbol':sym,'resolution':'5','date_format':'1',
                            'range_from':cur.strftime('%Y-%m-%d'),
                            'range_to':chunk_end.strftime('%Y-%m-%d'),'cont_flag':'1'})
        except Exception as e:
            print(f'{tk} {cur} EXC {e}'); cur = chunk_end + dt.timedelta(days=1); continue
        if r.get('s') == 'ok' and r.get('candles'):
            d = pd.DataFrame(r['candles'], columns=['ts','Open','High','Low','Close','Volume'])
            d['ts'] = pd.to_datetime(d['ts'], unit='s', utc=True).dt.tz_convert('Asia/Kolkata')
            for day, g in d.groupby(d['ts'].dt.date):
                p = os.path.join(OUT, f"{tk}_5min_{day.strftime('%Y%m%d')}.csv")
                if not os.path.exists(p):
                    g.to_csv(p, index=False); saved += 1
        cur = chunk_end + dt.timedelta(days=1)
        time.sleep(0.4)
    print(f'{tk:12} saved {saved} days', flush=True)
print('BACKFILL DONE')
