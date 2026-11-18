"""BUY LOW / SELL HIGH lab (mean reversion), 2012-2026 daily data: NIFTY, BANKNIFTY (index = future proxy) and the NIFTY 50 stocks.
Rules are textbook and FIXED BEFORE THE RUN; eight variants, each tested on the indices and on the stocks (the number of looks is 8 x 3 universes):
  M1  RSI2 < 10 and close > 200-day average  -> BUY at the close; sell when RSI2 > 70 or after 10 sessions
  M2  RSI2 > 90 and close < 200-day average  -> SELL SHORT at the close; cover when RSI2 < 30 or after 10 sessions
  M3L close >= 2 standard deviations below its 20-day mean (z <= -2) -> BUY; sell when z >= 0 (back to the mean) or after 10 sessions
  M3S z >= +2 -> SELL SHORT; cover when z <= 0 or after 10 sessions
  M6  close is the lowest close of 10 sessions and above the 200-day average -> BUY; sell when close >= 5-day average or after 10 sessions
  M7  close is the highest close of 10 sessions and below the 200-day average -> SELL SHORT; cover when close <= 5-day average or after 10 sessions
  M5L gap down: open <= -g% vs the prior close -> BUY at the open, sell at the same day's close      (g = 0.5% indices, 1.0% stocks)
  M5S gap up:   open >= +g%                    -> SELL SHORT at the open, cover at the close
Catastrophic stop 3 ATR on the multi-day rules.  Costs per round trip: NIFTY 0.047%, BANKNIFTY 0.054% (measured spreads), stocks 0.10%.
Halves: before 2020-01-01 vs from it (an effect must hold in both).  Indices also get a PLACEBO p-value (the entry signal circularly shifted >= 60 sessions,
500 draws, same exits) because the indices drifted up +300-450% and any long rule earns something by drift alone; stocks report the return net of NIFTY."""
import pickle
from pathlib import Path
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd

RES = Path("/opt/kronos_bot/research")
D = pickle.load(open(RES / "idx_data_long.pkl", "rb"))
IST = timezone(timedelta(hours=5, minutes=30)); TODAY = pd.Timestamp(datetime.now(IST).date())
SPLIT, WARM, HEDGE = pd.Timestamp("2020-01-01"), 210, 0.0003
rng = np.random.default_rng(17)
nf = D["idx"]["NIFTY"]["Close"]; nf = nf[nf.index < TODAY]


def prep(d, cost, gap):
    d = d[d.index < TODAY]
    o, h, l, c = (d[k].values.astype(float) for k in ("Open", "High", "Low", "Close"))
    cs = pd.Series(c); ds = cs.diff()
    up = ds.clip(lower=0).ewm(alpha=1 / 2, adjust=False).mean(); dn = (-ds.clip(upper=0)).ewm(alpha=1 / 2, adjust=False).mean()
    rsi2 = (100 - 100 / (1 + up / dn.replace(0, np.nan))).values
    tr = pd.concat([pd.Series(h) - pd.Series(l), (pd.Series(h) - cs.shift()).abs(), (pd.Series(l) - cs.shift()).abs()], axis=1).max(axis=1)
    sma20, sd20 = cs.rolling(20).mean(), cs.rolling(20).std()
    P = dict(dates=d.index, o=o, c=c, cost=cost, gap=gap, rsi2=rsi2, atr=tr.ewm(alpha=1 / 14, adjust=False).mean().values, sma5=cs.rolling(5).mean().values,
             sma200=cs.rolling(200).mean().values, z=((cs - sma20) / sd20).values, lo10=cs.rolling(10).min().values, hi10=cs.rolling(10).max().values,
             n=nf.reindex(d.index, method="ffill").values)
    P["gapret"] = np.r_[np.nan, o[1:] / c[:-1] - 1]
    return P


def signals(P, v):
    c = P["c"]; n = len(c); s = np.zeros(n, int); ok = ~np.isnan(P["sma200"])
    if v == "M1": s[ok & (P["rsi2"] < 10) & (c > P["sma200"])] = 1
    if v == "M2": s[ok & (P["rsi2"] > 90) & (c < P["sma200"])] = -1
    if v == "M3L": s[ok & (P["z"] <= -2)] = 1
    if v == "M3S": s[ok & (P["z"] >= 2)] = -1
    if v == "M6": s[ok & (c <= P["lo10"] + 1e-12) & (c > P["sma200"])] = 1
    if v == "M7": s[ok & (c >= P["hi10"] - 1e-12) & (c < P["sma200"])] = -1
    return s


def exit_rule(P, v, t, side):
    if v == "M1": return P["rsi2"][t] > 70
    if v == "M2": return P["rsi2"][t] < 30
    if v == "M3L": return P["z"][t] >= 0
    if v == "M3S": return P["z"][t] <= 0
    if v == "M6": return P["c"][t] >= P["sma5"][t]
    if v == "M7": return P["c"][t] <= P["sma5"][t]


def engine(P, v, sig):
    c, atr, n = P["c"], P["atr"], len(P["c"]); out, pos = [], 0
    for t in range(WARM, n):
        if pos:
            ex = None
            if side * (c[t] - ep) <= -3 * ea: ex = "STOP"
            elif exit_rule(P, v, t, side): ex = "RULE"
            elif t - et >= 10: ex = "TIME"
            if ex or t == n - 1:
                nret = P["n"][t] / P["n"][et] - 1; r = side * (c[t] - ep) / ep - P["cost"]
                out.append((P["dates"][et], r, r - side * nret - HEDGE, t - et, side, ex or "OPEN")); pos = 0
            continue
        if sig[t]:
            side, pos, ep, ea, et = int(sig[t]), 1, c[t], atr[t], t
    return out


def gap_trades(P, side):
    o, c, g = P["o"], P["c"], P["gapret"]; out = []
    for t in range(WARM, len(c)):
        if (side == 1 and g[t] <= -P["gap"]) or (side == -1 and g[t] >= P["gap"]):
            nret = P["n"][t] / P["n"][t - 1] - 1  # market move over the same day ~ close/prev close; use open->close of NIFTY unavailable, approximate with close/prev close
            r = side * (c[t] - o[t]) / o[t] - P["cost"]
            out.append((P["dates"][t], r, r - side * (nret - g[t]) - HEDGE, 0, side, "DAY"))
    return out


def stats(rows, label, show_side=True):
    T = pd.DataFrame(rows, columns=["date", "ret", "hedged", "held", "side", "reason"])
    if len(T) < 15:
        print(f"  {label:<26}{len(T):>6}"); return None
    w, l = T.ret[T.ret > 0], T.ret[T.ret <= 0]; pf = w.sum() / abs(l.sum()) if len(l) else float("inf")
    h = lambda x: f"{x.ret.mean()*100:+.2f}" if len(x) else " n/a"
    yrs = (T.date.max() - T.date.min()).days / 365.25
    print(f"  {label:<26}{len(T):>6}{len(T)/yrs:>6.0f}{(T.ret>0).mean()*100:>5.0f}{w.mean()*100:>+8.2f}{l.mean()*100:>+9.2f}{T.ret.mean()*100:>+8.2f}{pf:>6.2f}{h(T[T.date<SPLIT]):>7}{h(T[T.date>=SPLIT]):>7}"
          f"{T.hedged.mean()*100:>+8.2f}{T.held.mean():>6.1f}")
    return T


HDR = f"  {'variant':<26}{'n':>6}{'/yr':>6}{'win%':>5}{'avg win':>8}{'avg loss':>9}{'exp%':>8}{'PF':>6}{'H1':>7}{'H2':>7}{'hedged':>8}{'hold':>6}"
VARS = [("M1", "M1  RSI2<10 in uptrend: BUY"), ("M6", "M6  10-day low in uptrend: BUY"), ("M3L", "M3L z<=-2: BUY"), ("M5L", "M5L gap down: BUY open->close"),
        ("M2", "M2  RSI2>90 in downtrend: SHORT"), ("M7", "M7  10-day high in downtrend: SHORT"), ("M3S", "M3S z>=+2: SHORT"), ("M5S", "M5S gap up: SHORT open->close")]

for name in ("NIFTY", "BANKNIFTY"):
    P = prep(D["idx"][name], 0.00047 if name == "NIFTY" else 0.00054, 0.005)
    sub = pd.Series(P["c"], index=P["dates"]).iloc[WARM:]
    print(f"\n{'#'*118}\n#  {name} index ({'future proxy'}), {P['dates'][WARM].date()} -> {P['dates'][-1].date()}, cost {P['cost']*100:.3f}%/round trip; index buy-and-hold over the sample {(sub.iloc[-1]/sub.iloc[0]-1)*100:+.0f}%\n{'#'*118}")
    print(HDR + f"{'placebo p':>11}")
    for v, lab in VARS:
        rows = gap_trades(P, 1 if v == "M5L" else -1) if v.startswith("M5") else engine(P, v, signals(P, v))
        T = stats(rows, lab)
        if T is None: continue
        obs = T.ret.mean(); nulls = []
        for _ in range(300):
            lag = int(rng.integers(60, len(P["c"]) - 60))
            if v.startswith("M5"):
                g = np.roll(P["gapret"], lag); Q = dict(P, gapret=g)
                r = gap_trades(Q, 1 if v == "M5L" else -1)
            else:
                r = engine(P, v, np.roll(signals(P, v), lag))
            if r: nulls.append(np.mean([x[1] for x in r]))
        print(f"{'':<118}"[:0] + f"      placebo p = {float(np.mean(np.array(nulls) >= obs)):.3f}  (null mean {np.mean(nulls)*100:+.2f}%)")

print(f"\n{'#'*118}\n#  NIFTY 50 stocks (pooled, {len(D['n50'])} names), cost 0.10%/round trip, 'hedged' = net of the NIFTY move over the holding period\n{'#'*118}")
print(HDR)
SP = {s: prep(D["stocks"][s], 0.0010, 0.010) for s in D["n50"]}
for v, lab in VARS:
    rows = []
    for s, P in SP.items():
        rows += gap_trades(P, 1 if v == "M5L" else -1) if v.startswith("M5") else engine(P, v, signals(P, v))
    stats(rows, lab)
