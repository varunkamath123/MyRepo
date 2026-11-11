"""Build the dataset for the index-vs-constituents study: NIFTY 50 and BANKNIFTY constituent daily bars (cached F&O stock
prices where available, yfinance otherwise) plus NIFTY / BANKNIFTY index OHLC, 4 years daily.  Saved to idx_data.pkl."""
import io, json, pickle, time
from pathlib import Path
import pandas as pd
import requests
import yfinance as yf

RES = Path("/opt/kronos_bot/research")
prices, lots, _ = pickle.load(open(RES / "stk_prices.pkl", "rb"))
n50 = json.load(open(RES / "nifty50_syms.json"))
r = requests.get("https://archives.nseindia.com/content/indices/ind_niftybanklist.csv", headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
r.raise_for_status()
bank = pd.read_csv(io.StringIO(r.text))["Symbol"].str.strip().tolist()
print(f"NIFTY 50: {len(n50)} names | BANKNIFTY: {len(bank)} names: {bank}")


def dl(sym):
    for _ in range(2):
        try:
            d = yf.download(sym, period="4y", interval="1d", progress=False, auto_adjust=True)
            d.columns = [c[0] if isinstance(c, tuple) else c for c in d.columns]
            d = d.dropna(); d.index = pd.to_datetime(d.index).tz_localize(None).normalize()
            return d[["Open", "High", "Low", "Close", "Volume"]]
        except Exception:
            time.sleep(2)
    return None


stocks = {}
for s in sorted(set(n50) | set(bank)):
    if s in prices:
        d = prices[s].copy(); d.index = pd.to_datetime(d.index).normalize(); stocks[s] = d
    else:
        d = dl(f"{s}.NS")
        if d is not None and len(d) > 300:
            stocks[s] = d
            print(f"  fetched {s}: {len(d)} bars")
        else:
            print(f"  MISSING {s}")
idx = {"NIFTY": dl("^NSEI"), "BANKNIFTY": dl("^NSEBANK")}
for k, v in idx.items():
    print(f"{k}: {len(v)} bars {v.index[0].date()} -> {v.index[-1].date()}")
pickle.dump({"stocks": stocks, "idx": idx, "n50": [s for s in n50 if s in stocks], "bank": [s for s in bank if s in stocks]},
            open(RES / "idx_data.pkl", "wb"))
print("saved idx_data.pkl:", len(stocks), "stocks")
