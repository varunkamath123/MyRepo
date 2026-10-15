# -*- coding: utf-8 -*-
"""At what HOLDING HORIZON does buying ATM premium become payable at all?

    python research/payability_horizon.py       # on EC2

This is the question the rebuild actually turns on, and it is prior to any
signal work. An ATM option entry needs roughly 0.51% of spot to move its
premium through the round trip to break even. Measured Oct 1-2 2026 over
10:00->14:30 windows, the median best-case excursion was 0.31-0.36%, so even a
perfect directional oracle left 70-75% of entries unable to clear the bar.
That is the payability ceiling.

rv_iv_forward.py then found something sharper at a 60-minute horizon: median
excursion 0.17% and only 2-11% of windows payable. If that holds, a one-hour
ATM option buy is close to structurally unpayable no matter what the signal
says, and no amount of direction or magnitude research can fix it.

So sweep the horizon. Two things come out of it:
  1. the horizon at which payability stops being hopeless, which is a hard
     constraint on FORCE_CLOSE_TIME and on the entry window
  2. whether rv_iv predicts payability at any horizon -- tested per horizon
     on a strictly non-overlapping sample, because the overlapping version
     inflates n by ~35x and produced a p of literally 0

ALSO SETTLES A MEASUREMENT WORRY. rv_iv_forward.py reconstructed rv_iv from
per-minute spot stamps and got a range of 0.42-6.38, while the bot's own
logged rv_iv runs 0.23-1.41. Those are not the same quantity, so conclusions
about the GATE could not be drawn from it. Here the bot's own logged rv_iv is
joined in directly where it exists, so the gate's actual scale is used.
"""
import glob, json, os, sys, math, statistics as st, collections, random, re

sys.path.insert(0, '/opt/trading_bot/live_bot')

OI = '/opt/trading_bot/live_bot/logs/oi_intraday.jsonl'
LOGS = '/opt/trading_bot/live_bot/logs/'
HORIZONS = (15, 30, 60, 120, 180, 240, 300)
BREAKEVEN = 0.51          # % of spot an ATM entry needs, round trip
INSTS = ('NIFTY', 'BANKNIFTY', 'SENSEX')


def rank(v):
    s = sorted(range(len(v)), key=lambda i: v[i]); r = [0.0] * len(v); i = 0
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


def load_stamps():
    by = collections.defaultdict(list)
    with open(OI, encoding='utf-8', errors='ignore') as fh:
        for ln in fh:
            try:
                d = json.loads(ln)
            except Exception:
                continue
            inst, iv, sp, ts = (d.get('instrument'), d.get('atm_iv'),
                                d.get('spot'), d.get('ts'))
            if inst not in INSTS or None in (iv, sp, ts):
                continue
            try:
                iv, sp, ts = float(iv), float(sp), float(ts)
            except Exception:
                continue
            if sp <= 0 or iv <= 0:
                continue
            by[(inst, (d.get('ts_str') or '')[:10])].append((ts, sp, iv))
    for k in by:
        by[k].sort()
    return by


def logged_rv_iv():
    """The bot's OWN rv_iv, as the live gate computes it, keyed by session."""
    out = {}
    for f in glob.glob(LOGS + '*trades*.jsonl'):
        m = re.search(r'_([A-Z]+)_trades_(\d{4}-\d{2}-\d{2})', f)
        if not m:
            continue
        for ln in open(f, encoding='utf-8', errors='ignore'):
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if d.get('rv_iv') is None:
                continue
            out.setdefault((m.group(1), m.group(2)), []).append(
                (float(d['rv_iv']), d.get('rv_iv_src')))
    return out


def excursion(ser, i, horizon_min):
    ts, sp, _ = ser[i]
    fwd = [s for t, s, _ in ser[i + 1:] if t - ts <= horizon_min * 60]
    # stamps arrive about every 5 min, so require ~half the window filled
    if len(fwd) < max(3, horizon_min // 10):
        return None
    return max(abs(s - sp) for s in fwd) / sp * 100.0


def main():
    by = load_stamps()
    if not by:
        print('no stamps'); return
    days = sorted({k[1] for k in by})
    print('=' * 88)
    print('PAYABILITY vs HOLDING HORIZON   —  how long must a position be held')
    print('=' * 88)
    print(f'\n  {len(by)} instrument-sessions, {days[0]} .. {days[-1]}')
    print(f'  instruments {sorted({k[0] for k in by})}')
    print(f'  break-even for an ATM entry: {BREAKEVEN}% of spot, round trip')
    print(f'\n  One observation per instrument-session per horizon (entry ~45min')
    print(f'  after the first stamp), so forward windows never overlap.')

    print(f'\n  {"horizon":>8} {"n":>5} {"median exc":>12} {"mean":>8}'
          f' {"p90":>8} {"PAYABLE":>9}  {"verdict":<22}')
    rows_by_h = {}
    for h in HORIZONS:
        obs = []
        for (inst, day), ser in sorted(by.items()):
            if len(ser) < 40:
                continue
            # Entry stamp ~45 min into the session, consistent across
            # horizons. No trailing-HV warmup requirement here -- this script
            # needs only the forward excursion, and oi_levels stamps about
            # every 5 minutes, so demanding 30 prior stamps would have meant
            # entering 150 minutes in and rejecting every session.
            i = min(range(len(ser)),
                    key=lambda k: abs((ser[k][0] - ser[0][0]) / 60.0 - 45))
            if i + 3 >= len(ser):
                continue
            e = excursion(ser, i, h)
            if e is None:
                continue
            obs.append(dict(inst=inst, day=day, exc=e))
        if not obs:
            print(f'  {h:>6}min {0:>5}   (no windows of this length)')
            continue
        rows_by_h[h] = obs
        ex = sorted(o['exc'] for o in obs)
        pay = 100.0 * sum(1 for e in ex if e >= BREAKEVEN) / len(ex)
        p90 = ex[int(0.90 * (len(ex) - 1))]
        v = ('hopeless' if pay < 15 else 'thin' if pay < 35
             else 'workable' if pay < 55 else 'comfortable')
        print(f'  {h:>6}min {len(ex):>5} {st.median(ex):>11.3f}%'
              f' {st.mean(ex):>7.3f}% {p90:>7.3f}% {pay:>8.1f}%  {v:<22}')

    print('\n  How far must break-even fall for a 60-min hold to be workable?')
    if 60 in rows_by_h:
        ex = sorted(o['exc'] for o in rows_by_h[60])
        for target in (35, 50):
            need = ex[max(0, int((1 - target / 100.0) * (len(ex) - 1)))]
            print(f'    for {target}% of 60-min windows to pay, break-even must be'
                  f' <= {need:.3f}% of spot  ({BREAKEVEN / need:.1f}x cheaper'
                  f' than an ATM entry)')

    print('\n--- does the bot\'s OWN logged rv_iv predict payability? ---')
    lg = logged_rv_iv()
    print(f'  sessions with a logged rv_iv: {len(lg)}')
    if lg:
        allv = [v for vs in lg.values() for v, _ in vs]
        print(f'  logged rv_iv range {min(allv):.3f}-{max(allv):.3f}'
              f'  (the gate fires at 0.70)')
        for h in (30, 60, 120, 240):
            if h not in rows_by_h:
                continue
            pairs = []
            for o in rows_by_h[h]:
                k = (o['inst'], o['day'])
                if k in lg:
                    pairs.append((st.mean([v for v, _ in lg[k]]), o['exc']))
            if len(pairs) < 8:
                print(f'  {h:>4}min  n={len(pairs)} too few')
                continue
            rho, p, n = spearman([a for a, _ in pairs], [b for _, b in pairs])
            lo = [b for a, b in pairs if a < 0.70]
            hi = [b for a, b in pairs if a >= 0.70]
            msg = ''
            if lo and hi:
                msg = (f'   exc: rv_iv<0.70 {st.median(lo):.3f}%'
                       f' vs >=0.70 {st.median(hi):.3f}%  (n {len(lo)}/{len(hi)})')
            print(f'  {h:>4}min  n={n:3} rho={rho:+.3f} p={p:.3f}{msg}')
    print('\n  NOTE: this joins the bot\'s logged rv_iv on sessions where a TRADE')
    print('  was taken, so it inherits that selection. It is the right scale but')
    print('  a biased sample; treat the sign as indicative, not settled.')


if __name__ == '__main__':
    main()
