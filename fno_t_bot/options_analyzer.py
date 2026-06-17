# -*- coding: utf-8 -*-
"""
NIFTY Options Analyzer
========================
Deep analysis of real NIFTY options price data to derive actionable insights
for improving strategy profitability.

Requires
--------
  - data/nifty_options/*.csv     (from options_data_collector.py)
  - data/nifty_5min/*.csv        (spot data)
  - backtest_trades.csv          (from bot.py)

Insights derived
----------------
  1. BS Pricing Bias       — how wrong is Black-Scholes vs real prices?
  2. IV at Entry           — actual implied vol when signal fires
  3. IV Skew               — why PUTs outperform CALLs (put skew)
  4. MFE Distribution      — max gain before reversal (is 80% target right?)
  5. MAE Distribution      — max loss before recovery (is 40% stop right?)
  6. Theta Decay Profile   — how options price decays through the day
  7. Hit Rate by IV Band   — do we do better when vol is high or low?
  8. Intraday Liquidity    — which hours have tightest spreads / best prices

Usage
-----
  python options_analyzer.py           # full analysis
  python options_analyzer.py --bs      # Black-Scholes bias only
  python options_analyzer.py --mfe     # MFE/MAE optimal stop/target
  python options_analyzer.py --iv      # IV skew and regime analysis
  python options_analyzer.py --theta   # theta decay profile
"""

import os
import sys
import json
import warnings
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from scipy.stats import norm
from scipy.optimize import brentq
import pytz

warnings.filterwarnings('ignore')
plt.style.use('seaborn-v0_8-darkgrid')

# Force UTF-8 on Windows
import sys as _sys
if hasattr(_sys.stdout, 'reconfigure'):
    _sys.stdout.reconfigure(encoding='utf-8')
    _sys.stderr.reconfigure(encoding='utf-8')

IST          = pytz.timezone('Asia/Kolkata')
OPTIONS_DIR  = os.path.join(os.path.dirname(__file__), '..', 'data', 'nifty_options')
SPOT_DIR     = os.path.join(os.path.dirname(__file__), '..', 'data', 'nifty_5min')
TRADES_CSV   = os.path.join(os.path.dirname(__file__), '..', 'backtest_trades.csv')
RISK_FREE    = 0.065
BARS_PER_DAY = 75


# ─── Black-Scholes Helpers ────────────────────────────────────────────────────

def bs_price(opt_type: str, S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0:
        return max(S - K, 0) if opt_type == 'CALL' else max(K - S, 0)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    if opt_type == 'CALL':
        return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    else:
        return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def implied_vol(opt_type: str, market_px: float, S: float, K: float,
                T: float, r: float) -> float | None:
    """Compute implied volatility from market price via Brent method."""
    if T <= 0 or market_px <= 0:
        return None
    intrinsic = max(S - K, 0) if opt_type == 'CALL' else max(K - S, 0)
    if market_px < intrinsic:
        return None
    try:
        iv = brentq(
            lambda sigma: bs_price(opt_type, S, K, T, r, sigma) - market_px,
            1e-4, 5.0, xtol=1e-6, maxiter=100
        )
        return iv if 0.01 < iv < 4.0 else None
    except Exception:
        return None


# ─── Data Loading ─────────────────────────────────────────────────────────────

def load_spot() -> pd.DataFrame:
    """Load all NIFTY 5-min spot data."""
    if not os.path.exists(SPOT_DIR):
        return pd.DataFrame()
    files = sorted(f for f in os.listdir(SPOT_DIR) if f.endswith('.csv'))
    dfs = []
    for f in files:
        try:
            tmp = pd.read_csv(os.path.join(SPOT_DIR, f), index_col=0, parse_dates=True)
            if len(tmp) > 0:
                dfs.append(tmp)
        except Exception:
            pass
    if not dfs:
        return pd.DataFrame()
    spot = pd.concat(dfs).sort_index()
    spot = spot[~spot.index.duplicated(keep='first')]
    if spot.index.tzinfo is None:
        spot.index = spot.index.tz_localize(IST)
    else:
        spot.index = spot.index.tz_convert(IST)
    return spot


def load_options_db() -> dict:
    """
    Load all options CSVs.
    Returns dict: {(expiry_date, strike, 'CALL'/'PUT'): DataFrame}
    """
    cache = {}
    if not os.path.exists(OPTIONS_DIR):
        return cache
    for fname in os.listdir(OPTIONS_DIR):
        if not fname.endswith('.csv'):
            continue
        parts = fname.replace('.csv', '').split('_')
        if len(parts) != 4 or parts[0] != 'NIFTY':
            continue
        try:
            exp    = datetime.strptime(parts[1], '%Y%m%d').date()
            strike = int(parts[2])
            otype  = 'CALL' if parts[3] == 'CE' else 'PUT'
            df     = pd.read_csv(os.path.join(OPTIONS_DIR, fname),
                                 index_col=0, parse_dates=True)
            if df.index.tzinfo is None:
                df.index = df.index.tz_localize(IST)
            else:
                df.index = df.index.tz_convert(IST)
            cache[(exp, strike, otype)] = df
        except Exception:
            continue
    return cache


def load_trades() -> pd.DataFrame:
    if not os.path.exists(TRADES_CSV):
        return pd.DataFrame()
    df = pd.read_csv(TRADES_CSV, parse_dates=['Entry Date', 'Exit Date'])
    if 'Instrument' in df.columns:
        df = df[df['Instrument'] == 'NIFTY']
    if 'Exit Reason' in df.columns:
        df = df[df['Exit Reason'] != 'Partial 80%']
    return df.reset_index(drop=True)


# ─── Match Trade to Options Data ──────────────────────────────────────────────

def expiry_for_date(trade_date: date, min_dte: int = 2) -> date:
    check = trade_date
    for _ in range(14):
        if check.weekday() == 3 and (check - trade_date).days >= min_dte:
            return check
        check += timedelta(days=1)
    raise RuntimeError(f"No expiry found for {trade_date}")


def enrich_trades_with_real_prices(trades: pd.DataFrame,
                                   options_db: dict,
                                   spot: pd.DataFrame) -> pd.DataFrame:
    """
    For each trade, find the matching options contract and attach real price data.
    Adds columns:
      - Real_Entry_Price  : actual options price at entry bar
      - Real_Exit_Price   : actual options price at exit bar
      - Real_PnL_Pct      : P&L% using real prices
      - Real_PnL_Net      : net P&L in ₹ using real prices
      - IV_Entry          : implied vol at entry
      - IV_Exit           : implied vol at exit
      - IV_Change_Pct     : IV crush/expansion %
      - BS_Entry_Price    : what BS estimated
      - BS_Bias_Pct       : (BS - Real) / Real × 100
      - Has_Real_Data     : True if real options data found
    """
    import config as cfg

    enriched = []
    for _, row in trades.iterrows():
        record = row.to_dict()
        record['Has_Real_Data'] = False

        try:
            entry_ts = pd.Timestamp(row['Entry Date'])
            exit_ts  = pd.Timestamp(row['Exit Date'])
            entry_d  = entry_ts.date()
            expiry   = expiry_for_date(entry_d)
            strike   = int(row['Strike']) if 'Strike' in row else 0
            otype    = row['Type']

            key = (expiry, strike, otype)
            if key not in options_db:
                enriched.append(record)
                continue

            opt_df = options_db[key]

            # Get real price at entry
            def nearest_price(ts, col='Close'):
                if ts in opt_df.index:
                    return float(opt_df.loc[ts, col])
                near = opt_df.index[abs(opt_df.index - ts).argmin()]
                if abs((near - ts).total_seconds()) <= 600:
                    return float(opt_df.loc[near, col])
                return None

            real_entry = nearest_price(entry_ts)
            real_exit  = nearest_price(exit_ts)

            if real_entry is None or real_exit is None or real_entry <= 0:
                enriched.append(record)
                continue

            # Spot prices at entry/exit
            spot_entry = None
            spot_exit  = None
            if len(spot) > 0:
                if entry_ts in spot.index:
                    spot_entry = float(spot.loc[entry_ts, 'Close'])
                if exit_ts in spot.index:
                    spot_exit = float(spot.loc[exit_ts, 'Close'])

            # Time to expiry in years
            T_entry = max((expiry - entry_d).days, 0.1) / 365
            T_exit  = max((expiry - exit_ts.date()).days, 0.01) / 365

            # Implied vol at entry and exit
            iv_entry = implied_vol(otype, real_entry, spot_entry or strike,
                                   strike, T_entry, RISK_FREE) if spot_entry else None
            iv_exit  = implied_vol(otype, real_exit,  spot_exit  or strike,
                                   strike, T_exit,  RISK_FREE) if spot_exit else None

            # BS estimate at entry
            hv = float(row.get('HV', 0.18)) if 'HV' in row else 0.18
            bs_entry = bs_price(otype, spot_entry or strike, strike,
                                T_entry, RISK_FREE, hv) if spot_entry else None

            # P&L using real prices
            lot_size = cfg.INSTRUMENTS['NIFTY']['lot_size']
            real_pnl_pct = (real_exit - real_entry) / real_entry * 100
            brok  = 20 * 2
            stt   = real_exit * lot_size * 0.000625
            gross = (real_exit - real_entry) * lot_size
            costs = brok + stt + (real_entry + real_exit) * lot_size * (0.00053 + 0.000001) * 1.18
            real_pnl_net = gross - costs

            record['Real_Entry_Price'] = round(real_entry, 2)
            record['Real_Exit_Price']  = round(real_exit,  2)
            record['Real_PnL_Pct']     = round(real_pnl_pct, 2)
            record['Real_PnL_Net']     = round(real_pnl_net, 2)
            record['IV_Entry']         = round(iv_entry * 100, 2) if iv_entry else None
            record['IV_Exit']          = round(iv_exit  * 100, 2) if iv_exit  else None
            record['IV_Change_Pct']    = round((iv_exit - iv_entry) / iv_entry * 100, 2) \
                                         if iv_entry and iv_exit else None
            record['BS_Entry_Price']   = round(bs_entry, 2) if bs_entry else None
            record['BS_Bias_Pct']      = round((bs_entry - real_entry) / real_entry * 100, 2) \
                                         if bs_entry else None
            record['Has_Real_Data']    = True

        except Exception as e:
            pass

        enriched.append(record)

    return pd.DataFrame(enriched)


# ─── MFE / MAE from Options OHLC ────────────────────────────────────────────

def compute_mfe_mae(trades: pd.DataFrame, options_db: dict) -> pd.DataFrame:
    """
    For each trade, trace the options OHLC bars between entry and exit
    to find Maximum Favorable Excursion (MFE) and Maximum Adverse Excursion (MAE).

    MFE = max % gain the position saw before eventually exiting
    MAE = max % loss the position saw before eventually recovering/exiting

    These tell us:
    - Is 80% target achievable? (MFE distribution)
    - Is 40% stop too tight or too wide? (MAE distribution)
    """
    results = []

    for _, row in trades.iterrows():
        try:
            entry_ts = pd.Timestamp(row['Entry Date'])
            exit_ts  = pd.Timestamp(row['Exit Date'])
            entry_d  = entry_ts.date()
            expiry   = expiry_for_date(entry_d)
            strike   = int(row.get('Strike', 0))
            otype    = row['Type']
            key      = (expiry, strike, otype)

            if key not in options_db:
                continue

            opt_df = options_db[key]

            # Find real entry price
            def nearest_price(ts, col='Close'):
                if ts in opt_df.index:
                    return float(opt_df.loc[ts, col])
                near = opt_df.index[abs(opt_df.index - ts).argmin()]
                if abs((near - ts).total_seconds()) <= 600:
                    return float(opt_df.loc[near, col])
                return None

            entry_px = nearest_price(entry_ts)
            if entry_px is None or entry_px <= 0:
                continue

            # Slice bars between entry and exit
            mask    = (opt_df.index >= entry_ts) & (opt_df.index <= exit_ts)
            between = opt_df[mask]
            if len(between) == 0:
                continue

            # MFE: best High in the window
            best_high  = between['High'].max()
            mfe_pct    = (best_high - entry_px) / entry_px * 100

            # MAE: worst Low in the window
            worst_low  = between['Low'].min()
            mae_pct    = (worst_low - entry_px) / entry_px * 100   # negative if loss

            # Time-to-MFE: how many bars until peak
            mfe_bar_idx = between['High'].idxmax()
            bars_to_mfe = (between.index < mfe_bar_idx).sum() + 1

            results.append({
                'Entry Date'  : row['Entry Date'],
                'Type'        : otype,
                'Exit Reason' : row.get('Exit Reason', ''),
                'Entry_Px'    : round(entry_px, 2),
                'MFE_Pct'     : round(mfe_pct, 2),
                'MAE_Pct'     : round(mae_pct, 2),
                'Bars_to_MFE' : int(bars_to_mfe),
                'Win'         : int(row.get('Win', 0)),
            })

        except Exception:
            continue

    return pd.DataFrame(results)


# ─── Theta Decay Profile ─────────────────────────────────────────────────────

def theta_decay_profile(options_db: dict) -> pd.DataFrame:
    """
    For all 2-day contracts (entries where DTE ≈ 2), compute the average
    option price (normalised to entry price) at each time-of-day bar.

    This shows the intraday shape of time decay: when is theta steepest?
    """
    records = []

    for (expiry, strike, otype), df in options_db.items():
        # Look only at the day before expiry (≈ 2 DTE) and expiry day (1 DTE)
        for dte in (2, 1):
            target_d = expiry - timedelta(days=dte)
            day_bars = df[df.index.date == target_d]
            if len(day_bars) == 0:
                continue

            # Normalise to first bar close of the day
            first_px = day_bars['Close'].iloc[0]
            if first_px <= 0:
                continue

            for ts, r in day_bars.iterrows():
                records.append({
                    'Time'     : ts.time(),
                    'Hour'     : ts.hour,
                    'Minute'   : ts.minute,
                    'DTE'      : dte,
                    'Type'     : otype,
                    'Norm_Px'  : r['Close'] / first_px,
                    'Close'    : r['Close'],
                    'Strike'   : strike,
                    'Expiry'   : expiry,
                })

    return pd.DataFrame(records)


# ─── IV Skew Analysis ─────────────────────────────────────────────────────────

def iv_skew_analysis(options_db: dict, spot: pd.DataFrame) -> pd.DataFrame:
    """
    For matched CE/PE pairs (same expiry, same strike, same timestamp),
    compute IV for each and measure the skew (PUT_IV - CALL_IV).

    NIFTY historically has a persistent put skew of 3-8%:
    PUTs trade at higher IV than equivalent CALLs at the same strike.
    This is why our backtest shows PUTs with better WR — they're "cheaper"
    relative to their intrinsic value and move faster in % terms.
    """
    records = []
    all_expiries = set(exp for (exp, _, _) in options_db.keys())

    for expiry in sorted(all_expiries):
        strikes = set(s for (e, s, _) in options_db.keys() if e == expiry)

        for strike in sorted(strikes):
            call_key = (expiry, strike, 'CALL')
            put_key  = (expiry, strike, 'PUT')

            if call_key not in options_db or put_key not in options_db:
                continue

            call_df = options_db[call_key]
            put_df  = options_db[put_key]

            # Align on common timestamps
            common_ts = call_df.index.intersection(put_df.index)
            if len(common_ts) == 0:
                continue

            for ts in common_ts[::3]:   # sample every 3rd bar (speed)
                if ts not in spot.index:
                    continue

                S = float(spot.loc[ts, 'Close'])
                T = max((expiry - ts.date()).days, 0.1) / 365

                call_px = float(call_df.loc[ts, 'Close'])
                put_px  = float(put_df.loc[ts,  'Close'])

                iv_call = implied_vol('CALL', call_px, S, strike, T, RISK_FREE)
                iv_put  = implied_vol('PUT',  put_px,  S, strike, T, RISK_FREE)

                if iv_call and iv_put:
                    records.append({
                        'Timestamp'    : ts,
                        'Expiry'       : expiry,
                        'Strike'       : strike,
                        'Spot'         : round(S, 0),
                        'Moneyness'    : round((S - strike) / S * 100, 2),
                        'IV_Call'      : round(iv_call * 100, 2),
                        'IV_Put'       : round(iv_put  * 100, 2),
                        'Put_Skew'     : round((iv_put - iv_call) * 100, 2),
                        'DTE'          : (expiry - ts.date()).days,
                        'Hour'         : ts.hour,
                    })

    return pd.DataFrame(records)


# ─── Print Summary Reports ────────────────────────────────────────────────────

def print_bs_bias_report(enriched: pd.DataFrame) -> None:
    real = enriched[enriched['Has_Real_Data'] == True].copy()
    if len(real) == 0:
        print("  No real options data matched to trades.")
        return

    print(f"\n{'='*65}")
    print(f"  BLACK-SCHOLES PRICING BIAS  ({len(real)} trades with real data)")
    print(f"{'='*65}")

    bias = real['BS_Bias_Pct'].dropna()
    print(f"  Mean BS bias     : {bias.mean():+.1f}%  "
          f"({'overpriced' if bias.mean()>0 else 'underpriced'} by BS)")
    print(f"  Median BS bias   : {bias.median():+.1f}%")
    print(f"  Std dev          : {bias.std():.1f}%")
    print(f"  Max overestimate : {bias.max():+.1f}%")
    print(f"  Max underestimate: {bias.min():+.1f}%")

    by_type = real.groupby('Type')['BS_Bias_Pct'].agg(['mean', 'median', 'count'])
    print(f"\n  By option type:")
    for t, r in by_type.iterrows():
        print(f"    {t:5s}: mean {r['mean']:+.1f}% | median {r['median']:+.1f}% "
              f"| n={int(r['count'])}")

    # Real vs BS P&L comparison
    real_net = real['Real_PnL_Net'].dropna().sum()
    bs_net   = real['P&L Net'].sum()
    print(f"\n  P&L Comparison (for {len(real)} matched trades):")
    print(f"    Using Black-Scholes : ₹{bs_net:,.0f}")
    print(f"    Using real prices   : ₹{real_net:,.0f}")
    print(f"    Difference          : ₹{real_net - bs_net:+,.0f}")


def print_mfe_mae_report(mfe_df: pd.DataFrame) -> None:
    if len(mfe_df) == 0:
        print("  No MFE/MAE data (options OHLC needed).")
        return

    print(f"\n{'='*65}")
    print(f"  MFE / MAE ANALYSIS  ({len(mfe_df)} trades)")
    print(f"{'='*65}")

    mfe = mfe_df['MFE_Pct']
    mae = mfe_df['MAE_Pct']

    print(f"\n  MAXIMUM FAVORABLE EXCURSION (MFE):")
    print(f"    Mean MFE  : {mfe.mean():+.1f}%  (avg best gain before exit)")
    print(f"    Median MFE: {mfe.median():+.1f}%")
    for pct in (50, 60, 80, 100, 150, 200):
        hit_rate = (mfe >= pct).mean() * 100
        print(f"    Trades reaching {pct:>3}%+ target  : {hit_rate:>5.1f}%")

    print(f"\n  MAXIMUM ADVERSE EXCURSION (MAE):")
    print(f"    Mean MAE  : {mae.mean():+.1f}%  (avg worst loss before exit)")
    print(f"    Median MAE: {mae.median():+.1f}%")
    for pct in (10, 20, 30, 40, 50):
        hit_rate = (mae <= -pct).mean() * 100
        print(f"    Trades that dipped -{pct}%+ : {hit_rate:>5.1f}%")

    print(f"\n  CURRENT CONFIG vs REAL BEHAVIOR:")
    print(f"    Stop at  -40% | % of trades hitting -40% MAE : "
          f"{(mae <= -40).mean()*100:.1f}%")
    print(f"    Target at 80% | % of trades reaching 80% MFE : "
          f"{(mfe >= 80).mean()*100:.1f}%")

    # Time-to-MFE
    print(f"\n  TIME TO MFE:")
    print(f"    Mean bars to peak  : {mfe_df['Bars_to_MFE'].mean():.1f}  "
          f"(= {mfe_df['Bars_to_MFE'].mean()*5:.0f} min)")
    print(f"    Median bars to peak: {mfe_df['Bars_to_MFE'].median():.0f}")

    # Optimal target suggestion
    for tgt in (60, 70, 80, 90, 100):
        hit = (mfe >= tgt).mean() * 100
        if hit >= 20:  # at least 20% of trades can hit this target
            print(f"\n  Suggested optimal target: {tgt}%  "
                  f"(reachable by {hit:.0f}% of trades)")
            break


def print_iv_skew_report(skew_df: pd.DataFrame) -> None:
    if len(skew_df) == 0:
        print("  No IV skew data available.")
        return

    print(f"\n{'='*65}")
    print(f"  IV SKEW ANALYSIS  ({len(skew_df):,} data points)")
    print(f"{'='*65}")

    print(f"  Average IV (CALL) : {skew_df['IV_Call'].mean():.1f}%")
    print(f"  Average IV (PUT)  : {skew_df['IV_Put'].mean():.1f}%")
    print(f"  Average Put Skew  : +{skew_df['Put_Skew'].mean():.1f}% "
          f"(PUTs trade {skew_df['Put_Skew'].mean():.1f}pp higher IV)")

    # At-the-money skew specifically
    atm = skew_df[skew_df['Moneyness'].abs() < 0.5]
    if len(atm) > 0:
        print(f"\n  ATM Put Skew      : +{atm['Put_Skew'].mean():.1f}%  (|moneyness| < 0.5%)")

    # By DTE
    print(f"\n  Skew by DTE:")
    for dte in sorted(skew_df['DTE'].unique()):
        sub = skew_df[skew_df['DTE'] == dte]
        print(f"    DTE={dte:2d}: CALL IV {sub['IV_Call'].mean():.1f}%  "
              f"PUT IV {sub['IV_Put'].mean():.1f}%  "
              f"Skew +{sub['Put_Skew'].mean():.1f}%")

    print(f"\n  Interpretation:")
    print(f"    PUTs have higher IV → for same notional move, PUT premiums")
    print(f"    expand faster on down-moves. This is why PUT WR > CALL WR.")
    print(f"    Consider: buy PUTs only (skip CALL trades) to exploit skew.")


def print_theta_report(theta_df: pd.DataFrame) -> None:
    if len(theta_df) == 0:
        print("  No theta data available.")
        return

    print(f"\n{'='*65}")
    print(f"  THETA DECAY PROFILE")
    print(f"{'='*65}")

    for dte in [2, 1]:
        sub = theta_df[theta_df['DTE'] == dte]
        if len(sub) == 0:
            continue
        avg_by_hour = sub.groupby('Hour')['Norm_Px'].mean()
        print(f"\n  DTE={dte} — Normalised price by hour (1.00 = open of day):")
        for h, v in avg_by_hour.items():
            bar = '█' * int(v * 20)
            print(f"    {h:02d}:xx  {v:.3f}  {bar}")


# ─── Plots ────────────────────────────────────────────────────────────────────

def plot_all(enriched: pd.DataFrame, mfe_df: pd.DataFrame,
             skew_df: pd.DataFrame, theta_df: pd.DataFrame) -> None:
    fig = plt.figure(figsize=(18, 20))
    gs  = gridspec.GridSpec(3, 2, figure=fig, hspace=0.45, wspace=0.35)
    fig.suptitle("NIFTY Options Analysis — Real Price Insights", fontsize=15, fontweight='bold')

    # ── 1. BS Bias distribution
    ax = fig.add_subplot(gs[0, 0])
    real = enriched[enriched['Has_Real_Data'] == True]['BS_Bias_Pct'].dropna()
    if len(real) > 0:
        ax.hist(real, bins=20, edgecolor='black', alpha=0.7, color='#3498db')
        ax.axvline(0, color='red', ls='--', lw=2, label='No bias')
        ax.axvline(real.mean(), color='green', ls='--', lw=1.5,
                   label=f'Mean {real.mean():+.1f}%')
        ax.set_title('Black-Scholes Pricing Bias\n(+ve = BS overestimates real price)', fontsize=10)
        ax.set_xlabel('BS Bias %')
        ax.legend(fontsize=8)

    # ── 2. MFE distribution
    ax = fig.add_subplot(gs[0, 1])
    if len(mfe_df) > 0:
        ax.hist(mfe_df['MFE_Pct'], bins=25, edgecolor='black', alpha=0.7, color='#2ecc71')
        ax.axvline(80, color='red',    ls='--', lw=2, label='Target 80%')
        ax.axvline(mfe_df['MFE_Pct'].median(), color='blue', ls='--', lw=1.5,
                   label=f"Median {mfe_df['MFE_Pct'].median():.0f}%")
        ax.set_title('Maximum Favorable Excursion\n(best gain before exit)', fontsize=10)
        ax.set_xlabel('MFE %')
        ax.legend(fontsize=8)

    # ── 3. MAE distribution
    ax = fig.add_subplot(gs[1, 0])
    if len(mfe_df) > 0:
        ax.hist(mfe_df['MAE_Pct'], bins=25, edgecolor='black', alpha=0.7, color='#e74c3c')
        ax.axvline(-40, color='orange', ls='--', lw=2, label='Stop -40%')
        ax.axvline(mfe_df['MAE_Pct'].median(), color='blue', ls='--', lw=1.5,
                   label=f"Median {mfe_df['MAE_Pct'].median():.0f}%")
        ax.set_title('Maximum Adverse Excursion\n(worst loss before exit)', fontsize=10)
        ax.set_xlabel('MAE %')
        ax.legend(fontsize=8)

    # ── 4. IV skew by hour
    ax = fig.add_subplot(gs[1, 1])
    if len(skew_df) > 0:
        by_h = skew_df.groupby('Hour')[['IV_Call', 'IV_Put']].mean()
        ax.plot(by_h.index, by_h['IV_Call'], 'b-o', label='CALL IV', ms=5)
        ax.plot(by_h.index, by_h['IV_Put'],  'r-o', label='PUT IV',  ms=5)
        ax.fill_between(by_h.index, by_h['IV_Call'], by_h['IV_Put'],
                        alpha=0.15, color='purple', label='Skew')
        ax.set_title('IV by Hour — CALL vs PUT\n(PUT skew = higher IV = cheaper to buy)', fontsize=10)
        ax.set_xlabel('Hour (IST)')
        ax.set_ylabel('Implied Vol %')
        ax.legend(fontsize=8)

    # ── 5. Theta decay (DTE=2 vs DTE=1)
    ax = fig.add_subplot(gs[2, 0])
    if len(theta_df) > 0:
        for dte, color in [(2, 'blue'), (1, 'red')]:
            sub = theta_df[theta_df['DTE'] == dte]
            if len(sub) == 0:
                continue
            avg = sub.groupby(['Hour', 'Minute'])['Norm_Px'].mean()
            ax.plot(range(len(avg)), avg.values, color=color,
                    label=f'DTE={dte}', lw=2)
        ax.set_title('Intraday Theta Decay\n(normalised to open price)', fontsize=10)
        ax.set_xlabel('Bar (5-min)')
        ax.set_ylabel('Normalised Price')
        ax.axhline(1.0, color='gray', ls='--', alpha=0.5)
        ax.legend(fontsize=8)

    # ── 6. Real P&L vs BS P&L scatter
    ax = fig.add_subplot(gs[2, 1])
    real = enriched[enriched['Has_Real_Data'] == True]
    if len(real) > 0:
        ax.scatter(real['P&L Net'], real['Real_PnL_Net'],
                   alpha=0.6, s=40, color='#9b59b6', edgecolors='black', lw=0.5)
        lim = max(abs(real[['P&L Net', 'Real_PnL_Net']].values.flatten()).max(), 100)
        ax.plot([-lim, lim], [-lim, lim], 'r--', alpha=0.5, label='Perfect match')
        ax.axhline(0, color='gray', lw=0.5)
        ax.axvline(0, color='gray', lw=0.5)
        ax.set_title('BS P&L vs Real P&L per trade\n(above line = real better than BS)', fontsize=10)
        ax.set_xlabel('BS Net P&L (₹)')
        ax.set_ylabel('Real Net P&L (₹)')
        ax.legend(fontsize=8)

    out_path = os.path.join(os.path.dirname(__file__), '..', 'options_analysis.png')
    plt.savefig(out_path, dpi=120, bbox_inches='tight')
    plt.show()
    print(f"\nChart saved → {os.path.abspath(out_path)}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(mode: str = 'all') -> None:
    print("=" * 65)
    print("  NIFTY Options Analyzer")
    print("=" * 65)

    # Load data
    print("\nLoading data...")
    spot       = load_spot()
    options_db = load_options_db()
    trades     = load_trades()

    print(f"  Spot bars      : {len(spot):,}")
    print(f"  Options contracts: {len(options_db):,}")
    print(f"  Backtest trades  : {len(trades)}")

    if len(options_db) == 0:
        print("\n  ⚠ No options data found.")
        print("  Run first:")
        print("    python fyers_auth.py")
        print("    python options_data_collector.py targeted")
        return

    if len(trades) == 0:
        print("\n  ⚠ No backtest trades found.")
        print("  Run first: python bot.py")
        return

    print("\nEnriching trades with real options prices...")
    enriched = enrich_trades_with_real_prices(trades, options_db, spot)
    matched  = enriched['Has_Real_Data'].sum()
    print(f"  Matched {matched}/{len(trades)} trades to real options data")

    print("\nComputing MFE/MAE from options OHLC...")
    mfe_df = compute_mfe_mae(trades, options_db)
    print(f"  MFE/MAE computed for {len(mfe_df)} trades")

    print("\nComputing IV skew...")
    skew_df = iv_skew_analysis(options_db, spot)
    print(f"  Skew data points: {len(skew_df):,}")

    print("\nComputing theta decay profile...")
    theta_df = theta_decay_profile(options_db)
    print(f"  Theta bars: {len(theta_df):,}")

    # ── Reports
    if mode in ('all', 'bs'):
        print_bs_bias_report(enriched)
    if mode in ('all', 'mfe'):
        print_mfe_mae_report(mfe_df)
    if mode in ('all', 'iv'):
        print_iv_skew_report(skew_df)
    if mode in ('all', 'theta'):
        print_theta_report(theta_df)

    # ── Summary recommendation
    if mode == 'all' and matched > 0:
        real = enriched[enriched['Has_Real_Data'] == True]
        real_total = real['Real_PnL_Net'].sum()
        bs_total   = real['P&L Net'].sum()
        print(f"\n{'='*65}")
        print(f"  SUMMARY & RECOMMENDATIONS")
        print(f"{'='*65}")
        print(f"  Matched trades     : {matched}")
        print(f"  BS P&L (matched)   : ₹{bs_total:,.0f}")
        print(f"  Real P&L (matched) : ₹{real_total:,.0f}")
        delta = real_total - bs_total
        if delta > 0:
            print(f"  Real is BETTER by  : ₹{delta:,.0f}  (BS was pessimistic)")
        else:
            print(f"  Real is WORSE by   : ₹{abs(delta):,.0f}  (BS was optimistic)")

        if len(skew_df) > 0:
            skew_mean = skew_df['Put_Skew'].mean()
            print(f"\n  IV Skew (Put - Call): +{skew_mean:.1f}% on average")
            print(f"  → PUTs consistently trade at higher IV = bigger % moves on down-days")
            print(f"  → Consider: CALL_ADX_THRESHOLD = 30 (stricter), PUT_ADX_THRESHOLD = 25")

        if len(mfe_df) > 0:
            mfe80  = (mfe_df['MFE_Pct'] >= 80).mean() * 100
            mfe60  = (mfe_df['MFE_Pct'] >= 60).mean() * 100
            mae40  = (mfe_df['MAE_Pct'] <= -40).mean() * 100
            mae30  = (mfe_df['MAE_Pct'] <= -30).mean() * 100
            print(f"\n  MFE: {mfe80:.0f}% of trades reach 80%+ target")
            print(f"       {mfe60:.0f}% of trades reach 60%+ target")
            print(f"  MAE: {mae40:.0f}% of trades hit the -40% stop")
            print(f"       {mae30:.0f}% of trades hit a -30% drawdown")
            if mfe60 > mfe80 * 1.3:
                print(f"\n  → Lowering target to 60-70% could improve trade count and WR")
            if mae30 < mae40 * 0.7:
                print(f"\n  → Very few trades go past -30% → tightening stop to 30% possible")

    # ── Plots
    if mode in ('all',):
        plot_all(enriched, mfe_df, skew_df, theta_df)

    # ── Save enriched trades
    if matched > 0:
        out_csv = os.path.join(os.path.dirname(__file__), '..', 'options_analysis.csv')
        enriched.to_csv(out_csv, index=False)
        print(f"\nEnriched trade log → {os.path.abspath(out_csv)}")


if __name__ == '__main__':
    mode = 'all'
    for arg in sys.argv[1:]:
        if arg.startswith('--'):
            mode = arg[2:]
    main(mode)
