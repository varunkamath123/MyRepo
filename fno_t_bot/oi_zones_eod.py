# -*- coding: utf-8 -*-
"""
OI Zones — End-of-Session Fetcher
===================================
Runs at 15:35 IST (after market close) to fetch option chain OI data,
compute support / resistance zones, and save them as JSON for the
upcoming trading session.

Both the live paper_bot.py and the eventual live bot load these files
at startup to context-weight their entry decisions.

Data saved to: <project_root>/data/oi_zones/
  {date}_{INSTRUMENT}.json   — dated archive
  latest_{INSTRUMENT}.json   — always points to most recent (overwritten)

Schedule (EC2 systemd timer): Mon–Fri 15:35 IST (10:05 UTC)

Usage
-----
  python oi_zones_eod.py                    # all instruments
  python oi_zones_eod.py NIFTY              # single instrument
  python oi_zones_eod.py NIFTY BANKNIFTY    # selected instruments
  python oi_zones_eod.py --dry-run          # print zones, don't save
"""

from __future__ import annotations

import sys, os, json, math, datetime as dt, warnings
from pathlib import Path

import pandas as pd
import numpy as np
warnings.filterwarnings('ignore')

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
import config

# fetch_chain: NSE public API (jugaad-data) for NIFTY/BANKNIFTY;
#              Fyers fallback for SENSEX (BSE-listed).
import nse_oi
from oi_levels import identify_levels, fetch_chain as _fyers_fetch_chain, _get_fyers

# ── Config ────────────────────────────────────────────────────────────────────

OI_ZONES_DIR = _HERE.parent / 'data' / 'oi_zones'
STRIKE_COUNT  = 30   # strikes either side of ATM to fetch
TOP_N         = 8    # top OI levels to store (more than display-only tool)

# Thresholds stored in JSON so oi_zones.py can read without re-deriving
MAJOR_WALL_PCT    = 12.0   # % of total OI → "major wall" (strongest S/R)
WALL_PCT          = 6.0    # % of total OI → "wall" (significant S/R)
FRESH_BUILD_RATIO = 1.3    # OI_change / avg_change — "fresh wall being built"

ALL_INSTRUMENTS = ['NIFTY', 'BANKNIFTY', 'SENSEX']


# ── Zone computation ──────────────────────────────────────────────────────────

def _level_strength(oi: float, total_oi: float) -> str:
    """Return 'MAJOR', 'WALL', or 'MINOR' based on OI share."""
    pct = oi / total_oi * 100 if total_oi else 0
    if pct >= MAJOR_WALL_PCT:
        return 'MAJOR'
    if pct >= WALL_PCT:
        return 'WALL'
    return 'MINOR'


def compute_zones(chain: dict) -> dict:
    """
    Distil a raw option chain dict (from fetch_chain) into a compact
    zone map suitable for real-time trading decisions.

    Returns
    -------
    dict with keys:
      instrument, date, fetched_at, spot, expiry, pcr, max_pain,
      resistance  : list of {strike, call_oi, pct_total, strength,
                              fresh_build, dist_pct}
      support     : list of {strike, put_oi, pct_total, strength,
                              fresh_build, dist_pct}
      effective_range : {lower, upper, pts, pct}
      pcr_bias    : 'BULLISH' | 'BEARISH' | 'NEUTRAL'
      range_type  : 'TIGHT' | 'NORMAL' | 'WIDE'
    """
    import pytz
    IST = pytz.timezone('Asia/Kolkata')
    now = dt.datetime.now(IST)

    df   = chain['df']
    spot = chain['spot']

    total_call = chain['total_call_oi']
    total_put  = chain['total_put_oi']

    # Compute average OI change to detect "fresh build"
    avg_call_chg = df['call_oi_chg'].clip(lower=0).mean() + 1
    avg_put_chg  = df['put_oi_chg'].clip(lower=0).mean()  + 1

    # ── Resistance levels (Call OI, sorted by OI desc) ─────────────────────
    res_df = (df.nlargest(TOP_N, 'call_oi')
                .assign(
                    pct_total   = lambda x: x['call_oi'] / total_call * 100 if total_call else 0,
                    fresh_build = lambda x: x['call_oi_chg'] / avg_call_chg >= FRESH_BUILD_RATIO,
                    dist_pct    = lambda x: (x['strike'] - spot) / spot * 100,
                ))

    resistance = []
    for _, r in res_df.iterrows():
        resistance.append({
            'strike'     : float(r['strike']),
            'call_oi'    : int(r['call_oi']),
            'pct_total'  : round(float(r['pct_total']), 2),
            'strength'   : _level_strength(r['call_oi'], total_call),
            'fresh_build': bool(r['fresh_build']),
            'dist_pct'   : round(float(r['dist_pct']), 3),
            'call_iv'    : round(float(r.get('call_iv', 0)), 2),
        })

    # ── Support levels (Put OI, sorted by OI desc) ─────────────────────────
    sup_df = (df.nlargest(TOP_N, 'put_oi')
                .assign(
                    pct_total   = lambda x: x['put_oi'] / total_put * 100 if total_put else 0,
                    fresh_build = lambda x: x['put_oi_chg'] / avg_put_chg >= FRESH_BUILD_RATIO,
                    dist_pct    = lambda x: (x['strike'] - spot) / spot * 100,
                ))

    support = []
    for _, r in sup_df.iterrows():
        support.append({
            'strike'    : float(r['strike']),
            'put_oi'    : int(r['put_oi']),
            'pct_total' : round(float(r['pct_total']), 2),
            'strength'  : _level_strength(r['put_oi'], total_put),
            'fresh_build': bool(r['fresh_build']),
            'dist_pct'  : round(float(r['dist_pct']), 3),
            'put_iv'    : round(float(r.get('put_iv', 0)), 2),
        })

    # ── Effective range: nearest resistance above / nearest support below ──
    res_above = [r for r in resistance if r['strike'] > spot]
    sup_below = [s for s in support    if s['strike'] < spot]

    res_above.sort(key=lambda x: x['strike'])
    sup_below.sort(key=lambda x: x['strike'], reverse=True)

    eff_upper = res_above[0]['strike'] if res_above else spot * 1.02
    eff_lower = sup_below[0]['strike'] if sup_below else spot * 0.98
    eff_pts   = eff_upper - eff_lower
    eff_pct   = eff_pts / spot * 100

    range_type = ('TIGHT'  if eff_pct < 0.8 else
                  'WIDE'   if eff_pct > 2.5 else 'NORMAL')

    # ── PCR bias ──────────────────────────────────────────────────────────
    pcr     = chain['pcr']
    pcr_bias = ('BULLISH' if pcr > 1.3 else
                'BEARISH' if pcr < 0.7 else 'NEUTRAL')

    return {
        'instrument'      : chain['instrument'],
        'date'            : now.strftime('%Y-%m-%d'),
        'fetched_at'      : now.strftime('%H:%M:%S'),
        'spot_at_fetch'   : round(spot, 2),
        'expiry'          : chain['expiry'],
        'pcr'             : round(pcr, 3),
        'pcr_bias'        : pcr_bias,
        'max_pain'        : float(chain['max_pain']),
        'total_call_oi'   : int(total_call),
        'total_put_oi'    : int(total_put),
        'resistance'      : resistance,
        'support'         : support,
        'effective_range' : {
            'lower' : eff_lower,
            'upper' : eff_upper,
            'pts'   : round(eff_pts, 1),
            'pct'   : round(eff_pct, 3),
        },
        'range_type'      : range_type,
    }


# ── Save / print ──────────────────────────────────────────────────────────────

def save_zones(zones: dict, dry_run: bool = False) -> Path | None:
    """Save zones dict to dated + latest JSON files."""
    if dry_run:
        print(json.dumps(zones, indent=2))
        return None

    OI_ZONES_DIR.mkdir(parents=True, exist_ok=True)
    inst = zones['instrument']
    date = zones['date']

    dated_path  = OI_ZONES_DIR / f"{date}_{inst}.json"
    latest_path = OI_ZONES_DIR / f"latest_{inst}.json"

    for path in (dated_path, latest_path):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(zones, f, indent=2)

    print(f"    Saved → {dated_path.name}  and  {latest_path.name}")
    return latest_path


def print_zones_summary(zones: dict):
    """Human-readable summary of computed zones."""
    inst = zones['instrument']
    spot = zones['spot_at_fetch']
    pcr  = zones['pcr']
    mp   = zones['max_pain']
    er   = zones['effective_range']

    print(f"\n  ── {inst}  spot={spot:,.2f}  expiry={zones['expiry']} ─────")
    print(f"     PCR={pcr} ({zones['pcr_bias']})  MaxPain={mp:,.0f}  "
          f"Range={er['lower']:,.0f}–{er['upper']:,.0f} ({er['pct']:.2f}%  {zones['range_type']})")

    print(f"     RESISTANCE walls (top {len(zones['resistance'])}):")
    for r in sorted(zones['resistance'], key=lambda x: x['strike'], reverse=True)[:5]:
        fresh = ' [FRESH]' if r['fresh_build'] else ''
        dist  = (r['strike'] - spot) / spot * 100
        print(f"       {r['strike']:>9,.0f}  OI={r['call_oi']:>10,}  "
              f"{r['pct_total']:5.1f}%  {r['strength']:<6}{fresh}  (+{dist:.2f}%)")

    print(f"     SUPPORT walls (top {len(zones['support'])}):")
    for s in sorted(zones['support'], key=lambda x: x['strike'], reverse=True)[:5]:
        fresh = ' [FRESH]' if s['fresh_build'] else ''
        dist  = (spot - s['strike']) / spot * 100
        print(f"       {s['strike']:>9,.0f}  OI={s['put_oi']:>10,}  "
              f"{s['pct_total']:5.1f}%  {s['strength']:<6}{fresh}  (-{dist:.2f}%)")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args     = [a for a in sys.argv[1:] if a]
    dry_run  = '--dry-run' in args
    args     = [a for a in args if a != '--dry-run']

    valid    = set(ALL_INSTRUMENTS)
    targets  = [a.upper() for a in args if a.upper() in valid]
    if not targets:
        targets = ALL_INSTRUMENTS

    import pytz
    IST = pytz.timezone('Asia/Kolkata')
    now = dt.datetime.now(IST)
    print(f"\n{'='*70}")
    print(f"  OI Zones EOD Fetcher  [{now.strftime('%Y-%m-%d %H:%M:%S IST')}]")
    print(f"{'='*70}")
    print(f"  Instruments : {', '.join(targets)}")
    print(f"  Saving to   : {OI_ZONES_DIR}")
    if dry_run:
        print(f"  Mode        : DRY RUN (print only, not saving)")

    # Fyers is only needed as fallback for SENSEX (BSE-listed, not on NSE)
    fyers = None
    sensex_in_targets = 'SENSEX' in targets
    if sensex_in_targets:
        print(f"  Connecting to Fyers (for SENSEX fallback) ...", end=' ', flush=True)
        try:
            fyers = _get_fyers()
            print("OK")
        except Exception as e:
            print(f"SKIPPED ({e})")
            print("  SENSEX OI will be skipped (no valid Fyers token).")

    success = 0
    for inst in targets:
        print(f"\n  [{inst}] Fetching option chain ...", end=' ', flush=True)
        try:
            # NIFTY / BANKNIFTY → NSE public API (no auth needed)
            # SENSEX             → Fyers fallback (BSE, requires token)
            if inst in nse_oi._NSE_SUPPORTED:
                chain = nse_oi.fetch_chain(inst, expiry_index=0, force=True)
            elif fyers is not None:
                chain = _fyers_fetch_chain(inst, fyers,
                                           strike_count=STRIKE_COUNT,
                                           expiry_index=0)
            else:
                print("SKIPPED (SENSEX requires Fyers token)")
                continue

            if chain is None:
                print("FAILED (empty response)")
                continue
            print(f"OK  spot={chain['spot']:,.2f}  strikes={len(chain['df'])}")

            zones = compute_zones(chain)
            print_zones_summary(zones)
            save_zones(zones, dry_run=dry_run)
            success += 1

        except Exception as ex:
            print(f"ERROR: {ex}")
            import traceback; traceback.print_exc()

    print(f"\n  Done. {success}/{len(targets)} instruments saved.")
    print(f"  These zones will be loaded by paper_bot.py at next startup.")
    print(f"{'='*70}\n")


if __name__ == '__main__':
    main()
