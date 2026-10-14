# -*- coding: utf-8 -*-
"""India overnight strategy: paper ledger plus a 1-share live execution test, run by systemd timers."""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from statistics import mean, median

import pytz
import requests

import config

IST = pytz.timezone("Asia/Kolkata")
log = logging.getLogger("overnight")

# Pre-registered Oct 7 2026 from the walk-forward backtest; do not tune on live results.
TOP_N = 10
FORM_SESSIONS = 252
MIN_OBS = 200
TURNOVER_SESSIONS = 63
MIN_TURNOVER_CR = 25.0
HOLD_SESSIONS = 63
EMA_SPAN = 21
EMA_HISTORY = 120
# Opens beyond NSE's 20% band are split/bonus artifacts: sec_bhavdata_full does not always adjust PREV_CLOSE.
MAX_GAP = 0.205
PAPER_COST_BPS = 25.0
EXECUTION_LIMIT_BPS = 5.0

LIVE_QTY = 1
LIVE_MAX_ORDER_RS = float(getattr(config, "OVN_LIVE_MAX_ORDER_RS", 5000))
LIVE_MAX_DAY_RS = float(getattr(config, "OVN_LIVE_MAX_DAY_RS", 10000))
LIVE_MIN_FREE_FUNDS_RS = float(getattr(config, "OVN_LIVE_MIN_FREE_FUNDS_RS", 40000))

# Fyers API v3 order status codes.
CANCELLED, FILLED, TRANSIT, REJECTED, PENDING = 1, 2, 4, 5, 6
OPEN_LIVE = ("buy_filled", "amo_placed", "amo_rejected", "sell_placed")

BHAV_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv"
NIFTY500_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
BHAV_FIELDS = (("prev", "PREV_CLOSE"), ("open", "OPEN_PRICE"), ("close", "CLOSE_PRICE"),
               ("last", "LAST_PRICE"), ("turn_lacs", "TURNOVER_LACS"))


# ── paths and state ───────────────────────────────────────────────────────────

def root() -> str:
    return os.path.join(getattr(config, "LOG_DIRECTORY", "logs"), "overnight")


def path(*parts: str) -> str:
    p = os.path.join(root(), *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def now_ist() -> datetime:
    return datetime.now(IST)


def at(hh: int, mm: int, ss: int = 0) -> datetime:
    return now_ist().replace(hour=hh, minute=mm, second=ss, microsecond=0)


def read_json(fp: str):
    if not os.path.exists(fp):
        return None
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


def write_json(fp: str, data) -> None:
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, default=str)
    os.replace(tmp, fp)


def day_file(d: date) -> str:
    return path("days", f"{d:%Y%m%d}.json")


def day_dates() -> list:
    folder = os.path.dirname(day_file(date.today()))
    return sorted(datetime.strptime(n[:8], "%Y%m%d").date() for n in os.listdir(folder) if n.endswith(".json"))


def live_enabled() -> bool:
    return os.path.exists(path("LIVE_ENABLED"))


def fy_symbol(symbol: str) -> str:
    return f"NSE:{symbol}-EQ"


def has_live_orders(day) -> bool:
    return any(t["live"].get("buy_order_id") for t in (day or {}).get("trades", []))


# ── NSE bhavcopy store ────────────────────────────────────────────────────────

def bhav_file(d: date) -> str:
    return path("bhav", f"{d:%Y%m%d}.csv.gz")


def parse_bhav(text: str):
    reader = csv.reader(io.StringIO(text))
    header = [h.strip() for h in next(reader)]
    ix = {h: i for i, h in enumerate(header)}
    trade_date, rows = None, {}
    for rec in reader:
        if len(rec) < len(header) or rec[ix["SERIES"]].strip() != "EQ":
            continue
        try:
            row = {k: float(rec[ix[col]].strip()) for k, col in BHAV_FIELDS}
        except ValueError:
            continue
        if trade_date is None:
            trade_date = datetime.strptime(rec[ix["DATE1"]].strip(), "%d-%b-%Y").date()
        rows[rec[ix["SYMBOL"]].strip()] = row
    return trade_date, rows


def save_bhav(d: date, rows: dict) -> None:
    fp = bhav_file(d)
    with gzip.open(fp + ".tmp", "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol"] + [k for k, _ in BHAV_FIELDS])
        for sym in sorted(rows):
            w.writerow([sym] + [rows[sym][k] for k, _ in BHAV_FIELDS])
    os.replace(fp + ".tmp", fp)


_bhav_cache: dict = {}


def load_bhav(d: date):
    if d in _bhav_cache:
        return _bhav_cache[d]
    fp = bhav_file(d)
    if not os.path.exists(fp):
        return None
    with gzip.open(fp, "rt", newline="") as f:
        rows = {r["symbol"]: {k: float(r[k]) for k, _ in BHAV_FIELDS} for r in csv.DictReader(f)}
    _bhav_cache[d] = rows
    return rows


def stored_sessions() -> list:
    folder = os.path.dirname(bhav_file(date.today()))
    return sorted(datetime.strptime(n[:8], "%Y%m%d").date() for n in os.listdir(folder) if n.endswith(".csv.gz"))


def known_holidays() -> set:
    return {date.fromisoformat(x) for x in (read_json(path("holidays_seen.json")) or [])}


def remember_holiday(d: date) -> None:
    write_json(path("holidays_seen.json"), sorted(x.isoformat() for x in known_holidays() | {d}))


_nse = None


def nse_get(url: str):
    global _nse
    if _nse is None:
        _nse = requests.Session()
        _nse.headers.update(UA)
        try:
            _nse.get("https://www.nseindia.com", timeout=15)
        except Exception:
            pass
    return _nse.get(url, timeout=30)


def fetch_bhav(d: date) -> str:
    """'ok' when stored, 'holiday' when NSE served another session's file, 'missing' otherwise."""
    if os.path.exists(bhav_file(d)):
        return "ok"
    try:
        r = nse_get(BHAV_URL.format(d=d))
    except Exception as exc:
        log.warning(f"bhavcopy {d}: {exc}")
        return "missing"
    if r.status_code != 200 or "SYMBOL" not in r.text[:300]:
        return "missing"
    trade_date, rows = parse_bhav(r.text)
    # On holidays NSE serves the previous session's file under the requested name.
    if trade_date != d or not rows:
        remember_holiday(d)
        return "holiday"
    save_bhav(d, rows)
    return "ok"


def backfill(need: int = FORM_SESSIONS + 5, today=None, max_days: int = 450) -> int:
    today = today or now_ist().date()
    have = {d for d in stored_sessions() if d <= today}
    skip = known_holidays()
    d, fetched = today, 0
    for _ in range(max_days):
        if len(have) >= need:
            break
        if d.weekday() < 5 and d not in have and d not in skip:
            if fetch_bhav(d) == "ok":
                have.add(d)
                fetched += 1
            time.sleep(0.35)
        d -= timedelta(days=1)
    log.info(f"bhavcopy store: {len(have)} sessions (+{fetched} fetched)")
    return len(have)


def nifty500() -> set:
    cache = path("nifty500.csv")
    try:
        r = requests.get(NIFTY500_URL, headers=UA, timeout=30)
        r.raise_for_status()
        lines = r.text.splitlines()
        h = next(i for i, line in enumerate(lines) if "symbol" in line.lower())
        text = "\n".join(lines[h:])
        syms = _symbols(text)
        if len(syms) > 400:
            with open(cache, "w", encoding="utf-8") as f:
                f.write(text)
            return syms
        log.warning(f"NIFTY 500 list had only {len(syms)} symbols; using cache")
    except Exception as exc:
        log.warning(f"NIFTY 500 list fetch failed ({exc}); using cache")
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            return _symbols(f.read())
    raise RuntimeError("no NIFTY 500 constituent list available")


def _symbols(text: str) -> set:
    reader = csv.DictReader(io.StringIO(text))
    key = next(k for k in reader.fieldnames if k and k.strip().lower() == "symbol")
    return {row[key].strip() for row in reader if row.get(key)}


# ── picks ─────────────────────────────────────────────────────────────────────

def rank(sessions: list, universe: set) -> list:
    window = sessions[-FORM_SESSIONS:]
    turn_days = set(sessions[-TURNOVER_SESSIONS:])
    on, turn, prev_syms = {}, {}, None
    for d in window:
        rows = load_bhav(d) or {}
        for sym, r in rows.items():
            if sym not in universe:
                continue
            # A symbol's first session (listing/relisting) has no real previous close.
            if prev_syms is not None and sym in prev_syms and r["prev"] > 0 and r["open"] > 0:
                gap = r["open"] / r["prev"] - 1
                if abs(gap) <= MAX_GAP:
                    on.setdefault(sym, []).append(gap)
            if d in turn_days:
                turn.setdefault(sym, []).append(r["turn_lacs"] / 100.0)
        prev_syms = set(rows)
    out = []
    for sym, xs in on.items():
        t = median(turn.get(sym) or [0.0])
        if len(xs) >= MIN_OBS and t >= MIN_TURNOVER_CR:
            out.append({"symbol": sym, "on_bps": round(mean(xs) * 1e4, 2), "obs": len(xs), "turnover_cr": round(t, 1)})
    return sorted(out, key=lambda r: -r["on_bps"])


def ensure_picks(today: date, force: bool = False) -> dict:
    fp = path("picks.json")
    picks = read_json(fp)
    sessions = [d for d in stored_sessions() if d < today]
    if picks and not force:
        since = [d for d in sessions if d > date.fromisoformat(picks["as_of"])]
        if len(since) < HOLD_SESSIONS:
            return picks
    backfill(today=today - timedelta(days=1))
    sessions = [d for d in stored_sessions() if d < today]
    universe = nifty500()
    ranked = rank(sessions, universe)
    if len(ranked) < TOP_N:
        raise RuntimeError(f"only {len(ranked)} eligible stocks; bhavcopy history incomplete?")
    picks = {"as_of": sessions[-1].isoformat(), "computed_at": now_ist().isoformat(timespec="seconds"),
             "universe": len(universe), "eligible": len(ranked), "picks": ranked[:TOP_N]}
    write_json(fp, picks)
    write_json(path("picks_history", f"{sessions[-1]:%Y%m%d}.json"), picks)
    log.info(f"re-picked as of {picks['as_of']}: " + ", ".join(p["symbol"] for p in picks["picks"]))
    return picks


# ── 21 EMA decision ───────────────────────────────────────────────────────────

def adjusted_closes(symbol: str, sessions: list) -> list:
    """Official closes chained through CLOSE/PREV_CLOSE, restarting after any split/bonus artifact."""
    series, last = [], None
    for d in sessions:
        r = (load_bhav(d) or {}).get(symbol)
        if not r or r["prev"] <= 0 or r["close"] <= 0:
            continue
        if not series or (r["open"] > 0 and abs(r["open"] / r["prev"] - 1) > MAX_GAP):
            series = [r["close"]]
        else:
            series.append(series[-1] * r["close"] / r["prev"])
        last = r["close"]
    if not series:
        return []
    k = last / series[-1]
    return [x * k for x in series]


def ema(values: list, span: int = EMA_SPAN) -> float:
    a, e = 2.0 / (span + 1), values[0]
    for v in values[1:]:
        e += a * (v - e)
    return e


def new_trade(symbol: str) -> dict:
    return {"symbol": symbol, "fy": fy_symbol(symbol), "decision": None,
            "paper": {"entry": None, "exit": None, "exit_date": None, "gross_bps": None, "net_bps": None},
            "live": {"status": "off"}}


def decide(symbol: str, sessions: list, quote) -> dict:
    t = new_trade(symbol)
    series = adjusted_closes(symbol, sessions[-EMA_HISTORY:])
    last_close = (load_bhav(sessions[-1]) or {}).get(symbol, {}).get("close") if sessions else None
    if len(series) < 3 * EMA_SPAN:
        t["decision"] = "skip_no_history"
        return t
    if not quote or not quote.get("lp"):
        t["decision"] = "skip_no_quote"
        return t
    ltp = float(quote["lp"])
    ref = float(quote.get("prev_close_price") or 0) or last_close
    if not ref:
        t["decision"] = "skip_no_history"
        return t
    provisional = series[-1] * ltp / ref
    level = ema(series + [provisional])
    t.update({"ltp": ltp, "prev_close": ref, "ema21_adj": round(level, 4), "close_adj": round(provisional, 4),
              "pct_vs_ema": round((provisional / level - 1) * 100, 2),
              "decision": "enter" if provisional > level else "skip_below_ema"})
    return t


# ── Fyers ─────────────────────────────────────────────────────────────────────

def fyers_client(wait_until=None):
    token_file = os.path.join(getattr(config, "LOG_DIRECTORY", "logs"), "token.txt")
    while True:
        try:
            with open(token_file, encoding="utf-8") as f:
                lines = f.read().strip().split("\n")
            if len(lines) >= 2 and datetime.fromisoformat(lines[1].strip()).date() == now_ist().date():
                from fyers_auth import FyersAuth
                auth = FyersAuth()
                auth.access_token = lines[0].strip()
                return auth.get_fyers_client()
        except Exception as exc:
            log.warning(f"token read failed: {exc}")
        if wait_until is None or now_ist() >= wait_until:
            log.error("no Fyers token for today")
            return None
        time.sleep(30)


def quotes(fy, symbols: list) -> dict:
    out = {}
    for i in range(0, len(symbols), 40):
        try:
            r = fy.quotes({"symbols": ",".join(symbols[i:i + 40])})
        except Exception as exc:
            log.warning(f"quotes failed: {exc}")
            continue
        if r.get("s") != "ok":
            log.warning(f"quotes failed: {r}")
            continue
        for item in r.get("d") or []:
            v = item.get("v") or {}
            out[item.get("n") or v.get("symbol")] = v
    return out


def traded_today(fy) -> bool:
    today = now_ist().date()
    if today.weekday() >= 5:
        return False
    index = config.INSTRUMENTS["NIFTY"]["index_symbol"]
    v = quotes(fy, [index]).get(index) or {}
    if v.get("tt"):
        return datetime.fromtimestamp(int(v["tt"]), IST).date() == today
    log.warning(f"no trade timestamp in index quote (keys: {sorted(v)}); falling back to the holiday list")
    from nse_holidays import is_market_open_today
    return is_market_open_today(today)


def place(fy, symbol: str, qty: int, side: int, amo: bool, tag: str):
    data = {"symbol": symbol, "qty": int(qty), "type": 2, "side": side, "productType": "CNC",
            "limitPrice": 0, "stopPrice": 0, "validity": "DAY", "disclosedQty": 0,
            "offlineOrder": bool(amo), "orderTag": tag}
    try:
        resp = fy.place_order(data)
    except Exception as exc:
        return None, {"error": str(exc)}
    if resp.get("s") == "ok" and resp.get("id"):
        return str(resp["id"]), resp
    return None, resp


def order_info(fy, oid):
    try:
        r = fy.orderbook()
    except Exception as exc:
        log.warning(f"orderbook failed: {exc}")
        return None
    for o in r.get("orderBook") or []:
        if str(o.get("id")) == str(oid):
            return o
    return None


def status_of(o) -> int:
    try:
        return int((o or {}).get("status") or 0)
    except (TypeError, ValueError):
        return 0


def wait_final(fy, oid, timeout_s: float, poll_s: float = 3.0) -> dict:
    end, last = time.time() + timeout_s, {}
    while True:
        o = order_info(fy, oid)
        if o:
            last = o
            if status_of(o) in (FILLED, CANCELLED, REJECTED):
                return o
        if time.time() >= end:
            return last
        time.sleep(poll_s)


def fill_of(o: dict):
    if status_of(o) != FILLED:
        return None
    px = float(o.get("tradedPrice") or 0)
    return px if px > 0 else None


def order_brief(o: dict) -> dict:
    keep = ("id", "status", "tradedPrice", "filledQty", "qty", "orderDateTime", "message", "productType", "orderTag")
    return {k: o.get(k) for k in keep if k in (o or {})}


def trade_times(fy) -> dict:
    try:
        r = fy.tradebook()
    except Exception as exc:
        log.warning(f"tradebook failed: {exc}")
        return {}
    out = {}
    for tr in r.get("tradeBook") or []:
        oid = str(tr.get("orderNumber") or tr.get("orderId") or tr.get("id") or "")
        keep = ("orderDateTime", "tradePrice", "tradedQty", "tradeNumber", "exchangeOrderNo")
        out.setdefault(oid, []).append({k: tr.get(k) for k in keep if k in tr})
    return out


def available_funds(fy):
    from fyers_orders import get_available_funds
    return get_available_funds(fy)


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_backfill(args) -> int:
    backfill(need=args.sessions)
    return 0


def cmd_rebalance(args) -> int:
    p = ensure_picks(now_ist().date() + timedelta(days=1), force=True)
    for x in p["picks"]:
        log.info(f"  {x['symbol']:12s} {x['on_bps']:7.2f} bps/night  turnover Rs{x['turnover_cr']:,.0f} cr  n={x['obs']}")
    return 0


def cmd_entry(args) -> int:
    now = now_ist()
    today = now.date()
    in_window = at(15, 14) <= now <= at(15, 28)
    if not in_window and not args.force:
        log.error("entry runs 15:14-15:28 IST only (use --force for a paper dry run)")
        return 2
    existing = read_json(day_file(today))
    if has_live_orders(existing):
        log.info("entry already placed live orders today; not touching them")
        return 0
    if existing and existing.get("trades") and not args.force:
        log.info("entry already ran today")
        return 0
    fy = None if args.no_broker else fyers_client()
    if fy is None and not args.no_broker:
        return 1
    if fy is not None and not traded_today(fy):
        log.info("no session today")
        return 0
    live = fy is not None and in_window and not args.force and not args.no_live and live_enabled()
    picks = ensure_picks(today)
    sessions = [d for d in stored_sessions() if d < today]
    syms = [p["symbol"] for p in picks["picks"]]
    if fy is not None:
        q = quotes(fy, [fy_symbol(s) for s in syms])
    else:
        last = load_bhav(sessions[-1]) or {}
        q = {fy_symbol(s): {"lp": last[s]["close"], "prev_close_price": last[s]["close"]} for s in syms if s in last}
    trades = []
    for s in syms:
        t = decide(s, sessions, q.get(fy_symbol(s)))
        t["decided_at"] = now_ist().strftime("%H:%M:%S")
        trades.append(t)
    day = {"date": today.isoformat(), "picks_as_of": picks["as_of"], "live": live,
           "priced_from": "fyers" if fy is not None else "last_official_close", "trades": trades}
    write_json(day_file(today), day)
    enter = [t for t in trades if t["decision"] == "enter"]
    log.info(f"{len(enter)}/{len(trades)} picks above their 21 EMA: " + ", ".join(t["symbol"] for t in enter))
    if live:
        live_buys(fy, day)
        write_json(day_file(today), day)
    return 0


def live_buys(fy, day: dict) -> None:
    funds = available_funds(fy)
    spent = 0.0
    for t in day["trades"]:
        lv = t["live"]
        if t["decision"] != "enter":
            lv["status"] = "no_entry"
            continue
        cost = t["ltp"] * LIVE_QTY
        if cost > LIVE_MAX_ORDER_RS:
            lv["status"] = "skipped_order_cap"
        elif spent + cost > LIVE_MAX_DAY_RS:
            lv["status"] = "skipped_day_cap"
        elif funds is None or funds - spent - cost < LIVE_MIN_FREE_FUNDS_RS:
            lv.update({"status": "skipped_funds_floor", "funds": funds})
        else:
            oid, resp = place(fy, t["fy"], LIVE_QTY, side=1, amo=False, tag="ovnbuy")
            if oid:
                lv.update({"status": "buy_placed", "qty": LIVE_QTY, "buy_order_id": oid,
                           "buy_sent_at": now_ist().strftime("%H:%M:%S")})
                spent += cost
            else:
                lv.update({"status": "buy_failed", "buy_error": resp})
                log.error(f"{t['symbol']} buy rejected: {resp}")
    for t in day["trades"]:
        lv = t["live"]
        if lv.get("status") != "buy_placed":
            continue
        o = wait_final(fy, lv["buy_order_id"], timeout_s=90)
        lv["buy_order"] = order_brief(o)
        px = fill_of(o)
        if px:
            lv.update({"status": "buy_filled", "buy_fill": px, "filled_qty": int(o.get("filledQty") or LIVE_QTY)})
        else:
            lv["status"] = "buy_unfilled"
            log.error(f"{t['symbol']} buy not filled: {order_brief(o)}")
    trades_by_order = trade_times(fy)
    for t in day["trades"]:
        oid = t["live"].get("buy_order_id")
        if oid and trades_by_order.get(oid):
            t["live"]["buy_trades"] = trades_by_order[oid]
    filled = sum(t["live"].get("status") == "buy_filled" for t in day["trades"])
    log.info(f"live buys: {filled} filled, Rs{spent:,.0f} committed")


def cmd_amo(args) -> int:
    now = now_ist()
    if not (16 <= now.hour <= 23) and not args.force:
        log.error("AMO placement runs 16:00-23:59 IST only")
        return 2
    today = now.date()
    day = read_json(day_file(today))
    todo = [t for t in (day or {}).get("trades", [])
            if t["live"].get("status") == "buy_filled" and not t["live"].get("amo_order_id")]
    if not todo:
        log.info("no live positions need an AMO")
        return 0
    fy = fyers_client()
    if fy is None:
        return 1
    for t in todo:
        lv = t["live"]
        oid, resp = place(fy, t["fy"], lv["filled_qty"], side=-1, amo=True, tag="ovnamo")
        if oid:
            lv.update({"status": "amo_placed", "amo_order_id": oid, "amo_sent_at": now_ist().isoformat(timespec="seconds")})
        else:
            lv.update({"status": "amo_rejected", "amo_error": resp})
            log.error(f"{t['symbol']} AMO sell rejected: {resp}")
    write_json(day_file(today), day)
    return 0


def open_live_trades(today: date) -> list:
    out = []
    for d in day_dates():
        if d >= today or (today - d).days > 14:
            continue
        day = read_json(day_file(d))
        for t in day["trades"]:
            if t["live"].get("status") in OPEN_LIVE:
                out.append((d, day, t))
    return out


def sleep_until(target: datetime) -> None:
    while now_ist() < target:
        time.sleep(min(15.0, max(0.5, (target - now_ist()).total_seconds())))


def record_sell(t: dict, o: dict, route: str) -> bool:
    px = fill_of(o)
    if not px:
        return False
    t["live"].update({"status": "sold", "sell_fill": px, "sell_route": route, "sell_order": order_brief(o),
                      "sell_date": now_ist().date().isoformat()})
    return True


def cancel(fy, oid) -> None:
    try:
        fy.cancel_order({"id": oid})
    except Exception as exc:
        log.warning(f"cancel {oid} failed: {exc}")


def cmd_morning(args) -> int:
    now = now_ist()
    today = now.date()
    if not (at(8, 55) <= now <= at(9, 30)) and not args.force:
        log.error("morning exit runs 08:55-09:30 IST only")
        return 2
    trades = open_live_trades(today)
    if not trades:
        log.info("no open live positions")
        return 0
    fy = fyers_client(wait_until=at(9, 12))
    if fy is None:
        log.critical(f"cannot reach Fyers; {len(trades)} position(s) rely on their AMO alone")
        return 1
    if not traded_today(fy):
        log.info("no session today; AMOs stay queued")
        return 0

    def save():
        for d, day, _ in trades:
            write_json(day_file(d), day)

    for d, day, t in trades:
        lv = t["live"]
        if lv.get("amo_order_id"):
            st = status_of(order_info(fy, lv["amo_order_id"]))
            if st not in (CANCELLED, REJECTED):
                continue
        if now_ist() < at(9, 7):
            oid, resp = place(fy, t["fy"], lv["filled_qty"], side=-1, amo=False, tag="ovnpre")
            if oid:
                lv.update({"status": "sell_placed", "sell_order_id": oid})
            else:
                lv["preopen_error"] = resp
                log.warning(f"{t['symbol']} pre-open sell rejected: {resp}")
    save()

    sleep_until(at(9, 15, 30))
    for d, day, t in trades:
        lv = t["live"]
        oid = lv.get("sell_order_id") or lv.get("amo_order_id")
        route = "preopen" if lv.get("sell_order_id") else "amo"
        o = wait_final(fy, oid, timeout_s=30) if oid else {}
        if record_sell(t, o, route):
            continue
        if status_of(o) in (PENDING, TRANSIT):
            cancel(fy, oid)
            o = wait_final(fy, oid, timeout_s=20)
            if record_sell(t, o, route):
                continue
            if status_of(o) != CANCELLED:
                lv["status"] = "sell_unknown"
                log.critical(f"{t['symbol']}: sell order {oid} neither filled nor cancelled: {order_brief(o)}; check manually")
                continue
        mid, resp = place(fy, t["fy"], lv["filled_qty"], side=-1, amo=False, tag="ovnmkt")
        if not mid:
            lv.update({"status": "sell_failed", "market_error": resp})
            log.critical(f"{t['symbol']}: market sell rejected: {resp}; position may still be held, check manually")
            continue
        lv["sell_order_id"] = mid
        if not record_sell(t, wait_final(fy, mid, timeout_s=60), "market"):
            lv["status"] = "sell_placed"
            log.critical(f"{t['symbol']}: market sell {mid} not confirmed; check manually")
    trades_by_order = trade_times(fy)
    for _, _, t in trades:
        lv = t["live"]
        oid = str((lv.get("sell_order") or {}).get("id") or "")
        if oid and trades_by_order.get(oid):
            lv["sell_trades"] = trades_by_order[oid]
    save()
    sold = [t for _, _, t in trades if t["live"].get("status") == "sold"]
    log.info(f"morning exit: {len(sold)}/{len(trades)} sold: " +
             ", ".join(f"{t['symbol']} via {t['live']['sell_route']} @{t['live']['sell_fill']}" for t in sold))
    return 0 if len(sold) == len(trades) else 1


def settle(today: date) -> int:
    sessions = stored_sessions()
    changed = 0
    for d in day_dates():
        if d > today:
            continue
        day = read_json(day_file(d))
        entry_rows = load_bhav(d)
        later = [s for s in sessions if s > d]
        exit_day = later[0] if later else None
        exit_rows = load_bhav(exit_day) if exit_day else None
        dirty = False
        for t in day["trades"]:
            if t["decision"] != "enter":
                continue
            p, lv, sym = t["paper"], t["live"], t["symbol"]
            if p["entry"] is None and entry_rows and sym in entry_rows:
                p["entry"] = entry_rows[sym]["close"]
                p["entry_last"] = entry_rows[sym]["last"]
                dirty = True
            if p["exit"] is None and p["entry"] and exit_rows and sym in exit_rows:
                p.update({"exit": exit_rows[sym]["open"], "exit_date": exit_day.isoformat()})
                p["gross_bps"] = round((p["exit"] / p["entry"] - 1) * 1e4, 2)
                p["net_bps"] = round(p["gross_bps"] - PAPER_COST_BPS, 2)
                dirty = True
            if lv.get("buy_fill") and p["entry"] and lv.get("slip_buy_bps") is None:
                lv["slip_buy_bps"] = round((lv["buy_fill"] / p["entry"] - 1) * 1e4, 2)
                dirty = True
            if lv.get("sell_fill") and lv.get("slip_sell_bps") is None:
                rows = load_bhav(date.fromisoformat(lv["sell_date"]))
                if rows and sym in rows:
                    lv["official_open_on_sell_date"] = rows[sym]["open"]
                    lv["slip_sell_bps"] = round((rows[sym]["open"] / lv["sell_fill"] - 1) * 1e4, 2)
                    dirty = True
        if dirty:
            write_json(day_file(d), day)
            changed += 1
    return changed


def cmd_reconcile(args) -> int:
    today = now_ist().date()
    deadline = time.time() + args.wait_min * 60
    status = "missing"
    while today.weekday() < 5 and today not in known_holidays():
        status = fetch_bhav(today)
        if status != "missing" or time.time() >= deadline:
            break
        log.info("bhavcopy not published yet; retrying in 10 min")
        time.sleep(600)
    if status == "missing" and today.weekday() < 5 and today not in known_holidays():
        log.error(f"bhavcopy for {today} not available")
    backfill(need=len(stored_sessions()) + 1, today=today, max_days=10)
    log.info(f"settled {settle(today)} day file(s)")
    print_report(recent_days=5)
    return 0


# ── report ────────────────────────────────────────────────────────────────────

def summarize() -> dict:
    rows = [(d, t) for d in day_dates() for t in read_json(day_file(d))["trades"]]
    settled = [t for _, t in rows if t["decision"] == "enter" and t["paper"]["net_bps"] is not None]
    by_day: dict = {}
    for d, t in rows:
        if t["decision"] == "enter" and t["paper"]["net_bps"] is not None:
            by_day.setdefault(d, []).append(t["paper"]["net_bps"] / 1e4)
    equity = 1.0
    for d in sorted(by_day):
        equity *= 1 + sum(by_day[d]) / TOP_N
    live = [t["live"] for _, t in rows
            if t["live"].get("slip_buy_bps") is not None and t["live"].get("slip_sell_bps") is not None]
    sells = [t["live"] for _, t in rows if t["live"].get("status") == "sold"]
    out = {
        "sessions": len({d for d, _ in rows}),
        "paper_trades": len(settled),
        "paper_gross_bps": round(mean(t["paper"]["gross_bps"] for t in settled), 2) if settled else None,
        "paper_net_bps": round(mean(t["paper"]["net_bps"] for t in settled), 2) if settled else None,
        "paper_hit_rate_pct": round(100 * mean(t["paper"]["gross_bps"] > 0 for t in settled), 1) if settled else None,
        "paper_return_on_capital_pct": round((equity - 1) * 100, 2),
        "live_round_trips": len(live),
        "live_slip_buy_bps": round(mean(x["slip_buy_bps"] for x in live), 2) if live else None,
        "live_slip_sell_bps": round(mean(x["slip_sell_bps"] for x in live), 2) if live else None,
        "live_sell_routes": {r: sum(x.get("sell_route") == r for x in sells) for r in ("amo", "preopen", "market")},
        "live_sold_at_official_open": sum(x.get("slip_sell_bps") is not None and abs(x["slip_sell_bps"]) < 1 for x in sells),
    }
    if live:
        total = out["live_slip_buy_bps"] + out["live_slip_sell_bps"]
        verdict = "OK" if total <= EXECUTION_LIMIT_BPS else "TOO COSTLY"
        out["execution_verdict"] = f"{verdict} ({total:+.1f} bps per round trip vs official prices; limit {EXECUTION_LIMIT_BPS:.0f})"
    return out


def print_report(recent_days=None) -> dict:
    summary = summarize()
    write_json(path("summary.json"), summary)
    for k, v in summary.items():
        log.info(f"  {k}: {v}")
    for d in day_dates()[-(recent_days or 0):] if recent_days else []:
        cells = []
        for t in read_json(day_file(d))["trades"]:
            if t["decision"] == "enter":
                net = t["paper"]["net_bps"]
                cells.append(f"{t['symbol']}({'open' if net is None else format(net, '+.0f')})")
        log.info(f"  {d}: " + (" ".join(cells) or "no entries"))
    return summary


def cmd_report(args) -> int:
    print_report(recent_days=args.days)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="India overnight strategy (paper + 1-share live test)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill")
    b.add_argument("--sessions", type=int, default=FORM_SESSIONS + 5)
    sub.add_parser("rebalance")
    e = sub.add_parser("entry")
    e.add_argument("--force", action="store_true", help="ignore the time window; never places orders")
    e.add_argument("--no-broker", action="store_true", help="price from the last official close; no Fyers calls")
    e.add_argument("--no-live", action="store_true", help="never place orders, even if LIVE_ENABLED exists")
    a = sub.add_parser("amo")
    a.add_argument("--force", action="store_true")
    m = sub.add_parser("morning")
    m.add_argument("--force", action="store_true")
    r = sub.add_parser("reconcile")
    r.add_argument("--wait-min", type=int, default=150)
    rp = sub.add_parser("report")
    rp.add_argument("--days", type=int, default=10)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(path("overnight.log"), encoding="utf-8")])
    lock = _lock()
    try:
        return {"backfill": cmd_backfill, "rebalance": cmd_rebalance, "entry": cmd_entry, "amo": cmd_amo,
                "morning": cmd_morning, "reconcile": cmd_reconcile, "report": cmd_report}[args.cmd](args)
    finally:
        if lock:
            lock.close()


def _lock():
    try:
        import fcntl
    except ImportError:
        return None
    fh = open(path("run.lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("another overnight job is running")
        sys.exit(3)
    return fh


if __name__ == "__main__":
    sys.exit(main())
