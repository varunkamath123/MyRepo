"""Quick vT tuned backtest: Wed+Fri only, 09:30-09:45 entry, no force-close."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import pandas as pd
from pathlib import Path
from scipy.stats import norm
from ta.trend import ADXIndicator
import config

BARS_PER_DAY = 75

def bs_price(opt_type, S, K, T, r, sigma):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(S-K,0) if opt_type=='CALL' else max(K-S,0)
    d1 = (np.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*np.sqrt(T))
    d2 = d1 - sigma*np.sqrt(T)
    if opt_type == 'CALL':
        return S*norm.cdf(d1)-K*np.exp(-r*T)*norm.cdf(d2)
    return K*np.exp(-r*T)*norm.cdf(-d2)-S*norm.cdf(-d1)

def round_trip(entry, exit_p, lot):
    bv=entry*lot; sv=exit_p*lot
    br=config.BROKERAGE_PER_ORDER*2
    ex=(bv+sv)*config.NSE_EXCHANGE_CHARGE_RATE
    se=(bv+sv)*config.SEBI_CHARGES_RATE
    gt=(br+ex+se)*config.GST_RATE
    st=sv*config.STT_RATE; sd=bv*config.STAMP_DUTY_RATE
    return br+ex+se+gt+st+sd

def load(instrument):
    folder = {'NIFTY':'nifty_5min','BANKNIFTY':'banknifty_5min','SENSEX':'sensex_5min'}[instrument]
    data_dir = Path(__file__).parent.parent / 'data' / folder
    frames = []
    for f in sorted(data_dir.glob('*.csv')):
        df = pd.read_csv(f)
        if 'ts' not in df.columns and 'Datetime' in df.columns:
            df = df.rename(columns={'Datetime':'ts'})
        frames.append(df)
    all_df = pd.concat(frames, ignore_index=True)
    all_df['ts'] = pd.to_datetime(all_df['ts'], utc=True).dt.tz_convert('Asia/Kolkata')
    all_df = all_df.sort_values('ts').set_index('ts')
    adx = ADXIndicator(all_df['High'], all_df['Low'], all_df['Close'], 14)
    all_df['ADX'] = adx.adx()
    all_df['_date'] = all_df.index.date
    all_df['_tp'] = (all_df['High']+all_df['Low']+all_df['Close'])/3
    all_df['_cv'] = all_df.groupby('_date')['Volume'].cumsum()
    all_df['_ctpv'] = (all_df.groupby('_date')
                       .apply(lambda g: (g['_tp']*g['Volume']).cumsum())
                       .reset_index(level=0, drop=True))
    m = all_df['_cv'] > 0
    all_df['VWAP'] = np.where(m, all_df['_ctpv']/all_df['_cv'], all_df['_tp'])
    all_df['Returns'] = all_df['Close'].pct_change()
    all_df['HV'] = (all_df['Returns'].rolling(30).std() * np.sqrt(252*BARS_PER_DAY)
                    ).bfill().fillna(0.18)
    return all_df.drop(columns=['_date','_tp','_cv','_ctpv'])

results = []

for instrument in ['NIFTY','BANKNIFTY','SENSEX']:
    inst_cfg = config.INSTRUMENTS[instrument]
    lot = inst_cfg['lot_size']
    gap = inst_cfg['strike_gap']
    df_full = load(instrument)
    print(f'Loaded {instrument}: {len(set(df_full.index.date))} days')

    TRADE_DAYS  = {'Wed','Fri'}
    OR_BARS     = 3    # bars 0-2 = 09:15, 09:20, 09:25
    ENTRY_START = 3    # bar 3 = 09:30
    ENTRY_END   = 7    # bars 3,4,5,6 = 09:30-09:45 (stop trying at bar 7 = 09:50)
    ADX_MIN     = config.EARLY_SESSION_ADX_MIN
    STOP        = config.EARLY_SESSION_STOP
    TARGET      = config.EARLY_SESSION_TARGET
    T_ACT       = config.EARLY_SESSION_TRAIL_ACT
    T_DIST      = config.EARLY_SESSION_TRAIL_DIST
    BUF         = config.EARLY_SESSION_ORB_BUFFER
    DTE         = config.EARLY_SESSION_DAYS_TO_EXP

    trades = []
    for day, ddf in df_full.groupby(df_full.index.date):
        ddf = ddf.sort_index()
        if len(ddf) < ENTRY_END + 1:
            continue
        wd = ddf.index[0].strftime('%a')
        if wd not in TRADE_DAYS:
            continue

        or_high = float(ddf.iloc[:OR_BARS]['High'].max())
        or_low  = float(ddf.iloc[:OR_BARS]['Low'].min())
        call_t  = or_high * (1 + BUF)
        put_t   = or_low  * (1 - BUF)

        signal = None
        for i in range(ENTRY_START, ENTRY_END):
            if i >= len(ddf):
                break
            row   = ddf.iloc[i]
            close = float(row['Close'])
            adx   = float(row.get('ADX', 0))
            vwap  = float(row.get('VWAP', float('nan')))
            if np.isnan(adx) or adx < ADX_MIN:
                continue
            if close > call_t and (np.isnan(vwap) or close > vwap):
                signal = {'type':'CALL','price':close,'adx':adx,'bar_i':i}
                break
            elif close < put_t and (np.isnan(vwap) or close < vwap):
                signal = {'type':'PUT','price':close,'adx':adx,'bar_i':i}
                break

        if signal is None:
            continue

        hv_e       = float(ddf.iloc[signal['bar_i']].get('HV', 0.18))
        underlying = signal['price']
        strike     = int(round(underlying / gap) * gap)
        entry_px   = bs_price(signal['type'], underlying, strike,
                              DTE/365, config.RISK_FREE_RATE, hv_e)
        if entry_px < config.MIN_OPTION_PRICE:
            continue

        # No force-close — full day scan for SL / Target / Trail only
        highest     = 0.0
        exit_reason = None
        exit_px     = entry_px
        exit_bar    = signal['bar_i']

        for j in range(signal['bar_i']+1, len(ddf)):
            row     = ddf.iloc[j]
            spot    = float(row['Close'])
            hv_n    = float(row.get('HV', hv_e))
            elapsed = (j - signal['bar_i']) * 5   # minutes
            T_rem   = max(DTE - elapsed/(24*60), 0.001) / 365
            cur_px  = bs_price(signal['type'], spot, strike, T_rem,
                               config.RISK_FREE_RATE, hv_n)
            pnl_pct = (cur_px - entry_px) / entry_px
            if pnl_pct > highest:
                highest = pnl_pct
            if pnl_pct <= -STOP:
                exit_reason = 'Stop-Loss'; exit_px = cur_px; exit_bar = j; break
            elif pnl_pct >= TARGET:
                exit_reason = 'Target';    exit_px = cur_px; exit_bar = j; break
            elif highest >= T_ACT and pnl_pct < highest - T_DIST:
                exit_reason = 'Trail';     exit_px = cur_px; exit_bar = j; break

        if exit_reason is None:
            j       = len(ddf) - 1
            row     = ddf.iloc[j]
            spot    = float(row['Close'])
            hv_n    = float(row.get('HV', hv_e))
            elapsed = (j - signal['bar_i']) * 5
            T_rem   = max(DTE - elapsed/(24*60), 0.001) / 365
            exit_px = bs_price(signal['type'], spot, strike, T_rem,
                               config.RISK_FREE_RATE, hv_n)
            exit_reason = 'EOD'; exit_bar = j

        c   = round_trip(entry_px, exit_px, lot)
        pnl = (exit_px - entry_px) * lot - c

        trades.append({
            'instrument' : instrument,
            'date'       : day,
            'weekday'    : wd,
            'type'       : signal['type'],
            'entry_time' : ddf.index[signal['bar_i']].strftime('%H:%M'),
            'exit_time'  : ddf.index[exit_bar].strftime('%H:%M'),
            'adx'        : round(signal['adx'], 1),
            'entry_px'   : round(entry_px, 2),
            'exit_px'    : round(exit_px, 2),
            'pnl_net'    : round(pnl, 2),
            'exit_reason': exit_reason,
        })

    df = pd.DataFrame(trades)
    if df.empty:
        print(f'{instrument}: no trades'); continue

    n    = len(df)
    wins = (df['pnl_net'] > 0).sum()
    wr   = wins / n * 100
    net  = df['pnl_net'].sum()
    avg_w = df.loc[df['pnl_net']>0,'pnl_net'].mean() if wins else 0
    avg_l = df.loc[df['pnl_net']<=0,'pnl_net'].mean() if n-wins else 0
    cum   = df['pnl_net'].cumsum(); peak = cum.cummax()
    dd    = ((cum - peak) / config.EARLY_SESSION_CAPITAL * 100).min()
    sharpe = (df['pnl_net'].mean() / df['pnl_net'].std() * np.sqrt(252)
              if df['pnl_net'].std() > 0 else 0)

    print(f'\n{"="*60}')
    print(f'  {instrument} — vT (Wed+Fri | 09:30-09:45 entry | no force-close)')
    print(f'{"="*60}')
    print(f'  {n} trades | WR {wr:.1f}% | Net Rs.{net:+,.0f} | Sharpe {sharpe:.2f} | DD {dd:.1f}%')
    print(f'  Avg Win Rs.{avg_w:+,.0f} | Avg Loss Rs.{avg_l:+,.0f}')
    for sig_type in ['CALL','PUT']:
        sub = df[df['type']==sig_type]; w2 = (sub['pnl_net']>0).sum()
        if not sub.empty:
            print(f'    {sig_type}: {len(sub)} | WR {w2/len(sub)*100:.1f}% | Rs.{sub["pnl_net"].sum():+,.0f}')
    for r, g in df.groupby('exit_reason'):
        gw = (g['pnl_net']>0).sum()
        print(f'    [{r}]: {len(g)} trades | WR {gw/len(g)*100:.1f}% | Rs.{g["pnl_net"].sum():+,.0f}')
    results.extend(trades)

df_all = pd.DataFrame(results)
n    = len(df_all)
wins = (df_all['pnl_net'] > 0).sum()
net  = df_all['pnl_net'].sum()
cap  = config.EARLY_SESSION_CAPITAL * 3

print(f'\n{"="*60}')
print(f'  COMBINED vT — All 3 Instruments')
print(f'{"="*60}')
print(f'  {n} trades | WR {wins/n*100:.1f}% | Net Rs.{net:+,.0f} | Return {net/cap*100:.1f}%')
for inst, g in df_all.groupby('instrument'):
    gw=(g['pnl_net']>0).sum()
    print(f'    {inst}: {len(g)} | WR {gw/len(g)*100:.1f}% | Rs.{g["pnl_net"].sum():+,.0f}')
