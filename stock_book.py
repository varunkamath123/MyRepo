"""
Stock-futures paper book: NIFTY 50, long-only Donchian breakout with volume expansion
=====================================================================================
Separate from paper_trader.py (the BANKNIFTY Kronos book): own state, own logs, own cron.
Runs once a day near the close (15:25 IST) and paper-trades one lot of the stock's future,
priced on the underlying equity (lot sizes from the Upstox F&O master).

Why this rule, and why it is a paper experiment
  Edge here means net expectancy above break-even: win% x avg win > loss% x avg loss after costs.
  Replay over 3 years of daily bars (NIFTY 50 names, 0.10% round-trip cost deducted, rules fixed
  in advance, nothing tuned) for LONG breakouts with volume: 569 trades, 43% win, avg win +8.3%,
  avg loss -4.3% (payoff 1.9, break-even win rate 34%), expectancy +1.07% per trade (~Rs7.5k per
  lot), positive in every calendar year (2023 +5.0%, 2024 +0.6%, 2025 +0.1%, 2026 +0.4%).
  The short side loses in every variant and year, so the book is long-only.
  Known weaknesses, tracked rather than assumed away: the result leans on a few big winners
  (expectancy is ~0 once the best 20 of 569 trades are removed), it fades after 2023, the
  universe is today's NIFTY 50 (survivorship), and it ran in a rising market. Kronos was tested
  as an entry filter on ~2,400 breakout days and did NOT separate better trades from worse ones,
  so it is only logged here (kronos_dir / kronos_conf on each ENTRY), never gating.

Rules (research/stk_triggers.py, stk_expectancy.py; same definitions)
  Entry : price > highest high of the prior 20 completed sessions AND today's volume >= 1.5x the
          prior-20-session average. Today's volume is partial at 15:25, so it is scaled by
          LIVE_VOLUME_ADJ (approximation; the raw ratio is logged as well).
  ATR   : Wilder ATR14 including today's true range (day high/low from the quote), measured at entry.
  Exit  : STOP_LOSS price <= entry - 2 x ATR14 (ATR at entry) | CHANNEL_EXIT price < lowest low of
          the prior 10 completed sessions | TIME_EXIT after 30 sessions. Checked in that order.
  No re-entry in a stock on the day it exits. No entry when the quote is stale (holiday).

Two ledgers from one engine
  signal book  : every signal, one lot each, uncapped. Roughly 40 signals a quarter, which is what
                 the statistics need. Reported as Rs per lot.
  capital book : the subset that fit while fewer than MAX_SLOTS (3) capital-book positions were open
                 (strongest volume first). That is what Rs3.5L of margin could actually carry.

Usage
  python stock_book.py                    daily run (cron, 15:25 IST)
  python stock_book.py --status           positions + both ledgers (no network)
  python stock_book.py --dry-run          fetch live data, print what would happen, write nothing
  python stock_book.py --refresh-universe rebuild stock_universe.json (NIFTY 50 list + Upstox keys/lots)
  python stock_book.py --no-kronos        skip the Kronos A/B read on new entries

Roll back: delete the cron line; the BANKNIFTY book does not depend on this module.
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stock_book")

IST = timezone(timedelta(hours=5, minutes=30))
HERE = Path(__file__).parent
UNIVERSE_FILE = HERE / "stock_universe.json"
STATE_FILE    = HERE / "stock_state.json"
TRADES_LOG    = HERE / "stock_trades.jsonl"
SIGNALS_LOG   = HERE / "stock_signals.jsonl"

CHANNEL_IN, CHANNEL_OUT = 20, 10      # Donchian entry / exit windows (completed sessions)
VOL_AVG, VOL_MULT       = 20, 1.5     # volume expansion vs prior-20-session average
ATR_N, STOP_ATR         = 14, 2.0     # initial stop = 2 ATR from entry
MAX_HOLD                = 30          # sessions
COST_PCT                = 0.0010      # round trip, deducted at exit
MAX_SLOTS               = 3           # capital-book concurrency (Rs3.5L of margin)
LIVE_VOLUME_ADJ         = 1.03        # 15:25 volume is ~97% of the day: approximation
CONTEXT_BARS            = 150
_BASE                   = "https://api.upstox.com/v2"


@dataclass
class StockPos:
    symbol: str
    entry_date: str
    entry_price: float
    atr: float
    lot: int
    slot: bool                         # True = also in the capital book
    volx: float = 0.0
    kronos_dir: Optional[str] = None
    kronos_conf: Optional[float] = None


def _empty_stats() -> dict:
    return {"n": 0, "wins": 0, "pnl_inr": 0.0, "cost_inr": 0.0, "sum_ret_pct": 0.0}


def _load_state() -> dict:
    st = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    st.setdefault("positions", {})
    st.setdefault("stats", {})
    for k in ("signal", "capital"):
        st["stats"].setdefault(k, _empty_stats())
    return st


def _save_state(st: dict):
    STATE_FILE.write_text(json.dumps(st, indent=2))


def _append(path: Path, record: dict):
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def _load_universe() -> list[dict]:
    return json.loads(UNIVERSE_FILE.read_text())


# ── Pure rule functions (no network; the replay and the tests call these) ─────

def _atr(d: pd.DataFrame) -> float:
    h, l, c = d["high"], d["low"], d["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1 / ATR_N, adjust=False).mean().iloc[-1])


def evaluate_entry(df: pd.DataFrame, px: float, vol_today: float, vol_adj: float = LIVE_VOLUME_ADJ,
                   day_high: Optional[float] = None, day_low: Optional[float] = None) -> dict:
    """df = complete daily bars through the last closed session (columns datetime/open/high/low/close/volume).
    day_high / day_low (today's range so far) put today's true range into the ATR, as the research did."""
    d = df.tail(CONTEXT_BARS)
    hi_in = float(d["high"].tail(CHANNEL_IN).max())
    avg_vol = float(d["volume"].tail(VOL_AVG).mean())
    volx_raw = (vol_today / avg_vol) if avg_vol > 0 else 0.0
    volx = volx_raw * vol_adj
    sig = bool(px > hi_in and volx >= VOL_MULT and len(d) >= CHANNEL_IN)
    atr = None
    if sig:
        if day_high is not None and day_low is not None:
            today_bar = pd.DataFrame([{"datetime": pd.NaT, "open": px, "high": max(day_high, px), "low": min(day_low, px),
                                       "close": px, "volume": vol_today}])
            atr = _atr(pd.concat([d, today_bar], ignore_index=True))
        else:
            atr = _atr(d)
    return {"signal": sig, "hi_in": hi_in, "volx": volx, "volx_raw": volx_raw, "atr": atr}


def check_exit(pos: StockPos, df: pd.DataFrame, px: float) -> Optional[str]:
    d = df.tail(CONTEXT_BARS)
    if px - pos.entry_price <= -STOP_ATR * pos.atr:
        return "STOP_LOSS"
    if px < float(d["low"].tail(CHANNEL_OUT).min()):
        return "CHANNEL_EXIT"
    held = int((df["datetime"].dt.normalize() > pd.Timestamp(pos.entry_date)).sum()) + 1   # sessions incl. today
    if held >= MAX_HOLD:
        return "TIME_EXIT"
    return None


def run_day(st: dict, universe: list[dict], data: dict, today: str, vol_adj: float = LIVE_VOLUME_ADJ,
            kronos_fn=None, write_logs: bool = True) -> list[dict]:
    """One daily run on already-fetched data: data[symbol] = {"df", "px", "vol"}. Mutates `st`; returns the events."""
    events: list[dict] = []
    exited: set[str] = set()
    lots = {u["symbol"]: u["lot"] for u in universe}

    # PASS 1: exits
    for sym in list(st["positions"].keys()):
        d = data.get(sym)
        if d is None:
            continue
        pos = StockPos(**st["positions"][sym])
        if pos.entry_date == today:
            continue
        reason = check_exit(pos, d["df"], d["px"])
        if not reason:
            continue
        gross = (d["px"] - pos.entry_price) * pos.lot
        cost = COST_PCT * pos.entry_price * pos.lot
        net = gross - cost
        ret_pct = ((d["px"] - pos.entry_price) / pos.entry_price - COST_PCT) * 100
        for book in ("signal",) + (("capital",) if pos.slot else ()):
            s = st["stats"][book]
            s["n"] += 1; s["wins"] += 1 if net > 0 else 0
            s["pnl_inr"] += net; s["cost_inr"] += cost; s["sum_ret_pct"] += ret_pct
        rec = {"event": "EXIT", "symbol": sym, "direction": "LONG", "entry_date": pos.entry_date, "entry_price": pos.entry_price,
               "exit_date": today, "exit_price": d["px"], "exit_reason": reason, "lot": pos.lot, "slot": pos.slot,
               "pnl_gross_inr": gross, "cost_inr": cost, "pnl_inr": net, "ret_pct": ret_pct,
               "volx": pos.volx, "kronos_dir": pos.kronos_dir, "kronos_conf": pos.kronos_conf}
        events.append(rec)
        if write_logs:
            _append(TRADES_LOG, rec)
        log.info("[%s] EXIT %s @ %.2f  %s  net INR %+.0f (%+.2f%%)  slot=%s  [held from %s]",
                 sym, "LONG", d["px"], reason, net, ret_pct, pos.slot, pos.entry_date)
        del st["positions"][sym]
        exited.add(sym)

    # PASS 2: entries
    cands = []
    for u in universe:
        sym = u["symbol"]
        d = data.get(sym)
        if d is None or sym in st["positions"] or sym in exited:
            continue
        ev = evaluate_entry(d["df"], d["px"], d["vol"], vol_adj, d.get("high"), d.get("low"))
        if write_logs:
            _append(SIGNALS_LOG, {"date": today, "symbol": sym, "price": d["px"], "hi20": ev["hi_in"],
                                  "volx": round(ev["volx"], 3), "volx_raw": round(ev["volx_raw"], 3), "signal": ev["signal"]})
        if ev["signal"]:
            cands.append((ev["volx"], sym, ev))
    cands.sort(reverse=True)                                   # strongest volume first for the capital slots
    open_slots = sum(1 for p in st["positions"].values() if p["slot"])
    for volx, sym, ev in cands:
        d = data[sym]
        slot = open_slots < MAX_SLOTS
        open_slots += 1 if slot else 0
        kd = kc = None
        if kronos_fn is not None:
            try:
                kd, kc = kronos_fn(d["df"])
            except Exception as e:
                log.warning("[%s] Kronos read failed (logging only): %s", sym, e)
        pos = StockPos(symbol=sym, entry_date=today, entry_price=d["px"], atr=ev["atr"], lot=lots[sym], slot=slot,
                       volx=round(volx, 3), kronos_dir=kd, kronos_conf=(round(kc, 3) if kc is not None else None))
        st["positions"][sym] = asdict(pos)
        rec = {"event": "ENTRY", "symbol": sym, "direction": "LONG", "entry_date": today, "entry_price": d["px"],
               "atr": ev["atr"], "stop": d["px"] - STOP_ATR * ev["atr"], "lot": pos.lot, "slot": slot,
               "volx": pos.volx, "volx_raw": round(ev["volx_raw"], 3), "hi20": ev["hi_in"],
               "kronos_dir": kd, "kronos_conf": pos.kronos_conf,
               "kronos_agrees": (kd == "LONG") if kd is not None else None}
        events.append(rec)
        if write_logs:
            _append(TRADES_LOG, rec)
        log.info("[%s] ENTRY LONG @ %.2f  hi20=%.2f volx=%.2f  stop=%.2f  lot=%d  slot=%s  kronos=%s/%s",
                 sym, d["px"], ev["hi_in"], volx, rec["stop"], pos.lot, slot, kd, pos.kronos_conf)
    return events


# ── Data access (Upstox) ──────────────────────────────────────────────────────

def _headers() -> dict:
    from brokers.upstox_data import _headers as h
    return h()


def load_daily(key: str, bars: int = CONTEXT_BARS) -> pd.DataFrame:
    """Complete daily bars through the last closed session; weekend-dated special sessions dropped."""
    now = datetime.now(IST)
    frm = (now - timedelta(days=int(bars * 1.6) + 10)).strftime("%Y-%m-%d")
    to = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    r = requests.get(f"{_BASE}/historical-candle/{quote(key, safe='')}/day/{to}/{frm}", headers=_headers(), timeout=20)
    r.raise_for_status()
    candles = r.json()["data"]["candles"]
    df = pd.DataFrame(candles, columns=["datetime", "open", "high", "low", "close", "volume", "oi"]).drop(columns=["oi"])
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize(None)
    df = df.sort_values("datetime").reset_index(drop=True)
    df = df[df["datetime"].dt.normalize() < pd.Timestamp(now.date())]
    df = df[df["datetime"].dt.dayofweek < 5].tail(bars).reset_index(drop=True)      # e.g. the Sunday Diwali Muhurat bar
    return df


def load_quotes(keys: list[str]) -> dict:
    """{instrument_key: {"px", "vol", "fresh_date"}} for up to ~500 keys in one call."""
    out = {}
    for i in range(0, len(keys), 100):
        chunk = keys[i:i + 100]
        r = requests.get(f"{_BASE}/market-quote/quotes?instrument_key=" + ",".join(quote(k, safe="") for k in chunk),
                         headers=_headers(), timeout=30)
        r.raise_for_status()
        for v in r.json()["data"].values():
            ts = datetime.fromtimestamp(int(v["last_trade_time"]) / 1000, IST).date().isoformat()
            oh = v.get("ohlc") or {}
            out[v["instrument_token"]] = {"px": float(v["last_price"]), "vol": float(v.get("volume") or 0.0), "fresh_date": ts,
                                          "high": oh.get("high"), "low": oh.get("low")}
    return out


def _kronos_read(df: pd.DataFrame):
    from backtest import get_signal
    d, c, *_ = get_signal(df.tail(CONTEXT_BARS).reset_index(drop=True), False)
    return d, float(c)


def refresh_universe():
    """NIFTY 50 constituents (NSE) joined to Upstox NSE_EQ keys and the nearest-expiry futures lot."""
    r = requests.get("https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    syms = pd.read_csv(io.StringIO(r.text))["Symbol"].str.strip().tolist()
    m = json.loads(gzip.decompress(requests.get("https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz", timeout=90).content))
    now = datetime.now(IST).timestamp()
    eq = {x["trading_symbol"]: x for x in m if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ"}
    fut = {}
    for x in m:
        if x.get("segment") == "NSE_FO" and x.get("instrument_type") == "FUT" and x["expiry"] / 1000 > now:
            s = x.get("underlying_symbol")
            if s and (s not in fut or x["expiry"] < fut[s]["expiry"]):
                fut[s] = x
    rows = [{"symbol": s, "key": eq[s]["instrument_key"], "lot": int(fut[s]["lot_size"])} for s in syms if s in eq and s in fut]
    missing = [s for s in syms if s not in eq or s not in fut]
    UNIVERSE_FILE.write_text(json.dumps(rows, indent=1))
    log.info("universe: %d of %d NIFTY 50 names written (%s); missing %s", len(rows), len(syms), UNIVERSE_FILE.name, missing)


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_daily_run(dry: bool, use_kronos: bool):
    st = _load_state()
    universe = _load_universe()
    today = datetime.now(IST).date().isoformat()
    quotes = load_quotes([u["key"] for u in universe])
    data, stale, failed = {}, [], []
    for u in universe:
        q = quotes.get(u["key"])
        if q is None:
            failed.append(u["symbol"]); continue
        if q["fresh_date"] != today:
            stale.append(u["symbol"]); continue
        try:
            data[u["symbol"]] = {"df": load_daily(u["key"]), "px": q["px"], "vol": q["vol"], "high": q["high"], "low": q["low"]}
        except Exception as e:
            log.error("[%s] history fetch failed: %s", u["symbol"], e)
            failed.append(u["symbol"])
    if len(stale) > len(universe) // 2:
        log.info("%d of %d quotes are not from today -- market closed / holiday; nothing to do", len(stale), len(universe))
        return
    log.info("data ok for %d stocks; stale %s; failed %s", len(data), stale, failed)
    work = json.loads(json.dumps(st)) if dry else st
    events = run_day(work, universe, data, today, kronos_fn=(_kronos_read if use_kronos and not dry else None), write_logs=not dry)
    if dry:
        print(f"\nDRY RUN {today}: {len(events)} events (nothing written)")
        for e in events:
            print("  ", {k: e[k] for k in e if k in ("event", "symbol", "entry_price", "exit_price", "exit_reason", "volx", "slot", "pnl_inr", "stop")})
        return
    _save_state(st)
    print_status(st)


def print_status(st: dict):
    sep = "=" * 66
    print(f"\n{sep}\n  STOCK BOOK (NIFTY 50, long-only breakout + volume)  |  paper, 1 lot each\n{sep}")
    for name, key in (("SIGNAL BOOK  (every signal, uncapped)", "signal"), (f"CAPITAL BOOK (max {MAX_SLOTS} open)", "capital")):
        s = st["stats"][key]
        if s["n"]:
            print(f"  {name}: {s['n']} closed, win {s['wins']/s['n']*100:.0f}%, net INR {s['pnl_inr']:+,.0f} "
                  f"(avg {s['pnl_inr']/s['n']:+,.0f}/trade, {s['sum_ret_pct']/s['n']:+.2f}%/trade), costs INR {s['cost_inr']:,.0f}")
        else:
            print(f"  {name}: no closed trades yet")
    if st["positions"]:
        print("  OPEN:")
        for sym, p in sorted(st["positions"].items()):
            print(f"    {sym:<12} entry {p['entry_date']} @ {p['entry_price']:.2f}  stop {p['entry_price'] - STOP_ATR * p['atr']:.2f}  "
                  f"lot {p['lot']}  {'CAPITAL' if p['slot'] else 'signal-only'}  volx {p['volx']:.2f}  kronos {p['kronos_dir']}/{p['kronos_conf']}")
    else:
        print("  No open positions.")
    print(sep)


def main():
    ap = argparse.ArgumentParser(description="NIFTY 50 stock-futures paper book (long-only breakout + volume)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--refresh-universe", action="store_true")
    ap.add_argument("--no-kronos", action="store_true")
    a = ap.parse_args()
    if a.refresh_universe:
        refresh_universe()
    elif a.status:
        print_status(_load_state())
    else:
        cmd_daily_run(a.dry_run, not a.no_kronos)


if __name__ == "__main__":
    main()
