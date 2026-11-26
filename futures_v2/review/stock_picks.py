"""What has the stock book picked, and how are the picks doing?  Reads the live state, marks every open position (long and short shadow) at the latest quote, and prints
entry, current price, return, return vs NIFTY since entry, which ledger it sits in (signal / capital / risk), the stop and the 4-ATR trail level with the distance to each, and cohort / sector roll-ups.
Run on EC2:  cd /opt/kronos_bot/repo && python futures_v2/review/stock_picks.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import pandas as pd
import stock_book as sb

SECTOR = {"APOLLOHOSP": "Healthcare", "MAXHEALTH": "Healthcare", "AXISBANK": "Banks", "HDFCBANK": "Banks", "SBIN": "Banks", "BAJAJFINSV": "Financials", "HDFCLIFE": "Financials",
          "EICHERMOT": "Auto", "M&M": "Auto", "MARUTI": "Auto", "HINDUNILVR": "FMCG", "ITC": "FMCG", "TATACONSUM": "FMCG", "INDIGO": "Aviation", "TCS": "IT",
          "TRENT": "Retail", "ULTRACEMCO": "Cement", "TATASTEEL": "Metals"}

st = sb._load_state(); uni = {u["symbol"]: u for u in sb._load_universe()}
q = sb.load_quotes([uni[s]["key"] for s in set(st["positions"]) | set(st["shorts"])])
reg = sb.load_regime(); nifty_now = reg["nifty"] if reg else None
print(f"NIFTY now {nifty_now:,.1f} (200-DMA {reg['sma200']:,.1f}); {len(st['positions'])} open longs, {len(st['shorts'])} open short-shadow; closed: signal {st['stats']['signal']['n']}, capital {st['stats']['capital']['n']}, risk {st['stats']['risk']['n']}, short shadow {st['stats']['short_shadow']['n']}\n")

rows = []
for side, book in ((1, st["positions"]), (-1, st["shorts"])):
    for sym, p in book.items():
        pos = sb.StockPos(**p); px = q[uni[sym]["key"]]["px"]; df = sb.load_daily(uni[sym]["key"], bars=60)
        ret = side * (px / pos.entry_price - 1) * 100
        nret = (nifty_now / pos.nifty_entry - 1) * 100 if (nifty_now and pos.nifty_entry) else None
        inr = side * (px - pos.entry_price) * pos.lot
        stop = pos.entry_price - side * sb.STOP_ATR * pos.atr
        if side == 1:
            hw = sb._hwm(pos, df, px); atr = sb._atr_now(df, px); trail = hw - sb.TRAIL_ATR * atr
            lvl = f"trail {trail:,.1f} ({(px / trail - 1) * 100:+.1f}%)"
        else:
            hw = None; lvl = "old rules (10d high / 30d)"
        peak = (hw / pos.entry_price - 1) * 100 if hw else None
        rows.append(dict(side="LONG" if side == 1 else "SHORT", sym=sym, entry=pos.entry_date, ep=pos.entry_price, px=px, ret=ret, vs_nifty=(ret - side * nret) if nret is not None else None,
                         inr=inr, ledger=("CAPITAL" if pos.slot else "signal") + (f"+risk x{pos.risk_lots}" if pos.risk_lots else ""), risk_inr=inr * pos.risk_lots if pos.risk_lots else 0.0,
                         volx=pos.volx, kron=f"{pos.kronos_dir}/{pos.kronos_conf}" if pos.kronos_dir else "-", stop=stop, stop_d=(px / stop - 1) * 100 * side, lvl=lvl, peak=peak,
                         sector=SECTOR.get(sym, "?"), lot=pos.lot, slot=pos.slot, risk_lots=pos.risk_lots))
R = pd.DataFrame(rows)
for title, g in (("LONGS (signal book; capital and risk marks shown by ledger)", R[R.side == "LONG"].sort_values("ret", ascending=False)), ("SHORT SHADOW (no capital)", R[R.side == "SHORT"])):
    if g.empty: continue
    print(f"=== {title} ===")
    print(f"  {'symbol':<11}{'sector':<11}{'entered':<11}{'entry':>9}{'now':>9}{'ret%':>7}{'vs NIFTY':>9}{'Rs/lot':>10}{'volx':>6}  {'ledger':<14}{'kronos':<14}{'stop (dist%)':<20}{'exit level'}")
    for _, r in g.iterrows():
        print(f"  {r.sym:<11}{r.sector:<11}{r.entry:<11}{r.ep:>9,.1f}{r.px:>9,.1f}{r.ret:>+7.2f}{(r.vs_nifty if r.vs_nifty is not None else float('nan')):>+9.2f}{r.inr:>+10,.0f}{r.volx:>6.2f}  {r.ledger:<14}{r.kron:<14}{r.stop:>8,.1f} ({r.stop_d:+.1f}%)   {r.lvl}" + (f"  peak {r.peak:+.1f}%" if r.peak is not None else ""))
L = R[R.side == "LONG"]
if len(L):
    print(f"\nLONG roll-up: {len(L)} positions, {int((L.ret > 0).sum())} up / {int((L.ret <= 0).sum())} down, mean {L.ret.mean():+.2f}% (vs NIFTY {L.vs_nifty.mean():+.2f}%), signal book Rs{L.inr.sum():+,.0f} per-lot")
    print(f"  capital ledger (3 slots) Rs{L[L.slot].inr.sum():+,.0f}   risk ledger (1% sizing) Rs{L.risk_inr.sum():+,.0f}")
    print("  by entry date:  " + "   ".join(f"{d}: n={len(g)} mean {g.ret.mean():+.1f}%" for d, g in L.groupby("entry")))
    print("  by sector:      " + "   ".join(f"{s}: n={len(g)} mean {g.ret.mean():+.1f}%" for s, g in L.groupby("sector")))
    kl = L[L.kron.str.startswith("LONG")]; kn = L[~L.kron.str.startswith("LONG")]
    print(f"  Kronos read at entry (logged, never gates): LONG n={len(kl)} mean {kl.ret.mean() if len(kl) else float('nan'):+.2f}%  |  NEUTRAL/SHORT n={len(kn)} mean {kn.ret.mean() if len(kn) else float('nan'):+.2f}%")
S = R[R.side == "SHORT"]
if len(S):
    print(f"SHORT SHADOW: {len(S)} open, mean {S.ret.mean():+.2f}% (vs NIFTY {S.vs_nifty.mean():+.2f}%), Rs{S.inr.sum():+,.0f} per lot")
