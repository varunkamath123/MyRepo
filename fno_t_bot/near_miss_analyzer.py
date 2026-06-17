"""
near_miss_analyzer.py — statistical analysis of near-miss events.

Run manually after accumulating 30+ trading days of data:
    python near_miss_analyzer.py
    python near_miss_analyzer.py NIFTY          # single instrument
    python near_miss_analyzer.py --days 14      # last N days only

Reads:  live_bot/logs/near_miss_{INSTRUMENT}_{YYYY-MM-DD}.jsonl
Prints: per-instrument ADX gap distributions, weekday/hour breakdowns,
        HTF alignment rates, and a plain-English recommendation.

DO NOT use this to tweak strategy parameters day-by-day.
Use it to build a statistical case for threshold changes after 30–60 days.
"""

from __future__ import annotations

import glob
import io
import json
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean, median, stdev

# Force UTF-8 output on Windows terminals that default to cp1252
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

_LOG_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
_INSTRUMENTS = ['NIFTY', 'BANKNIFTY', 'SENSEX']
_WEEKDAYS    = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']


# ── Data loading ─────────────────────────────────────────────────────────────

def _load_events(instrument: str, days: int | None = None) -> list[dict]:
    """Load near_miss records (record_type='near_miss') for the given instrument."""
    pattern = os.path.join(_LOG_DIR, f'near_miss_{instrument}_*.jsonl')
    paths = sorted(glob.glob(pattern))

    if days is not None:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        paths = [p for p in paths if os.path.basename(p).split('_')[-1].replace('.jsonl', '') >= cutoff]

    events: list[dict] = []
    for path in paths:
        try:
            with open(path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        # Accept both old records (no record_type) and new near_miss records
                        if rec.get('record_type', 'near_miss') == 'near_miss':
                            events.append(rec)
                    except Exception:
                        pass
        except Exception:
            pass
    return events


def _load_outcomes(instrument: str, days: int | None = None) -> list[dict]:
    """Load outcome records (record_type='outcome') for the given instrument."""
    pattern = os.path.join(_LOG_DIR, f'near_miss_{instrument}_*.jsonl')
    paths = sorted(glob.glob(pattern))

    if days is not None:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        paths = [p for p in paths if os.path.basename(p).split('_')[-1].replace('.jsonl', '') >= cutoff]

    outcomes: list[dict] = []
    for path in paths:
        try:
            with open(path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        if rec.get('record_type') == 'outcome':
                            outcomes.append(rec)
                    except Exception:
                        pass
        except Exception:
            pass
    return outcomes


# ── Analysis helpers ──────────────────────────────────────────────────────────

def _bucket(value: float, boundaries: list[float]) -> str:
    """Return a human-readable bucket label for value given boundary list."""
    prev = 0.0
    for b in boundaries:
        if value < b:
            return f'{prev:.0f}–{b:.0f}pt'
        prev = b
    return f'>={boundaries[-1]:.0f}pt'


def _counter_summary(counter: dict, total: int) -> str:
    parts = [f'{k}={v}({v*100//total}%)' for k, v in sorted(counter.items())]
    return '  '.join(parts)


def _hour(time_str: str) -> str:
    """'11:25' → '11'"""
    return time_str.split(':')[0]


def _weekday(date_str: str) -> str:
    """'2026-03-20' → 'Fri'"""
    try:
        d = date.fromisoformat(date_str)
        return _WEEKDAYS[d.weekday()]
    except Exception:
        return '?'


# ── Outcome analysis ─────────────────────────────────────────────────────────

def analyze_outcomes(instrument: str, events: list[dict], outcomes: list[dict]) -> None:
    """Print reversal vs continuation statistics for near-miss events.

    For each gate type (ADX_LOW / STALE_CROSS) and direction (CALL / PUT),
    shows what fraction of near-misses resulted in price moving WITH the
    signal (continued) vs AGAINST it (reversed) at +30/60/90 min.

    A reversal rate >= 65% on CALL near-misses means price fell 65% of the
    time after the bot almost bought a CALL — that's a potential sell signal.
    """
    if not outcomes:
        print(f'\n  {instrument} Outcome Analysis: No outcome records yet.')
        print('    → Outcomes are recorded automatically by the live bot at +30/60/90 min')
        print('    → Check back after a few trading days.')
        return

    # Dedup outcomes by (date, time, reason, direction, outcome_at)
    seen_out: set[tuple] = set()
    deduped_out: list[dict] = []
    for o in outcomes:
        key = (o['date'], o['time'], o['reason'], o['direction'], o['outcome_at'])
        if key not in seen_out:
            seen_out.add(key)
            deduped_out.append(o)
    outcomes = deduped_out

    print(f'\n  {instrument} Outcome Analysis  ({len(outcomes)} outcome records)')
    print('  ' + '-' * 56)

    REVERSAL_SIGNAL_THRESHOLD = 65  # % — below this = not statistically useful

    for gate in ('ADX_LOW', 'STALE_CROSS'):
        gate_outcomes = [o for o in outcomes if o.get('reason') == gate]
        if not gate_outcomes:
            continue
        print(f'\n  {gate}:')
        for direction in ('CALL', 'PUT'):
            dir_out = [o for o in gate_outcomes if o.get('direction') == direction]
            if not dir_out:
                continue
            # Group by outcome_at
            total_events = len({(o['date'], o['time']) for o in dir_out})
            print(f'    {direction} ({total_events} near-miss events, {len(dir_out)} outcomes):')
            for horizon in ('30m', '60m', '90m'):
                h_out = [o for o in dir_out if o.get('outcome_at') == horizon]
                if not h_out:
                    continue
                n = len(h_out)
                cont = sum(1 for o in h_out if o.get('result') == 'CONTINUED')
                rev  = sum(1 for o in h_out if o.get('result') == 'REVERSED')
                flat = sum(1 for o in h_out if o.get('result') == 'FLAT')
                rev_pct = rev * 100 // n
                cont_pct = cont * 100 // n

                # Average move (signed: + = price went up)
                moves = [o.get('move_pts', 0.0) for o in h_out]
                avg_move = sum(moves) / n
                move_str = f'{avg_move:+.0f}pts avg'

                flag = ''
                if direction == 'CALL' and rev_pct >= REVERSAL_SIGNAL_THRESHOLD:
                    flag = '  *** REVERSAL SIGNAL'
                elif direction == 'PUT' and rev_pct >= REVERSAL_SIGNAL_THRESHOLD:
                    flag = '  *** REVERSAL SIGNAL'

                print(f'      +{horizon}: '
                      f'CONT={cont}({cont_pct}%)  '
                      f'REV={rev}({rev_pct}%)  '
                      f'FLAT={flat}({flat * 100 // n}%)'
                      f'  [{move_str}]{flag}')

    # ── Overall reversal verdict ──────────────────────────────────────────────
    print()
    call_out_30m = [o for o in outcomes
                    if o.get('direction') == 'CALL' and o.get('outcome_at') == '30m']
    put_out_30m  = [o for o in outcomes
                    if o.get('direction') == 'PUT'  and o.get('outcome_at') == '30m']

    for dir_name, dir_data in [('CALL', call_out_30m), ('PUT', put_out_30m)]:
        if len(dir_data) < 10:
            print(f'  {dir_name} near-misses: only {len(dir_data)} +30m outcomes'
                  f' — need ≥10 for a verdict.')
            continue
        rev_n = sum(1 for o in dir_data if o.get('result') == 'REVERSED')
        rev_pct = rev_n * 100 // len(dir_data)
        opp = 'PUT' if dir_name == 'CALL' else 'CALL'
        if rev_pct >= REVERSAL_SIGNAL_THRESHOLD:
            print(f'  *** {dir_name} near-miss → REVERSAL signal: {rev_pct}% reversed within 30min')
            print(f'      → Warrants backtesting a "{dir_name} near-miss as {opp} context" gate')
        elif rev_pct >= 55:
            print(f'  {dir_name} near-miss reversal rate: {rev_pct}% — borderline, keep monitoring')
        else:
            print(f'  {dir_name} near-miss reversal rate: {rev_pct}% — no clear signal'
                  f' (need ≥{REVERSAL_SIGNAL_THRESHOLD}%)')


# ── Main per-instrument analysis ─────────────────────────────────────────────

def analyze(instrument: str, events: list[dict],
            outcomes: list[dict] | None = None) -> None:
    if not events:
        print(f'\n{instrument}: No near-miss events found.')
        print('  → Need at least 1 trading day with in-window bars that almost fired.')
        return

    trading_days = len({e['date'] for e in events})
    adx_low    = [e for e in events if e['reason'] == 'ADX_LOW']
    stale      = [e for e in events if e['reason'] == 'STALE_CROSS']

    # ── Dedup: one event per (date, time, reason, direction) ─────────────────
    # Bot polls every ~30s but bars are 5-min, so the same bar may appear
    # multiple times if the JSONL recorder fired more than once in a session.
    def dedup(ev_list: list[dict]) -> list[dict]:
        seen: set[tuple] = set()
        out: list[dict] = []
        for e in ev_list:
            key = (e['date'], e['time'], e['reason'], e['direction'])
            if key not in seen:
                seen.add(key)
                out.append(e)
        return out

    adx_low = dedup(adx_low)
    stale   = dedup(stale)

    print(f'\n{"="*60}')
    print(f'  {instrument} Near-Miss Analysis')
    print(f'  Trading days with data: {trading_days}  |  Total events: {len(adx_low)+len(stale)}')
    print(f'{"="*60}')

    # ── ADX_LOW analysis ─────────────────────────────────────────────────────
    print(f'\n  ADX_LOW events: {len(adx_low)}')
    if adx_low:
        gaps = [e['adx_gap'] for e in adx_low]
        print(f'    ADX gap  avg={mean(gaps):.1f}  median={median(gaps):.1f}  '
              f'min={min(gaps):.1f}  max={max(gaps):.1f}'
              + (f'  stdev={stdev(gaps):.1f}' if len(gaps) >= 2 else ''))

        # Gap buckets
        buckets: dict[str, int] = defaultdict(int)
        for g in gaps:
            b = _bucket(g, [1, 2, 3, 4, 5])
            buckets[b] += 1
        print(f'    Gap dist: {_counter_summary(dict(buckets), len(gaps))}')

        # Direction
        dirs: dict[str, int] = defaultdict(int)
        for e in adx_low:
            dirs[e['direction']] += 1
        print(f'    Direction: {_counter_summary(dict(dirs), len(adx_low))}')

        # By hour
        hours: dict[str, int] = defaultdict(int)
        for e in adx_low:
            hours[_hour(e['time'])] += 1
        print(f'    By hour:    {_counter_summary(dict(hours), len(adx_low))}')

        # By weekday
        wdays: dict[str, int] = defaultdict(int)
        for e in adx_low:
            wdays[_weekday(e['date'])] += 1
        print(f'    By weekday: {_counter_summary(dict(wdays), len(adx_low))}')

        # VWAP ok rate
        vwap_ok  = sum(1 for e in adx_low if e.get('vwap_ok'))
        vwap_tot = sum(1 for e in adx_low if e.get('vwap') is not None)
        if vwap_tot:
            print(f'    VWAP ok:    {vwap_ok}/{vwap_tot} = {vwap_ok*100//vwap_tot}%')

        # HTF alignment
        htf_aligned = sum(1 for e in adx_low if (
            (e['direction'] == 'CALL' and e.get('htf_bull')) or
            (e['direction'] == 'PUT'  and e.get('htf_bear'))
        ))
        print(f'    HTF aligned (15m agrees): {htf_aligned}/{len(adx_low)} = '
              f'{htf_aligned*100//len(adx_low)}%')

        # Threshold proximity — what would have fired at lower threshold?
        for test_thr in [23, 22, 20]:
            would_fire = sum(1 for e in adx_low if e['adx_actual'] >= test_thr)
            if would_fire:
                print(f'    If threshold lowered to {test_thr}: +{would_fire} trades would have fired '
                      f'({would_fire*100//len(adx_low)}% of ADX_LOW misses)')

    # ── STALE_CROSS analysis ──────────────────────────────────────────────────
    print(f'\n  STALE_CROSS events: {len(stale)}')
    if stale:
        ages = [e['cross_bars_ago'] for e in stale if e.get('cross_bars_ago')]
        if ages:
            print(f'    Cross age  avg={mean(ages):.1f} bars  median={median(ages):.1f}  '
                  f'min={min(ages)}  max={max(ages)}')
            age_buckets: dict[str, int] = defaultdict(int)
            for a in ages:
                lbl = '4-5' if a <= 5 else ('6-7' if a <= 7 else '8-10')
                age_buckets[lbl] += 1
            print(f'    Age dist: {_counter_summary(dict(age_buckets), len(ages))}')

        dirs2: dict[str, int] = defaultdict(int)
        for e in stale:
            dirs2[e['direction']] += 1
        print(f'    Direction: {_counter_summary(dict(dirs2), len(stale))}')

        hours2: dict[str, int] = defaultdict(int)
        for e in stale:
            hours2[_hour(e['time'])] += 1
        print(f'    By hour:    {_counter_summary(dict(hours2), len(stale))}')

        wdays2: dict[str, int] = defaultdict(int)
        for e in stale:
            wdays2[_weekday(e['date'])] += 1
        print(f'    By weekday: {_counter_summary(dict(wdays2), len(stale))}')

        # How many would fire if lookback extended to 4 bars?
        lb4 = sum(1 for e in stale if e.get('cross_bars_ago') == 4)
        if lb4:
            print(f'    If lookback extended to 4 bars: +{lb4} trades would fire '
                  f'({lb4*100//len(stale)}% of STALE_CROSS misses) — backtest first!')

    # ── Outcome analysis (reversal vs continuation) ──────────────────────────
    if outcomes is not None:
        analyze_outcomes(instrument, events, outcomes)

    # ── Recommendation ───────────────────────────────────────────────────────
    print(f'\n  → RECOMMENDATION for {instrument}:')
    if len(adx_low) + len(stale) < 20:
        print(f'    Insufficient data ({len(adx_low)+len(stale)} events across {trading_days} days).')
        print('    Wait until 30+ trading days before drawing conclusions.')
    else:
        gaps = [e['adx_gap'] for e in adx_low] if adx_low else []
        med = median(gaps) if gaps else 0
        if med >= 3.5:
            print(f'    ADX median gap is {med:.1f}pts — threshold may be consistently too high.')
            print('    → Consider backtesting a 2pt reduction in ADX threshold.')
        elif med >= 2.0:
            print(f'    ADX median gap is {med:.1f}pts — borderline. Monitor for another 30 days.')
        else:
            print(f'    ADX median gap is {med:.1f}pts — threshold appears correctly calibrated.')
            print('    → No ADX threshold change recommended at this time.')

        if stale:
            ages = [e['cross_bars_ago'] for e in stale if e.get('cross_bars_ago')]
            lb4_count = sum(1 for a in ages if a == 4) if ages else 0
            if lb4_count >= 5:
                print(f'    Stale-cross: {lb4_count} misses at exactly 4 bars.')
                print('    → Consider backtesting EMA_CROSSOVER_LOOKBACK=4 (currently 3).')
            else:
                print('    → No lookback change recommended at this time.')


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]

    # Parse --days N
    days_limit: int | None = None
    if '--days' in args:
        idx = args.index('--days')
        try:
            days_limit = int(args[idx + 1])
            args = [a for i, a in enumerate(args) if i not in (idx, idx + 1)]
        except (IndexError, ValueError):
            print('Usage: python near_miss_analyzer.py [INSTRUMENT] [--days N]')
            sys.exit(1)

    # Which instruments to analyse
    instruments = [a.upper() for a in args if a.upper() in _INSTRUMENTS]
    if not instruments:
        instruments = _INSTRUMENTS

    tag = f'(last {days_limit} days)' if days_limit else '(all available data)'
    print(f'\nNear-Miss Analyzer  {tag}')
    print(f'Log directory: {_LOG_DIR}')

    for inst in instruments:
        events   = _load_events(inst, days=days_limit)
        outcomes = _load_outcomes(inst, days=days_limit)
        analyze(inst, events, outcomes=outcomes)

    print('\n' + '='*60)
    print('Run again after more data accumulates for stronger conclusions.')
    print('Only change thresholds after running bot.py backtest to confirm impact.')


if __name__ == '__main__':
    main()
