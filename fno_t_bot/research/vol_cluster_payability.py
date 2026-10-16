# -*- coding: utf-8 -*-
"""Does volatility CLUSTER usefully -- i.e. can yesterday tell us whether today
is even worth trading?

    python research/vol_cluster_payability.py        # on EC2

THE PREMISE UNDER TEST, not the model. GARCH says conditional variance is
autocorrelated: big moves follow big moves. If that is true and useful here,
then a cheap lag must already show it, because GARCH(1,1) is essentially a
weighted average of recent squared returns and recent variance. So test the
PREMISE with a one-line predictor before fitting anything. If lagged realised
vol does not predict today's payability, a fitted GARCH will not rescue it --
it would only be a smoother version of a predictor that does not work.

WHY PAYABILITY IS THE RIGHT TARGET, not volatility itself. Forecasting vol
better than trailing HV is easy and useless here: the option price already
embeds a vol forecast, and ours is biased high -- 90.7% of entries bought
volatility the index never delivered, a median 34% overpayment. So the useful
question is not "will vol be high" but "will today's move clear the bar an ATM
option has to clear", which is about 0.51% of spot round trip.

The payability table measured so far is UNCONDITIONAL:

    runway    median excursion    can reach break-even
     30min        0.139%                 2.1%
     60min        0.196%                 4.3%
    120min        0.259%                16.2%
    240min        0.404%                37.2%

If volatility clusters usefully, splitting those same windows by YESTERDAY's
realised vol should separate them. If it does not, the unconditional ceiling is
the whole story and no vol model changes it.

PRE-REGISTERED, per REBUILD_PLAN process rules:
  Passes   top-vol-quintile sessions show a materially higher payable share
           than bottom-quintile, in BOTH chronological halves, n>=30 per cohort
  Fails    separation that appears only pooled, or only in one half
"""
import glob, math, os, re, statistics as st, sys, collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA = os.environ.get('FNO_DATA', '/opt/trading_bot/data')
DIRS = {'NIFTY': 'nifty_5min', 'BANKNIFTY': 'banknifty_5min', 'SENSEX': 'sensex_5min'}
BREAKEVEN = 0.51        # % of spot an ATM entry needs, round trip
HORIZONS = (60, 120, 240)
ENTRY_MIN = 45          # minutes into the session


def rank(v):
    s = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and v[s[j + 1]] == v[s[i]]:
            j += 1
        a = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            r[s[k]] = a
        i = j + 1
    return r


def spearman(x, y):
    n = len(x)
    if n < 6:
        return 0.0, 1.0, n
    rx, ry = rank(x), rank(y)
    mx, my = st.mean(rx), st.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    rho = num / den if den else 0.0
    if abs(rho) >= 1:
        return rho, 0.0, n
    t = rho * math.sqrt((n - 2) / (1 - rho * rho))
    return rho, 2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2)))), n


def load_session(path):
    """(bars) as [(hhmm, close)] from a 5-min csv, header-agnostic."""
    out = []
    with open(path, encoding='utf-8', errors='ignore') as fh:
        head = fh.readline().strip().split(',')
        try:
            ci = next(i for i, h in enumerate(head) if h.strip().lower() == 'close')
            ti = 0
        except StopIteration:
            return out
        for ln in fh:
            p = ln.rstrip('\n').split(',')
            if len(p) <= ci:
                continue
            try:
                out.append((p[ti][-8:-3], float(p[ci])))
            except Exception:
                continue
    return out


def session_stats(bars):
    """realised vol % and the forward excursions from the entry bar."""
    if len(bars) < 30:
        return None
    closes = [c for _, c in bars]
    rets = [math.log(closes[i + 1] / closes[i])
            for i in range(len(closes) - 1)
            if closes[i] > 0 and closes[i + 1] > 0]
    if len(rets) < 20:
        return None
    try:
        rv = st.stdev(rets) * math.sqrt(len(rets)) * 100.0   # session realised vol %
    except Exception:
        return None
    # entry bar ~ENTRY_MIN into the session (5-min bars)
    ei = min(len(bars) - 5, max(0, ENTRY_MIN // 5))
    sp = closes[ei]
    exc = {}
    for h in HORIZONS:
        nb = h // 5
        fwd = closes[ei + 1: ei + 1 + nb]
        if len(fwd) < max(3, nb // 2):
            continue
        exc[h] = max(abs(c - sp) for c in fwd) / sp * 100.0
    if not exc:
        return None
    return dict(rv=rv, exc=exc, rng=(max(closes) - min(closes)) / sp * 100.0)


rows = []
for inst, sub in DIRS.items():
    files = sorted(glob.glob(os.path.join(DATA, sub, '*.csv')))
    prev = None
    for f in files:
        m = re.search(r'(\d{4}-?\d{2}-?\d{2})', os.path.basename(f))
        day = m.group(1) if m else os.path.basename(f)
        s = session_stats(load_session(f))
        if s is None:
            prev = None
            continue
        if prev is not None:
            rows.append(dict(inst=inst, day=day, rv_prev=prev['rv'],
                             rv_today=s['rv'], exc=s['exc'], rng=s['rng']))
        prev = s

print('=' * 88)
print('DOES VOLATILITY CLUSTER USEFULLY?  lagged RV -> today\'s payability')
print('=' * 88)
if not rows:
    print(f'\n  no sessions loaded from {DATA}')
    sys.exit(0)
rows.sort(key=lambda r: (r['day'], r['inst']))
print(f'\n  {len(rows)} instrument-sessions with a usable previous day')
print(f'  {sorted({r["inst"] for r in rows})}   {rows[0]["day"]} .. {rows[-1]["day"]}')
print(f'  break-even for an ATM entry: {BREAKEVEN}% of spot')

print('\n--- 1. is there vol clustering AT ALL? (the GARCH premise) ---')
rho, p, n = spearman([r['rv_prev'] for r in rows], [r['rv_today'] for r in rows])
print(f'  RV(t-1) vs RV(t):          n={n:4} rho={rho:+.3f} p={p:.3g}')
print('  (if this is weak, nothing downstream can work -- GARCH is a weighted')
print('   version of exactly this autocorrelation)')

print('\n--- 2. does it predict PAYABILITY, which is what we actually need? ---')
for h in HORIZONS:
    sub = [r for r in rows if h in r['exc']]
    if len(sub) < 20:
        continue
    rho, p, n = spearman([r['rv_prev'] for r in sub], [r['exc'][h] for r in sub])
    print(f'  RV(t-1) vs {h:>3}min excursion:  n={n:4} rho={rho:+.3f} p={p:.3g}')

print('\n--- 3. the decision that matters: trade today or not? ---')
print('  Split sessions by YESTERDAY\'s realised vol. If clustering is usable,')
print('  the top quintile must be materially more payable than the bottom.')
for h in HORIZONS:
    sub = [r for r in rows if h in r['exc']]
    if len(sub) < 25:
        continue
    s = sorted(sub, key=lambda r: r['rv_prev'])
    q = len(s) // 5
    print(f'\n  horizon {h}min   (n={len(s)})')
    print(f'    {"RV(t-1) quintile":22} {"n":>4} {"median exc":>11} {"PAYABLE":>9}')
    shares = []
    for k in range(5):
        part = s[k * q:(k + 1) * q] if k < 4 else s[4 * q:]
        if not part:
            continue
        ex = [r['exc'][h] for r in part]
        pay = 100.0 * sum(1 for e in ex if e >= BREAKEVEN) / len(ex)
        shares.append(pay)
        print(f'    Q{k+1} {part[0]["rv_prev"]:.2f}-{part[-1]["rv_prev"]:.2f}'
              f'{"":6} {len(part):>4} {st.median(ex):>10.3f}% {pay:>8.1f}%')
    if len(shares) == 5:
        print(f'    top-minus-bottom payable share: {shares[-1] - shares[0]:+.1f} points')

print('\n--- 4. both halves, on the 240min horizon (process rule 2) ---')
sub = [r for r in rows if 240 in r['exc']]
if len(sub) >= 40:
    half = len(sub) // 2
    for nm, part in (('1st half', sub[:half]), ('2nd half', sub[half:])):
        s = sorted(part, key=lambda r: r['rv_prev'])
        q = max(1, len(s) // 5)
        lo = [r['exc'][240] for r in s[:q]]
        hi = [r['exc'][240] for r in s[-q:]]
        pl = 100.0 * sum(1 for e in lo if e >= BREAKEVEN) / len(lo)
        ph = 100.0 * sum(1 for e in hi if e >= BREAKEVEN) / len(hi)
        print(f'  {nm}: low-vol payable {pl:.1f}%  high-vol payable {ph:.1f}%'
              f'   gap {ph - pl:+.1f} pts   (n={len(s)})')
    print('\n  A gap that holds in BOTH halves is the only version worth acting on.')
else:
    print(f'  only {len(sub)} sessions at 240min -- too few to halve')
