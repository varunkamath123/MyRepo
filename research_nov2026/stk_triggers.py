"""Stock-futures entry indications, PRICE-ONLY first (no Kronos): is there any edge worth adding Kronos to?
Universe : every stock with a live NSE futures contract (Upstox master), top TOPN by 1y median turnover, 4y daily bars
           from yfinance (adjusted, real volume).  SURVIVORSHIP: today's F&O list only -> long-side tailwind, see shift null.
Rules    : textbook values FIXED HERE, nothing tuned.  5 variants (so ~5 looks, plus long/short/halves splits).
  V1 Breakout+Vol : close > prior-20d high (short: < prior-20d low) AND volume >= 1.5x prior-20d avg.
                    exit: close beyond prior-10d opposite extreme | 2 ATR14 stop | 30 bars
  V2 Breakout     : V1 without the volume filter
  V3 RSI2 MR      : long RSI2<10 & close>SMA200, short RSI2>90 & close<SMA200.
                    exit: RSI2>65 / <35 or close crosses SMA5 | 2 ATR14 stop | 7 bars
  V4 Pullback     : long close>SMA50 & close = 5d low, short close<SMA50 & close = 5d high.  exits as V3
  V5 RS pullback  : V4 but only long the top-quintile / short the bottom-quintile 60d return vs NIFTY (cross-section)
Mechanics: decision on the day's close (~14:55 price), entry at that close, exits checked at later closes, one position per
           stock at a time, no portfolio cap (per-trade edge), cost 0.10% round trip.
Null     : each stock's signal series circularly shifted by a random offset (keeps frequency/clustering, breaks timing)
           -> controls for drift, long bias and survivorship.  t-stat is clustered by ISO week of entry."""
import gzip, json, os, sys, time, pickle
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np
import pandas as pd
import requests
import yfinance as yf

RES = Path("/opt/kronos_bot/research"); CACHE = RES / "stk_prices.pkl"
COST, TOPN, WARM = 0.0010, 60, 200
N_SHIFT = int(sys.argv[1]) if len(sys.argv) > 1 else 100
IST = timezone(timedelta(hours=5, minutes=30)); TODAY = pd.Timestamp(datetime.now(IST).date())
INDEXES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


def fo_universe():
    r = requests.get("https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz", timeout=90)
    data = json.loads(gzip.decompress(r.content)); now = datetime.now(IST).timestamp()
    best = {}
    for x in data:
        if x.get("segment") != "NSE_FO" or x.get("instrument_type") != "FUT" or x["expiry"] / 1000 <= now:
            continue
        s = x.get("underlying_symbol")
        if not s or s in INDEXES or x.get("underlying_type") == "INDEX":
            continue
        if s not in best or x["expiry"] < best[s]["expiry"]:
            best[s] = x
    return {s: int(x["lot_size"]) for s, x in best.items()}


def fetch(sym):
    for _ in range(2):
        try:
            d = yf.download(f"{sym}.NS", period="4y", interval="1d", progress=False, auto_adjust=True)
            d.columns = [c[0] if isinstance(c, tuple) else c for c in d.columns]
            d = d.dropna()
            d.index = pd.to_datetime(d.index).tz_localize(None)
            d = d[d.index.normalize() < TODAY]
            if len(d) >= 700:
                return d[["Open", "High", "Low", "Close", "Volume"]]
        except Exception:
            time.sleep(2)
    return None


if CACHE.exists():
    prices, lots, nifty = pickle.load(open(CACHE, "rb"))
    print(f"cache: {len(prices)} stocks", flush=True)
else:
    lots = fo_universe()
    print(f"F&O stock futures found: {len(lots)}", flush=True)
    prices = {}
    for i, s in enumerate(sorted(lots)):
        d = fetch(s)
        if d is not None:
            prices[s] = d
        if i % 30 == 29:
            print(f"  fetched {i+1}/{len(lots)} ({len(prices)} usable)", flush=True)
    nf = yf.download("^NSEI", period="4y", interval="1d", progress=False, auto_adjust=True)
    nf.columns = [c[0] if isinstance(c, tuple) else c for c in nf.columns]
    nifty = nf["Close"].dropna(); nifty.index = pd.to_datetime(nifty.index).tz_localize(None)
    nifty = nifty[nifty.index.normalize() < TODAY]
    pickle.dump((prices, lots, nifty), open(CACHE, "wb"))

turn = {s: float((d["Close"] * d["Volume"]).tail(250).median()) for s, d in prices.items()}
names = json.load(open(os.environ["STK_UNIV"])) if os.environ.get("STK_UNIV") else sorted(turn, key=turn.get, reverse=True)[:TOPN]
print(f"universe: {len(names)} of {len(prices)} usable F&O stocks ({os.environ.get('STK_UNIV', 'top by turnover')}); median daily turnover Rs{np.median([turn[s] for s in names])/1e7:,.0f} cr "
      f"(lowest in set Rs{min(turn[s] for s in names)/1e7:,.0f} cr)", flush=True)
nots = [lots[s] * float(prices[s]["Close"].iloc[-1]) for s in names]
print(f"lot notional Rs{min(nots)/1e5:.1f}L .. median Rs{np.median(nots)/1e5:.1f}L .. max Rs{max(nots)/1e5:.1f}L", flush=True)


def rsi_wilder(c, n=2):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


# calendar + relative-strength cross-section
cal = sorted(set().union(*[set(prices[s].index) for s in names]))
ret60 = pd.DataFrame({s: prices[s]["Close"].pct_change(60) for s in names}).reindex(cal)
rs = ret60.sub(nifty.pct_change(60).reindex(cal), axis=0)
rs_pct = rs.rank(axis=1, pct=True)

S = {}
for s in names:
    d = prices[s]; c, h, l, v = d["Close"], d["High"], d["Low"], d["Volume"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    f = dict(c=c, atr=tr.ewm(alpha=1 / 14, adjust=False).mean(), rsi2=rsi_wilder(c), sma5=c.rolling(5).mean(),
             sma50=c.rolling(50).mean(), sma200=c.rolling(200).mean(),
             hi20=h.shift(1).rolling(20).max(), lo20=l.shift(1).rolling(20).min(),
             hi10=h.shift(1).rolling(10).max(), lo10=l.shift(1).rolling(10).min(),
             min5=c.rolling(5).min(), max5=c.rolling(5).max(), volx=v / v.shift(1).rolling(20).mean(),
             rsp=rs_pct[s].reindex(d.index))
    F = pd.DataFrame(f)
    sig = {}
    bl, bs = (F.c > F.hi20), (F.c < F.lo20)
    vol = F.volx >= 1.5
    sig["V1 Breakout+Vol"] = np.where(bl & vol, 1, np.where(bs & vol, -1, 0))
    sig["V2 Breakout"] = np.where(bl, 1, np.where(bs, -1, 0))
    sig["V3 RSI2 MR"] = np.where((F.rsi2 < 10) & (F.c > F.sma200), 1, np.where((F.rsi2 > 90) & (F.c < F.sma200), -1, 0))
    pl, ps = (F.c > F.sma50) & (F.c <= F.min5), (F.c < F.sma50) & (F.c >= F.max5)
    sig["V4 Pullback"] = np.where(pl, 1, np.where(ps, -1, 0))
    sig["V5 RS pullback"] = np.where(pl & (F.rsp >= 0.8), 1, np.where(ps & (F.rsp <= 0.2), -1, 0))
    sig = {k: np.where(np.arange(len(F)) >= WARM, a, 0) for k, a in sig.items()}
    S[s] = dict(dates=list(F.index), sig=sig, **{k: F[k].tolist() for k in
                ("c", "atr", "rsi2", "sma5", "hi10", "lo10")})
STYLE = {"V1 Breakout+Vol": "don", "V2 Breakout": "don", "V3 RSI2 MR": "mr", "V4 Pullback": "mr", "V5 RS pullback": "mr"}


def simulate(s, sig, style):
    A = S[s]; c, atr, n = A["c"], A["atr"], len(A["c"])
    pos, out = 0, []
    for t in range(WARM, n):
        if pos:
            ex = None
            if pos * (c[t] - ep) <= -2 * ea:
                ex = "STOP"
            elif style == "don":
                if (pos == 1 and c[t] < A["lo10"][t]) or (pos == -1 and c[t] > A["hi10"][t]): ex = "CHANNEL"
                elif t - et >= 30: ex = "TIME"
            else:
                if (pos == 1 and (A["rsi2"][t] > 65 or c[t] > A["sma5"][t])) or (pos == -1 and (A["rsi2"][t] < 35 or c[t] < A["sma5"][t])): ex = "MR"
                elif t - et >= 7: ex = "TIME"
            if ex or t == n - 1:
                out.append((s, A["dates"][et], A["dates"][t], pos, pos * (c[t] - ep) / ep - COST, ex or "OPEN"))
                pos = 0
            continue
        if sig[t]:
            pos, ep, ea, et = int(sig[t]), c[t], atr[t], t
    return out


def run(variant, shift_rng=None):
    rows = []
    for s in names:
        sg = S[s]["sig"][variant]
        if shift_rng is not None:
            seg = np.roll(sg[WARM:], int(shift_rng.integers(10, max(11, len(sg) - WARM - 10))))
            sg = np.concatenate([sg[:WARM], seg])
        rows += simulate(s, sg.tolist(), STYLE[variant])
    return pd.DataFrame(rows, columns=["stock", "entry", "exit", "dir", "ret", "reason"])


def tstat(T):
    r = T["ret"].values; n = len(r); mu = r.mean()
    if n < 3: return float("nan"), float("nan")
    t_naive = mu / (r.std(ddof=1) / np.sqrt(n))
    wk = T["entry"].dt.isocalendar()
    key = (wk["year"].astype(str) + "-" + wk["week"].astype(str)).values
    g = pd.Series(r - mu).groupby(key).sum()
    se = np.sqrt((g ** 2).sum()) / n
    return t_naive, mu / se


span_q = (cal[-1] - cal[WARM]).days / 91.3
print(f"\n{len(names)} stocks | {cal[WARM].date()} -> {cal[-1].date()} ({span_q:.1f} quarters) | cost {COST*100:.2f}% round trip | {N_SHIFT} shift draws\n", flush=True)
print(f"{'variant':<17}{'trades':>7}{'/qtr':>6}{'win%':>6}{'net/trd%':>9}{'gross%':>8}{'t(naive)':>9}{'t(week)':>8}{'null med%':>10}{'p(shift)':>9}{'H1%':>7}{'H2%':>7}{'LONG%':>7}{'SHORT%':>8}", flush=True)
concurrency = {}
for v in S[names[0]]["sig"]:
    T = run(v)
    if T.empty:
        print(f"{v:<17}{0:>7}"); continue
    mu = T.ret.mean() * 100
    tn, tw = tstat(T)
    mid = T["entry"].sort_values().iloc[len(T) // 2]
    rng = np.random.default_rng(5)
    nulls = np.array([run(v, rng).ret.mean() * 100 for _ in range(N_SHIFT)])
    p = float((nulls >= mu).mean())
    L, Sh = T[T.dir == 1], T[T.dir == -1]
    f = lambda x: f"{x.ret.mean()*100:+.2f}" if len(x) else "  n/a"
    print(f"{v:<17}{len(T):>7}{len(T)/span_q:>6.0f}{(T.ret>0).mean()*100:>5.0f}%{mu:>+9.2f}{mu+COST*100:>+8.2f}{tn:>+9.2f}{tw:>+8.2f}{np.median(nulls):>+10.2f}{p:>9.3f}"
          f"{f(T[T.entry<mid]):>7}{f(T[T.entry>=mid]):>7}{f(L):>7}{f(Sh):>8}  (n L/S {len(L)}/{len(Sh)})", flush=True)
    ix = {d: i for i, d in enumerate(cal)}
    cnt = np.zeros(len(cal) + 1)
    for _, r in T.iterrows():
        cnt[ix[r.entry]] += 1; cnt[ix[r.exit]] -= 1
    open_ = np.cumsum(cnt)[:len(cal)]
    concurrency[v] = (open_.mean(), np.percentile(open_, 95), open_.max())
print("\nconcurrent open positions across the universe (mean / p95 / max):")
for v, (m, p95, mx) in concurrency.items():
    print(f"  {v:<17}{m:>5.1f} / {p95:>4.0f} / {mx:>3.0f}")
