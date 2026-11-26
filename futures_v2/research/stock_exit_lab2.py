"""Stock book exits driven by PRICE ACTION, VOLUME and PROFIT instead of a session count.  Follow-up to stock_exit_lab.py (H23).  Same entries (live signal: close > prior-20d high and volume >= 1.5x prior-20d average), NIFTY 50 names
(today's constituents -> survivorship bias flatters longer holds: read differences), daily bars 2012-2026, cost 0.10% round trip, one position per stock, exits on closes.  'excess' = return minus NIFTY over the same dates.
Every adaptive variant keeps the 2-ATR initial stop and has NO session cap (a 250-session backstop only closes data at the end).  Fixed BEFORE the run (7 adaptive looks + 3 references):
  BASE            2 ATR stop, 10-day-low channel, 30 sessions                              (live)
  C20T60 / CH4    channel 20 + cap 60  /  chandelier 4 ATR + cap 120                       (H23 best candidates, for reference)
  A1 trail4       price action only: exit when close < highest close since entry - 4 x ATR14
  A2 trail4+vol   A1 plus a DISTRIBUTION exit: in profit, close below the prior close on volume >= 1.5x average with the close in the lower 40% of the day's range
  A3 profit-tight A1 with the multiple tightening as peak profit grows: 4 ATR below +15% peak, 3 ATR to +30%, 2 ATR above
  A3w profit-wide the opposite (more room as profit grows): 3 ATR below +15%, 4 ATR to +30%, 5 ATR above
  A4 20DMA        trend rule: exit after TWO consecutive closes below the 20-day average (no ATR trail)
  A5 trail4+stale A1 plus a progress test: at 20 sessions, exit if the peak gain is below 1.5 ATR (dead money frees the slot)
  A6 full         A1 + distribution exit + stale test + climax tightening (after a bar with volume >= 2.5x average and close >= 20-DMA + 3 ATR the trail becomes 2 ATR)
Metrics: excess return per position-day (sum of excess / sum of sessions held, bp), per-trade expectancy, halves (split 2019-07-01), years beating BASE, capture ratio (exit gain / peak gain on trades that peaked >= +10%),
share of the total made by the top 5% of trades.  Portfolio view: 5 slots x 20% of equity, signals ranked by volume ratio, cash idle otherwise, same trades as the per-trade table (slot-skipped signals dropped): CAGR / Sharpe / max drawdown.
Decision rule: an adaptive variant is a CANDIDATE only if its excess return per position-day beats BASE and CH4 in BOTH halves, it beats BASE in >= 10 of 15 entry years, its portfolio Sharpe >= BASE's and its max drawdown is no more than 5 points worse than BASE's."""
import pickle
from pathlib import Path
import numpy as np
import pandas as pd

RES = Path("/opt/kronos_bot/research")
DD = pickle.load(open(RES / "idx_data_long.pkl", "rb"))
NIFTY = DD["idx"]["NIFTY"]["Close"]; NIFTY = NIFTY[~NIFTY.index.duplicated()]
COST = 0.001
SPLIT = pd.Timestamp("2019-07-01")
BACK = 250
VARIANTS = {
    "BASE":            dict(stop=2.0, ch=10, cap=30),
    "C20T60":          dict(stop=2.0, ch=20, cap=60),
    "CH4":             dict(stop=2.0, chand=4.0, cap=120),
    "A1 trail4":       dict(stop=2.0, chand=4.0, cap=BACK),
    "A2 trail4+vol":   dict(stop=2.0, chand=4.0, cap=BACK, dist=True),
    "A3 profit-tight": dict(stop=2.0, chand=4.0, cap=BACK, ladder="tight"),
    "A3w profit-wide": dict(stop=2.0, chand=4.0, cap=BACK, ladder="wide"),
    "A4 20DMA":        dict(stop=2.0, sma2=True, cap=BACK),
    "A5 trail4+stale": dict(stop=2.0, chand=4.0, cap=BACK, dead=(20, 1.5)),
    "A6 full":         dict(stop=2.0, chand=4.0, cap=BACK, dist=True, dead=(20, 1.5), climax=True),
}


def prep(d):
    d = d[["Open", "High", "Low", "Close", "Volume"]].dropna()
    d = d[~d.index.duplicated()]
    pc = d.Close.shift(1)
    tr = pd.concat([d.High - d.Low, (d.High - pc).abs(), (d.Low - pc).abs()], axis=1).max(axis=1)
    A = dict(idx=d.index, c=d.Close.values, h=d.High.values, l=d.Low.values, atr=tr.rolling(14).mean().values,
             hi20=d.High.rolling(20).max().shift(1).values, sma20=d.Close.rolling(20).mean().values,
             vr=(d.Volume / d.Volume.rolling(20).mean().shift(1)).values)
    for n in (10, 20): A[f"lo{n}"] = d.Low.rolling(n).min().shift(1).values
    A["sig"] = (d.Close.values > A["hi20"]) & (A["vr"] >= 1.5) & ~np.isnan(A["atr"]) & (d.Volume.values > 0)
    return A


def run(A, v):
    c, h, l, atr, vr, sma = A["c"], A["h"], A["l"], A["atr"], A["vr"], A["sma20"]; n = len(c); out = []; pos = False; last_exit = -1
    for t in range(n):
        if pos:
            held = t - et; hc = max(hc, c[t]); peak = hc / ep - 1; why = None
            if vr[t] >= 2.5 and c[t] >= sma[t] + 3 * atr[t]: climax = True
            if c[t] <= ep - v["stop"] * ea: why = "STOP"
            if why is None and "chand" in v:
                kk = v["chand"]
                if v.get("ladder") == "tight": kk = 4.0 if peak < 0.15 else (3.0 if peak < 0.30 else 2.0)
                elif v.get("ladder") == "wide": kk = 3.0 if peak < 0.15 else (4.0 if peak < 0.30 else 5.0)
                if v.get("climax") and climax: kk = min(kk, 2.0)
                if c[t] < hc - kk * atr[t]: why = "TRAIL"
            if why is None and v.get("ch") and c[t] < A[f"lo{v['ch']}"][t]: why = "CHAN"
            if why is None and v.get("dist") and c[t] > ep and c[t] < c[t - 1] and vr[t] >= 1.5 and h[t] > l[t] and (c[t] - l[t]) / (h[t] - l[t]) <= 0.4: why = "DIST"
            if why is None and v.get("sma2") and c[t] < sma[t] and c[t - 1] < sma[t - 1]: why = "TREND"
            if why is None and v.get("dead") and held == v["dead"][0] and peak < v["dead"][1] * ea / ep: why = "STALE"
            if why is None and held >= v["cap"]: why = "TIME"
            if why or t == n - 1:
                out.append((A["idx"][et], A["idx"][t], c[t] / ep - 1 - COST, c[t] / ep - 1, peak, held, why or "OPEN", vr_e)); pos = False; last_exit = t
            continue
        if A["sig"][t] and t != last_exit:
            pos, ep, ea, et, hc, climax, vr_e = True, c[t], atr[t], t, c[t], False, vr[t]
    return out


names = [s for s in DD["n50"] if s in DD["stocks"] and len(DD["stocks"][s]) > 300]
data = {s: prep(DD["stocks"][s]) for s in names}
CLS = pd.DataFrame({s: pd.Series(A["c"], index=A["idx"]) for s, A in data.items()})
print(f"{len(names)} stocks, {CLS.index[0].date()} -> {CLS.index[-1].date()}")

res = {}
for k, v in VARIANTS.items():
    rows = []
    for s in names:
        rows += [(s,) + r for r in run(data[s], v)]
    T = pd.DataFrame(rows, columns=["sym", "entry", "exit", "ret", "gross", "peak", "held", "why", "vr"])
    T = T[T.why != "OPEN"].copy()
    ent = NIFTY.reindex(T.entry, method="ffill").values; ext = NIFTY.reindex(T.exit, method="ffill").values
    T["exc"] = T.ret - (ext / ent - 1)
    res[k] = T


def perday(x): return x.exc.sum() / x.held.sum() * 1e4


yr = {k: T.groupby(T.entry.dt.year).apply(perday) for k, T in res.items()}
print(f"\n{'variant':<17}{'n':>5}{'win%':>5}{'avg win':>8}{'avg loss':>9}{'exp%':>7}{'exc%':>7}{'hold':>6}{'exc bp/d':>9}{'H1':>6}{'H2':>6}{'yrs>BASE':>9}{'capture%':>9}{'ex-top1% exc':>13}{'top5%':>6}  exits")
for k, T in res.items():
    h1, h2 = T[T.entry < SPLIT], T[T.entry >= SPLIT]
    w, lo = T.ret[T.ret > 0], T.ret[T.ret <= 0]
    top = T.ret.sort_values(ascending=False).head(int(len(T) * 0.05)).sum() / T.ret.sum() * 100
    big = T[T.peak >= 0.10]; cap = (big.gross / big.peak).mean() * 100
    y = (yr[k].reindex(yr["BASE"].index) > yr["BASE"]).sum() if k != "BASE" else 0
    ex1 = T[T.ret <= T.ret.quantile(0.99)].exc.mean() * 100
    ex = ", ".join(f"{a} {b}" for a, b in T.why.value_counts().items())
    print(f"{k:<17}{len(T):>5}{(T.ret>0).mean()*100:>5.0f}{w.mean()*100:>+8.2f}{lo.mean()*100:>+9.2f}{T.ret.mean()*100:>+7.2f}{T.exc.mean()*100:>+7.2f}{T.held.mean():>6.0f}{perday(T):>+9.1f}{perday(h1):>+6.1f}{perday(h2):>+6.1f}"
          f"{('-' if k=='BASE' else str(y)+'/'+str(len(yr['BASE']))):>9}{cap:>9.0f}{ex1:>+13.2f}{top:>5.0f}%  {ex}")

# ---- portfolio: 5 slots x 20% of equity, ranked by volume ratio
cal = sorted(set(NIFTY.index) | set(pd.concat([T.entry for T in res.values()])) | set(pd.concat([T.exit for T in res.values()])))
CL = CLS.reindex(cal).ffill()


def portfolio(T, max_pos=5, w=0.20):
    T = T.sort_values(["entry", "vr"], ascending=[True, False]); byday = {d: g for d, g in T.groupby("entry")}
    start = T.entry.min(); cash, pos, eqs, invs, took = 1.0, [], [], [], 0
    days = [d for d in cal if d >= start]
    for d in days:
        keep = []
        for p in pos:
            if p["exit"] <= d: cash += p["stake"] * (CL.at[d, p["sym"]] / p["ep"]) * (1 - COST)
            else: keep.append(p)
        pos = keep
        marks = sum(p["stake"] * CL.at[d, p["sym"]] / p["ep"] for p in pos); equity = cash + marks
        if d in byday:
            for _, r in byday[d].iterrows():
                if len(pos) >= max_pos: break
                stake = w * equity; cash -= stake; took += 1
                pos.append(dict(sym=r.sym, exit=r.exit, ep=CL.at[d, r.sym], stake=stake))
        marks = sum(p["stake"] * CL.at[d, p["sym"]] / p["ep"] for p in pos)
        eqs.append(cash + marks); invs.append(marks / (cash + marks))
    eq = pd.Series(eqs, index=days); r = eq.pct_change().fillna(0); yrs = (days[-1] - days[0]).days / 365.25
    return dict(cagr=(eq.iloc[-1] ** (1 / yrs) - 1) * 100, sharpe=r.mean() / r.std() * np.sqrt(252), mdd=((eq / eq.cummax()) - 1).min() * 100, inv=np.mean(invs) * 100, took=took, tot=len(T), yrs=yrs)


PF = {k: portfolio(T) for k, T in res.items()}
nb = NIFTY[NIFTY.index >= min(T.entry.min() for T in res.values())]; nr = nb.pct_change().fillna(0); ne = (1 + nr).cumprod(); ny = (nb.index[-1] - nb.index[0]).days / 365.25
print(f"\nPortfolio (5 slots x 20% of equity, ranked by volume ratio).  NIFTY buy&hold same window: CAGR {(ne.iloc[-1]**(1/ny)-1)*100:.1f}%  Sharpe {nr.mean()/nr.std()*np.sqrt(252):.2f}  maxDD {((ne/ne.cummax())-1).min()*100:.0f}%")
print(f"  {'variant':<17}{'CAGR%':>7}{'Sharpe':>8}{'maxDD%':>8}{'invested%':>10}{'trades taken':>14}")
for k, p in PF.items():
    print(f"  {k:<17}{p['cagr']:>7.1f}{p['sharpe']:>8.2f}{p['mdd']:>8.0f}{p['inv']:>10.0f}{p['took']:>8}/{p['tot']:<5}")

print("\nDecision rule (candidate vs BASE and CH4):")
for k in VARIANTS:
    if not k.startswith("A"): continue
    T = res[k]; h1, h2 = T[T.entry < SPLIT], T[T.entry >= SPLIT]
    bh1, bh2 = res["BASE"][res["BASE"].entry < SPLIT], res["BASE"][res["BASE"].entry >= SPLIT]; ch1, ch2 = res["CH4"][res["CH4"].entry < SPLIT], res["CH4"][res["CH4"].entry >= SPLIT]
    c1 = perday(h1) > perday(bh1) and perday(h1) > perday(ch1); c2 = perday(h2) > perday(bh2) and perday(h2) > perday(ch2)
    c3 = (yr[k].reindex(yr["BASE"].index) > yr["BASE"]).sum() >= 10
    c4 = PF[k]["sharpe"] >= PF["BASE"]["sharpe"] and PF[k]["mdd"] >= PF["BASE"]["mdd"] - 5
    print(f"  {k:<17} per-day > BASE&CH4 in H1: {c1!s:<5} H2: {c2!s:<5} | >=10/15 years vs BASE: {c3!s:<5} | portfolio Sharpe/DD ok: {c4!s:<5} -> {'CANDIDATE' if (c1 and c2 and c3 and c4) else 'no'}")
