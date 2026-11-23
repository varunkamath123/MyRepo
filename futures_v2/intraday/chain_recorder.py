"""Option-chain recorder (Upstox /v2/option/chain): a full-depth snapshot every 5 minutes of the NIFTY and BANKNIFTY option chains.
No historical option quotes exist for expired contracts, so a chain that is not recorded is gone for good; every study of implied volatility, skew, term structure,
open interest by strike, option spreads or the cost of hedging needs this history.

  python chain_recorder.py                cron 09:10 IST Mon-Fri; waits for each 5-minute slot from 09:15 to 15:30, then compresses the day and exits
  python chain_recorder.py --force 2      2 snapshots right now regardless of the clock (testing; writes to options_test/)

Coverage per snapshot
  NIFTY      the next 3 expiries (weeklies) + the next monthly expiry if it is not among them
  BANKNIFTY  the next 2 expiries (monthlies; there are no BANKNIFTY weeklies)
  strikes within +/-10% of spot; per strike and side: ltp, bid, bid qty, ask, ask qty, volume, oi, previous-day oi, previous close, iv, delta, gamma, theta, vega
File: options/<date>/<UNDERLYING>.jsonl.gz, one line per snapshot:
  {"t": ISO time, "slot": n, "spot": index level, "columns": [...], "exp": {"<expiry>": {"n": strikes, "rows": [[strike, call fields..., put fields...], ...]}}}
Holiday-aware (index quote not from today after 09:25 -> exit), lock-protected, never raises out of the loop (a failed poll is logged and the next slot goes ahead)."""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import shutil
import sys
import time
import urllib.parse
from datetime import date, datetime, time as dtime
from pathlib import Path

try:
    import fcntl
except ImportError:                                  # Windows (tests only)
    fcntl = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C

log = logging.getLogger("chain")
FIELDS = ["ltp", "bid", "bq", "ask", "aq", "vol", "oi", "poi", "close", "iv", "delta", "gamma", "theta", "vega"]
COLUMNS = ["strike"] + [f"c_{f}" for f in FIELDS] + [f"p_{f}" for f in FIELDS]
SPOT_BAND = 0.10
START, FIRST_SLOT, LAST_SLOT, HOLIDAY_CHECK = dtime(9, 10), 9 * 60 + 15, 15 * 60 + 30, dtime(9, 25)
N_EXPIRIES = {"NIFTY": 3, "BANKNIFTY": 2}


def pick_expiries(contracts: list[dict], underlying: str, today: date) -> list[str]:
    """contracts: Upstox /option/contract rows. NIFTY: the next 3 expiries + the next monthly if absent; BANKNIFTY: the next 2."""
    exp = sorted({c["expiry"] for c in contracts if c["expiry"] >= today.isoformat()})
    chosen = exp[:N_EXPIRIES[underlying]]
    if underlying == "NIFTY":
        monthly = sorted({c["expiry"] for c in contracts if c["expiry"] >= today.isoformat() and not c.get("weekly", False)})
        for m in monthly:
            if m not in chosen:
                chosen.append(m); break
    return chosen


def shape(rows: list[dict], band: float = SPOT_BAND) -> tuple[float | None, list[list]]:
    """Upstox chain rows -> (spot, compact rows within +/-band of spot). Missing numbers stay None."""
    spot = next((r.get("underlying_spot_price") for r in rows if r.get("underlying_spot_price")), None)
    out = []
    for r in rows:
        k = r.get("strike_price")
        if spot and k is not None and abs(k / spot - 1) > band:
            continue
        row = [k]
        for side in ("call_options", "put_options"):
            o = r.get(side) or {}; m, g = o.get("market_data") or {}, o.get("option_greeks") or {}
            row += [m.get("ltp"), m.get("bid_price"), m.get("bid_qty"), m.get("ask_price"), m.get("ask_qty"), m.get("volume"), m.get("oi"), m.get("prev_oi"),
                    m.get("close_price"), g.get("iv"), g.get("delta"), g.get("gamma"), g.get("theta"), g.get("vega")]
        out.append(row)
    return spot, out


def expiries_for(u: str, today: date) -> list[str]:
    cache = C.DATA / "master" / f"opt_expiries_{u}_{today.isoformat()}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    r = C._get(f"{C._BASE}/v2/option/contract?instrument_key={urllib.parse.quote(C.INDEX_KEYS[u], safe='')}")
    if r.status_code != 200:
        raise RuntimeError(f"contracts {r.status_code} {r.text[:120]}")
    ex = pick_expiries(r.json()["data"], u, today)
    cache.parent.mkdir(parents=True, exist_ok=True); cache.write_text(json.dumps(ex))
    return ex


def fetch_chain(u: str, expiry: str) -> list[dict]:
    r = C._get(f"{C._BASE}/v2/option/chain?instrument_key={urllib.parse.quote(C.INDEX_KEYS[u], safe='')}&expiry_date={expiry}", timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"chain {u} {expiry}: {r.status_code} {r.text[:120]}")
    return r.json()["data"]


def compress_day(folder: Path):
    for p in folder.glob("*.jsonl"):
        with open(p, "rb") as f, gzip.open(str(p) + ".gz", "wb") as g:
            shutil.copyfileobj(f, g)
        p.unlink()


def snapshot(u: str, expiries: list[str], t: datetime, slot: int) -> dict | None:
    exp, spot = {}, None
    for e in expiries:
        try:
            s, rows = shape(fetch_chain(u, e))
        except Exception as ex:
            log.warning("[%s %s] %s", u, e, str(ex)[:140]); continue
        spot = spot or s
        exp[e] = {"n": len(rows), "rows": rows}
        time.sleep(0.25)
    if not exp:
        return None
    return {"t": t.isoformat(timespec="seconds"), "slot": slot, "spot": spot, "columns": COLUMNS, "exp": exp}


def main(force: int | None) -> int:
    today = C.now_ist().date(); test = force is not None
    root = C.DATA / ("options_test" if test else "options"); day_dir = root / today.isoformat(); day_dir.mkdir(parents=True, exist_ok=True)
    lock = open(day_dir / ".lock", "w")
    if fcntl is not None:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.info("another chain recorder holds the lock for %s -- exiting", today); return 0
    for old in root.glob("*"):
        if old.is_dir() and old.name != today.isoformat():
            compress_day(old)
    exps = {u: expiries_for(u, today) for u in C.UNDERLYINGS}
    log.info("recording %s into %s", exps, day_dir)
    handles = {u: open(day_dir / f"{u}.jsonl", "a") for u in C.UNDERLYINGS}
    done, ok_polls, fails = set(), 0, 0
    try:
        while True:
            t = C.now_ist(); minute = t.hour * 60 + t.minute
            if not test:
                if t.time() < START: time.sleep(20); continue
                if minute > LAST_SLOT + 1: break
                slot = (minute - FIRST_SLOT) // 5
                if minute < FIRST_SLOT or slot in done:
                    time.sleep(5); continue
            else:
                if ok_polls >= force: break
                slot = ok_polls
            done.add(slot); t0 = time.time()
            try:
                q = C.quotes([C.INDEX_KEYS[u] for u in C.UNDERLYINGS]); fresh = False
                for v in q.values():
                    if v.get("last_trade_time"):
                        fresh = fresh or datetime.fromtimestamp(int(v["last_trade_time"]) / 1000, C.IST).date() == today
                if not test and t.time() >= HOLIDAY_CHECK and not fresh:
                    log.info("index quotes are not from %s -- market closed (holiday); exiting", today); break
                n = 0
                for u in C.UNDERLYINGS:
                    snap = snapshot(u, exps[u], t, slot)
                    if snap:
                        handles[u].write(json.dumps(snap, separators=(",", ":")) + "\n"); handles[u].flush(); n += 1
                ok_polls += 1 if n else 0
                log.info("slot %d (%s): %d/%d underlyings recorded in %.1fs", slot, t.strftime("%H:%M"), n, len(C.UNDERLYINGS), time.time() - t0)
                fails = 0 if n else fails + 1
            except Exception as e:
                fails += 1; log.warning("slot %d failed (%d in a row): %s", slot, fails, str(e)[:140])
                if test: ok_polls += 1
            if fails >= 12:
                log.error("12 failed slots in a row -- giving up"); return 1
            time.sleep(1 if test else 20)
    finally:
        for h in handles.values():
            h.close()
        compress_day(day_dir)
        log.info("done: %d snapshots per underlying, compressed in %s", ok_polls, day_dir)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(); ap.add_argument("--force", type=int, default=None, help="N snapshots now, any time of day (test mode)")
    sys.exit(main(ap.parse_args().force))
