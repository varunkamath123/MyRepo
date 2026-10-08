# -*- coding: utf-8 -*-
"""Pin the 2026 holiday list to NSE circular NSE/FAOP/71777 (Dec 12 2025)."""
import os, sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nse_holidays as H

P = F = 0


def ck(name, cond, detail=''):
    global P, F
    if cond:
        P += 1
        print(f'  PASS  {name}')
    else:
        F += 1
        print(f'  FAIL  {name}  {detail}')


OFFICIAL = [(1, 26), (3, 3), (3, 26), (3, 31), (4, 3), (4, 14), (5, 1), (5, 28), (6, 26), (9, 14),
            (10, 2), (10, 20), (11, 10), (11, 24), (12, 25)]

ck('2026 list matches the 15 weekday holidays in the NSE circular',
   sorted(H.NSE_HOLIDAYS_2026) == [date(2026, m, d) for m, d in OFFICIAL], sorted(H.NSE_HOLIDAYS_2026))
ck('Fri Oct 9 2026 is a trading day (was wrongly a holiday)', H.is_market_open_today(date(2026, 10, 9)))
ck('Tue Oct 20 2026 (Dussehra) is closed', not H.is_market_open_today(date(2026, 10, 20)))
ck('Mon Nov 9 2026 is a trading day (was wrongly a holiday)', H.is_market_open_today(date(2026, 11, 9)))
ck('Tue Nov 10 2026 (Diwali-Balipratipada) is closed', not H.is_market_open_today(date(2026, 11, 10)))
ck('Sun Nov 8 2026 (Muhurat) is not treated as a normal session', not H.is_market_open_today(date(2026, 11, 8)))
ck('every listed holiday is a weekday', all(d.weekday() < 5 for d in H.NSE_HOLIDAYS_2026))

print(f'\n{P} passed, {F} failed')
sys.exit(1 if F else 0)
