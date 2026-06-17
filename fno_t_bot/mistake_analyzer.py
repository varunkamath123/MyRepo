"""
FnO_T_Bot — Mistake Analyzer & Trade Pattern Learning
======================================================
Learns from recurring mistakes in backtested and live/paper trade data.

Reads:
  1. C:\\quant_trading\\backtest_trades.csv   — output from bot.py backtest
  2. live_bot/logs/FnO_T_Bot_*_trades.json   — paper/live trade journals

Outputs:
  • Terminal report  : pattern analysis, danger zones, regime warnings
  • mistake_report.txt : persistent lesson summary (appended on each run)
  • mistake_heatmap.png : hour × weekday win-rate heatmap

Usage:
  python mistake_analyzer.py             # analyse backtest + live trades
  python mistake_analyzer.py --live      # live/paper trades only
  python mistake_analyzer.py --backtest  # backtest trades only
"""

import os
import sys
import json
import warnings
import textwrap
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors

warnings.filterwarnings('ignore')
plt.style.use('seaborn-v0_8-darkgrid')

# ─── Paths ────────────────────────────────────────────────────────────────────
ROOT         = Path(__file__).parent.parent          # C:\quant_trading
LIVE_BOT_DIR = Path(__file__).parent                 # C:\quant_trading\live_bot
LOG_DIR      = LIVE_BOT_DIR / 'logs'
BT_CSV       = ROOT / 'backtest_trades.csv'
REPORT_FILE  = ROOT / 'mistake_report.txt'
HEATMAP_FILE = ROOT / 'mistake_heatmap.png'

# ─── Thresholds for "live deviation" warnings ──────────────────────────────
WARN_WIN_RATE_DROP  = 0.10   # alert if live WR drops 10pp below backtest
WARN_LOSS_STREAK    = 5      # alert if ≥ 5 consecutive losses
WARN_DD_THRESHOLD   = 0.15   # alert if daily loss > 15% of capital
MIN_LIVE_TRADES     = 10     # need at least 10 live trades for comparison

W = 72
DIV = "─" * W


# ─── 1. Load Data ─────────────────────────────────────────────────────────────

def load_backtest(path: Path) -> pd.DataFrame:
    """Load bot.py backtest trade log CSV."""
    if not path.exists():
        print(f"  [Backtest] No file at {path}. Run bot.py first.")
        return pd.DataFrame()
    df = pd.read_csv(path, parse_dates=['Entry Date', 'Exit Date'])
    print(f"  [Backtest] Loaded {len(df)} trades from {path.name}")
    return _enrich(df, source='backtest')


def load_live_trades() -> pd.DataFrame:
    """Load paper/live trade JSON logs from all instruments."""
    dfs = []
    for jf in LOG_DIR.glob('FnO_T_Bot_*_trades.json'):
        try:
            with open(jf, encoding='utf-8') as f:
                records = json.load(f)
            if records:
                tmp = pd.DataFrame(records)
                tmp['instrument'] = jf.stem.split('_')[3]  # NIFTY / BANKNIFTY
                dfs.append(tmp)
                print(f"  [Live] Loaded {len(tmp)} trades from {jf.name}")
        except Exception as e:
            print(f"  [Live] Could not load {jf.name}: {e}")

    if not dfs:
        return pd.DataFrame()

    df = pd.concat(dfs, ignore_index=True)
    # Normalise column names to match backtest
    rename = {
        'entry_time'  : 'Entry Date',
        'exit_time'   : 'Exit Date',
        'type'        : 'Type',
        'entry_price' : 'Entry Price',
        'exit_price'  : 'Exit Price',
        'pnl_net'     : 'P&L Net',
        'pnl_pct'     : 'P&L %',
        'exit_reason' : 'Exit Reason',
        'costs'       : 'Transaction Costs',
        'strike'      : 'Strike',
    }
    df.rename(columns=rename, inplace=True)
    df['Entry Date'] = pd.to_datetime(df['Entry Date'])
    df['Exit Date']  = pd.to_datetime(df['Exit Date'])
    df['Win']        = (df['P&L Net'] > 0).astype(int)
    return _enrich(df, source='live')


def _enrich(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """Add derived analysis columns."""
    if df.empty:
        return df
    df = df.copy()
    df['Source']       = source
    df['Entry Hour']   = df['Entry Date'].dt.hour
    df['Entry Minute'] = df['Entry Date'].dt.minute
    df['Entry HH:MM']  = df['Entry Date'].dt.strftime('%H:%M')
    df['Entry Weekday']= df['Entry Date'].dt.day_name()
    df['Exit Hour']    = df['Exit Date'].dt.hour
    df['Hours Held']   = (df['Exit Date'] - df['Entry Date']).dt.total_seconds() / 3600
    if 'Win' not in df.columns:
        df['Win']      = (df['P&L Net'] > 0).astype(int)
    df['Month']        = df['Entry Date'].dt.to_period('M').astype(str)
    return df


# ─── 2. Analysis Functions ────────────────────────────────────────────────────

def win_rate_by_group(df: pd.DataFrame, col: str, label: str,
                      min_trades: int = 3) -> pd.DataFrame:
    """Win rate + avg P&L grouped by a categorical column."""
    g = (df.groupby(col)
           .agg(Trades=('Win', 'count'),
                Wins=('Win', 'sum'),
                WR=('Win', 'mean'),
                AvgPnL=('P&L Net', 'mean'),
                TotalPnL=('P&L Net', 'sum'))
           .reset_index())
    g = g[g['Trades'] >= min_trades].copy()
    g['WR%'] = (g['WR'] * 100).round(1)
    g.sort_values('WR%', ascending=False, inplace=True)
    return g


def danger_zones(df: pd.DataFrame) -> dict:
    """
    Identify recurring patterns in LOSING trades that are absent or rare
    in winning trades. Outputs a dict of {condition: description}.
    """
    losers  = df[df['Win'] == 0]
    winners = df[df['Win'] == 1]
    findings = {}

    # ── 1. Entry hour danger zones ──────────────────────────────────────────
    hour_wr = (df.groupby('Entry Hour')['Win'].agg(['mean', 'count'])
                 .query('count >= 3'))
    bad_hours = hour_wr[hour_wr['mean'] < 0.30].index.tolist()
    good_hours = hour_wr[hour_wr['mean'] >= 0.50].index.tolist()
    if bad_hours:
        findings['Bad Entry Hours'] = (
            f"Win rate < 30% when entering at: "
            f"{', '.join(f'{h:02d}:xx' for h in sorted(bad_hours))}. "
            f"Consider skipping these windows."
        )
    if good_hours:
        findings['Best Entry Hours'] = (
            f"Win rate ≥ 50% at: "
            f"{', '.join(f'{h:02d}:xx' for h in sorted(good_hours))}. "
            f"Prioritise entries here."
        )

    # ── 2. Day-of-week bias ─────────────────────────────────────────────────
    dow_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']
    dow_wr = (df.groupby('Entry Weekday')['Win'].agg(['mean', 'count'])
                .reindex([d for d in dow_order if d in df['Entry Weekday'].unique()])
                .query('count >= 3'))
    if not dow_wr.empty:
        worst_dow = dow_wr['mean'].idxmin()
        best_dow  = dow_wr['mean'].idxmax()
        findings['Worst Day'] = (
            f"{worst_dow}: {dow_wr.loc[worst_dow, 'mean']*100:.0f}% WR "
            f"({int(dow_wr.loc[worst_dow, 'count'])} trades). "
            f"Higher failure rate — be extra selective."
        )
        findings['Best Day'] = (
            f"{best_dow}: {dow_wr.loc[best_dow, 'mean']*100:.0f}% WR "
            f"({int(dow_wr.loc[best_dow, 'count'])} trades). "
            f"Most reliable trending conditions."
        )

    # ── 3. CALL vs PUT bias ─────────────────────────────────────────────────
    type_wr = df.groupby('Type')['Win'].agg(['mean', 'count']).query('count >= 3')
    if len(type_wr) == 2 and abs(type_wr.loc['CALL', 'mean'] - type_wr.loc['PUT', 'mean']) > 0.10:
        better = 'CALL' if type_wr.loc['CALL', 'mean'] > type_wr.loc['PUT', 'mean'] else 'PUT'
        worse  = 'PUT'  if better == 'CALL' else 'CALL'
        findings['Signal Bias'] = (
            f"{better}s outperform {worse}s significantly "
            f"({type_wr.loc[better,'mean']*100:.0f}% vs "
            f"{type_wr.loc[worse,'mean']*100:.0f}% WR). "
            f"Investigate if VWAP filter is sufficient or if trend alignment needs tightening."
        )

    # ── 4. Exit reason insight ──────────────────────────────────────────────
    er_dist = df['Exit Reason'].value_counts(normalize=True) * 100
    stop_pct = er_dist.get('Stop (40%)', 0)
    eod_pct  = er_dist.filter(like='EOD').sum() if not er_dist.filter(like='EOD').empty else 0
    tgt_pct  = er_dist.get('Target (80%)', 0)
    trail_pct= er_dist.get('Trailing Stop', 0)

    if stop_pct > 40:
        findings['High Stop Rate'] = (
            f"{stop_pct:.0f}% of trades exit via stop loss. "
            f"Signal quality or entry timing needs improvement — "
            f"too many bad-trend entries."
        )
    if eod_pct > 35:
        findings['High EOD Exits'] = (
            f"{eod_pct:.0f}% of trades reach EOD force-close. "
            f"Many signals trigger late in the day. "
            f"Consider tightening AVOID_LAST_MINUTES or the entry window."
        )
    if tgt_pct + trail_pct > 35:
        findings['Good Exit Profile'] = (
            f"{tgt_pct:.0f}% hit target, {trail_pct:.0f}% trail out — "
            f"{'winners are being protected well.' if trail_pct > 5 else 'trailing stop capturing gains.'}"
        )

    # ── 5. Fast losers ──────────────────────────────────────────────────────
    if 'Hours Held' in losers.columns and len(losers) > 5:
        fast_loss_pct = (losers['Hours Held'] < 1.0).mean() * 100
        if fast_loss_pct > 40:
            findings['Fast Losses'] = (
                f"{fast_loss_pct:.0f}% of losses exit within 1 hour of entry. "
                f"Signals are reversing quickly — market may be choppy "
                f"at entry. ADX threshold could be raised, or add a "
                f"1-bar confirmation candle (re-test of EMA) before entry."
            )

    # ── 6. Consecutive loss streaks ─────────────────────────────────────────
    df_sorted = df.sort_values('Entry Date').reset_index(drop=True)
    max_streak = streak = 0
    streak_start = None
    worst_streak_start = None
    for _, row in df_sorted.iterrows():
        if row['Win'] == 0:
            streak += 1
            if streak == 1:
                streak_start = row['Entry Date']
            if streak > max_streak:
                max_streak = streak
                worst_streak_start = streak_start
        else:
            streak = 0
    if max_streak >= 4:
        findings['Max Loss Streak'] = (
            f"Longest consecutive losing streak: {max_streak} trades "
            f"(starting ~{worst_streak_start.strftime('%Y-%m-%d') if worst_streak_start else '?'}). "
            f"Consider a 'cool-down' rule: skip 1 day after 3 consecutive losses."
        )

    # ── 7. Recovery analysis ────────────────────────────────────────────────
    if 'P&L Net' in df.columns:
        monthly_pnl = df.groupby('Month')['P&L Net'].sum()
        negative_months = (monthly_pnl < 0).sum()
        positive_months = (monthly_pnl >= 0).sum()
        if negative_months > positive_months:
            worst_month  = monthly_pnl.idxmin()
            worst_loss   = monthly_pnl.min()
            findings['Monthly Regime'] = (
                f"{negative_months} loss months vs {positive_months} profit months. "
                f"Worst: {worst_month} (₹{worst_loss:,.0f}). "
                f"Check if NIFTY trended strongly that month — "
                f"if so, signal quality gap exists in volatile/correction periods."
            )

    return findings


def consecutive_loss_table(df: pd.DataFrame) -> list[tuple]:
    """Return a list of (streak_len, start_date, end_date, total_loss)."""
    df_s = df.sort_values('Entry Date').reset_index(drop=True)
    streaks = []
    i = 0
    while i < len(df_s):
        if df_s.at[i, 'Win'] == 0:
            j = i
            loss = 0.0
            while j < len(df_s) and df_s.at[j, 'Win'] == 0:
                loss += df_s.at[j, 'P&L Net']
                j += 1
            if (j - i) >= 3:
                streaks.append((j - i,
                                df_s.at[i, 'Entry Date'].strftime('%Y-%m-%d'),
                                df_s.at[j-1, 'Exit Date'].strftime('%Y-%m-%d'),
                                round(loss, 2)))
            i = j
        else:
            i += 1
    return sorted(streaks, key=lambda x: x[0], reverse=True)


def live_vs_backtest_warnings(live: pd.DataFrame, bt: pd.DataFrame) -> list[str]:
    """
    Compare live/paper stats to backtest. Emit warnings when deviations
    exceed thresholds — this is the core 'future learning' mechanism.
    """
    warnings_out = []
    if len(live) < MIN_LIVE_TRADES:
        return [f"Only {len(live)} live trades — need ≥{MIN_LIVE_TRADES} for comparison."]

    bt_wr   = bt['Win'].mean() if len(bt) > 0 else None
    live_wr = live['Win'].mean()

    if bt_wr is not None and (bt_wr - live_wr) > WARN_WIN_RATE_DROP:
        warnings_out.append(
            f"⚠ WIN RATE DROP: Live {live_wr*100:.1f}% vs Backtest {bt_wr*100:.1f}% "
            f"(−{(bt_wr-live_wr)*100:.1f}pp). Market regime may have changed — "
            f"consider pausing and re-optimising on recent data."
        )

    # Live loss streak
    df_s = live.sort_values('Entry Date').reset_index(drop=True)
    streak = 0
    for _, row in df_s.iterrows():
        streak = streak + 1 if row['Win'] == 0 else 0
    if streak >= WARN_LOSS_STREAK:
        warnings_out.append(
            f"⚠ LIVE LOSS STREAK: {streak} consecutive losses in progress. "
            f"Consider reducing position size or pausing until next signal."
        )

    # CALL vs PUT live split
    live_type = live.groupby('Type')['Win'].mean()
    if len(live_type) == 2:
        call_wr = live_type.get('CALL', None)
        put_wr  = live_type.get('PUT',  None)
        if call_wr is not None and call_wr < 0.25:
            warnings_out.append(
                f"⚠ CALL win rate very low live ({call_wr*100:.0f}%). "
                f"Check if NIFTY is in downtrend — VWAP filter may need tightening."
            )
        if put_wr is not None and put_wr < 0.25:
            warnings_out.append(
                f"⚠ PUT win rate very low live ({put_wr*100:.0f}%). "
                f"Check if NIFTY is in sustained uptrend — bias is against PUT signals."
            )

    if not warnings_out:
        warnings_out.append(
            f"✓ Live stats within normal range vs backtest "
            f"(WR: {live_wr*100:.1f}% | Trades: {len(live)})."
        )
    return warnings_out


# ─── 3. Heatmap ───────────────────────────────────────────────────────────────

def plot_heatmap(df: pd.DataFrame, title: str = "Win Rate by Hour & Day") -> None:
    """Hour-of-day × Day-of-week win rate heatmap."""
    dow_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']
    hours = sorted(df['Entry Hour'].unique())

    pivot = (df.pivot_table(index='Entry Hour', columns='Entry Weekday',
                            values='Win', aggfunc='mean')
               .reindex(columns=[d for d in dow_order if d in df['Entry Weekday'].unique()]))

    fig, ax = plt.subplots(figsize=(10, 6))
    cmap = mcolors.LinearSegmentedColormap.from_list(
        'wr', ['#e74c3c', '#f39c12', '#27ae60'])
    im = ax.imshow(pivot.values, cmap=cmap, aspect='auto', vmin=0, vmax=1)
    plt.colorbar(im, ax=ax, label='Win Rate')

    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels(pivot.columns, fontsize=11)
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels([f'{h:02d}:00' for h in pivot.index], fontsize=10)

    # Annotate cells
    for r in range(pivot.shape[0]):
        for c in range(pivot.shape[1]):
            v = pivot.values[r, c]
            if not np.isnan(v):
                ax.text(c, r, f'{v*100:.0f}%', ha='center', va='center',
                        fontsize=9, color='white' if v < 0.35 or v > 0.65 else 'black',
                        fontweight='bold')

    ax.set_title(title, fontsize=14, fontweight='bold', pad=12)
    ax.set_xlabel('Day of Week')
    ax.set_ylabel('Entry Hour (IST)')
    plt.tight_layout()
    plt.savefig(HEATMAP_FILE, dpi=120, bbox_inches='tight')
    plt.close()
    print(f"  Heatmap saved → {HEATMAP_FILE}")


# ─── 4. Main Report ───────────────────────────────────────────────────────────

def print_section(title: str) -> None:
    print(f"\n{DIV}")
    print(f"  {title}")
    print(DIV)


def run_analysis(mode: str = 'all') -> None:
    print("=" * W)
    print("  FnO_T_Bot — MISTAKE ANALYZER & TRADE PATTERN LEARNER")
    print("=" * W)
    print(f"  Run time : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Mode     : {mode}\n")

    bt   = load_backtest(BT_CSV) if mode in ('all', 'backtest') else pd.DataFrame()
    live = load_live_trades()    if mode in ('all', 'live')     else pd.DataFrame()

    # Choose primary dataset for deep analysis
    primary = bt if not bt.empty else live
    if primary.empty:
        print("\n⚠ No trade data found. Run bot.py (backtest) or paper_bot.py (live) first.")
        return

    source_label = "Backtest" if (not bt.empty and mode != 'live') else "Live/Paper"
    total_trades = len(primary)
    wins         = primary['Win'].sum()
    wr           = primary['Win'].mean() * 100
    avg_win      = primary[primary['Win'] == 1]['P&L Net'].mean() if wins > 0 else 0
    avg_loss     = primary[primary['Win'] == 0]['P&L Net'].mean() if (total_trades - wins) > 0 else 0
    net_pnl      = primary['P&L Net'].sum()

    print_section(f"SUMMARY — {source_label} ({total_trades} trades)")
    print(f"  Win Rate       : {wr:.1f}%   ({int(wins)} wins / {total_trades - int(wins)} losses)")
    print(f"  Avg Win        : ₹{avg_win:,.0f}")
    print(f"  Avg Loss       : ₹{avg_loss:,.0f}")
    print(f"  Win/Loss Ratio : {abs(avg_win/avg_loss):.2f}x" if avg_loss else "  Win/Loss: N/A")
    print(f"  Net P&L        : ₹{net_pnl:,.0f}")

    # ── 1. Entry Hour Analysis ───────────────────────────────────────────────
    print_section("ENTRY HOUR WIN RATES (IST)")
    hr_g = win_rate_by_group(primary, 'Entry Hour', 'Hour', min_trades=2)
    print(f"  {'Hour':<8} {'Trades':>7} {'WR%':>6} {'Avg P&L':>10} {'Total P&L':>12}")
    for _, r in hr_g.sort_values('Entry Hour').iterrows():
        flag = " ← HIGH" if r['WR%'] >= 55 else (" ← LOW" if r['WR%'] <= 30 else "")
        print(f"  {int(r['Entry Hour']):02d}:xx   {int(r['Trades']):>7} {r['WR%']:>5.1f}%"
              f"  ₹{r['AvgPnL']:>8,.0f}  ₹{r['TotalPnL']:>10,.0f}{flag}")

    # ── 2. Day of Week Analysis ──────────────────────────────────────────────
    print_section("DAY-OF-WEEK WIN RATES")
    dow_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']
    dow_g = win_rate_by_group(primary, 'Entry Weekday', 'Day', min_trades=2)
    dow_g['_order'] = dow_g['Entry Weekday'].map({d: i for i, d in enumerate(dow_order)})
    dow_g = dow_g.sort_values('_order')
    print(f"  {'Day':<12} {'Trades':>7} {'WR%':>6} {'Avg P&L':>10} {'Total P&L':>12}")
    for _, r in dow_g.iterrows():
        flag = " ← BEST" if r['WR%'] == dow_g['WR%'].max() else \
               (" ← WORST" if r['WR%'] == dow_g['WR%'].min() else "")
        print(f"  {r['Entry Weekday']:<12} {int(r['Trades']):>7} {r['WR%']:>5.1f}%"
              f"  ₹{r['AvgPnL']:>8,.0f}  ₹{r['TotalPnL']:>10,.0f}{flag}")

    # ── 3. CALL vs PUT Analysis ──────────────────────────────────────────────
    print_section("CALL vs PUT WIN RATES")
    type_g = win_rate_by_group(primary, 'Type', 'Type', min_trades=1)
    print(f"  {'Type':<8} {'Trades':>7} {'WR%':>6} {'Avg P&L':>10} {'Total P&L':>12}")
    for _, r in type_g.iterrows():
        print(f"  {r['Type']:<8} {int(r['Trades']):>7} {r['WR%']:>5.1f}%"
              f"  ₹{r['AvgPnL']:>8,.0f}  ₹{r['TotalPnL']:>10,.0f}")

    # ── 4. Exit Reason Analysis ──────────────────────────────────────────────
    print_section("EXIT REASON BREAKDOWN")
    er_g = (primary.groupby('Exit Reason')
                   .agg(Count=('Win', 'count'),
                        Wins=('Win', 'sum'),
                        AvgPnL=('P&L Net', 'mean'),
                        TotalPnL=('P&L Net', 'sum'))
                   .reset_index())
    er_g['WR%'] = (er_g['Wins'] / er_g['Count'] * 100).round(1)
    er_g.sort_values('Count', ascending=False, inplace=True)
    print(f"  {'Exit Reason':<28} {'N':>4} {'WR%':>6} {'Avg P&L':>10} {'Total P&L':>12}")
    for _, r in er_g.iterrows():
        print(f"  {r['Exit Reason']:<28} {int(r['Count']):>4} {r['WR%']:>5.1f}%"
              f"  ₹{r['AvgPnL']:>8,.0f}  ₹{r['TotalPnL']:>10,.0f}")

    # ── 5. Consecutive Loss Streaks ──────────────────────────────────────────
    print_section("CONSECUTIVE LOSS STREAKS (≥ 3)")
    streaks = consecutive_loss_table(primary)
    if streaks:
        print(f"  {'Streak':>7} {'Start':>12} {'End':>12} {'Total Loss':>12}")
        for s in streaks[:10]:
            print(f"  {s[0]:>7}  {s[1]:>12}  {s[2]:>12}  ₹{s[3]:>10,.0f}")
    else:
        print("  No loss streaks of ≥ 3 consecutive trades found.")

    # ── 6. Danger Zones & Lessons ───────────────────────────────────────────
    print_section("DANGER ZONES & LESSONS LEARNED")
    dz = danger_zones(primary)
    lessons_lines = []
    for i, (key, msg) in enumerate(dz.items(), 1):
        wrapped = textwrap.fill(f"{i:2d}. [{key}] {msg}",
                                width=W - 4, subsequent_indent="      ")
        print(f"  {wrapped}")
        lessons_lines.append(wrapped)
        print()

    # ── 7. Live vs Backtest Warnings ────────────────────────────────────────
    if not live.empty and not bt.empty:
        print_section("LIVE vs BACKTEST COMPARISON")
        for w in live_vs_backtest_warnings(live, bt):
            wrapped = textwrap.fill(w, width=W - 4, subsequent_indent="    ")
            print(f"  {wrapped}")

    # ── 8. Heatmap ───────────────────────────────────────────────────────────
    if len(primary) >= 10:
        print_section("GENERATING HEATMAP")
        plot_heatmap(primary, f"Win Rate Heatmap — {source_label} ({total_trades} trades)")

    # ── 9. Save Report ───────────────────────────────────────────────────────
    print_section("SAVING REPORT")
    _save_report(primary, source_label, dz, lessons_lines)

    print(f"\n{'=' * W}")
    print("  Analysis complete.")
    print("  Key action items are in mistake_report.txt — review before each trading week.")
    print(f"{'=' * W}\n")


def _save_report(df: pd.DataFrame, label: str,
                 dz: dict, lessons: list[str]) -> None:
    """Append a timestamped lesson summary to mistake_report.txt."""
    ts = datetime.now().strftime('%Y-%m-%d %H:%M')
    wr = df['Win'].mean() * 100
    net = df['P&L Net'].sum()

    lines = [
        "",
        "=" * W,
        f"  RUN: {ts}  |  Source: {label}  |  WR: {wr:.1f}%  |  Net: ₹{net:,.0f}",
        "=" * W,
    ]
    for key, msg in dz.items():
        lines.append(f"\n  [{key}]")
        lines.append(
            textwrap.fill(f"  {msg}", width=W - 4, subsequent_indent="    ")
        )
    lines.append("")

    with open(REPORT_FILE, 'a', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f"  Report appended → {REPORT_FILE}")


# ─── CLI Entry ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    mode = 'all'
    if '--live'     in sys.argv: mode = 'live'
    if '--backtest' in sys.argv: mode = 'backtest'
    run_analysis(mode)
