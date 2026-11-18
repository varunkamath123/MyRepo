"""Does the idiosyncratic gap fade survive realistic fills in the CURRENT market?  1-minute candles, NIFTY 50 stocks + NIFTY index, sessions since 2026-06-01.
Signal (known at the open): idiosyncratic gap = stock open/prior close - NIFTY open/prior close, |.| >= 1.0%; trade the 3 largest per day (G3): BUY if the gap is down, SHORT if up.
Entry fills compared:  open print (the research)  |  close of the 09:16 bar  |  close of the 09:20 bar
Exit fills compared:   official close (15:29 bar close = the auction price)  |  15:14 bar close (last continuous price)
Cost 0.10% round trip; 'hedged' removes the NIFTY move over the same holding window.  Small sample: read it as a sanity check on execution, not as proof."""
import json, sys
from datetime import date, timedelta
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent)); sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import common as C

uni = json.load(open(Path(__file__).resolve().parents[2] / "stock_universe.json"))
today = C.now_ist().date(); frm = date(2026, 5, 1); START = pd.Timestamp("2026-06-01")


def minutes(key):
    m = C.candles_chunked(key, "minutes", 1, frm, today - timedelta(days=1)); m = m[m.ts.dt.dayofweek < 5].copy()
    m["day"] = m.ts.dt.normalize(); m["hm"] = m.ts.dt.strftime("%H:%M"); return m


def daily(key):
    d = C.candles_chunked(key, "days", 1, date(2026, 3, 1), today - timedelta(days=1), chunk_days=200); d = d[d.ts.dt.dayofweek < 5].copy()
    d["day"] = d.ts.dt.normalize(); return d.set_index("day")["c"]


def pick(g, hm, col="c"):
    x = g[g.hm == hm]; return float(x[col].iloc[0]) if len(x) else np.nan


nm, nd = minutes(C.INDEX_KEYS["NIFTY"]), daily(C.INDEX_KEYS["NIFTY"])
nidx = {day: g for day, g in nm.groupby("day")}
rows = []
for u in uni:
    try:
        m, dc = minutes(u["key"]), daily(u["key"])
    except Exception as e:
        print(u["symbol"], "failed", str(e)[:60]); continue
    prev = dc.shift(1)
    for day, g in m.groupby("day"):
        if day < START or day not in prev.index or pd.isna(prev[day]) or day not in nidx or pd.isna(nd.shift(1).get(day)) or g.hm.min() != "09:15":
            continue
        ng = nidx[day]
        o = float(g[g.hm == "09:15"].o.iloc[0]); idio = (o / prev[day] - 1) - (float(ng[ng.hm == "09:15"].o.iloc[0]) / nd.shift(1)[day] - 1)
        if abs(idio) < 0.01:
            continue
        rows.append(dict(day=day, sym=u["symbol"], idio=idio, side=1 if idio < 0 else -1, o=o, e1=pick(g, "09:16"), e2=pick(g, "09:20"), x_close=pick(g, "15:29"), x1514=pick(g, "15:14"),
                         n_o=float(ng[ng.hm == "09:15"].o.iloc[0]), n_e1=pick(ng, "09:16"), n_e2=pick(ng, "09:20"), n_close=pick(ng, "15:29"), n_1514=pick(ng, "15:14")))
R = pd.DataFrame(rows).sort_values(["day", "idio"], key=lambda s: s.abs() if s.name == "idio" else s, ascending=[True, False])
R["rank"] = R.groupby("day")["idio"].transform(lambda x: x.abs().rank(ascending=False, method="first"))
T = R[R["rank"] <= 3].copy()
print(f"sessions {T.day.nunique()}, signals {len(R)} (|idio gap| >= 1%), top-3 per day: {len(T)} trades; long {int((T.side==1).sum())}, short {int((T.side==-1).sum())}\n")
print(f"  {'entry fill':<22}{'exit fill':<24}{'net exp%':>9}{'win%':>6}{'PF':>6}{'hedged%':>9}{'long hedged':>12}{'short hedged':>13}")
for en, ek, nk in (("open print", "o", "n_o"), ("09:16 bar close", "e1", "n_e1"), ("09:20 bar close", "e2", "n_e2")):
    for ex, xk, nxk in (("official close", "x_close", "n_close"), ("15:14 last continuous", "x1514", "n_1514")):
        r = T.side * (T[xk] / T[ek] - 1) - 0.0010; mk = T[nxk] / T[nk] - 1; h = r - T.side * mk - 0.0003
        w, l = r[r > 0], r[r <= 0]
        print(f"  {en:<22}{ex:<24}{r.mean()*100:>+9.2f}{(r>0).mean()*100:>6.0f}{w.sum()/abs(l.sum()):>6.2f}{h.mean()*100:>+9.2f}{h[T.side==1].mean()*100:>+12.2f}{h[T.side==-1].mean()*100:>+13.2f}")
print(f"\n  average distance of the 09:16 bar close from the open print, in the direction of the trade: {(T.side * (T.e1 / T.o - 1)).mean()*100:+.3f}% (positive = the fill is WORSE than the open)")
print(f"  average gap size traded: {T.idio.abs().mean()*100:.2f}%; average intraday move toward the fade (open -> close): {(T.side * (T.x_close / T.o - 1)).mean()*100:+.2f}%")
