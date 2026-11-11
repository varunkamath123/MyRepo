"""Market-hours recorder: polls one batched Upstox quote every N seconds for the near and next NIFTY / BANKNIFTY futures (5-level depth,
total buy/sell quantity, volume, open interest, average price) and both index spot quotes, and appends one JSON line per instrument.
Spread, depth imbalance and order-book pressure cannot be rebuilt after the fact, and the measured spread is what the cost model needs.

  python depth_recorder.py                 cron, 09:10 IST Mon-Fri; runs until 15:45 IST, then compresses the day's files and exits
  python depth_recorder.py --force 4       4 polls right now regardless of the clock (testing; writes to a separate test folder)

A holiday is detected from the index quote's last-trade time (not today after 09:25 -> exit).  A lock file stops a second copy.
Line format (futures):  {"t": ISO time with ms, "s": symbol, "ltp", "v", "oi", "avg", "tbq", "tsq", "b": [[price, qty, orders] x5], "a": [...],
                         "o","h","l","c" (day open/high/low, previous close), "ltt": last trade time ms}
Index lines carry ltp, o/h/l/c and ltt only."""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import shutil
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

try:
    import fcntl
except ImportError:                                  # Windows (tests only)
    fcntl = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C

log = logging.getLogger("depth")
START, END, HOLIDAY_CHECK = dtime(9, 10), dtime(15, 45), dtime(9, 25)


def _levels(side: list) -> list:
    return [[x.get("price"), x.get("quantity"), x.get("orders")] for x in (side or [])[:5]]


def record(v: dict, label: str, ts: str, future: bool) -> dict:
    oh = v.get("ohlc") or {}
    rec = {"t": ts, "s": label, "ltp": v.get("last_price"), "o": oh.get("open"), "h": oh.get("high"), "l": oh.get("low"), "c": oh.get("close"),
           "ltt": int(v["last_trade_time"]) if v.get("last_trade_time") else None}
    if future:
        d = v.get("depth") or {}
        rec.update(v=v.get("volume"), oi=v.get("oi"), avg=v.get("average_price"), tbq=v.get("total_buy_quantity"), tsq=v.get("total_sell_quantity"),
                   b=_levels(d.get("buy")), a=_levels(d.get("sell")))
    return rec


def compress_day(folder: Path):
    for p in folder.glob("*.jsonl"):
        with open(p, "rb") as f, gzip.open(str(p) + ".gz", "wb") as g:
            shutil.copyfileobj(f, g)
        p.unlink()


def main(interval: int, force: int | None) -> int:
    today = C.now_ist().date()
    test = force is not None
    day_dir = C.DATA / ("depth_test" if test else "depth") / today.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    lock = open(day_dir / ".lock", "w")
    if fcntl is not None:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.info("another recorder already holds the lock for %s -- exiting", today); return 0
    for old in (C.DATA / ("depth_test" if test else "depth")).glob("*"):         # compress leftovers from a crashed day
        if old.is_dir() and old.name != today.isoformat():
            compress_day(old)

    ranked = C.ranked_contracts(today)
    watch = {}                                                                     # instrument_token -> (label, is_future, is_index)
    for u in C.UNDERLYINGS:
        watch[C.INDEX_KEYS[u]] = (f"{u}_IDX", False, True)
        for c in ranked[u][:2]:
            watch[c["key"]] = (c["symbol"], True, False)
    keys = list(watch)
    log.info("recording %s every %ds into %s", [w[0] for w in watch.values()], interval, day_dir)
    handles = {w[0]: open(day_dir / f"{w[0]}.jsonl", "a") for w in watch.values()}
    fails, polls, ok_polls = 0, 0, 0
    try:
        while True:
            t = C.now_ist()
            if not test and t.time() > END:
                break
            if not test and t.time() < START:
                time.sleep(20); continue
            if test and polls >= force:
                break
            polls += 1
            t0 = time.time()
            try:
                q = C.quotes(keys)
                fails = 0
            except Exception as e:
                fails += 1
                log.warning("poll failed (%d in a row): %s", fails, e)
                if fails >= 60:
                    log.error("60 failed polls in a row -- giving up"); return 1
                time.sleep(5); continue
            ts = t.isoformat(timespec="milliseconds")
            fresh = False
            for tok, v in q.items():
                if tok not in watch:
                    continue
                label, is_fut, is_idx = watch[tok]
                handles[label].write(json.dumps(record(v, label, ts, is_fut), separators=(",", ":")) + "\n")
                if is_idx and v.get("last_trade_time"):
                    fresh = fresh or datetime.fromtimestamp(int(v["last_trade_time"]) / 1000, C.IST).date() == today
            for h in handles.values():
                h.flush()
            ok_polls += 1
            if not test and t.time() >= HOLIDAY_CHECK and not fresh and ok_polls > 3:
                log.info("index quotes are not from %s -- market closed (holiday); exiting", today); break
            time.sleep(max(0.0, interval - (time.time() - t0)))
    finally:
        for h in handles.values():
            h.close()
        compress_day(day_dir)
        log.info("done: %d polls written, files compressed in %s", ok_polls, day_dir)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=15)
    ap.add_argument("--force", type=int, default=None, help="N polls now, any time of day (test mode)")
    a = ap.parse_args()
    sys.exit(main(a.interval, a.force))
