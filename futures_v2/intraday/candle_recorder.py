"""End-of-day candle recorder: 1-minute and 10-minute bars (with volume and open interest for the futures) for NIFTY / BANKNIFTY
index + the near / next / far futures.  Upstox only serves candles for contracts that are still listed, so every day not recorded is
history that cannot be rebuilt later.

  python candle_recorder.py --today                 cron, 16:00 IST: today's session (holiday -> no bars -> exits quietly)
  python candle_recorder.py --backfill --days 130   everything the listed contracts still have (futures list ~3 months; index goes back further)
  python candle_recorder.py --check                 re-verify stored 10-minute bars against the stored 1-minute bars

After each capture the 10-minute bars are cross-checked against the 1-minute bars resampled to the same grid (session buckets start at 09:15)."""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C

log = logging.getLogger("candles")
EXPECT = {"index": {1: 375, 10: 38}, "future": {1: 385, 10: 39}}      # bars per normal session


def resample_10m(d1: pd.DataFrame) -> pd.DataFrame:
    """1-minute bars -> 10-minute bars on the exchange grid (buckets start 09:15, 09:25, ...)."""
    x = d1.set_index("ts")
    r = x.resample("10min", offset="5min").agg({"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum", "oi": "last"}).dropna(subset=["o"])
    return r.reset_index()


def consistency(d1: pd.DataFrame, d10: pd.DataFrame, tol: float = 1e-6) -> dict:
    """Compare stored 10-minute bars with the resampled 1-minute bars (OHLC exact, volume exact)."""
    r = resample_10m(d1).merge(d10, on="ts", suffixes=("_r", "_u"), how="outer", indicator=True)
    both = r[r["_merge"] == "both"]
    bad_px = ((both[["o_r", "h_r", "l_r", "c_r"]].values - both[["o_u", "h_u", "l_u", "c_u"]].values).__abs__() > tol).any(axis=1).sum()
    bad_v = (both["v_r"] != both["v_u"]).sum()
    return {"bars": len(both), "only_resampled": int((r["_merge"] == "left_only").sum()), "only_upstox": int((r["_merge"] == "right_only").sum()),
            "px_mismatch": int(bad_px), "vol_mismatch": int(bad_v)}


def log_contracts(today: date):
    path = C.DATA / "contracts.csv"
    rows = [dict(date=today.isoformat(), underlying=c["underlying"], rank=c["rank"], symbol=c["symbol"], key=c["key"], expiry=c["expiry"], lot=c["lot"])
            for u, cs in C.ranked_contracts(today).items() for c in cs]
    new = pd.DataFrame(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        old = pd.read_csv(path)
        old = old[old["date"] != today.isoformat()]
        new = pd.concat([old, new])
    new.to_csv(path, index=False)


def record_today() -> int:
    today = C.now_ist().date()
    got_any = False
    for t in C.targets(today):
        res = {}
        for iv in (1, 10):
            try:
                d = C.candles_intraday(t["key"], "minutes", iv)
            except Exception as e:
                log.error("[%s] %dm fetch failed: %s", t["label"], iv, e); continue
            d = d[d["ts"].dt.date == today] if len(d) else d
            if len(d) == 0:
                continue
            got_any = True
            total = C.merge_write(C.candle_path(t["label"], iv), d)
            res[iv] = d
            exp = EXPECT[t["kind"]][iv]
            note = "" if abs(len(d) - exp) <= 2 else f"  <-- expected ~{exp}"
            log.info("[%s] %2dm: %d bars %s -> %s  vol %s  oi %s  (file %d rows)%s", t["label"], iv, len(d), d.ts.iloc[0].strftime("%H:%M"),
                     d.ts.iloc[-1].strftime("%H:%M"), f"{int(d.v.sum()):,}", f"{int(d.oi.iloc[-1]):,}" if d.oi.iloc[-1] else "-", total, note)
        if 1 in res and 10 in res:
            c = consistency(res[1], res[10])
            level = log.info if (c["px_mismatch"] == 0 and c["vol_mismatch"] == 0) else log.warning
            level("[%s] 10m vs resampled 1m: %s", t["label"], c)
    if not got_any:
        log.info("no bars for %s: market closed (holiday) -- nothing recorded", today)
        return 0
    log_contracts(today)
    return 0


def backfill(days: int) -> int:
    today = C.now_ist().date(); frm = today - timedelta(days=days)
    for t in C.targets(today):
        for iv in (1, 10):
            try:
                d = C.candles_chunked(t["key"], "minutes", iv, frm, today)
            except Exception as e:
                log.error("[%s] %dm backfill failed: %s", t["label"], iv, e); continue
            if len(d) == 0:
                log.info("[%s] %dm: no history", t["label"], iv); continue
            total = C.merge_write(C.candle_path(t["label"], iv), d)
            log.info("[%s] %2dm backfill: %d bars %s -> %s (file %d rows)", t["label"], iv, len(d), d.ts.iloc[0], d.ts.iloc[-1], total)
    log_contracts(today)
    return 0


def check() -> int:
    bad = 0
    for t in C.targets():
        p1, p10 = C.candle_path(t["label"], 1), C.candle_path(t["label"], 10)
        if not (p1.exists() and p10.exists()):
            continue
        d1, d10 = pd.read_csv(p1, parse_dates=["ts"]), pd.read_csv(p10, parse_dates=["ts"])
        c = consistency(d1, d10)
        log.info("[%s] %s", t["label"], c)
        bad += c["px_mismatch"] + c["vol_mismatch"]
    return 1 if bad else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--today", action="store_true"); ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--check", action="store_true"); ap.add_argument("--days", type=int, default=130)
    a = ap.parse_args()
    sys.exit(backfill(a.days) if a.backfill else check() if a.check else record_today())
