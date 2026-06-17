"""
morning_briefing.py — Pre-Market Intelligence Brief
Runs at 09:10 IST Mon–Fri via fno_t_bot_morning.timer

Answers "what should I know before the market opens today?" covering:
  1. Regime context — what kind of market have we had lately?
  2. Day-of-week intelligence — historical WR for today in this regime
  3. OI zone key levels — where PUT/CALL walls sit vs current price
  4. Gap estimate — yesterday close vs today's likely open
  5. Trading posture — AGGRESSIVE / NORMAL / CAUTIOUS recommendation
  6. Quick risk flags — anything that should make us extra cautious today

Output:
  • Printed to stdout → journalctl (fno_t_bot_morning.service)
  • Written to logs/morning_brief_YYYYMMDD.txt (persistent reference)

Usage:
  python morning_briefing.py              # both NF + BNF
  python morning_briefing.py NIFTY        # single instrument
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, date
from pathlib import Path

import pytz

_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(_DIR)

import config
from market_regime import RegimeAnalyzer, _load_recent_csvs

IST = pytz.timezone('Asia/Kolkata')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [MORNING] %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('morning_briefing')

LOG_DIR  = os.path.join(_DIR, 'logs')
OI_DIR   = os.path.join(_DIR, '..', 'data', 'oi_zones')       # local dev
OI_DIR_EC2 = '/opt/trading_bot/data/oi_zones'                  # EC2 path
JSONL_PATH = os.path.join(LOG_DIR, 'market_learnings.jsonl')

os.makedirs(LOG_DIR, exist_ok=True)


# ── OI Zone reader ────────────────────────────────────────────────────────────

def load_oi_zones(instrument: str) -> dict | None:
    """Load latest OI zone JSON for an instrument."""
    # Try latest_*.json first (EOD script always writes this)
    for oi_dir in [OI_DIR_EC2, OI_DIR]:
        if not os.path.isdir(oi_dir):
            continue
        latest = os.path.join(oi_dir, f'latest_{instrument}.json')
        if os.path.exists(latest):
            try:
                with open(latest, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        # Fallback: most recent dated file
        files = sorted(Path(oi_dir).glob(f'*_{instrument}.json'))
        if files:
            try:
                with open(files[-1], 'r') as f:
                    return json.load(f)
            except Exception:
                pass
    return None


def format_oi_summary(oi: dict, current_price: float) -> list[str]:
    """Format OI zone levels into brief lines."""
    lines = []
    if not oi:
        return ['  OI zones  : unavailable (run oi_zones_eod.py after 15:30)']

    pcr      = oi.get('pcr', '?')
    pcr_bias = oi.get('pcr_bias', '?')
    max_pain = oi.get('max_pain', '?')
    lines.append(f"  OI        : PCR={pcr} ({pcr_bias}) | MaxPain={max_pain}")

    # Nearest resistance (CALL walls — overhead)
    resistance = oi.get('resistance', [])
    nearby_res = [r for r in resistance
                  if isinstance(r.get('dist_pct'), (int, float)) and r['dist_pct'] < 3.0]
    if nearby_res:
        r = nearby_res[0]
        lines.append(f"  Resistance: {r['strike']} ({r['strength']}) dist={r.get('dist_pct', '?'):.1f}%")

    # Nearest support (PUT walls — below)
    support = oi.get('support', [])
    nearby_sup = [s for s in support
                  if isinstance(s.get('dist_pct'), (int, float)) and s['dist_pct'] < 3.0]
    if nearby_sup:
        s = nearby_sup[0]
        lines.append(f"  Support   : {s['strike']} ({s['strength']}) dist={s.get('dist_pct', '?'):.1f}%")

    # MaxPain distance
    if isinstance(max_pain, (int, float)) and current_price > 0:
        mp_dist = (current_price - max_pain) / current_price * 100
        lines.append(f"  MaxPain Δ : {mp_dist:+.2f}% from current {current_price:.0f}")

    return lines


# ── Gap estimate ──────────────────────────────────────────────────────────────

def get_yesterday_close(instrument: str) -> tuple[float, date] | None:
    """Get yesterday's closing price from 5-min CSV data."""
    df = _load_recent_csvs(instrument, days_back=5)
    if df is None or df.empty:
        return None

    today = datetime.now(IST).date()
    past_days = sorted(set(df.index.date))
    past_days = [d for d in past_days if d < today]
    if not past_days:
        return None

    yesterday = past_days[-1]
    df_yest   = df[df.index.date == yesterday]
    if df_yest.empty:
        return None

    return float(df_yest['Close'].iloc[-1]), yesterday


# ── DOW intelligence from JSONL ───────────────────────────────────────────────

def dow_regime_stats(instrument: str, today_dow: str, regime: str) -> dict:
    """
    Historical win-rate for today's day-of-week in the current regime.
    Returns {'n': int, 'wr': float, 'avg_pnl': float}
    """
    if not os.path.exists(JSONL_PATH):
        return {}

    matches = []
    try:
        with open(JSONL_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if (rec.get('instrument') == instrument and
                            rec.get('day_of_week') == today_dow and
                            rec.get('market_regime') == regime and
                            rec.get('path_a_fired')):
                        matches.append(rec)
                except json.JSONDecodeError:
                    pass
    except OSError:
        return {}

    if not matches:
        return {}

    wins   = sum(1 for r in matches if (r.get('total_pnl_net') or 0) > 0)
    avg_pnl = sum(r.get('total_pnl_net', 0) for r in matches) / len(matches)
    return {'n': len(matches), 'wr': round(wins / len(matches) * 100, 1),
            'avg_pnl': round(avg_pnl, 0)}


# ── Risk flags ────────────────────────────────────────────────────────────────

def compute_risk_flags(snap, oi: dict | None, today_dow: str) -> list[str]:
    """Return list of risk flag strings worth highlighting."""
    flags = []

    # Regime flags
    if snap.regime == 'HIGH_VOL_CHOPPY':
        flags.append('⚠ HIGH_VOL_CHOPPY — options overpriced, stops widen; reduce lot size')
    if snap.regime == 'CHOPPY' and snap.confidence in ('HIGH', 'MEDIUM'):
        flags.append('⚠ Persistent CHOP — breakouts frequently fail; raise ADX bar mentally')
    if snap.streak >= 4 and snap.regime.startswith('TRENDING'):
        flags.append(f'ℹ Trend aging ({snap.streak}d streak) — mean-reversion risk rising')

    # Day flags
    if today_dow == 'Thu':
        flags.append('ℹ Thursday — CALL suppressed (PATH_A_NO_CALL_DAYS). PUT only.')
    if today_dow in ('Mon', 'Tue'):
        flags.append(f'ℹ {today_dow} — elevated ADX bar applies ({today_dow})')

    # OI flags
    if oi:
        pcr = oi.get('pcr')
        if isinstance(pcr, (int, float)):
            if pcr < 0.75:
                flags.append(f'⚠ PCR={pcr:.2f} (BEARISH extreme) — OI Phase 2 would suppress CALL')
            elif pcr > 1.30:
                flags.append(f'⚠ PCR={pcr:.2f} (BULLISH extreme) — OI Phase 2 would suppress PUT')
            elif pcr < 0.85:
                flags.append(f'ℹ PCR={pcr:.2f} (mild bearish) — CALL entries face headwind')

    return flags


# ── Per-instrument brief ──────────────────────────────────────────────────────

def brief_instrument(instrument: str, today: date) -> list[str]:
    """Generate the morning brief for one instrument."""
    lines = [f'\n{"─"*56}']
    lines.append(f'  {instrument} — {today.strftime("%A %d %b %Y")}')
    lines.append(f'{"─"*56}')

    today_dow = today.strftime('%a')

    # Regime
    analyzer = RegimeAnalyzer(instrument, lookback=12)
    snap     = analyzer.get_snapshot(snap_lookback=5)

    lines.append(f'  Regime    : {snap.regime} | {snap.confidence} conf | '
                 f'{snap.streak}d streak | bias={snap.bias}')
    lines.append(f'  Posture   : {snap.posture}')

    # Recent day pattern (last 7 days)
    if snap.day_profiles:
        recent = snap.day_profiles[-7:]
        pattern = '  History   : ' + '  '.join(
            f"{p.date.strftime('%d')}/{p.regime[:5]}({p.change_pct:+.1f}%)"
            for p in recent
        )
        lines.append(pattern)

    # DOW + regime intelligence from JSONL
    dow_stats = dow_regime_stats(instrument, today_dow, snap.regime)
    if dow_stats:
        lines.append(
            f'  {today_dow}+{snap.regime[:5]}: '
            f'{dow_stats["wr"]:.0f}% WR ({dow_stats["n"]} trades) '
            f'avg_pnl=₹{dow_stats["avg_pnl"]:+,.0f}'
        )
    else:
        lines.append(f'  {today_dow}+{snap.regime[:5]}: no historical data yet')

    # Yesterday close / gap estimate
    yest_result = get_yesterday_close(instrument)
    if yest_result:
        yest_close, yest_date = yest_result
        lines.append(f'  Yest Close: {yest_close:.1f} ({yest_date})')
        lines.append(f'  Gap detect: will be visible at 09:15 open')

    # OI zones
    oi = load_oi_zones(instrument)
    if oi:
        oi_date = oi.get('date', '?')
        lines.append(f'  OI date   : {oi_date}')
        if yest_result:
            for oi_line in format_oi_summary(oi, yest_result[0]):
                lines.append(oi_line)

    # Risk flags
    flags = compute_risk_flags(snap, oi, today_dow)
    if flags:
        lines.append('')
        for flag in flags:
            lines.append(f'  {flag}')

    # Bottom line
    lines.append('')
    if snap.posture == 'AGGRESSIVE':
        bottom = f'  → FULL SIZE. {snap.regime} confirmed. Take clean ORB breaks.'
    elif snap.posture == 'CAUTIOUS':
        bottom = f'  → 1 LOT ONLY. {snap.regime} — borderline setups get the pass.'
    else:
        bottom = f'  → NORMAL. Follow config. {snap.regime} with {snap.confidence.lower()} confidence.'

    # Bias note
    if snap.bias != 'NEUTRAL':
        bottom += f' Lean {snap.bias}.'

    lines.append(bottom)

    return lines


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    today = datetime.now(IST).date()
    instruments = sys.argv[1:] if len(sys.argv) > 1 else ['NIFTY', 'BANKNIFTY']

    # Skip weekends
    if today.weekday() >= 5:
        logger.info(f'Weekend ({today.strftime("%a")}) — no briefing needed.')
        return

    header = [
        f'\n{"═"*56}',
        f'  FnO_T_Bot Morning Brief — {today.strftime("%A %d %b %Y")}',
        f'  Generated: {datetime.now(IST).strftime("%H:%M IST")}',
        f'{"═"*56}',
    ]

    all_lines = header.copy()
    for inst in instruments:
        if inst not in config.INSTRUMENTS:
            logger.error(f'Unknown instrument: {inst}')
            continue
        try:
            all_lines.extend(brief_instrument(inst, today))
        except Exception as e:
            all_lines.append(f'  {inst}: ERROR — {e}')
            logger.error(f'{inst} brief failed: {e}', exc_info=True)

    all_lines.append(f'\n{"═"*56}\n')
    output = '\n'.join(all_lines)

    # Print to stdout → journalctl
    print(output, flush=True)

    # Write to file for persistent reference
    brief_file = os.path.join(LOG_DIR, f'morning_brief_{today.strftime("%Y%m%d")}.txt')
    with open(brief_file, 'w', encoding='utf-8') as f:
        f.write(output)
    logger.info(f'Brief written to {brief_file}')


if __name__ == '__main__':
    main()
