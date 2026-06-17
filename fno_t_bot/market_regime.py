"""
market_regime.py — Real-time Market Regime Detector

Primary data source: 5-min index CSV files (available from day 1).
Enrichment source: market_learnings.jsonl (grows with daily_debrief.py).

Regime types:
  TRENDING_BULL  — ADX>25 during main session + index closed >0.3% above open
  TRENDING_BEAR  — ADX>25 during main session + index closed >0.3% below open
  HIGH_VOL_CHOPPY — large range (>1.5%) but close ≈ open (|change|<0.3%)
  CHOPPY         — low ADX (<25) or small range with no clear direction
  UNKNOWN        — insufficient data

Confidence levels:
  HIGH   — ≥4 of last 5 days in same regime
  MEDIUM — ≥3 of last 5 days
  LOW    — <3 of last 5 days agree

Posture (trading suggestion):
  AGGRESSIVE — trending + HIGH confidence → full sizing allowed
  NORMAL     — moderate conditions
  CAUTIOUS   — choppy / HIGH_VOL / low confidence → reduce sizing, skip borderline

Import pattern:
    from market_regime import RegimeAnalyzer
    snap = RegimeAnalyzer('NIFTY').get_snapshot()
    logger.info(snap.brief)
    if snap.posture == 'CAUTIOUS':
        lots = 1  # override strength score
"""

from __future__ import annotations

import os
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, time as dtime
from pathlib import Path
from typing import Optional

import pytz

logger = logging.getLogger('market_regime')

IST = pytz.timezone('Asia/Kolkata')

_DIR = os.path.dirname(os.path.abspath(__file__))

# Instrument → 5-min data directory
_DATA_DIRS = {
    'NIFTY'    : os.path.join(_DIR, '..', 'data', 'nifty_5min'),
    'BANKNIFTY': os.path.join(_DIR, '..', 'data', 'banknifty_5min'),
    'SENSEX'   : os.path.join(_DIR, '..', 'data', 'sensex_5min'),
}

_JSONL_PATH = os.path.join(_DIR, 'logs', 'market_learnings.jsonl')


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class DayProfile:
    """Single-day market profile computed from 5-min data."""
    date       : date
    regime     : str   # TRENDING_BULL / TRENDING_BEAR / CHOPPY / HIGH_VOL_CHOPPY
    direction  : str   # BULL / BEAR / NEUTRAL
    adx_avg    : float # average ADX 11:00–14:30
    adx_peak   : float # peak ADX during the day
    range_pct  : float # (high-low) / open × 100
    change_pct : float # (close-open) / open × 100
    or_width   : float # opening range width % (9:15–9:25)
    gap_pct    : float # today open vs yesterday close %


@dataclass
class RegimeSnapshot:
    """Current regime assessment from last N trading days."""
    regime      : str   # current regime (most common in lookback window)
    confidence  : str   # HIGH / MEDIUM / LOW
    streak      : int   # consecutive days in current regime
    bias        : str   # CALL / PUT / NEUTRAL (trade direction preference)
    posture     : str   # AGGRESSIVE / NORMAL / CAUTIOUS
    brief       : str   # one-line human-readable summary
    lookback    : int   # how many days were analyzed
    day_profiles: list[DayProfile] = field(default_factory=list)
    jsonl_stats : dict = field(default_factory=dict)  # from market_learnings.jsonl


# ── 5-min CSV loader ───────────────────────────────────────────────────────────

def _load_recent_csvs(instrument: str, days_back: int = 15) -> 'pd.DataFrame | None':
    """Load recent 5-min bars for an instrument."""
    try:
        import pandas as pd
    except ImportError:
        return None

    data_dir = _DATA_DIRS.get(instrument)
    if not data_dir or not os.path.isdir(data_dir):
        return None

    csvs = sorted(Path(data_dir).glob('*.csv'))
    if not csvs:
        return None

    needed = csvs[-max(days_back * 2, 20):]  # grab extra for indicator warm-up
    frames = []
    for p in needed:
        try:
            # Support both 'Datetime' (local dev) and 'ts' (EC2 / Fyers format)
            try:
                df = pd.read_csv(p, parse_dates=['Datetime'], index_col='Datetime')
            except (KeyError, ValueError):
                df = pd.read_csv(p, parse_dates=['ts'], index_col='ts')
            frames.append(df)
        except Exception:
            pass
    if not frames:
        return None

    df = pd.concat(frames).sort_index()
    return df[~df.index.duplicated(keep='first')]


def _compute_adx_series(df: 'pd.DataFrame', period: int = 14) -> 'pd.Series':
    """Wilder's ADX."""
    import pandas as pd
    import numpy as np

    high, low, close = df['High'], df['Low'], df['Close']
    tr   = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr  = tr.ewm(alpha=1/period, min_periods=period).mean()
    up   = high.diff()
    dn   = -low.diff()
    dip  = up.where(up > dn, 0).clip(lower=0)
    dim  = dn.where(dn > up, 0).clip(lower=0)
    dip_s = dip.ewm(alpha=1/period, min_periods=period).mean() / atr.replace(0, float('nan')) * 100
    dim_s = dim.ewm(alpha=1/period, min_periods=period).mean() / atr.replace(0, float('nan')) * 100
    dx    = (dip_s - dim_s).abs() / (dip_s + dim_s).replace(0, float('nan')) * 100
    return dx.ewm(alpha=1/period, min_periods=period).mean()


def _analyze_day(df_day: 'pd.DataFrame', prev_close: float) -> Optional[DayProfile]:
    """Compute DayProfile for a single trading day's bars."""
    if len(df_day) < 10:
        return None

    import pandas as pd
    import numpy as np

    day = df_day.index[0].date()

    # Compute ADX
    adx = _compute_adx_series(df_day)

    # Main session (11:00–14:30) for regime classification
    main = df_day[df_day.index.time >= dtime(11, 0)]
    adx_main = adx[main.index] if len(main) > 0 else adx

    adx_avg  = float(adx_main.mean())  if len(adx_main) > 0 else 0.0
    adx_peak = float(adx.max())

    # Price levels
    open_px  = float(df_day['Open'].iloc[0])
    close_px = float(df_day['Close'].iloc[-1])
    high_px  = float(df_day['High'].max())
    low_px   = float(df_day['Low'].min())

    if open_px <= 0:
        return None

    change_pct = (close_px - open_px) / open_px * 100
    range_pct  = (high_px - low_px)   / open_px * 100
    gap_pct    = (open_px - prev_close) / prev_close * 100 if prev_close > 0 else 0.0

    # Opening Range width (9:15–9:25)
    or_bars   = df_day[df_day.index.time < dtime(9, 30)]
    or_width  = ((float(or_bars['High'].max()) - float(or_bars['Low'].min())) / open_px * 100
                 if len(or_bars) >= 2 else 0.0)

    # Direction
    if change_pct > 0.3:
        direction = 'BULL'
    elif change_pct < -0.3:
        direction = 'BEAR'
    else:
        direction = 'NEUTRAL'

    # Regime
    if adx_avg >= 25:
        if direction == 'BULL':
            regime = 'TRENDING_BULL'
        elif direction == 'BEAR':
            regime = 'TRENDING_BEAR'
        else:
            regime = 'CHOPPY'  # strong ADX but indecisive direction
    elif range_pct >= 1.5 and abs(change_pct) < 0.3:
        regime = 'HIGH_VOL_CHOPPY'
    else:
        regime = 'CHOPPY'

    return DayProfile(
        date=day, regime=regime, direction=direction,
        adx_avg=round(adx_avg, 1), adx_peak=round(adx_peak, 1),
        range_pct=round(range_pct, 3), change_pct=round(change_pct, 3),
        or_width=round(or_width, 3), gap_pct=round(gap_pct, 3),
    )


def _build_day_profiles(instrument: str, lookback: int = 15) -> list[DayProfile]:
    """Return list of DayProfile for the last `lookback` complete trading days."""
    df = _load_recent_csvs(instrument, days_back=lookback + 3)
    if df is None or df.empty:
        return []

    today = datetime.now(IST).date()
    all_dates = sorted(set(df.index.date))
    trading_days = [d for d in all_dates if d < today][-lookback:]

    profiles = []
    for i, day in enumerate(trading_days):
        df_day   = df[df.index.date == day]
        prev_idx = i - 1
        if prev_idx >= 0:
            prev_day   = trading_days[prev_idx]
            prev_bars  = df[df.index.date == prev_day]
            prev_close = float(prev_bars['Close'].iloc[-1]) if len(prev_bars) > 0 else 0.0
        else:
            prev_close = 0.0

        profile = _analyze_day(df_day, prev_close)
        if profile:
            profiles.append(profile)

    return profiles


# ── JSONL enrichment ──────────────────────────────────────────────────────────

def _load_jsonl_stats(instrument: str, lookback: int = 30) -> dict:
    """
    Compute win-rate statistics from market_learnings.jsonl broken down by regime.
    Returns dict: regime → {wr, n, avg_pnl}
    """
    if not os.path.exists(_JSONL_PATH):
        return {}

    cutoff = (datetime.now(IST).date() - timedelta(days=lookback * 2)).isoformat()
    records = []
    try:
        with open(_JSONL_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if (rec.get('instrument') == instrument and
                            rec.get('date', '') >= cutoff and
                            rec.get('path_a_fired')):
                        records.append(rec)
                except json.JSONDecodeError:
                    pass
    except OSError:
        return {}

    if not records:
        return {}

    from collections import defaultdict
    regime_buckets: dict[str, list] = defaultdict(list)
    for r in records:
        regime_buckets[r.get('market_regime', 'UNKNOWN')].append(r)

    stats = {}
    for regime, recs in regime_buckets.items():
        wins   = sum(1 for r in recs if (r.get('total_pnl_net') or 0) > 0)
        avg_pnl = sum(r.get('total_pnl_net', 0) for r in recs) / len(recs)
        stats[regime] = {
            'n'      : len(recs),
            'wr'     : round(wins / len(recs) * 100, 1),
            'avg_pnl': round(avg_pnl, 0),
        }

    return stats


# ── Regime snapshot builder ────────────────────────────────────────────────────

def _classify_snapshot(profiles: list[DayProfile], lookback: int = 5) -> RegimeSnapshot:
    """Build RegimeSnapshot from a list of DayProfiles."""
    if not profiles:
        return RegimeSnapshot(
            regime='UNKNOWN', confidence='LOW', streak=0,
            bias='NEUTRAL', posture='NORMAL',
            brief='UNKNOWN regime — insufficient data',
            lookback=0,
        )

    recent = profiles[-lookback:]
    regime_counts: dict[str, int] = {}
    for p in recent:
        regime_counts[p.regime] = regime_counts.get(p.regime, 0) + 1

    dominant = max(regime_counts, key=regime_counts.get)
    dominant_count = regime_counts[dominant]
    n = len(recent)

    if dominant_count >= max(n - 1, 1) and n >= 4:
        confidence = 'HIGH'
    elif dominant_count >= max(n // 2 + 1, 2):
        confidence = 'MEDIUM'
    else:
        confidence = 'LOW'

    # Streak: consecutive days with the same regime (from most recent)
    streak = 0
    for p in reversed(profiles):
        if p.regime == dominant:
            streak += 1
        else:
            break

    # Bias
    bull_days = sum(1 for p in recent if p.direction == 'BULL')
    bear_days = sum(1 for p in recent if p.direction == 'BEAR')
    if bull_days > bear_days and bull_days >= 3:
        bias = 'CALL'
    elif bear_days > bull_days and bear_days >= 3:
        bias = 'PUT'
    else:
        bias = 'NEUTRAL'

    # Posture
    if dominant in ('TRENDING_BULL', 'TRENDING_BEAR') and confidence == 'HIGH':
        posture = 'AGGRESSIVE'
    elif dominant in ('CHOPPY', 'HIGH_VOL_CHOPPY') or confidence == 'LOW':
        posture = 'CAUTIOUS'
    else:
        posture = 'NORMAL'

    # Brief summary
    avg_adx = sum(p.adx_avg for p in recent) / len(recent)
    brief = (
        f"{dominant} | conf={confidence} | streak={streak}d | "
        f"bias={bias} | posture={posture} | "
        f"ADX_avg={avg_adx:.0f}"
    )

    return RegimeSnapshot(
        regime=dominant, confidence=confidence, streak=streak,
        bias=bias, posture=posture, brief=brief,
        lookback=n, day_profiles=list(recent),
    )


# ── Public API ────────────────────────────────────────────────────────────────

class RegimeAnalyzer:
    """
    Analyzes recent market conditions and returns a RegimeSnapshot.

    Usage:
        snap = RegimeAnalyzer('NIFTY').get_snapshot()
        print(snap.brief)
        # → "TRENDING_BEAR | conf=HIGH | streak=3d | bias=PUT | posture=CAUTIOUS | ADX_avg=32"

    The analyzer uses 5-min CSV data for regime classification (available from
    day 1) and enriches with JSONL win-rate data once it accumulates.
    """

    def __init__(self, instrument: str = 'NIFTY', lookback: int = 10):
        self.instrument = instrument
        self.lookback   = lookback

    def get_snapshot(self, snap_lookback: int = 5) -> RegimeSnapshot:
        """
        Build and return a RegimeSnapshot.

        snap_lookback: how many recent days to use for regime classification
                       (default 5 = 1 trading week). lookback controls how many
                       days of data to load for context.
        """
        profiles = _build_day_profiles(self.instrument, self.lookback)
        snap     = _classify_snapshot(profiles, snap_lookback)
        snap.jsonl_stats = _load_jsonl_stats(self.instrument, self.lookback * 2)
        return snap

    def format_morning_context(self) -> str:
        """Multi-line formatted context for morning briefing."""
        snap = self.get_snapshot()
        lines = [
            f"  Regime    : {snap.regime} ({snap.confidence} confidence, {snap.streak}-day streak)",
            f"  Bias      : {snap.bias}",
            f"  Posture   : {snap.posture}",
        ]

        if snap.day_profiles:
            recent = snap.day_profiles[-5:]
            day_line = '  Recent    : ' + '  '.join(
                f"{p.date.strftime('%d')}/{p.regime[:4]}({p.direction[0]})"
                for p in recent
            )
            lines.append(day_line)

        if snap.jsonl_stats:
            lines.append("  JSONL WR  : " + " | ".join(
                f"{r}→{v['wr']:.0f}%({v['n']})"
                for r, v in snap.jsonl_stats.items()
            ))

        return '\n'.join(lines)


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    instruments = sys.argv[1:] or ['NIFTY', 'BANKNIFTY']
    for inst in instruments:
        print(f'\n=== {inst} Regime ===')
        try:
            snap = RegimeAnalyzer(inst).get_snapshot()
            print(f'  {snap.brief}')
            if snap.day_profiles:
                for p in snap.day_profiles[-7:]:
                    print(f'    {p.date}  {p.regime:<18} ADX={p.adx_avg:>5.1f}'
                          f'  chg={p.change_pct:>+5.2f}%  rng={p.range_pct:.2f}%')
            if snap.jsonl_stats:
                print(f'  JSONL stats (by regime):')
                for r, v in snap.jsonl_stats.items():
                    print(f'    {r:<18} n={v["n"]}  WR={v["wr"]}%  avg_pnl=₹{v["avg_pnl"]:+,.0f}')
        except Exception as e:
            print(f'  ERROR: {e}')
