"""Cleaned 10-minute NIFTY / BANKNIFTY index dataset: pull everything Upstox serves, validate it, flag (never silently repair) what is wrong.

  python build_dataset.py                 both indices -> clean/index_10m_<UNDERLYING>.csv.gz + clean/quality_<UNDERLYING>.json
  python build_dataset.py --only NIFTY

What is checked / flagged (columns in the output):
  post_close    bars stamped after 15:25 (frozen prints published after the close, seen in Apr-May 2025); never usable
  frozen_close  since 2026-08-03 cash-market trading is continuous only until 15:15; 15:15 -> ~15:28 is a call-auction window with no volume and
                no price movement, then one auction print (= the official close).  The index is frozen over that window, so the 15:15 bar is flat
                and the 15:25 bar holds the auction jump.  Both bars are flagged and not usable; FUTURES keep trading through the window and are
                the source of truth for anything near the close.
  session_type  normal (starts 09:15, 36-38 regular bars) | short | special_hours (e.g. Diwali Muhurat) | weekend_special
  bad_ohlc      high < max(open, close, low), low > min(...), a price <= 0 or NaN
  flat          high == low (a bar with no price movement: a stale feed shows up as flat bars at fixed times)
  gap_before    the previous 10-minute slot of the session is missing
  outlier       |return| > 8 robust sd of that time slot
  usable        normal session, not bad_ohlc, not post_close, not frozen_close   (outlier bars stay usable, see ret_clean_bp)
  ret_bp        within-session close-to-close return in basis points (NaN on a session's first bar or after a gap)
  ret_clean_bp  ret_bp with NaN wherever the bar or the previous bar is flat / bad / follows a gap (so a stale bar cannot fake a pattern)
The result is cross-checked against Upstox's own daily candles (open, high, low, close per session)."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C

log = logging.getLogger("build")
OPEN_MIN = C.SESSION_OPEN_MIN


def clean(raw: pd.DataFrame, daily: pd.DataFrame | None = None) -> tuple[pd.DataFrame, dict]:
    rep: dict = {"raw_bars": int(len(raw))}
    d = raw.copy()
    dup = d.duplicated("ts", keep=False)
    rep["duplicate_timestamps"] = int(d[dup]["ts"].nunique())
    rep["conflicting_duplicates"] = int(sum((g[["o", "h", "l", "c"]].nunique() > 1).any() for _, g in d[dup].groupby("ts"))) if dup.any() else 0
    d = d.drop_duplicates("ts", keep="last").sort_values("ts").reset_index(drop=True)
    d["session"] = d["ts"].dt.normalize()
    d["slot"] = ((d["ts"].dt.hour * 60 + d["ts"].dt.minute) - OPEN_MIN) // 10
    fm = d.groupby("session")["ts"].transform("min")
    d["post_close"] = (d["slot"] > 37) & ((fm.dt.hour * 60 + fm.dt.minute) == OPEN_MIN)      # only in sessions that open at 09:15 (not Muhurat evenings)

    s = d[~d["post_close"]].groupby("session").agg(n=("ts", "size"), first=("ts", "min"))
    s["first_min"] = s["first"].dt.hour * 60 + s["first"].dt.minute
    s["dow"] = s["first"].dt.dayofweek
    s["session_type"] = np.select([s["dow"] >= 5, s["first_min"] != OPEN_MIN, (s["n"] >= 36) & (s["n"] <= 38)],
                                  ["weekend_special", "special_hours", "normal"], default="short")
    d = d.merge(s[["session_type"]], left_on="session", right_index=True)

    d["bad_ohlc"] = ~((d.h >= d[["o", "c", "l"]].max(axis=1)) & (d.l <= d[["o", "c", "h"]].min(axis=1)) & (d[["o", "h", "l", "c"]] > 0).all(axis=1))
    d["flat"] = (d.h == d.l)
    d["gap_before"] = (d.groupby("session")["slot"].diff() > 1)
    d["ret_bp"] = d.groupby("session")["c"].pct_change() * 1e4
    d.loc[d["gap_before"], "ret_bp"] = np.nan
    mad = d.groupby("slot")["ret_bp"].transform(lambda x: 1.4826 * (x - x.median()).abs().median())
    d["outlier"] = d["ret_bp"].abs() > 8 * mad
    prev_flat = d.groupby("session")["flat"].shift(1).fillna(False).astype(bool)
    prev_bad = d.groupby("session")["bad_ohlc"].shift(1).fillna(False).astype(bool)
    d["ret_clean_bp"] = d["ret_bp"].where(~(d["flat"] | prev_flat | d["bad_ohlc"] | prev_bad | d["gap_before"]))
    frozen_sessions = d.loc[(d["slot"] == 36) & d["flat"] & ~d["post_close"], "session"]
    d["frozen_close"] = d["session"].isin(frozen_sessions) & d["slot"].isin([36, 37])
    d["ret_clean_bp"] = d["ret_clean_bp"].where(~d["frozen_close"])
    d["usable"] = (d["session_type"] == "normal") & ~d["bad_ohlc"] & ~d["post_close"] & ~d["frozen_close"]

    rep["bars"] = int(len(d)); rep["sessions"] = int(d["session"].nunique())
    rep["first"] = str(d["ts"].iloc[0]); rep["last"] = str(d["ts"].iloc[-1])
    rep["session_types"] = {k: int(v) for k, v in s["session_type"].value_counts().items()}
    rep["short_sessions"] = [str(x.date()) + f" ({int(s.loc[x, 'n'])} bars)" for x in s.index[s["session_type"] == "short"]][:12]
    rep["special_sessions"] = [str(x.date()) + f" ({s.loc[x, 'session_type']}, {int(s.loc[x, 'n'])} bars)" for x in s.index[s["session_type"].isin(["special_hours", "weekend_special"])]][:12]
    rep["post_close_bars"] = int(d["post_close"].sum())
    rep["frozen_close_sessions"] = int(frozen_sessions.nunique())
    rep["frozen_close_first_session"] = str(frozen_sessions.min().date()) if len(frozen_sessions) else None
    rep["bad_ohlc_bars"] = int(d["bad_ohlc"].sum()); rep["gap_bars"] = int(d["gap_before"].sum()); rep["outlier_bars"] = int(d["outlier"].sum())
    rep["zero_volume_share"] = float((d["v"] == 0).mean())
    normal = d[(d["session_type"] == "normal") & ~d["post_close"]]
    fr = normal.groupby("slot")["flat"].mean()
    rep["flat_bar_rate_overall"] = float(normal["flat"].mean())
    rep["flat_bar_slots"] = {f"{(OPEN_MIN + int(k) * 10) // 60:02d}:{(OPEN_MIN + int(k) * 10) % 60:02d}": round(float(v), 3) for k, v in fr[fr > 0.05].items()}
    if daily is not None and len(daily):
        dd = daily.copy(); dd["session"] = dd["ts"].dt.normalize()
        agg = normal.groupby("session").agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"))
        j = agg.join(dd.set_index("session")[["o", "h", "l", "c"]], rsuffix="_d", how="inner")
        out = {}
        for col in ("o", "h", "l", "c"):
            err = ((j[col] / j[col + "_d"]) - 1).abs() * 100
            out[col] = {"sessions": int(len(err)), "median_err_pct": round(float(err.median()), 4), "over_0.05pct": int((err > 0.05).sum()),
                        "worst": [f"{x.date()} {e:.2f}%" for x, e in err.sort_values(ascending=False).head(3).items()]}
        rep["daily_crosscheck"] = out
        rep["sessions_without_daily_bar"] = int(len(set(agg.index) - set(dd["session"])))
    cols = ["ts", "session", "slot", "o", "h", "l", "c", "v", "session_type", "bad_ohlc", "flat", "gap_before", "outlier", "post_close", "frozen_close", "usable", "ret_bp", "ret_clean_bp"]
    return d[cols], rep


def fetch_history(u: str, earliest: date = date(2015, 1, 1)) -> pd.DataFrame:
    key, today = C.INDEX_KEYS[u], C.now_ist().date()
    hi, empties, parts = today, 0, []
    while hi >= earliest and empties < 4:
        lo = hi - timedelta(days=27)
        try:
            d = C.candles_range(key, "minutes", 10, lo, hi)
        except Exception as e:
            log.warning("[%s] %s..%s: %s", u, lo, hi, str(e)[:100]); d = pd.DataFrame()
        if len(d):
            parts.append(d); empties = 0
        else:
            empties += 1
        hi = lo - timedelta(days=1)
        time.sleep(0.2)
    raw = pd.concat(parts).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return raw[raw["ts"].dt.date < today].reset_index(drop=True)                  # today is partial until the recorder has it


def build(u: str):
    raw = fetch_history(u)
    log.info("[%s] fetched %d bars %s -> %s", u, len(raw), raw.ts.iloc[0], raw.ts.iloc[-1])
    daily = C.candles_chunked(C.INDEX_KEYS[u], "days", 1, raw.ts.iloc[0].date(), raw.ts.iloc[-1].date(), chunk_days=360)
    d, rep = clean(raw, daily)
    out = C.DATA / "clean"; out.mkdir(parents=True, exist_ok=True)
    d.to_csv(out / f"index_10m_{u}.csv.gz", index=False, compression="gzip")
    (out / f"quality_{u}.json").write_text(json.dumps(rep, indent=1))
    print(f"\n===== {u} quality report =====")
    for k, v in rep.items():
        print(f"  {k}: {v}")
    return rep


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(); ap.add_argument("--only", choices=C.UNDERLYINGS)
    a = ap.parse_args()
    for u in ([a.only] if a.only else C.UNDERLYINGS):
        build(u)
