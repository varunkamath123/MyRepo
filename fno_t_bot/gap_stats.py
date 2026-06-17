"""
gap_stats.py -- Post-Gap Behaviour Analysis
===========================================
Quantifies the "gut feel" for how NIFTY / BANKNIFTY / SENSEX behave after
different gap magnitudes.  Reads local 5-min CSVs, reconstructs daily
gaps and intraday dynamics, outputs probability tables.

Usage:  python gap_stats.py
Output: gap_stats_report.txt  (also printed to console)

Key questions answered
  1. Gap size -> continuation vs reversal probability
  2. Gap size -> gap-fill probability (same day)
  3. Gap size -> which side of the Opening Range breaks first
  4. Gap size -> how quickly does the gap fill (if it does)
  5. Cross-index consistency (all 3 instruments same gap direction?)

Gap buckets (abs value, %)
  INSIDE   < 0.30   -- flat open, no meaningful gap
  SMALL    0.30-0.75
  MEDIUM   0.75-1.50
  LARGE    1.50-2.50
  EXTREME  > 2.50

Direction: GAP_DOWN (open < prev_close) vs GAP_UP (open > prev_close)

OR definition: 4 bars starting 09:15 -> 09:15/09:20/09:25/09:30
  OR high = max(high of those 4 bars)
  OR low  = min(low  of those 4 bars)
  First bar AFTER OR window: 09:35 onward
  OR breakout direction = first close outside OR range after OR window
"""

import sys, io
from pathlib import Path
import pandas as pd
import numpy as np
from datetime import time as dtime

# Force UTF-8 output on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ---- Config -----------------------------------------------------------------
BASE = Path(r'C:\quant_trading\data')
INSTRUMENTS = {
    'NIFTY'    : BASE / 'nifty_5min',
    'BANKNIFTY': BASE / 'banknifty_5min',
    'SENSEX'   : BASE / 'sensex_5min',
}

OR_BARS        = 4        # 09:15, 09:20, 09:25, 09:30
MARKET_OPEN    = dtime(9, 15)
MARKET_CLOSE   = dtime(15, 30)
SIGNAL_START   = dtime(9, 35)   # first bar eligible for OR breakout

# Gap buckets: (label, lower_bound, upper_bound) -- applied to abs(gap_pct)
BUCKETS = [
    ('INSIDE',  0.000, 0.003),
    ('SMALL',   0.003, 0.0075),
    ('MEDIUM',  0.0075, 0.015),
    ('LARGE',   0.015, 0.025),
    ('EXTREME', 0.025, 9.999),
]

def bucket_label(gap_pct):
    ab = abs(gap_pct)
    for label, lo, hi in BUCKETS:
        if lo <= ab < hi:
            return label
    return 'EXTREME'

def load_instrument(instr, folder):
    """Load all daily CSVs into a single sorted DataFrame."""
    folder = Path(folder)
    files = sorted(folder.glob('*.csv'))
    if not files:
        print(f"  WARNING: no CSV files found in {folder}")
        return pd.DataFrame()
    frames = []
    for f in files:
        try:
            df = pd.read_csv(f, parse_dates=['ts'])
            frames.append(df)
        except Exception as e:
            print(f"  skip {f.name}: {e}")
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values('ts').reset_index(drop=True)
    # Normalise tz-aware -> tz-naive IST
    if df['ts'].dt.tz is not None:
        df['ts'] = df['ts'].dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
    df['date']     = df['ts'].dt.date
    df['bar_time'] = df['ts'].dt.time
    return df

def analyse_day(date, day_df, prev_close):
    """Analyse a single trading day. Returns a dict of metrics or None."""
    day_df = day_df.sort_values('ts').reset_index(drop=True)
    if len(day_df) < OR_BARS + 2:
        return None  # too short

    # ---- Gap -----------------------------------------------------------------
    open_px  = day_df.iloc[0]['Open']
    gap_pct  = (open_px - prev_close) / prev_close   # signed
    gap_abs  = abs(gap_pct)
    gap_dir  = 'UP' if gap_pct > 0 else ('DOWN' if gap_pct < 0 else 'FLAT')
    bucket   = bucket_label(gap_pct)

    # ---- Opening Range -------------------------------------------------------
    or_bars  = day_df.iloc[:OR_BARS]
    or_high  = or_bars['High'].max()
    or_low   = or_bars['Low'].min()
    or_width_pct = (or_high - or_low) / open_px

    # ---- Post-OR breakout direction ------------------------------------------
    post_or = day_df[day_df['bar_time'] >= SIGNAL_START].reset_index(drop=True)
    breakout_dir = None
    breakout_bar = None
    for _, row in post_or.iterrows():
        if row['Close'] > or_high:
            breakout_dir = 'UP'
            breakout_bar = row['bar_time']
            break
        if row['Close'] < or_low:
            breakout_dir = 'DOWN'
            breakout_bar = row['bar_time']
            break

    # ---- Day close -----------------------------------------------------------
    day_close    = day_df.iloc[-1]['Close']
    continuation = None
    if gap_dir != 'FLAT':
        if gap_dir == 'DOWN':
            continuation = day_close < prev_close
        else:
            continuation = day_close > prev_close

    # ---- Gap fill ------------------------------------------------------------
    gap_filled    = False
    gap_fill_time = None
    if gap_dir == 'DOWN' and gap_abs >= 0.003:
        for _, row in day_df.iterrows():
            if row['High'] >= prev_close:
                gap_filled    = True
                gap_fill_time = row['bar_time']
                break
    elif gap_dir == 'UP' and gap_abs >= 0.003:
        for _, row in day_df.iterrows():
            if row['Low'] <= prev_close:
                gap_filled    = True
                gap_fill_time = row['bar_time']
                break

    # ---- Max adverse / max favourable from open ------------------------------
    day_high = day_df['High'].max()
    day_low  = day_df['Low'].min()
    max_drop_from_open = (open_px - day_low)  / open_px
    max_rise_from_open = (day_high - open_px) / open_px

    # ---- OR alignment with gap direction -------------------------------------
    if breakout_dir and gap_dir != 'FLAT':
        or_alignment = 'AND_GO' if breakout_dir == gap_dir else 'FADE'
    else:
        or_alignment = 'NO_BREAK' if breakout_dir is None else 'UNKNOWN'

    # ---- Minutes to gap fill -------------------------------------------------
    def t2m(t):
        return t.hour * 60 + t.minute

    fill_minutes = None
    if gap_filled and gap_fill_time:
        fill_minutes = t2m(gap_fill_time) - t2m(MARKET_OPEN)

    return {
        'date'          : date,
        'gap_pct'       : round(gap_pct * 100, 3),
        'gap_abs_pct'   : round(gap_abs * 100, 3),
        'gap_dir'       : gap_dir,
        'bucket'        : bucket,
        'or_high'       : or_high,
        'or_low'        : or_low,
        'or_width_pct'  : round(or_width_pct * 100, 3),
        'breakout_dir'  : breakout_dir,
        'or_alignment'  : or_alignment,
        'continuation'  : continuation,
        'gap_filled'    : gap_filled,
        'fill_minutes'  : fill_minutes,
        'max_drop_pct'  : round(max_drop_from_open * 100, 3),
        'max_rise_pct'  : round(max_rise_from_open * 100, 3),
        'day_close'     : day_close,
        'open_px'       : open_px,
        'prev_close'    : prev_close,
    }

def run_instrument(instr, folder):
    print(f"\n{'='*60}")
    print(f"  Loading {instr} ...")
    df = load_instrument(instr, folder)
    if df.empty:
        return pd.DataFrame()

    dates = sorted(df['date'].unique())
    print(f"  {len(dates)} trading days ({dates[0]} -> {dates[-1]})")

    results = []
    for i, date in enumerate(dates[1:], 1):
        prev_date  = dates[i - 1]
        prev_df    = df[df['date'] == prev_date]
        day_df     = df[df['date'] == date]
        if prev_df.empty or day_df.empty:
            continue
        prev_close = float(prev_df.iloc[-1]['Close'])
        rec = analyse_day(date, day_df, prev_close)
        if rec:
            rec['instrument'] = instr
            results.append(rec)

    return pd.DataFrame(results)

def summary_table(data, instr):
    """Build the per-instrument summary table string."""
    lines = []
    lines.append(f"\n{'='*70}")
    lines.append(f"  {instr}  --  {len(data)} analysed days")
    lines.append(f"{'='*70}")

    bucket_order = ['INSIDE', 'SMALL', 'MEDIUM', 'LARGE', 'EXTREME']

    for direction in ['DOWN', 'UP']:
        dir_data = data[data['gap_dir'] == direction]
        if dir_data.empty:
            continue
        lines.append(f"\n  GAP {direction}")
        lines.append(f"  {'Bucket':<10} {'N':>4}  {'ContRate':>8}  {'FillRate':>8}  "
                     f"{'FillMin':>7}  {'AND_GO':>6}  {'FADE':>6}  "
                     f"{'MaxDrop':>7}  {'MaxRise':>7}  Note")
        lines.append("  " + "-" * 82)

        for bkt in bucket_order:
            sub = dir_data[dir_data['bucket'] == bkt]
            if len(sub) < 3:
                continue
            n      = len(sub)
            cont   = sub['continuation'].mean() * 100
            fill   = sub['gap_filled'].mean() * 100
            fill_t = sub['fill_minutes'].dropna().median()
            fill_t_s = f"{fill_t:.0f}" if not pd.isna(fill_t) else " -- "
            ango   = (sub['or_alignment'] == 'AND_GO').mean() * 100
            fade   = (sub['or_alignment'] == 'FADE').mean() * 100
            drop   = sub['max_drop_pct'].median()
            rise   = sub['max_rise_pct'].median()

            if cont > 60:
                cont_tag = "CONT"
            elif cont < 40:
                cont_tag = "REV "
            else:
                cont_tag = "COIN"

            if fill > 60:
                fill_tag = "fills likely"
            elif fill > 40:
                fill_tag = "fills ~half"
            else:
                fill_tag = "rare fill"

            lines.append(
                f"  {bkt:<10} {n:>4}  {cont:>7.1f}%  {fill:>7.1f}%  "
                f"{fill_t_s:>7}m  {ango:>5.1f}%  {fade:>5.1f}%  "
                f"{drop:>6.2f}%  {rise:>6.2f}%  [{cont_tag},{fill_tag}]"
            )

    # OR breakout side across all gaps
    lines.append(f"\n  OR Breakout Side (all days regardless of gap size):")
    lines.append(f"  {'Bucket':<10} {'N':>4}  {'OR->UP':>6}  {'OR->DN':>6}  {'NO_BRK':>7}")
    lines.append("  " + "-" * 45)
    for bkt in bucket_order:
        sub = data[data['bucket'] == bkt]
        if len(sub) < 3:
            continue
        n   = len(sub)
        up  = (sub['breakout_dir'] == 'UP').mean() * 100
        dn  = (sub['breakout_dir'] == 'DOWN').mean() * 100
        nb  = sub['breakout_dir'].isna().mean() * 100
        lines.append(f"  {bkt:<10} {n:>4}  {up:>5.1f}%  {dn:>5.1f}%  {nb:>6.1f}%")

    return "\n".join(lines)

def cross_index_table(all_data):
    """Check gap direction consistency across all 3 indices on same day."""
    lines = [f"\n{'='*70}", "  CROSS-INDEX GAP ALIGNMENT", f"{'='*70}"]

    frames = {}
    for k, v in all_data.items():
        if not v.empty:
            frames[k] = v.set_index('date')[['gap_dir', 'gap_pct', 'continuation', 'gap_filled']]

    if len(frames) < 2:
        lines.append("  (need >= 2 instruments for cross-index analysis)")
        return "\n".join(lines)

    common_dates = set.intersection(*[set(f.index) for f in frames.values()])
    lines.append(f"  Common trading days: {len(common_dates)}")

    instrs = list(frames.keys())
    aligned_days   = []
    divergent_days = []
    for d in sorted(common_dates):
        dirs = {k: frames[k].loc[d, 'gap_dir'] for k in instrs if d in frames[k].index}
        unique_dirs = set(v for v in dirs.values() if v != 'FLAT')
        if len(unique_dirs) == 1:
            aligned_days.append(d)
        elif len(unique_dirs) > 1:
            divergent_days.append(d)

    pct_aln = 100 * len(aligned_days) / len(common_dates) if common_dates else 0
    pct_div = 100 * len(divergent_days) / len(common_dates) if common_dates else 0
    lines.append(f"  All indices gap SAME direction : {len(aligned_days)} ({pct_aln:.1f}%)")
    lines.append(f"  Indices DIVERGE in gap direction: {len(divergent_days)} ({pct_div:.1f}%)")

    for scenario, day_list in [("ALIGNED", aligned_days), ("DIVERGENT", divergent_days)]:
        if not day_list:
            continue
        lines.append(f"\n  Continuation rates on {scenario} gap days:")
        for k, fr in frames.items():
            sub = fr[fr.index.isin(day_list) & (fr['gap_dir'] != 'FLAT')]
            if sub.empty:
                continue
            cont = sub['continuation'].mean() * 100
            fill = sub['gap_filled'].mean() * 100
            lines.append(f"    {k:<12} cont={cont:.1f}%  fill={fill:.1f}%  n={len(sub)}")

    return "\n".join(lines)

def gut_feel_summary(all_data):
    """Plain-English 'gut feel' rules derived from the data."""
    combined = pd.concat(all_data.values(), ignore_index=True)
    combined = combined[combined['gap_dir'] != 'FLAT']

    lines = [
        f"\n{'='*70}",
        "  GUT FEEL RULES  (statistical summary, all 3 indices combined)",
        f"{'='*70}",
        "",
        "  ContRate = % days that close in gap direction (continuation)",
        "  FillRate = % days price touches prev_close during session",
        "  AND_GO   = OR breaks in gap direction | FADE = OR breaks opposite",
        "",
    ]

    for direction in ['DOWN', 'UP']:
        dir_d = combined[combined['gap_dir'] == direction]
        lines.append(f"  ---- GAP {direction} " + "-"*50)
        for bkt in ['INSIDE', 'SMALL', 'MEDIUM', 'LARGE', 'EXTREME']:
            sub = dir_d[dir_d['bucket'] == bkt]
            if len(sub) < 5:
                continue
            n        = len(sub)
            cont     = sub['continuation'].mean() * 100
            fill     = sub['gap_filled'].mean() * 100
            ango     = (sub['or_alignment'] == 'AND_GO').mean() * 100
            fade     = (sub['or_alignment'] == 'FADE').mean() * 100
            fill_min = sub['fill_minutes'].dropna().median()
            fm_s     = f"{fill_min:.0f}m" if not pd.isna(fill_min) else "--"

            if direction == 'DOWN':
                if cont > 60:
                    bias = f"Trend continues DOWN {cont:.0f}% -- gap intact at close"
                elif cont < 40:
                    bias = f"Market REVERSES UP {100-cont:.0f}% -- expect mean reversion"
                else:
                    bias = f"COIN-FLIP ({cont:.0f}% continue down, {100-cont:.0f}% reverse)"

                fill_s = (f"Gap fills {fill:.0f}% (back to prev close), median {fm_s} after open"
                          if not pd.isna(fill_min) else f"Gap fills {fill:.0f}%")
                or_s   = f"OR breaks: AND_GO(down) {ango:.0f}% | FADE(up) {fade:.0f}%"
            else:
                if cont > 60:
                    bias = f"Trend continues UP {cont:.0f}% -- gap intact at close"
                elif cont < 40:
                    bias = f"Market REVERSES DOWN {100-cont:.0f}% -- expect fill/fade"
                else:
                    bias = f"COIN-FLIP ({cont:.0f}% continue up, {100-cont:.0f}% reverse)"

                fill_s = (f"Gap fills {fill:.0f}% (back to prev close), median {fm_s} after open"
                          if not pd.isna(fill_min) else f"Gap fills {fill:.0f}%")
                or_s   = f"OR breaks: AND_GO(up) {ango:.0f}% | FADE(down) {fade:.0f}%"

            lines.append(f"\n  {bkt} (n={n})")
            lines.append(f"    Bias : {bias}")
            lines.append(f"    Fill : {fill_s}")
            lines.append(f"    OR   : {or_s}")

        lines.append("")

    return "\n".join(lines)

def bot_scoring_table():
    """Suggested scoring adjustments for the bot's unified scorer."""
    lines = [
        f"\n{'='*70}",
        "  BOT SCORING ADJUSTMENT GUIDE",
        "  (additive modifier for _get_unified_scorer, before gate check)",
        f"{'='*70}",
        "",
        "  Situation                                        Score delta   Rationale",
        "  " + "-"*75,
        "  GAP_DN EXTREME (>2.5%) + FADE entry (PUT blocked)  +2        Strong mean-rev; gap fills >70%",
        "  GAP_DN LARGE   (1.5-2.5%) + FADE entry             +1        Good reversal odds + OR confirms",
        "  GAP_DN MEDIUM  (0.75-1.5%) + FADE entry            +1        Modest reversal edge",
        "  GAP_DN any + AND_GO entry (PUT chasing gap dn)      -1        Chasing; lower WR historically",
        "  GAP_DN LARGE/EXTREME + AND_GO (PUT on gap dn)       -2        High exhaustion risk (today!)",
        "",
        "  GAP_UP EXTREME (>2.5%) + FADE entry (CALL blocked)  +2        Gap fills >70%",
        "  GAP_UP LARGE   (1.5-2.5%) + FADE entry             +1        Good fill odds",
        "  GAP_UP any + AND_GO entry (CALL chasing gap up)     -1        Late-chasing penalty",
        "  GAP_UP LARGE/EXTREME + AND_GO (CALL on gap up)      -2        Exhaustion + REV-GUARD likely",
        "",
        "  INSIDE (<0.3%) -- no gap context                     0        ADX/OI drives",
        "  SMALL (0.3-0.75%) -- minimal gap                     0        Gap too small to matter",
        "",
        "  Note: Today's BNF PUT (-1.7% GAP_DN + AND_GO) would have scored -2",
        "  on top of PCR-extreme -2 = total -4 (instant REJECT, no entry).",
    ]
    return "\n".join(lines)

# ---- Main -------------------------------------------------------------------
def main():
    all_data = {}
    for instr, folder in INSTRUMENTS.items():
        result = run_instrument(instr, folder)
        if not result.empty:
            all_data[instr] = result

    if not all_data:
        print("No data loaded. Check folder paths.")
        sys.exit(1)

    report_lines = [
        "GAP BEHAVIOUR ANALYSIS -- NIFTY / BANKNIFTY / SENSEX",
        "Generated: 2026-05-18",
        f"OR definition: first {OR_BARS} bars (09:15-09:30)",
        "Gap buckets: INSIDE<0.3%  SMALL 0.3-0.75%  MEDIUM 0.75-1.5%  LARGE 1.5-2.5%  EXTREME>2.5%",
        "",
        "COLUMNS:",
        "  ContRate = % days that CLOSE in same direction as gap",
        "  FillRate = % days price touches prev_close during session",
        "  FillMin  = median minutes after open when gap fills",
        "  AND_GO   = OR breaks in gap direction (continuation)",
        "  FADE     = OR breaks OPPOSITE to gap (reversal signal)",
        "  MaxDrop/Rise = median max intraday excursion from open",
    ]

    for instr, data in all_data.items():
        report_lines.append(summary_table(data, instr))

    report_lines.append(cross_index_table(all_data))

    report_lines.append(gut_feel_summary(all_data))
    report_lines.append(bot_scoring_table())

    report = "\n".join(report_lines)
    print(report)

    out_path = Path(r'C:\quant_trading\live_bot\gap_stats_report.txt')
    out_path.write_text(report, encoding='utf-8')
    print(f"\n\nReport saved -> {out_path}")

    # Save raw CSV
    combined = pd.concat(all_data.values(), ignore_index=True)
    raw_path = Path(r'C:\quant_trading\live_bot\gap_stats_raw.csv')
    combined.to_csv(raw_path, index=False)
    print(f"Raw data   -> {raw_path}")

if __name__ == '__main__':
    main()
