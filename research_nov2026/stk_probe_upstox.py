"""Can the live bot get NIFTY 50 stock data from Upstox that matches the research data (yfinance, adjusted, real volume)?
Checks: NSE_EQ keys + FUT lots from the instrument master; daily history vs yfinance (close, volume); quote volume field."""
import gzip, json, pickle, urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np
import pandas as pd
import requests

RES = Path("/opt/kronos_bot/research")
IST = timezone(timedelta(hours=5, minutes=30)); now = datetime.now(IST)
H = {"Authorization": "Bearer " + open("/opt/kronos_bot/repo/logs/upstox_token.txt").readline().strip(), "Accept": "application/json"}
syms = json.load(open(RES / "nifty50_syms.json"))
prices, lots_yf, nifty = pickle.load(open(RES / "stk_prices.pkl", "rb"))

r = requests.get("https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz", timeout=90)
master = json.loads(gzip.decompress(r.content))
eq = {x["trading_symbol"]: x for x in master if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ"}
fut = {}
for x in master:
    if x.get("segment") == "NSE_FO" and x.get("instrument_type") == "FUT" and x["expiry"] / 1000 > now.timestamp():
        s = x.get("underlying_symbol")
        if s and (s not in fut or x["expiry"] < fut[s]["expiry"]):
            fut[s] = x
rows, miss = [], []
for s in syms:
    if s in eq and s in fut:
        rows.append({"symbol": s, "key": eq[s]["instrument_key"], "isin": eq[s].get("isin"), "lot": int(fut[s]["lot_size"]),
                     "fut_key": fut[s]["instrument_key"], "fut_expiry": datetime.fromtimestamp(fut[s]["expiry"] / 1000, IST).strftime("%Y-%m-%d")})
    else:
        miss.append((s, s in eq, s in fut))
print(f"NIFTY 50 -> NSE_EQ key + FUT lot found for {len(rows)}/{len(syms)}; missing: {miss}")
json.dump(rows, open(RES / "stock_universe_probe.json", "w"), indent=1)


def daily(key, bars=170):
    enc = urllib.parse.quote(key, safe="")
    frm = (now - timedelta(days=int(bars * 1.6) + 10)).strftime("%Y-%m-%d"); to = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    j = requests.get(f"https://api.upstox.com/v2/historical-candle/{enc}/day/{to}/{frm}", headers=H, timeout=20).json()
    d = pd.DataFrame(j["data"]["candles"], columns=["datetime", "open", "high", "low", "close", "volume", "oi"])
    d["datetime"] = pd.to_datetime(d["datetime"]).dt.tz_localize(None).dt.normalize()
    return d.drop(columns=["oi"]).sort_values("datetime").set_index("datetime")


print(f"\n{'symbol':<12}{'bars':>5}{'last bar':>12}{'max |dclose|%':>14}{'med vol ratio':>14}{'quote last':>11}{'quote vol':>12}{'bar vol':>12}{'q/bar':>7}")
for rw in rows[:50:3]:
    s = rw["symbol"]
    try:
        d = daily(rw["key"])
        y = prices[s]; y.index = pd.to_datetime(y.index).normalize()
        j = d.join(y[["Close", "Volume"]], how="inner").tail(120)
        dc = ((j["close"] / j["Close"]) - 1).abs().max() * 100
        vr = (j["volume"] / j["Volume"]).median()
        q = requests.get("https://api.upstox.com/v2/market-quote/quotes?instrument_key=" + urllib.parse.quote(rw["key"], safe=""), headers=H, timeout=15).json()
        qd = list(q["data"].values())[0]
        lastbar = d.iloc[-1]
        print(f"{s:<12}{len(d):>5}{str(d.index[-1].date()):>12}{dc:>14.2f}{vr:>14.3f}{qd['last_price']:>11.2f}{qd.get('volume', float('nan')):>12,.0f}{lastbar['volume']:>12,.0f}{qd.get('volume', 0)/max(lastbar['volume'],1):>7.2f}")
    except Exception as e:
        print(f"{s:<12} ERROR {type(e).__name__}: {e}")
print("\nquote fields:", sorted(qd.keys()))
