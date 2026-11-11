"""Long-history dataset for the index-vs-constituents study: same names as idx_data.pkl, daily bars from 2012 (yfinance, adjusted, real volume)."""
import pickle, time
from pathlib import Path
import pandas as pd
import yfinance as yf

RES = Path("/opt/kronos_bot/research")
D = pickle.load(open(RES / "idx_data.pkl", "rb"))
names = sorted(set(D["n50"]) | set(D["bank"]))


def dl(sym):
    for _ in range(3):
        try:
            d = yf.download(sym, start="2012-01-01", interval="1d", progress=False, auto_adjust=True)
            d.columns = [c[0] if isinstance(c, tuple) else c for c in d.columns]
            d = d.dropna(); d.index = pd.to_datetime(d.index).tz_localize(None).normalize()
            return d[["Open", "High", "Low", "Close", "Volume"]]
        except Exception:
            time.sleep(2)
    return None


stocks = {}
for s in names:
    d = dl(f"{s}.NS")
    if d is not None and len(d) > 300:
        stocks[s] = d
idx = {"NIFTY": dl("^NSEI"), "BANKNIFTY": dl("^NSEBANK")}
print(f"stocks {len(stocks)}/{len(names)}; first bar by name (earliest 8 / latest 8):")
first = sorted(((d.index[0], s, len(d)) for s, d in stocks.items()))
for f, s, n in first[:8]: print(f"  {s:<12}{f.date()}  {n} bars")
print("  ...")
for f, s, n in first[-8:]: print(f"  {s:<12}{f.date()}  {n} bars")
for k, v in idx.items():
    print(f"{k}: {len(v)} bars {v.index[0].date()} -> {v.index[-1].date()}; zero-volume share {(v['Volume']==0).mean()*100:.0f}%")
pickle.dump({"stocks": stocks, "idx": idx, "n50": [s for s in D["n50"] if s in stocks], "bank": [s for s in D["bank"] if s in stocks]},
            open(RES / "idx_data_long.pkl", "wb"))
print("saved idx_data_long.pkl")
