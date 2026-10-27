# -*- coding: utf-8 -*-
"""Settle the rv_iv sign question on the FORWARD-MOVE dependent variable.

    python research/rv_iv_forward.py            # on EC2

WHY THIS EXISTS. Two numbers were in conflict and they turned out to measure
different things:

    recorded   rho -0.2269, p=4.9e-77, n=6,529   rv_iv -> forward MOVE SIZE
    measured   rho +0.325,  p=2.6e-28            provenance unclear
    Oct 15     pooled null (p=0.18), n=62        rv_iv -> realised TRADE P&L,
                                                 sign unstable across halves
                                                 (chain -0.667 / +0.439,
                                                  permutation p=0.0002)

The P&L question is answered: as a continuous input rv_iv is unusable, as a
binary gate at 0.70 it survives both halves. This script answers the OTHER
one -- whether the underlying forward-move relationship is real at all.

THE PROBLEM WITH THE ORIGINAL n=6,529. ivinfo.py says so itself, in its own
header: "Overlapping windows -> p optimistic". Every 5-minute bar contributed
an observation whose forward window overlapped its neighbours' almost
entirely. With ~1 independent observation per session per instrument, the
effective n is nearer 300 than 6,529, and p=4.9e-77 is meaningless as stated.
A 4.9e-77 on a sample that is really ~300 wide is not evidence of a strong
effect; it is evidence of a miscounted denominator.

So this reports BOTH: the overlapping figure for comparability with the
recorded number, and a strictly non-overlapping figure -- one observation per
instrument-session -- which is the one that may be believed.

DEPENDENT VARIABLE. Not signed return: an option buyer is paid for MOVEMENT,
either way. Forward max excursion over the next 60 minutes as a percent of
spot, which is the quantity the payability ceiling is denominated in (median
0.31-0.36% observed against ~0.51% needed to break even on an ATM entry).

INPUTS, both already on the box:
    logs/oi_intraday.jsonl      instrument, ts, spot, atm_iv  (n~17,578)
    data/<inst>_5min/*.csv      for the bot's own HV definition
"""
import glob, json, os, sys, math, statistics as st, collections, random

sys.path.insert(0, '/opt/trading_bot/live_bot')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OI = '/opt/trading_bot/live_bot/logs/oi_intraday.jsonl'
HORIZON_MIN = 60
INSTS = ('NIFTY', 'BANKNIFTY', 'SENSEX')


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
    if n < 4:
        return 0.0, 1.0, n
    rx, ry = rank(x), rank(y)
    mx, my = st.mean(rx), st.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    rho = num / den if den else 0.0
    if abs(rho) >= 1:
        return rho, 0.0, n
    t = rho * math.sqrt((n - 2) / (1 - rho * rho))
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2))))
    return rho, p, n


def load():
    """(inst, epoch, spot, atm_iv) stamps, sorted."""
    rows = []
    with open(OI, encoding='utf-8', errors='ignore') as fh:
        for ln in fh:
            try:
                d = json.loads(ln)
            except Exception:
                continue
            iv, sp, ts = d.get('atm_iv'), d.get('spot'), d.get('ts')
            inst = d.get('instrument')
            if None in (iv, sp, ts) or inst not in INSTS:
                continue
            try:
                iv, sp, ts = float(iv), float(sp), float(ts)
            except Exception:
                continue
            if iv <= 0 or sp <= 0:
                continue
            rows.append((inst, ts, sp, iv, (d.get('ts_str') or '')[:10]))
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


def build(rows):
    """Attach trailing realised vol and forward max excursion to each stamp."""
    out = []
    by = collections.defaultdict(list)
    for inst, ts, sp, iv, day in rows:
        by[(inst, day)].append((ts, sp, iv))
    for (inst, day), ser in sorted(by.items()):
        ser.sort()
        if len(ser) < 40:
            continue
        for i, (ts, sp, iv) in enumerate(ser):
            # trailing realised vol from the spot path itself, annualised.
            # 30 stamps back, matching the bot's 30-bar HV window in spirit.
            if i < 30:
                continue
            seg = [s for _, s, _ in ser[i - 30:i + 1]]
            rets = [math.log(seg[k + 1] / seg[k]) for k in range(len(seg) - 1)
                    if seg[k] > 0 and seg[k + 1] > 0]
            if len(rets) < 20:
                continue
            try:
                sd = st.stdev(rets)
            except Exception:
                continue
            # stamps are ~1/min -> 375 per session, 252 sessions
            hv = sd * math.sqrt(375 * 252) * 100.0
            if hv <= 0:
                continue
            rv_iv = hv / iv
            fwd = [s for t, s, _ in ser[i + 1:] if t - ts <= HORIZON_MIN * 60]
            if len(fwd) < 10:
                continue
            exc = max(abs(s - sp) for s in fwd) / sp * 100.0
            out.append(dict(inst=inst, day=day, ts=ts, rv_iv=rv_iv, iv=iv,
                            hv=hv, exc=exc, spot=sp,
                            mins=(ts - ser[0][0]) / 60.0))
    return out


def report(name, rs, note=''):
    if len(rs) < 8:
        print(f'  {name:34} n={len(rs)} too few')
        return
    rho, p, n = spearman([r['rv_iv'] for r in rs], [r['exc'] for r in rs])
    print(f'  {name:34} n={n:6} rho={rho:+.4f} p={p:.3g}  '
          f'median exc {st.median([r["exc"] for r in rs]):.3f}%{note}')
    return rho, p, n


def quintiles(rs, label):
    if len(rs) < 25:
        print(f'    {label}: n={len(rs)} too few for quintiles')
        return
    s = sorted(rs, key=lambda r: r['rv_iv'])
    q = len(s) // 5
    print(f'    {label} — is it MONOTONE?')
    print(f'      {"rv_iv band":18} {"n":>5} {"median exc":>11} {"mean exc":>10}'
          f' {"% payable":>10}')
    prev = None
    mono = True
    for k in range(5):
        sub = s[k * q:(k + 1) * q] if k < 4 else s[4 * q:]
        if not sub:
            continue
        med = st.median([r['exc'] for r in sub])
        pay = 100.0 * sum(1 for r in sub if r['exc'] >= 0.51) / len(sub)
        lo, hi = sub[0]['rv_iv'], sub[-1]['rv_iv']
        print(f'      {lo:.2f}-{hi:.2f}       {len(sub):>5} {med:>10.3f}% '
              f'{st.mean([r["exc"] for r in sub]):>9.3f}% {pay:>9.0f}%')
        if prev is not None and med > prev:
            mono = False
        prev = med
    print(f'      monotone decreasing in rv_iv: {mono}')


def main():
    rows = load()
    print('=' * 86)
    print('rv_iv  vs  FORWARD MAX EXCURSION      (the magnitude question)')
    print('=' * 86)
    if not rows:
        print('\n  oi_intraday.jsonl produced no usable stamps.')
        return
    print(f'\n  {len(rows):,} raw stamps   '
          f'{rows[0][4]} .. {max(r[4] for r in rows)}   '
          f'instruments {sorted(set(r[0] for r in rows))}')
    data = build(rows)
    if not data:
        print('  no stamps survived the HV/forward-window requirements.')
        return
    sess = sorted({(d['inst'], d['day']) for d in data})
    print(f'  {len(data):,} usable observations over {len(sess)} '
          f'instrument-sessions  (horizon {HORIZON_MIN}min)')
    print(f'  break-even reference: an ATM entry needs ~0.51% of spot')

    print('\n--- 1. OVERLAPPING, as the recorded n=6,529 was measured ---')
    print('  Every stamp contributes; adjacent forward windows overlap almost')
    print('  entirely, so n is inflated and p is meaningless. Shown only for')
    print('  comparability with the recorded rho -0.2269, p=4.9e-77.')
    report('pooled (overlapping)', data)
    for i in INSTS:
        report(f'  {i} (overlapping)', [d for d in data if d['inst'] == i])

    print('\n--- 2. NON-OVERLAPPING: one observation per instrument-session ---')
    print('  The 10:00-ish stamp from each session. Windows cannot overlap, so')
    print('  n is the real independent sample and p can be believed.')
    one = {}
    for d in data:
        k = (d['inst'], d['day'])
        if k not in one or abs(d['mins'] - 45) < abs(one[k]['mins'] - 45):
            one[k] = d
    nov = sorted(one.values(), key=lambda d: (d['day'], d['inst']))
    report('pooled (independent)', nov)
    for i in INSTS:
        report(f'  {i} (independent)', [d for d in nov if d['inst'] == i])

    print('\n--- 3. does it hold in BOTH halves? (process rule 2) ---')
    h = len(nov) // 2
    report('1st half (independent)', nov[:h])
    report('2nd half (independent)', nov[h:])

    print('\n--- 4. monotonicity, which is what the recorded claim rests on ---')
    quintiles(nov, 'independent sample')
    print()
    quintiles(data, 'overlapping sample')

    print('\n--- 5. permutation test on the independent sample ---')
    print('  H0: rv_iv carries no information about forward excursion.')
    x = [d['rv_iv'] for d in nov]
    y = [d['exc'] for d in nov]
    obs, _, n = spearman(x, y)
    random.seed(13)
    N = 20000
    hits = 0
    for _ in range(N):
        ys = y[:]
        random.shuffle(ys)
        r, _, _ = spearman(x, ys)
        if abs(r) >= abs(obs):
            hits += 1
    print(f'  observed rho={obs:+.4f} on n={n};  '
          f'|rho| matched or beaten by {hits}/{N} shuffles = p={hits/N:.4f}')

    print('\n--- 6. the question that actually matters ---')
    print('  Does low rv_iv raise the share of windows that CLEAR break-even?')
    lo = [d for d in nov if d['rv_iv'] < 0.70]
    hi = [d for d in nov if d['rv_iv'] >= 0.70]
    for nm, sub in (('rv_iv <  0.70', lo), ('rv_iv >= 0.70', hi)):
        if not sub:
            continue
        pay = 100.0 * sum(1 for d in sub if d['exc'] >= 0.51) / len(sub)
        print(f'  {nm}  n={len(sub):4}  median exc {st.median([d["exc"] for d in sub]):.3f}%'
              f'   payable {pay:.1f}%')
    if lo and hi:
        pl = 100.0 * sum(1 for d in lo if d['exc'] >= 0.51) / len(lo)
        ph = 100.0 * sum(1 for d in hi if d['exc'] >= 0.51) / len(hi)
        print(f'  difference in payable share: {pl - ph:+.1f} points')
        print('  (the payability ceiling says 70-75% of windows are unpayable')
        print('   regardless; the useful question is whether rv_iv moves that)')


if __name__ == '__main__':
    main()
