# -*- coding: utf-8 -*-
"""
day_classifier.py — Realized day-shape taxonomy (Jul 2026).

Answers "what kind of day was it ACTUALLY?" — as opposed to regime_at_open
(a forecast from prior days) and gap_type (open print only). Labels every
(date, instrument) day from the intraday price path already recorded in the
daily log files' [SCAN] lines, so it backfills the full live history with
no API calls, then joins the labels onto trade JSONLs for cohort stats.

Day types (checked in priority order):
  WATERFALL   — move concentrated late: last-150min share ≥ 55% of range,
                net move ≥ 0.45% in that direction     (Jul 8)
  V_REVERSAL  — extreme printed in first ~2h, then closed ≥ 55% of range
                away from it on the other side          (Jul 13 morning)
  TREND       — efficiency ≥ 0.60 and range ≥ 0.60%     (directional day)
  GRIND       — efficiency ≥ 0.50, range 0.35–1.0%, ADX never > 32
                before 13:00                            (Jul 16)
  WHIPSAW     — efficiency < 0.35 with ≥ 2 direction legs > 0.30% each
                                                        (Jul 14)
  CHOP        — range < 0.35%                           (dead day)
  MIXED       — everything else

efficiency = |close − first| / (day high − day low).

Usage:
  python day_classifier.py                # classify all days, write JSONL
  python day_classifier.py --join         # + join trades, print cohort table
Output: logs/day_types.jsonl  (one record per date×instrument)
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')

_SCAN_RE = re.compile(
    r'^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}):\d{2},\d+ .*\[SCAN\] '
    r'(NIFTY|BANKNIFTY|SENSEX) Px=([0-9,]+).*?ADX=([0-9.]+)'
)


def _series_from_log(path: str):
    """Extract [(hh:mm, px, adx)] from one daily log file, keyed by instrument."""
    out = defaultdict(list)
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                m = _SCAN_RE.match(line)
                if m:
                    _, hm, inst, px, adx = m.groups()
                    out[inst].append((hm, float(px.replace(',', '')), float(adx)))
    except OSError:
        pass
    return out


def classify(series: list[tuple]) -> dict | None:
    """Classify one day's (hh:mm, px, adx) series into a day_type record."""
    if len(series) < 12:          # need a reasonable sampling of the session
        return None
    times  = [s[0] for s in series]
    px     = [s[1] for s in series]
    adx    = [s[2] for s in series]
    first, last = px[0], px[-1]
    hi, lo = max(px), min(px)
    rng    = hi - lo
    if rng <= 0 or first <= 0:
        return None
    rng_pct  = rng / first * 100
    net      = last - first
    net_pct  = net / first * 100
    eff      = abs(net) / rng

    # last-150-minutes share of the range, in the net direction
    late_idx = [i for i, t in enumerate(times) if t >= '13:00']
    late_share = 0.0
    if late_idx and rng > 0:
        li = late_idx[0]
        late_move = last - px[li]
        late_share = (late_move / rng) if net >= 0 else (-late_move / rng)

    # first-2h extreme + reversal distance. A true V needs a COUNTER-LEG first:
    # the early extreme must sit ≥0.25% beyond the open (a real move away),
    # then the close ≥55% of the range back on the other side. Days that open
    # AT their extreme and walk away all session are TREND, not V_REVERSAL.
    early_idx = [i for i, t in enumerate(times) if t <= '11:15']
    v_rev = False
    if early_idx:
        e_hi = max(px[i] for i in early_idx)
        e_lo = min(px[i] for i in early_idx)
        if (e_lo == lo and (first - e_lo) / first * 100 >= 0.25
                and (last - lo) / rng >= 0.55):
            v_rev = True
        if (e_hi == hi and (e_hi - first) / first * 100 >= 0.25
                and (hi - last) / rng >= 0.55):
            v_rev = True

    # direction legs > 0.30% (whipsaw detector)
    legs, leg_start, leg_dir = 0, px[0], 0
    for p in px[1:]:
        d = p - leg_start
        if abs(d) / first * 100 >= 0.30:
            direction = 1 if d > 0 else -1
            if direction != leg_dir:
                legs += 1
                leg_dir = direction
            leg_start = p
    # ADX before 13:00
    pre13_adx = [a for (t, _, a) in series if t < '13:00']
    adx_max_pre13 = max(pre13_adx) if pre13_adx else 0.0

    if late_share >= 0.55 and abs(net_pct) >= 0.45:
        day_type = 'WATERFALL'
    elif v_rev:
        day_type = 'V_REVERSAL'
    elif eff >= 0.60 and rng_pct >= 0.60:
        day_type = 'TREND'
    elif eff >= 0.50 and 0.35 <= rng_pct <= 1.00 and adx_max_pre13 <= 32:
        day_type = 'GRIND'
    elif eff < 0.35 and legs >= 2:
        day_type = 'WHIPSAW'
    elif rng_pct < 0.35:
        day_type = 'CHOP'
    else:
        day_type = 'MIXED'

    return {
        'day_type'     : day_type,
        'net_pct'      : round(net_pct, 3),
        'range_pct'    : round(rng_pct, 3),
        'efficiency'   : round(eff, 2),
        'late_share'   : round(late_share, 2),
        'legs'         : legs,
        'adx_max_pre13': round(adx_max_pre13, 1),
        'direction'    : 'UP' if net > 0 else 'DOWN',
    }


def run(join_trades: bool = False) -> None:
    day_recs = []
    for fname in sorted(os.listdir(LOG_DIR)):
        m = re.match(r'FnO_T_Bot_(NIFTY|BANKNIFTY|SENSEX)_(\d{8})\.log$', fname)
        if not m:
            continue
        inst, ymd = m.groups()
        date_str = f'{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}'
        series = _series_from_log(os.path.join(LOG_DIR, fname)).get(inst, [])
        rec = classify(series)
        if rec:
            rec.update({'date': date_str, 'instrument': inst})
            day_recs.append(rec)

    out_path = os.path.join(LOG_DIR, 'day_types.jsonl')
    with open(out_path, 'w', encoding='utf-8') as f:
        for r in day_recs:
            f.write(json.dumps(r) + '\n')
    print(f'Classified {len(day_recs)} instrument-days -> {out_path}')

    counts = defaultdict(int)
    for r in day_recs:
        counts[r['day_type']] += 1
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f'  {k:12s} {v}')

    if not join_trades:
        return

    # join trades by (date, instrument)
    day_by_key = {(r['date'], r['instrument']): r for r in day_recs}
    skip = ('near_miss', 'challenger', 'test', 'predictions', 'weekly', 'day_types')
    stats = defaultdict(lambda: {'n': 0, 'w': 0, 'pnl': 0.0})
    for fname in sorted(os.listdir(LOG_DIR)):
        if 'trades' not in fname or not fname.endswith('.jsonl'):
            continue
        if any(s in fname for s in skip):
            continue
        for line in open(os.path.join(LOG_DIR, fname), encoding='utf-8'):
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            et = t.get('entry_time', '')[:10]
            key = (et, t.get('instrument', ''))
            dt = day_by_key.get(key, {}).get('day_type', 'UNCLASSIFIED')
            p = t.get('pnl_net', 0.0)
            s = stats[dt]
            s['n'] += 1
            s['w'] += 1 if p > 0 else 0
            s['pnl'] += p
    print('\nLive trades by realized day shape:')
    for k, v in sorted(stats.items(), key=lambda x: x[1]['pnl']):
        wr = 100 * v['w'] / v['n'] if v['n'] else 0
        print(f'  {k:12s} {v["n"]:3d}t  WR {wr:3.0f}%  net {v["pnl"]:+10,.0f}  avg {v["pnl"]/v["n"]:+8,.0f}')


if __name__ == '__main__':
    run(join_trades='--join' in sys.argv)
