import io, pickle, json
from pathlib import Path
import pandas as pd
import requests

RES = Path("/opt/kronos_bot/research")
r = requests.get("https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
                 headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
r.raise_for_status()
df = pd.read_csv(io.StringIO(r.text))
syms = df["Symbol"].str.strip().tolist()
print(f"NIFTY 50 list: {len(syms)} symbols")
prices, lots, nifty = pickle.load(open(RES / "stk_prices.pkl", "rb"))
turn = {s: float((d["Close"] * d["Volume"]).tail(250).median()) for s, d in prices.items()}
top60 = sorted(turn, key=turn.get, reverse=True)[:60]
have = [s for s in syms if s in prices]
missing = [s for s in syms if s not in prices]
print(f"  with 4y price data cached: {len(have)}; missing: {missing}")
print(f"  in current top-60 universe: {len([s for s in have if s in top60])}; NIFTY50 names NOT in top-60: {[s for s in have if s not in top60]}")
print(f"  top-60 names that are NOT in NIFTY 50: {[s for s in top60 if s not in syms]}")
bars = {s: len(prices[s]) for s in have}
print(f"  fewest bars: {min(bars, key=bars.get)} {min(bars.values())}")
Path(RES / "nifty50_syms.json").write_text(json.dumps(have))
reads = json.load(open(RES / "stk_kronos_reads.json"))
print(f"  Kronos reads cached so far: {len(reads)}")
