# -*- coding: utf-8 -*-
"""Option-chain recorder. Logs the chain; ALSO supplies the latest snapshot to the OI entry
gate when OI_GATE_ENABLED (it was logging-only until 2026-10-23; see bnf_config).

WHY RECORD IT AT ALL. The research (`research/oi_pcr_adx_study.py`) found no directional
or magnitude information in PCR, max-pain or IV skew. So this is not a signal and must
not become one by accident. It is recorded because it CANNOT BE REBUILT: Fyers deletes
expired chains, and NSE's public page shows only the current one. Every day not recorded
is a day permanently missing from any later study of IV, skew and open interest by strike
(which is where the untested ideas live: event IV crush, dealer positioning near expiry,
strike and expiry choice).

IT COSTS NO FYERS REQUESTS. For NIFTY/BANKNIFTY the chain comes from NSE's public website
via `nse_oi` (jugaad-data), as the parked system did. The Fyers quota is untouched.

IT CANNOT SLOW THE TRADING LOOP. The fetch is a web request with no timeout of its own,
and the runner's main loop is what prices an open position. So the fetch runs in a
daemon thread; `trigger()` returns at once; at most one fetch is in flight; and a fetch
that has hung for longer than CHAIN_FETCH_STALE_SEC is abandoned so later slots proceed.

IT CANNOT RAISE. Every failure is counted, logged at most once an hour, and swallowed.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime

import bnf_config as cfg


def _rows(df) -> list:
    """A pandas DataFrame or a list of dicts -> a list of dicts."""
    if df is None:
        return []
    if hasattr(df, 'to_dict'):
        return df.to_dict('records')
    return list(df)


def default_loader(c=cfg):
    """nse_oi.fetch_chain for the configured instrument, or None. Imported lazily so the
    package works (and tests run) on a machine without the parked codebase."""
    if c.LEGACY_DIR not in sys.path:
        sys.path.insert(0, c.LEGACY_DIR)
    import nse_oi
    hist = getattr(c, 'CHAIN_LEGACY_HISTORY_FILE', None)
    if hist:
        # also continue the old oi_intraday.jsonl in its original shape, so the studies
        # written against it keep working across the gap
        nse_oi.set_history_path(hist)
    return nse_oi.fetch_chain(c.INSTRUMENT, expiry_index=0, force=True)


class ChainRecorder:
    def __init__(self, log_dir: str, c=cfg, loader=None, logger=None):
        self.dir, self.c, self.log = log_dir, c, logger
        self.loader = loader or (lambda: default_loader(c))
        self._last_slot = None
        self._thread = None
        self._started = 0.0
        self._lock = threading.Lock()
        self._last_warn = 0.0
        self.ok = 0
        self.fail = 0
        self.last_error = ''
        self.last_ok_ts = None
        self._latest = None                 # (datetime, record) of the last good snapshot

    # ── scheduling ────────────────────────────────────────────────────────
    def slot_for(self, now: datetime):
        """15-minute slot number since 09:15, or None outside market hours."""
        mins = now.hour * 60 + now.minute - (9 * 60 + 15)
        if mins < 0 or mins > 375:                    # 09:15 .. 15:30
            return None
        return mins // max(int(self.c.CHAIN_RECORD_EVERY_MIN), 1)

    def trigger(self, now: datetime, spot: float | None = None) -> bool:
        """Start a background fetch if this slot has not been recorded. Returns at once."""
        try:
            if not getattr(self.c, 'CHAIN_RECORD_ENABLED', False):
                return False
            slot = self.slot_for(now)
            if slot is None or slot == self._last_slot:
                return False
            with self._lock:
                t = self._thread
                if t is not None and t.is_alive() and (time.monotonic() - self._started) < self.c.CHAIN_FETCH_STALE_SEC:
                    return False                      # one in flight; do not stack
                # Claim the slot NOW, success or not: a failed fetch waits for the next
                # slot rather than hammering NSE, which rate-limits.
                self._last_slot = slot
                self._started = time.monotonic()
                self._thread = threading.Thread(target=self._work, args=(now, slot, spot),
                                                name='bnf-chain', daemon=True)
                self._thread.start()
            return True
        except Exception:
            return False

    def wait(self, timeout: float = 5.0):
        """Join the in-flight fetch (tests)."""
        t = self._thread
        if t is not None:
            t.join(timeout)

    # ── the work ──────────────────────────────────────────────────────────
    def _work(self, now: datetime, slot: int, spot):
        try:
            chain = self.loader()
            rec = self._shape(chain, now, slot, spot)
            if rec is None:
                raise RuntimeError('empty chain (market closed, or NSE blocked/limited the request)')
            os.makedirs(self.dir, exist_ok=True)
            p = os.path.join(self.dir, f'bnf_chain_{now:%Y-%m-%d}.jsonl')
            with open(p, 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(rec) + '\n')
            self.ok += 1
            self.last_ok_ts = now.isoformat(timespec='seconds')
            with self._lock:
                self._latest = (now, rec)
        except Exception as e:
            self.fail += 1
            self.last_error = str(e)[:160]
            if self.log and time.time() - self._last_warn > 3600:
                self._last_warn = time.time()
                self.log.warning(f'[CHAIN] snapshot failed ({self.fail} so far): {self.last_error}')

    def _shape(self, chain, now: datetime, slot: int, spot):
        if not chain:
            return None
        rows = _rows(chain.get('df'))
        if not rows:
            return None
        px = float(chain.get('spot') or spot or 0.0)
        gap = int(self.c.STRIKE_GAP)
        atm = int(round(px / gap) * gap) if px > 0 else None
        keep = int(self.c.CHAIN_STRIKES_EACH_SIDE) * gap
        strikes = {}
        for r in rows:
            try:
                k = int(r['strike'])
            except Exception:
                continue
            if atm is not None and abs(k - atm) > keep:
                continue
            strikes[str(k)] = [int(r.get('call_oi') or 0), _f(r.get('call_iv')),
                               int(r.get('put_oi') or 0), _f(r.get('put_iv'))]
        return dict(ts=now.isoformat(timespec='seconds'), slot=slot, spot=px or None,
                    expiry=str(chain.get('expiry')), expiries=[str(x) for x in (chain.get('all_expiries') or [])[:3]],
                    pcr=_f(chain.get('pcr')), max_pain=_f(chain.get('max_pain')),
                    atm_iv=_f(chain.get('atm_iv')), iv_skew=_f(chain.get('iv_skew')),
                    total_call_oi=chain.get('total_call_oi'), total_put_oi=chain.get('total_put_oi'),
                    columns=['call_oi', 'call_iv', 'put_oi', 'put_iv'], n_strikes=len(strikes),
                    strikes=strikes)

    def snapshot(self, now: datetime):
        """The latest good snapshot with its age in minutes, or None. After a restart the
        in-memory copy is gone, so fall back to the last line of today's file."""
        with self._lock:
            latest = self._latest
        rec, ts = None, None
        if latest is not None:
            ts, rec = latest
        else:
            try:
                p = os.path.join(self.dir, f'bnf_chain_{now:%Y-%m-%d}.jsonl')
                with open(p, encoding='utf-8') as fh:
                    last = None
                    for ln in fh:
                        if ln.strip():
                            last = ln
                rec = json.loads(last) if last else None
                ts = datetime.fromisoformat(rec['ts']) if rec else None
            except Exception:
                rec, ts = None, None
        if rec is None or ts is None:
            return None
        out = dict(rec)
        out['age_min'] = (now - ts).total_seconds() / 60.0
        return out

    def status(self) -> dict:
        return dict(ok=self.ok, fail=self.fail, last_ok=self.last_ok_ts, last_error=self.last_error or None)


def _f(x):
    try:
        v = float(x)
        return None if v != v else round(v, 4)
    except Exception:
        return None


class PcrHistory:
    """PCR at a given time of day on previous sessions, read from the oi_intraday.jsonl the
    recorder (and, before Oct 21, the parked bots) append to. Same source, same computation,
    so today's PCR and its history are comparable."""

    def __init__(self, path: str, instrument: str):
        self.path, self.instrument = path, instrument
        self._by_day = {}
        self._loaded = 0.0

    def _load(self):
        if time.time() - self._loaded < 600:
            return
        by = {}
        try:
            with open(self.path, encoding='utf-8', errors='ignore') as fh:
                for ln in fh:
                    try:
                        d = json.loads(ln)
                    except Exception:
                        continue
                    if d.get('instrument') != self.instrument or d.get('pcr') is None:
                        continue
                    ts = d.get('ts_str') or ''
                    by.setdefault(ts[:10], []).append((ts[11:16], float(d['pcr'])))
        except Exception:
            pass
        for k in by:
            by[k].sort()
        self._by_day, self._loaded = by, time.time()

    def prior(self, day: str, hhmm: str, n: int = 20) -> list:
        self._load()
        out = []
        for d in sorted(k for k in self._by_day if k < day)[-n:]:
            v = [p for t, p in self._by_day[d] if t <= hhmm]
            if v:
                out.append(v[-1])
        return out
