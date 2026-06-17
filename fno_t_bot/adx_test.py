import sys, io
from contextlib import redirect_stdout, redirect_stderr
sys.argv = ['bot.py', 'vX']
sys.path.insert(0, '.')
import config

orig = {}
for k, v in config.INSTRUMENT_STRATEGY.items():
    orig[k] = dict(v)

test_cases = [
    (30, 25, 'Current   CALL>=30 PUT>=25'),
    (28, 23, 'Relaxed-1 CALL>=28 PUT>=23'),
    (25, 20, 'Relaxed-2 CALL>=25 PUT>=20'),
]

import bot   # import once after sys.argv set

results = []
for call_adx, put_adx, label in test_cases:
    for inst in config.INSTRUMENT_STRATEGY:
        config.INSTRUMENT_STRATEGY[inst]['call_adx_min'] = call_adx
        config.INSTRUMENT_STRATEGY[inst]['put_adx_min']  = put_adx

    buf = io.StringIO()
    with redirect_stdout(buf):
        m = bot.run_variant('vX')

    out = buf.getvalue()
    t = wr = net = dd = None
    for line in out.split('\n'):
        s = line.strip()
        if s.startswith('TRADES'):
            try: t = int(s.split(':')[1].strip().split()[0].replace(',',''))
            except: pass
        if s.startswith('Win Rate'):
            try: wr = float(s.split(':')[1].strip().split('%')[0])
            except: pass
        if s.startswith('Net P&L') and ('Rs' in s or chr(8377) in s):
            parts = s.replace(chr(8377),'').replace(',','').split()
            for p in reversed(parts):
                try: net = int(p); break
                except: pass
        if s.startswith('Max Drawdown'):
            try: dd = s.split(':')[1].strip()
            except: pass

    results.append((label, t or 0, wr or 0, net or 0, dd or 'N/A'))
    for k in config.INSTRUMENT_STRATEGY:
        config.INSTRUMENT_STRATEGY[k]['call_adx_min'] = orig[k]['call_adx_min']
        config.INSTRUMENT_STRATEGY[k]['put_adx_min']  = orig[k]['put_adx_min']

print()
print(f"{'Variant':<38} {'Trades':>7} {'WR%':>7} {'Net P&L':>11} {'MaxDD':>10}")
print('-'*77)
for label, t, wr, net, dd in results:
    print(f"{label:<38} {t:>7} {wr:>6.1f}% {net:>+11,} {dd:>10}")
