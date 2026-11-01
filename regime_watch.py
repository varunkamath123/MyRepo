"""
Kronos Futures -- India "long" regime watch (report only)
=========================================================
Turns a non-quantitative thesis into observable, fixed-in-advance footprints and
reports them once a day. It NEVER trades, never touches paper_state.json, and
changes nothing in the bot. Its only outputs are a printed report,
regime_watch_data/regime_watch_latest.txt and a one-row-per-day CSV log.

Thesis (Oct 31 2026): AI-trade stress rotates foreign money toward India; the
footprint would be FII index-futures shorts being covered then re-longed, price
breaking out, INR/crude easing, and futures participation rising. Narrative
alone is not a signal, so each leg has a threshold FIXED HERE, in advance:

  T1 squeeze  FII index-futures net short has shrunk >= 20% vs 20 sessions ago
  T2 re-long  FII long share of index-futures OI >= 15% (covering alone is not enough)
  T3 breakout NIFTY close above the highest close of the prior 20 sessions
  T4 participation  near+next futures volume >= 1.2x the median of our own last
                    20 logged days AND OI rising on an up day. n/a until 10 days
                    are logged (the roll makes historical contract volume
                    non-comparable, so the baseline is built from our own log)
  T5 macro    USD/INR and Brent both flat-or-lower over 5 sessions
  T6 AI stress easing  SOXX 5-session return >= -2%

HISTORY CHECK (run Nov 4 2026, `--history-test`, 701 sessions Nov 2023 - Oct 2026): these footprints have
NOT predicted higher NIFTY returns. Episodes (clusters >14 days apart), average forward return vs the
unconditional +0.16% over 10 sessions:  T1 alone 15 episodes +0.25% (perm p 0.44);  T3 alone 19 episodes
-0.56%, 32% up (p 0.90);  T1 & T3 14 episodes -0.25%, 29% up (p 0.74);  T1 & T3 & T2 13 episodes -0.12% (p 0.65).
Correlation of the 20-session change in FII net short with the next 10-session NIFTY return: +0.04.
T2 is true on most historical days (median FII long share 20.5%), so it adds nothing there; today's 12% is unusual.
Treat this as a dashboard of footprints, NOT as an entry signal, until forward data says otherwise.

State: ALERT = T1 and T3 and at least 2 of {T2, T4, T5, T6};
       WATCH = score >= 3 of the evaluable triggers (or T1 and T3 only);
       otherwise NONE.  An ALERT is a prompt to LOOK, not a trade signal.

Data: NSE participant-wise OI (archives.nseindia.com, public CSV, needs a
browser User-Agent), yfinance (NIFTY, USDINR, Brent, SOXX), Upstox index
futures (volume/OI/basis; token at logs/upstox_token.txt).

Usage:
    python regime_watch.py                   # daily report (cron 09:00 IST: previous session's data)
    python regime_watch.py --backfill 760    # cache ~3y of FII participant OI (one-off, polite throttle)
    python regime_watch.py --history-test    # does T1/T2/T3 history show anything? (needs --backfill first)
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

HERE = Path(__file__).parent
IST = timezone(timedelta(hours=5, minutes=30))
NSE_URL = "https://archives.nseindia.com/content/nsccl/fao_participant_oi_{d}.csv"
UA = {"User-Agent": "Mozilla/5.0"}

# Thresholds -- fixed in advance (see module docstring)
T1_SHRINK = 0.80          # net short today <= 0.80 x net short 20 sessions ago
T2_LONG_SHARE = 0.15
T3_LOOKBACK = 20
T4_VOL_MULT = 1.20
T4_MIN_DAYS = 10
T6_SOXX_FLOOR = -0.02


# ── FII participant OI cache ────────────────────────────────────────────────

def _fetch_fii_day(d: date) -> dict | None:
    r = requests.get(NSE_URL.format(d=d.strftime("%d%m%Y")), headers=UA, timeout=20)
    if r.status_code != 200 or "Client Type" not in r.text:
        return None
    df = pd.read_csv(io.StringIO(r.text), skiprows=1)
    df.columns = [c.strip() for c in df.columns]
    df["Client Type"] = df["Client Type"].astype(str).str.strip()
    out = {"date": d.isoformat()}
    for who in ("FII", "Client", "DII", "Pro"):
        row = df[df["Client Type"] == who]
        if row.empty:
            return None
        out[f"{who.lower()}_long"] = int(row["Future Index Long"].iloc[0])
        out[f"{who.lower()}_short"] = int(row["Future Index Short"].iloc[0])
    return out


def update_fii(data_dir: Path, start: date, end: date, throttle: float = 0.15) -> pd.DataFrame:
    """Fetch every weekday in [start, end] not already cached/known-missing. Returns the full cache."""
    csv = data_dir / "fii_oi.csv"
    miss_f = data_dir / "fii_missing.txt"
    have = pd.read_csv(csv) if csv.exists() else pd.DataFrame(columns=["date"])
    seen = set(have["date"].astype(str))
    missing = set(miss_f.read_text().split()) if miss_f.exists() else set()
    rows, d = [], start
    while d <= end:
        if d.weekday() < 5 and d.isoformat() not in seen and d.isoformat() not in missing:
            failed = False
            try:
                rec = _fetch_fii_day(d)
            except Exception:
                rec, failed = None, True                      # transient network error: retry next run
                time.sleep(1.0)
            if rec:
                rows.append(rec)
            elif not failed and d <= date.today() - timedelta(days=3):   # holiday / never published; don't retry
                missing.add(d.isoformat())
            time.sleep(throttle)
        d += timedelta(days=1)
    if rows:
        have = pd.concat([have, pd.DataFrame(rows)], ignore_index=True)
    have = have.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    have.to_csv(csv, index=False)
    miss_f.write_text("\n".join(sorted(missing)))
    return have


def fii_series(fii: pd.DataFrame) -> pd.DataFrame:
    f = fii.copy()
    f["date"] = pd.to_datetime(f["date"])
    f = f.set_index("date").sort_index()
    f["net_short"] = f["fii_short"] - f["fii_long"]                 # positive = net short
    f["long_share"] = f["fii_long"] / (f["fii_long"] + f["fii_short"])
    return f


# ── Market data ─────────────────────────────────────────────────────────────

def _yf(tickers: dict[str, str], period: str = "6mo") -> pd.DataFrame:
    import yfinance as yf
    cols = {}
    for k, sym in tickers.items():
        try:
            d = yf.download(sym, period=period, interval="1d", progress=False, auto_adjust=True)
            d.columns = [c[0] if isinstance(c, tuple) else c for c in d.columns]
            d.index = pd.to_datetime(d.index).tz_localize(None)
            cols[k] = d["Close"].dropna()
        except Exception:
            pass
    return pd.DataFrame(cols)


def upstox_futures(und: str = "NIFTY") -> dict | None:
    """Near+next index-futures volume/OI/basis for the last session. None if Upstox is unavailable."""
    try:
        tok = (HERE / "logs" / "upstox_token.txt").read_text().splitlines()[0].strip()
        H = {"Authorization": f"Bearer {tok}", "Accept": "application/json"}
        now = datetime.now(IST)
        inst = json.loads(gzip.decompress(requests.get(
            "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz", timeout=90).content))
        rows = sorted([x for x in inst if x.get("segment") == "NSE_FO" and x.get("instrument_type") == "FUT"
                       and x.get("underlying_symbol") == und and x["expiry"] / 1000 > now.timestamp()],
                      key=lambda x: x["expiry"])[:2]

        def candles(key):
            enc = urllib.parse.quote(key, safe="")
            r = requests.get(f"https://api.upstox.com/v2/historical-candle/{enc}/day/"
                             f"{now.date() + timedelta(days=1)}/{now.date() - timedelta(days=45)}", headers=H, timeout=30)
            df = pd.DataFrame(r.json()["data"]["candles"], columns=["dt", "o", "h", "l", "c", "v", "oi"])
            df["date"] = pd.to_datetime(df["dt"]).dt.tz_localize(None).dt.normalize()
            return df.drop_duplicates("date").sort_values("date").set_index("date")

        near, nxt = candles(rows[0]["instrument_key"]), candles(rows[1]["instrument_key"])
        spot = candles({"NIFTY": "NSE_INDEX|Nifty 50", "BANKNIFTY": "NSE_INDEX|Nifty Bank"}[und])
        last = near.index[-1]
        prev = near.index[-2]
        return {
            "date": last.date().isoformat(),
            "vol_total": float(near["v"].iloc[-1] + (nxt["v"].get(last, 0.0))),
            "oi_total": float(near["oi"].iloc[-1] + (nxt["oi"].get(last, 0.0))),
            "near_oi_chg": float(near["oi"].iloc[-1] / near["oi"].iloc[-2] - 1) if near["oi"].iloc[-2] else float("nan"),
            "near_px_chg": float(near["c"].iloc[-1] / near["c"].iloc[-2] - 1),
            "basis_pct": float((near["c"].iloc[-1] / spot["c"].get(last, np.nan) - 1) * 100),
            "near_symbol": rows[0]["trading_symbol"],
            "prev_date": prev.date().isoformat(),
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ── Triggers ────────────────────────────────────────────────────────────────

def evaluate(f: pd.DataFrame, px: pd.DataFrame, fut: dict | None, log: pd.DataFrame) -> dict:
    asof = min(f.index[-1], px["NIFTY"].dropna().index[-1])
    ff = f[f.index <= asof]
    nifty = px["NIFTY"].dropna()
    nifty = nifty[nifty.index <= asof]
    out = {"asof": asof.date().isoformat(), "nifty_close": float(nifty.iloc[-1]),
           "fii_long": int(ff["fii_long"].iloc[-1]), "fii_short": int(ff["fii_short"].iloc[-1]),
           "net_short": int(ff["net_short"].iloc[-1]), "long_share": float(ff["long_share"].iloc[-1])}
    # T1
    if len(ff) > 20 and ff["net_short"].iloc[-21] > 0:
        out["net_short_20ago"] = int(ff["net_short"].iloc[-21])
        out["T1"] = bool(ff["net_short"].iloc[-1] <= T1_SHRINK * ff["net_short"].iloc[-21])
    else:
        out["T1"] = None
    # T2
    out["T2"] = bool(out["long_share"] >= T2_LONG_SHARE)
    # T3
    prior = nifty.iloc[-(T3_LOOKBACK + 1):-1]
    out["T3"] = bool(nifty.iloc[-1] > prior.max()) if len(prior) == T3_LOOKBACK else None
    out["prior20_high_close"] = float(prior.max()) if len(prior) else float("nan")
    # T4 (baseline from our own log)
    hist = log["fut_vol_total"].dropna() if "fut_vol_total" in log else pd.Series(dtype=float)
    hist = hist[log.loc[hist.index, "asof"] < out["asof"]] if len(hist) else hist
    out["T4_days_logged"] = int(len(hist))
    if fut and "error" not in fut and len(hist) >= T4_MIN_DAYS:
        base = float(hist.tail(20).median())
        up_build = fut["near_px_chg"] > 0 and fut["near_oi_chg"] > 0
        out["fut_vol_x_median"] = float(fut["vol_total"] / base)
        out["T4"] = bool(fut["vol_total"] >= T4_VOL_MULT * base and up_build)
    else:
        out["T4"] = None
    # T5
    def chg5(col):
        s = px[col].dropna() if col in px else pd.Series(dtype=float)
        s = s[s.index <= asof + timedelta(days=1)]
        return float(s.iloc[-1] / s.iloc[-6] - 1) if len(s) > 6 else float("nan")
    inr, brent, soxx = chg5("USDINR"), chg5("BRENT"), chg5("SOXX")
    out.update(usdinr_5d=inr, brent_5d=brent, soxx_5d=soxx)
    out["T5"] = bool(inr <= 0 and brent <= 0) if not (np.isnan(inr) or np.isnan(brent)) else None
    out["T6"] = bool(soxx >= T6_SOXX_FLOOR) if not np.isnan(soxx) else None
    ev = {k: out[k] for k in ("T1", "T2", "T3", "T4", "T5", "T6") if out[k] is not None}
    score = sum(1 for v in ev.values() if v)
    out["score"], out["evaluable"] = score, len(ev)
    others = sum(1 for k in ("T2", "T4", "T5", "T6") if out.get(k))
    if out["T1"] and out["T3"] and others >= 2:
        out["state"] = "ALERT"
    elif score >= 3 or (out["T1"] and out["T3"]):
        out["state"] = "WATCH"
    else:
        out["state"] = "NONE"
    return out


def render(o: dict, fut: dict | None, hist_net: pd.Series) -> str:
    tick = lambda v: "n/a" if v is None else ("YES" if v else "no ")
    L = [f"INDIA LONG REGIME WATCH   as of {o['asof']} (previous session)   -> {o['state']}  "
         f"[{o['score']}/{o['evaluable']} evaluable triggers true]", "=" * 78]
    L.append(f"NIFTY {o['nifty_close']:,.0f}   prior-20-session high close {o['prior20_high_close']:,.0f}")
    L.append(f"FII index futures: long {o['fii_long']:,}  short {o['fii_short']:,}  net short {o['net_short']:,}  "
             f"long share {o['long_share']*100:.1f}%")
    if "net_short_20ago" in o:
        L.append(f"   net short 20 sessions ago {o['net_short_20ago']:,}  -> now {o['net_short']/o['net_short_20ago']*100:.0f}% of that "
                 f"(T1 needs <= {T1_SHRINK*100:.0f}%)")
    L.append("")
    L.append(f"T1 squeeze (FII net short shrinking)      {tick(o['T1'])}")
    L.append(f"T2 re-long (FII long share >= {T2_LONG_SHARE*100:.0f}%)        {tick(o['T2'])}   now {o['long_share']*100:.1f}%")
    L.append(f"T3 breakout (NIFTY > prior 20-ses high)   {tick(o['T3'])}")
    extra = f"{o['fut_vol_x_median']:.2f}x median" if "fut_vol_x_median" in o else f"baseline {o['T4_days_logged']}/{T4_MIN_DAYS} days logged"
    L.append(f"T4 participation (vol + OI build)         {tick(o['T4'])}   {extra}")
    L.append(f"T5 macro (USD/INR & Brent not rising)     {tick(o['T5'])}   USD/INR 5d {o['usdinr_5d']*100:+.2f}%  Brent 5d {o['brent_5d']*100:+.2f}%")
    L.append(f"T6 AI stress easing (SOXX 5d >= -2%)      {tick(o['T6'])}   SOXX 5d {o['soxx_5d']*100:+.1f}%")
    if fut and "error" not in fut:
        L.append("")
        L.append(f"Futures ({fut['near_symbol']}, last bar {fut['date']}): near+next volume {fut['vol_total']:,.0f}, "
                 f"OI {fut['oi_total']:,.0f}, near-month price {fut['near_px_chg']*100:+.2f}% / OI {fut['near_oi_chg']*100:+.1f}%, "
                 f"basis {fut['basis_pct']:+.2f}%  (OI changes around expiry/roll are distorted)")
    elif fut:
        L.append(f"\nFutures data unavailable: {fut.get('error')}")
    L.append("")
    L.append("Net short, recent sessions (FII index futures): " + "  ".join(
        f"{d.strftime('%d-%b')} {v/1000:,.0f}k" for d, v in hist_net.tail(6).items()))
    L.append("ALERT = look closer, not a trade signal. FII cash flows are not automated here -- check NSE's provisional FII/DII table.")
    return "\n".join(L)


def run_daily(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    today = date.today()
    fii = update_fii(data_dir, today - timedelta(days=45), today)
    f = fii_series(fii)
    px = _yf({"NIFTY": "^NSEI", "USDINR": "USDINR=X", "BRENT": "BZ=F", "SOXX": "SOXX"})
    fut = upstox_futures("NIFTY")
    log_f = data_dir / "regime_watch_log.csv"
    log = pd.read_csv(log_f) if log_f.exists() else pd.DataFrame(columns=["asof"])
    o = evaluate(f, px, fut, log)
    txt = render(o, fut, f["net_short"])
    print(txt)
    (data_dir / "regime_watch_latest.txt").write_text(txt + "\n")
    row = {k: o.get(k) for k in ("asof", "nifty_close", "fii_long", "fii_short", "net_short", "long_share",
                                 "T1", "T2", "T3", "T4", "T5", "T6", "score", "evaluable", "state")}
    if fut and "error" not in fut:
        row.update(fut_vol_total=fut["vol_total"], fut_oi_total=fut["oi_total"], fut_basis_pct=fut["basis_pct"],
                   fut_date=fut["date"])
    log = log[log["asof"] != row["asof"]] if "asof" in log else log
    pd.concat([log, pd.DataFrame([row])], ignore_index=True).to_csv(log_f, index=False)


# ── Does the history say anything? ──────────────────────────────────────────

def history_test(data_dir: Path):
    f = fii_series(pd.read_csv(data_dir / "fii_oi.csv"))
    px = _yf({"NIFTY": "^NSEI"}, period="max")["NIFTY"].dropna()
    px = px[px.index >= f.index[0]]
    d = pd.DataFrame({"close": px}).join(f[["net_short", "long_share"]], how="inner").dropna()
    d["ns20"] = d["net_short"].shift(20)
    d["t1"] = (d["ns20"] > 0) & (d["net_short"] <= T1_SHRINK * d["ns20"])
    d["t2"] = d["long_share"] >= T2_LONG_SHARE
    d["t3"] = d["close"] > d["close"].shift(1).rolling(T3_LOOKBACK).max()
    for h in (5, 10, 20):
        d[f"f{h}"] = d["close"].shift(-h) / d["close"] - 1
    d = d.dropna(subset=["f20", "ns20"])
    rng = np.random.default_rng(11)
    print(f"{len(d)} sessions {d.index[0].date()} -> {d.index[-1].date()} | unconditional mean fwd return: "
          + "  ".join(f"{h}d {d[f'f{h}'].mean()*100:+.2f}%" for h in (5, 10, 20)))
    print(f"FII net short range {d['net_short'].min():,.0f} .. {d['net_short'].max():,.0f}; long share median {d['long_share'].median()*100:.1f}%")
    print(f"\n{'condition':<22}{'days':>5}{'episodes':>9}   " + "".join(f"{'fwd '+str(h)+'d (avg / up%)':>22}" for h in (5, 10, 20)) + "   perm p (10d)")
    for name, mask in [("T1 alone", d.t1), ("T3 alone", d.t3), ("T1 & T3 (core)", d.t1 & d.t3), ("T1 & T3 & T2", d.t1 & d.t3 & d.t2)]:
        sel = d[mask]
        if sel.empty:
            print(f"{name:<22}{0:>5}"); continue
        gaps = sel.index.to_series().diff().dt.days.fillna(999)
        ep = sel[gaps > 14]                                # one observation per cluster
        cells = "".join(f"{ep[f'f{h}'].mean()*100:>+11.2f}% /{(ep[f'f{h}']>0).mean()*100:>4.0f}% " for h in (5, 10, 20)) if len(ep) else ""
        obs = ep["f10"].mean() if len(ep) else np.nan
        cnt = sum(1 for _ in range(5000) if d["f10"].sample(len(ep), random_state=int(rng.integers(1e9))).mean() >= obs) if len(ep) else 0
        print(f"{name:<22}{len(sel):>5}{len(ep):>9}   {cells}   {cnt/5000 if len(ep) else float('nan'):.3f}")
    blk = d.iloc[::10]
    blk = blk[blk["ns20"] > 0]
    d_ns = (blk["net_short"] / blk["ns20"] - 1)
    print(f"\nContinuous check (non-overlapping 10-session blocks, n={len(blk)}): corr(20-session change in FII net short, "
          f"next-10-session NIFTY return) = {d_ns.corr(blk['f10']):+.2f}")


def main():
    ap = argparse.ArgumentParser(description="India long regime watch (report only)")
    ap.add_argument("--data-dir", default=str(HERE / "regime_watch_data"))
    ap.add_argument("--backfill", type=int, metavar="DAYS", help="cache FII participant OI for the last DAYS calendar days")
    ap.add_argument("--history-test", action="store_true")
    a = ap.parse_args()
    dd = Path(a.data_dir); dd.mkdir(parents=True, exist_ok=True)
    if a.backfill:
        fii = update_fii(dd, date.today() - timedelta(days=a.backfill), date.today())
        print(f"cache: {len(fii)} sessions {fii['date'].iloc[0]} -> {fii['date'].iloc[-1]}")
    elif a.history_test:
        history_test(dd)
    else:
        run_daily(dd)


if __name__ == "__main__":
    main()
