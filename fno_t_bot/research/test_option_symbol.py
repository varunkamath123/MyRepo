# -*- coding: utf-8 -*-
"""Regression test for the Oct/Nov/Dec weekly option symbol month code.

From Sep 24 2026 every NIFTY and SENSEX weekly quote silently failed and fell
back to Black-Scholes, because build_option_symbol mapped October to 'A'
(continuing the alphabet past 9) rather than 'O' (the month's first letter).
BS pricing carries a measured 41-123% error in this project, so several days of
P&L -- real trades and counterfactual phantoms alike -- were fiction.

The failure was silent, which is what made it expensive. These tests pin the
format so it cannot recur, including in November and December.
"""
import os, sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fyers_orders import build_option_symbol, alt_month_symbol

P = F = 0


def ck(name, got, want):
    global P, F
    if got == want:
        P += 1
        print(f'  PASS  {name}')
    else:
        F += 1
        print(f'  FAIL  {name}\n          got  {got}\n          want {want}')


print('=' * 74)
print('OPTION SYMBOL — Oct/Nov/Dec weekly month code')
print('=' * 74)

# Known-good: these exact strings returned real LTPs in the logs, so they are
# ground truth for the format, not a guess.
ck('Sep weekly unchanged (this shape resolved live)',
   build_option_symbol('NIFTY', 23800, 'CALL', date(2026, 9, 8)),
   'NSE:NIFTY2690823800CE')
ck('monthly format unchanged (this shape resolved live)',
   build_option_symbol('NIFTY', 23200, 'CALL', date(2026, 9, 24)),
   'NSE:NIFTY26SEP23200CE')

# The bug and its siblings
ck('October weekly uses O, not A',
   build_option_symbol('NIFTY', 22850, 'PUT', date(2026, 10, 6)),
   'NSE:NIFTY26O0622850PE')
ck('October weekly SENSEX uses O',
   build_option_symbol('SENSEX', 73000, 'CALL', date(2026, 10, 1)),
   'BSE:SENSEX26O0173000CE')
ck('November weekly uses N, not B',
   build_option_symbol('NIFTY', 22850, 'PUT', date(2026, 11, 3)),
   'NSE:NIFTY26N0322850PE')
ck('December weekly uses D, not C',
   build_option_symbol('NIFTY', 22850, 'PUT', date(2026, 12, 1)),
   'NSE:NIFTY26D0122850PE')

# no A/B/C may survive anywhere in a weekly symbol
for m, d in ((10, 6), (11, 3), (12, 1)):
    sym = build_option_symbol('NIFTY', 22850, 'PUT', date(2026, m, d))
    body = sym.split('NIFTY')[1]
    ck(f'month {m}: no stale A/B/C in {sym}',
       any(ch in body[2:3] for ch in 'OND'), True)

# the self-verifying alternate
ck('alternate flips O back to A',
   alt_month_symbol('NSE:NIFTY26O0622850PE'), 'NSE:NIFTY26A0622850PE')
ck('alternate flips N back to B',
   alt_month_symbol('NSE:NIFTY26N0322850PE'), 'NSE:NIFTY26B0322850PE')
ck('alternate flips D back to C',
   alt_month_symbol('NSE:NIFTY26D0122850PE'), 'NSE:NIFTY26C0122850PE')
ck('alternate is None for a digit month (nothing to flip)',
   alt_month_symbol('NSE:NIFTY2690823800CE'), None)
ck('alternate is None for a monthly symbol',
   alt_month_symbol('NSE:BANKNIFTY26OCT22850PE'), None)

print('=' * 74)
print(f'RESULT: {P} passed, {F} failed')
print('=' * 74)
sys.exit(1 if F else 0)
