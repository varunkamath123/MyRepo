"""What does a 10-minute futures strategy on NIFTY / BANKNIFTY start from?  Data availability (Upstox v3 candles) and the economics of a 10-min bar."""
import gzip, json, urllib.parse
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
import requests

IST = timezone(timedelta(hours=5, minutes=30)); now = datetime.now(IST)
H = {"Authorization": "Bearer " + open("/opt/kronos_bot/repo/logs/upstox_token.txt").readline().strip(), "Accept": "application/json"}


def candles(key, unit, interval, frm, to):
    url = f"https://api.upstox.com/v3/historical-candle/{urllib.parse.quote(key, safe='')}/{unit}/{interval}/{to}/{frm}"
    r = requests.get(url, headers=H, timeout=30)
    if r.status_code != 200:
        return None, r.text[:160]
    c = r.json().get("data", {}).get("candles", [])
    d = pd.DataFrame(c, columns=["ts", "o", "h", "l", "c", "v", "oi"])
    if d.empty: return d, "empty"
    d["ts"] = pd.to_datetime(d["ts"]).dt.tz_localize(None)
    return d.sort_values("ts").reset_index(drop=True), "ok"


master = json.loads(gzip.decompress(requests.get("https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz", timeout=90).content))
futs = {}
for x in master:
    if x.get("segment") == "NSE_FO" and x.get("instrument_type") == "FUT" and x.get("underlying_symbol") in ("NIFTY", "BANKNIFTY") and x["expiry"] / 1000 > now.timestamp():
        s = x["underlying_symbol"]
        if s not in futs or x["expiry"] < futs[s]["expiry"]:
            futs[s] = x
print("near-month futures:", {k: (v["instrument_key"], datetime.fromtimestamp(v["expiry"] / 1000, IST).date().isoformat(), "lot", v["lot_size"]) for k, v in futs.items()})

today = now.date()
print("\n--- availability: v3 10-minute candles, how far back can one request reach? ---")
for key, name in (("NSE_INDEX|Nifty 50", "NIFTY index"), (futs["NIFTY"]["instrument_key"], "NIFTY near FUT")):
    for back in (30, 120, 400, 1200):
        frm = (today - timedelta(days=back)).isoformat(); to = (today - timedelta(days=max(back - 28, 0))).isoformat()
        d, msg = candles(key, "minutes", 10, frm, to)
        print(f"  {name:<16} window {frm}..{to}: {('%d bars, %s -> %s' % (len(d), d.ts.iloc[0], d.ts.iloc[-1])) if d is not None and len(d) else msg}")

print("\n--- economics of a 10-minute bar, last ~60 sessions ---")
for key, name, fk in (("NSE_INDEX|Nifty 50", "NIFTY", futs["NIFTY"]["instrument_key"]), ("NSE_INDEX|Nifty Bank", "BANKNIFTY", futs["BANKNIFTY"]["instrument_key"])):
    parts = []
    for i in range(3):
        to = (today - timedelta(days=28 * i)).isoformat(); frm = (today - timedelta(days=28 * (i + 1))).isoformat()
        d, msg = candles(key, "minutes", 10, frm, to)
        if d is not None and len(d): parts.append(d)
    d = pd.concat(parts).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    d["date"] = d.ts.dt.date; d["tod"] = d.ts.dt.strftime("%H:%M")
    d["ret"] = d.c.pct_change() * 100; d.loc[d.date != d.date.shift(), "ret"] = np.nan           # within-day returns only
    d["rng"] = (d.h - d.l) / d.c * 100
    per_day = d.groupby("date").size()
    print(f"\n{name}: {len(d)} bars over {d.date.nunique()} sessions ({per_day.mode().iloc[0]} bars/session), {d.ts.iloc[0].date()} -> {d.ts.iloc[-1].date()}")
    sd = d.ret.std(); rg = d.rng.mean()
    cost = 0.03
    print(f"  10-min return sd {sd:.3f}% | mean bar range {rg:.3f}% | all-in round-trip cost assumed {cost:.2f}% = {cost/sd*100:.0f}% of one sd, {cost/rg*100:.0f}% of a mean bar range")
    r = d.ret.dropna()
    ac = [r.autocorr(l) for l in (1, 2, 3)]
    print(f"  autocorrelation of 10-min returns, lags 1/2/3: {ac[0]:+.3f} / {ac[1]:+.3f} / {ac[2]:+.3f}  (|0.1| would matter; noise band about +/-{1.96/np.sqrt(len(r)):.3f})")
    tod = d.groupby("tod").agg(sd=("ret", "std"), rng=("rng", "mean"), n=("ret", "count"))
    sel = ["09:15", "09:25", "10:15", "12:15", "13:15", "14:15", "15:05", "15:15", "15:25"]
    print("  time-of-day bar range % (mean): " + "  ".join(f"{t} {tod.loc[t, 'rng']:.3f}" for t in sel if t in tod.index))
    # overnight vs intraday
    day = d.groupby("date").agg(o=("o", "first"), c=("c", "last"))
    intr = (day.c / day.o - 1) * 100; ovn = (day.o / day.c.shift() - 1) * 100
    print(f"  per-session: intraday move sd {intr.std():.2f}% (mean {intr.mean():+.3f}%), overnight gap sd {ovn.std():.2f}% (mean {ovn.mean():+.3f}%)")
    fd, msg = candles(fk, "minutes", 10, (today - timedelta(days=28)).isoformat(), today.isoformat())
    if fd is not None and len(fd):
        print(f"  near-month future 10-min bars (last 4 weeks): {len(fd)} bars, mean volume/bar {fd.v.mean():,.0f}, OI field present: {bool((fd.oi > 0).any())}; index candles volume: {d.v.sum():,.0f}")
