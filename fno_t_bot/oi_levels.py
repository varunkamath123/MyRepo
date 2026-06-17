# -*- coding: utf-8 -*-
"""
OI Levels — Open Interest Support & Resistance Scanner
=======================================================
Fetches option chain for NIFTY + BANKNIFTY + SENSEX via Fyers API
(same data feed as NSE/BSE, no browser needed — uses daily token).

Identifies OI-based support + resistance walls, max pain, PCR, and
flags which levels the spot price is nearest to or has recently broken.

How OI walls work as S/R
-------------------------
  Call OI wall  → resistance: call writers need price to stay BELOW
  Put  OI wall  → support   : put writers need price to stay ABOVE

  When price breaks through a wall with ADX momentum:
  → Option writers are forced to delta-hedge rapidly
  → This "gamma squeeze" ACCELERATES the breakout move
  → Exactly the kind of directional velocity our EMA entries ride

Integration with vX bot
------------------------
  1. Pre-market run: find top walls for each instrument
  2. Breakout entry: EMA signal fires + price just crossed a wall → 2 lots
  3. Wall sitting: price is AT a wall, not broken → 1 lot (cautious)
  4. Range-bound: spot between two tight walls (< 1%) → skip entry

Usage
-----
  python oi_levels.py               # all 3 instruments
  python oi_levels.py NIFTY         # single instrument
  python oi_levels.py NIFTY --next  # next weekly expiry (vs nearest)
  python oi_levels.py --monitor     # refresh every 5 min
"""

from __future__ import annotations

import sys, os, time, json, math, datetime as dt, warnings
from pathlib import Path

import pandas as pd
import numpy as np
warnings.filterwarnings('ignore')

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# ── Path setup ────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
import config

TOP_N       = 5      # top N call / put OI levels to show
NEAR_PCT    = 0.003  # "near" when within 0.3% of spot
BREAK_PCT   = 0.001  # "just broken" when crossed within 0.1%
REFRESH_SEC = 300    # monitor mode refresh interval


# ── Fyers connection ──────────────────────────────────────────────────────────

def _get_fyers():
    """Load saved Fyers token and return a live fyers client."""
    token_file = os.path.join(config.LOG_DIRECTORY, 'token.txt')
    if not os.path.exists(token_file):
        raise FileNotFoundError(
            f"Token file not found: {token_file}\n"
            "Run: python fyers_direct_auth.py  (or fyers_auth.py locally)"
        )
    with open(token_file) as f:
        lines = f.read().strip().split('\n')
    if len(lines) < 2:
        raise ValueError("Token file is malformed (need token + date on 2 lines)")

    import pytz
    from fyers_auth import FyersAuth
    IST = pytz.timezone('Asia/Kolkata')
    try:
        token_date = dt.datetime.fromisoformat(lines[1]).date()
    except ValueError:
        token_date = None

    today = dt.datetime.now(IST).date()
    if token_date != today:
        raise RuntimeError(
            f"Token is from {token_date}, today is {today}.\n"
            "Re-run authentication: python fyers_direct_auth.py"
        )

    auth = FyersAuth()
    auth.access_token = lines[0]
    fyers = auth.get_fyers_client()
    return fyers


# ── Option chain fetch ────────────────────────────────────────────────────────

def fetch_chain(instrument: str, fyers, strike_count: int = 20,
                expiry_index: int = 0) -> dict | None:
    """
    Fetch option chain from Fyers for NIFTY / BANKNIFTY / SENSEX.
    Returns dict with spot, expiry, pcr, max_pain, df (per-strike OI).

    Parameters
    ----------
    instrument   : 'NIFTY', 'BANKNIFTY', or 'SENSEX'
    fyers        : fyers_apiv3 client (authenticated)
    strike_count : number of strikes either side of ATM
    expiry_index : 0 = nearest, 1 = next weekly, etc.
    """
    inst_cfg = config.INSTRUMENTS.get(instrument)
    if inst_cfg is None:
        print(f"  [ERROR] Unknown instrument: {instrument}")
        return None

    # Fyers option_prefix: 'NSE:NIFTY', 'NSE:BANKNIFTY', 'BSE:SENSEX'
    prefix = inst_cfg.get('option_prefix', f'NSE:{instrument}')

    resp = fyers.optionchain({
        "symbol"     : prefix,
        "strikecount": strike_count,
        "timestamp"  : "",
    })

    if resp.get('s') != 'ok':
        print(f"  [ERROR] Fyers option chain for {instrument}: "
              f"status={resp.get('s')} msg={resp.get('message', resp)}")
        return None

    data      = resp.get('data', {})
    opt_chain = data.get('optionChain', [])
    expiryData= data.get('expiryData', [])

    if not opt_chain:
        print(f"  [ERROR] Empty option chain for {instrument}")
        return None

    # Spot price
    spot = data.get('underlyingValue') or data.get('ltp')
    # Sometimes it's inside each row
    if not spot and opt_chain:
        spot = opt_chain[0].get('underlyingValue', float('nan'))
    spot = float(spot or 0)

    # Available expiries
    expiries = sorted(set(r['expiryDate'] for r in opt_chain))
    if not expiries:
        print(f"  [ERROR] No expiry dates in response for {instrument}")
        return None

    chosen_expiry = expiries[min(expiry_index, len(expiries) - 1)]

    # Parse per-strike OI
    rows = {}
    for r in opt_chain:
        if r.get('expiryDate') != chosen_expiry:
            continue
        k  = float(r.get('strikePrice', 0))
        ot = r.get('option_type', r.get('optionType', ''))
        is_ce = (ot in ('CE', 'CALL') or 'CE' in str(ot))
        is_pe = (ot in ('PE', 'PUT')  or 'PE' in str(ot))

        rows.setdefault(k, {
            'strike': k,
            'call_oi': 0, 'call_oi_chg': 0, 'call_vol': 0, 'call_iv': 0, 'call_ltp': 0,
            'put_oi' : 0, 'put_oi_chg' : 0, 'put_vol' : 0, 'put_iv' : 0, 'put_ltp' : 0,
        })
        oi      = float(r.get('oi',        r.get('openInterest',         0)) or 0)
        oi_chg  = float(r.get('oiChange',  r.get('changeinOpenInterest', 0)) or 0)
        vol     = float(r.get('volume',    r.get('totalTradedVolume',     0)) or 0)
        iv      = float(r.get('iv',        r.get('impliedVolatility',     0)) or 0)
        ltp     = float(r.get('ltp',       r.get('lastPrice',             0)) or 0)

        if is_ce:
            rows[k].update(call_oi=oi, call_oi_chg=oi_chg,
                           call_vol=vol, call_iv=iv, call_ltp=ltp)
        elif is_pe:
            rows[k].update(put_oi=oi, put_oi_chg=oi_chg,
                           put_vol=vol, put_iv=iv, put_ltp=ltp)

    if not rows:
        print(f"  [ERROR] No strikes parsed for {instrument} expiry {chosen_expiry}")
        return None

    df = (pd.DataFrame(list(rows.values()))
            .sort_values('strike')
            .reset_index(drop=True))

    total_call_oi = df['call_oi'].sum()
    total_put_oi  = df['put_oi'].sum()
    pcr           = round(total_put_oi / total_call_oi, 3) if total_call_oi else float('nan')

    # Max Pain: strike where total notional loss to option buyers is maximum
    # = the strike option writers are most incentivised to pin price toward
    mp_scores = {}
    for k in df['strike'].values:
        call_loss = sum(
            oi * (s - k)
            for s, oi in zip(df['strike'], df['call_oi'])
            if s > k
        )
        put_loss = sum(
            oi * (k - s)
            for s, oi in zip(df['strike'], df['put_oi'])
            if s < k
        )
        mp_scores[k] = call_loss + put_loss
    max_pain = min(mp_scores, key=mp_scores.get) if mp_scores else spot

    return {
        'instrument'    : instrument,
        'spot'          : spot,
        'expiry'        : chosen_expiry,
        'all_expiries'  : expiries,
        'pcr'           : pcr,
        'max_pain'      : max_pain,
        'total_call_oi' : total_call_oi,
        'total_put_oi'  : total_put_oi,
        'df'            : df,
    }


# ── Analysis ──────────────────────────────────────────────────────────────────

def identify_levels(chain: dict, top_n: int = TOP_N) -> dict:
    """Identify top support (put OI) and resistance (call OI) levels."""
    df   = chain['df']
    spot = chain['spot']

    resistance = (df.nlargest(top_n, 'call_oi')
                    [['strike','call_oi','call_oi_chg','call_iv']]
                    .sort_values('strike').reset_index(drop=True))

    support    = (df.nlargest(top_n, 'put_oi')
                    [['strike','put_oi','put_oi_chg','put_iv']]
                    .sort_values('strike', ascending=False).reset_index(drop=True))

    fresh_call = df.nlargest(3, 'call_oi_chg')[['strike','call_oi_chg']].reset_index(drop=True)
    fresh_put  = df.nlargest(3, 'put_oi_chg')[['strike','put_oi_chg']].reset_index(drop=True)

    return {
        'resistance': resistance,
        'support'   : support,
        'fresh_call': fresh_call,
        'fresh_put' : fresh_put,
        'spot'      : spot,
    }


def _status(strike: float, spot: float) -> str:
    dist = abs(strike - spot) / spot
    if dist < BREAK_PCT:
        return '⚡ AT SPOT'
    if dist < NEAR_PCT:
        return '🎯 NEAR'
    return ''


# ── Display ───────────────────────────────────────────────────────────────────

def print_chain(chain: dict, analysis: dict):
    sym     = chain['instrument']
    spot    = chain['spot']
    expiry  = chain['expiry']
    pcr     = chain['pcr']
    mp      = chain['max_pain']
    res     = analysis['resistance']
    sup     = analysis['support']
    fc      = analysis['fresh_call']
    fp      = analysis['fresh_put']
    all_exp = chain.get('all_expiries', [expiry])
    ts      = dt.datetime.now().strftime('%H:%M:%S')

    print(f"\n{'='*74}")
    print(f"  {sym}  ─  OI Support & Resistance  [{ts}]")
    print(f"  Spot: {spot:>10,.2f}  │  Expiry: {expiry}  │  PCR: {pcr}  │  Max Pain: {mp:,.0f}")
    print(f"  All expiries: {', '.join(all_exp[:5])}")
    print(f"{'='*74}")

    # ── Resistance ──
    print(f"\n  🔴 RESISTANCE  (Call OI walls — writers need price BELOW these)")
    print(f"  {'Strike':>9}  {'Call OI':>10}  {'OI Δ today':>12}  {'IV':>6}  {'vs Spot':>8}  Note")
    print(f"  {'─'*68}")
    for _, r in res.sort_values('strike', ascending=False).iterrows():
        k       = r['strike']
        oi      = int(r['call_oi'])
        oic     = int(r['call_oi_chg'])
        iv      = r['call_iv']
        dist    = (k - spot) / spot * 100
        note    = _status(k, spot)
        pct_tot = oi / chain['total_call_oi'] * 100 if chain['total_call_oi'] else 0
        wall    = ' ◀ MAJOR WALL' if pct_tot > 15 else (' ◀ WALL' if pct_tot > 8 else '')
        chg_s   = f"+{oic:,}" if oic > 0 else str(f"{oic:,}")
        print(f"  {k:>9,.0f}  {oi:>10,}  {chg_s:>12}  {iv:>5.1f}%  {dist:>+7.2f}%  {note}{wall}")

    # ── Spot ──
    print(f"\n  {'─'*12} SPOT  {spot:,.2f} {'─'*12}\n")

    # ── Support ──
    print(f"  🟢 SUPPORT  (Put OI walls — writers need price ABOVE these)")
    print(f"  {'Strike':>9}  {'Put  OI':>10}  {'OI Δ today':>12}  {'IV':>6}  {'vs Spot':>8}  Note")
    print(f"  {'─'*68}")
    for _, r in sup.sort_values('strike', ascending=False).iterrows():
        k       = r['strike']
        oi      = int(r['put_oi'])
        oic     = int(r['put_oi_chg'])
        iv      = r['put_iv']
        dist    = (k - spot) / spot * 100
        note    = _status(k, spot)
        pct_tot = oi / chain['total_put_oi'] * 100 if chain['total_put_oi'] else 0
        wall    = ' ◀ MAJOR WALL' if pct_tot > 15 else (' ◀ WALL' if pct_tot > 8 else '')
        chg_s   = f"+{oic:,}" if oic > 0 else str(f"{oic:,}")
        print(f"  {k:>9,.0f}  {oi:>10,}  {chg_s:>12}  {iv:>5.1f}%  {dist:>+7.2f}%  {note}{wall}")

    # ── Fresh OI build ──
    print(f"\n  📈 TODAY'S FRESH OI BUILD (new positions since open)")
    for _, r in fc.iterrows():
        if r['call_oi_chg'] > 0:
            print(f"     CALL {r['strike']:>9,.0f}  +{int(r['call_oi_chg']):,}  ← fresh resistance being built")
    for _, r in fp.iterrows():
        if r['put_oi_chg'] > 0:
            print(f"     PUT  {r['strike']:>9,.0f}  +{int(r['put_oi_chg']):,}  ← fresh support being built")

    # ── ASCII ladder ──
    _print_ladder(spot, res, sup, mp, chain)

    # ── Signals ──
    _print_signals(spot, res, sup, mp, pcr, chain)


def _print_ladder(spot, res, sup, max_pain, chain):
    levels = {}
    for _, r in res.iterrows():
        levels[r['strike']] = ('R', int(r['call_oi']), chain['total_call_oi'])
    for _, r in sup.iterrows():
        k = r['strike']
        if k not in levels:
            levels[k] = ('S', int(r['put_oi']), chain['total_put_oi'])
    if max_pain not in levels:
        levels[max_pain] = ('MP', 0, 1)

    all_k  = sorted(levels.keys(), reverse=True)
    max_oi = max((v[1] for v in levels.values()), default=1) or 1
    BAR_W  = 18

    print(f"\n  ── Price Ladder ─────────────────────────────────────────────")
    print(f"  {'Strike':>9}   {'OI bar':<{BAR_W}}  Type   Label")
    print(f"  {'─'*55}")

    spot_done = False
    for k in all_k:
        if not spot_done and k < spot:
            spot_done = True
            arrow = '──▶' if spot > max_pain else '──▶'
            print(f"  {'':>9}   {'◆ SPOT ' + f'{spot:,.0f}':^{BAR_W}}  {'─'*6}")

        ltype, oi, total = levels[k]
        bar   = '█' * int(oi / max_oi * BAR_W)
        pct   = oi / total * 100 if total else 0
        label = (f"CALL {oi:>8,}" if ltype == 'R' else
                 f"PUT  {oi:>8,}" if ltype == 'S' else 'MAX PAIN')
        icon  = '🔴' if ltype == 'R' else '🟢' if ltype == 'S' else '🟡'
        near  = ' ← NEAR' if abs(k - spot) / spot < NEAR_PCT else ''
        print(f"  {icon}{k:>8,.0f}   {bar:<{BAR_W}}  {pct:4.1f}%  {label}{near}")

    if not spot_done:
        print(f"  {'':>9}   {'◆ SPOT ' + f'{spot:,.0f}':^{BAR_W}}")
    print()


def _print_signals(spot, res, sup, max_pain, pcr, chain):
    print(f"  ── Signal Interpretation ────────────────────────────────────")

    res_above = res[res['strike'] > spot].sort_values('strike')
    sup_below = sup[sup['strike'] < spot].sort_values('strike', ascending=False)

    nr = res_above.iloc[0] if not res_above.empty else None
    ns = sup_below.iloc[0] if not sup_below.empty else None

    if nr is not None:
        dist   = (nr['strike'] - spot) / spot * 100
        oi     = int(nr['call_oi'])
        pct_w  = oi / chain['total_call_oi'] * 100 if chain['total_call_oi'] else 0
        strong = '🔴 MAJOR WALL' if pct_w > 15 else '🟠 WALL'
        print(f"  Next resistance : {nr['strike']:>9,.0f}  (+{dist:.2f}%)  "
              f"OI={oi:,} ({pct_w:.1f}% of total)  {strong}")
        if dist < 0.4:
            print(f"  ⚡ Price within 0.4% of resistance — breakout risk HIGH")
            print(f"     CALL signal + cross above {nr['strike']:,.0f} → high-conviction long")

    if ns is not None:
        dist   = (spot - ns['strike']) / spot * 100
        oi     = int(ns['put_oi'])
        pct_w  = oi / chain['total_put_oi'] * 100 if chain['total_put_oi'] else 0
        strong = '🟢 MAJOR WALL' if pct_w > 15 else '🟩 WALL'
        print(f"  Next support    : {ns['strike']:>9,.0f}  (-{dist:.2f}%)  "
              f"OI={oi:,} ({pct_w:.1f}% of total)  {strong}")
        if dist < 0.4:
            print(f"  ⚡ Price within 0.4% of support — breakdown risk HIGH")
            print(f"     PUT signal + break below {ns['strike']:,.0f} → high-conviction short")

    # Max pain gravity
    mp_dist = spot - max_pain
    if abs(mp_dist) / spot < 0.005:
        print(f"  Max pain {max_pain:,.0f}: very close to spot — expect pinning / chop")
    elif spot > max_pain:
        print(f"  Max pain {max_pain:,.0f}: spot is {mp_dist:,.0f}pts ABOVE → "
              f"gravity pulls DOWN (avoid CALL entries near expiry close)")
    else:
        print(f"  Max pain {max_pain:,.0f}: spot is {abs(mp_dist):,.0f}pts BELOW → "
              f"gravity pulls UP (avoid PUT entries near expiry close)")

    # PCR
    if   pcr > 1.4: pcr_note = "heavy put writing → BULLISH bias (market expects to hold)"
    elif pcr > 1.1: pcr_note = "mild put writing → mild bullish tilt"
    elif pcr < 0.7: pcr_note = "heavy call writing → BEARISH bias (market expects to fall)"
    elif pcr < 0.9: pcr_note = "mild call writing → mild bearish tilt"
    else:           pcr_note = "balanced — no strong directional bias"
    print(f"  PCR {pcr}: {pcr_note}")

    # Effective range
    if nr is not None and ns is not None:
        r_pts = nr['strike'] - ns['strike']
        r_pct = r_pts / spot * 100
        print(f"\n  Effective range : {ns['strike']:,.0f} – {nr['strike']:,.0f}  "
              f"({r_pts:,.0f} pts  /  {r_pct:.2f}%)")
        if r_pct < 1.0:
            print(f"  ⚠️  TIGHT RANGE ({r_pct:.2f}%) — options strategies favour iron condors.")
            print(f"     Directional entries low conviction until a wall breaks.")
        elif r_pct > 2.5:
            print(f"  ✅ WIDE RANGE ({r_pct:.2f}%) — good trending conditions.")
            print(f"     EMA/ADX entries have room to run to the 130% target.")
        else:
            print(f"  📊 NORMAL RANGE ({r_pct:.2f}%) — standard trending conditions.")


# ── Main ──────────────────────────────────────────────────────────────────────

def run_once(instruments: list[str], expiry_index: int = 0):
    import nse_oi as _nse_oi

    # Fyers is only needed as fallback for SENSEX (BSE-listed)
    fyers = None
    if any(i not in _nse_oi._NSE_SUPPORTED for i in instruments):
        print(f"  Connecting to Fyers (SENSEX fallback) ...", end=' ')
        try:
            fyers = _get_fyers()
            print("OK")
        except Exception as e:
            print(f"SKIPPED ({e})")

    for inst in instruments:
        print(f"  Fetching {inst} option chain ...", end=' ', flush=True)

        if inst in _nse_oi._NSE_SUPPORTED:
            chain = _nse_oi.fetch_chain(inst, expiry_index=expiry_index, force=True)
        elif fyers is not None:
            chain = fetch_chain(inst, fyers, strike_count=25, expiry_index=expiry_index)
        else:
            print(f"SKIPPED (SENSEX requires Fyers token — run fyers_direct_auth.py)")
            continue

        if chain is None:
            print("FAILED")
            continue
        print(f"OK  spot={chain['spot']:,.2f}  expiry={chain['expiry']}  "
              f"strikes={len(chain['df'])}")
        analysis = identify_levels(chain)
        print_chain(chain, analysis)

    print(f"\n{'='*74}")
    print("  HOW TO USE WITH vX BOT")
    print(f"{'='*74}")
    print("""
  BREAKOUT ENTRY  (+1 conviction point)
    EMA/ADX signal fires AND price just crossed through a WALL strike
    → Use 2 lots. The wall break triggers forced delta-hedging which
      amplifies the move and makes the option more likely to hit target.

  WALL SITTING  (−1 conviction point)
    EMA/ADX signal fires BUT price is sitting RIGHT AT a major OI wall
    → Use 1 lot only. The wall will absorb initial momentum; option may
      not reach the 130% target before the wall kills the move.

  RANGE BOUND  (skip)
    Spot is between two walls that are < 1% apart
    → Skip entries. EMA will whipsaw inside the box. Wait for a break.

  MAX PAIN GRAVITY  (near-expiry awareness)
    Within 75 min of expiry close, price drifts toward max pain.
    Avoid entries AGAINST max pain direction in last hour of trading.

  FRESH OI BUILD  (real-time signal)
    If new OI is being added to a call/put wall intraday → that wall is
    getting stronger, not weaker. Breakout above fresh-build call OI
    is a stronger signal than breaking stale (unchanged) OI.
""")


def main():
    args     = [a for a in sys.argv[1:] if a != '']
    monitor  = '--monitor' in args;  args = [a for a in args if a != '--monitor']
    use_next = '--next'    in args;  args = [a for a in args if a != '--next']
    expiry_i = 1 if use_next else 0

    valid = {'NIFTY', 'BANKNIFTY', 'SENSEX'}
    instruments = [a.upper() for a in args if a.upper() in valid]
    if not instruments:
        instruments = ['NIFTY', 'BANKNIFTY']

    if monitor:
        print(f"  Monitor mode — refreshing every {REFRESH_SEC//60} min. Ctrl+C to stop.")
        while True:
            try:
                run_once(instruments, expiry_i)
                print(f"\n  Next refresh at "
                      f"{(dt.datetime.now() + dt.timedelta(seconds=REFRESH_SEC)).strftime('%H:%M:%S')}")
                time.sleep(REFRESH_SEC)
            except KeyboardInterrupt:
                print("\n  Stopped.")
                break
    else:
        run_once(instruments, expiry_i)


if __name__ == '__main__':
    main()
