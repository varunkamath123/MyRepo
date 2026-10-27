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

import glob
import json
import os
import sys
import threading
import time
from datetime import datetime

import bnf_broker
import bnf_config as cfg
from bnf_data import IST


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
    def __init__(self, log_dir: str, c=cfg, loader=None, logger=None, prefix: str = 'bnf_chain',
                 needs_client: bool = False):
        self.dir, self.c, self.log = log_dir, c, logger
        self.prefix = prefix
        self.needs_client = needs_client    # True: the source uses the Fyers client, so trigger only once it exists
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
        """Slot number (CHAIN_RECORD_EVERY_MIN minutes wide) since 09:15, or None outside market hours."""
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
            p = os.path.join(self.dir, f'{self.prefix}_{now:%Y-%m-%d}.jsonl')
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
        expiry_day = _is_expiry_day(chain.get('expiry'), now)
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
                    source=str(chain.get('source') or 'nse'),
                    expiry=str(chain.get('expiry')), expiry_day=expiry_day, expiries=[str(x) for x in (chain.get('all_expiries') or [])[:3]],
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
                p = os.path.join(self.dir, f'{self.prefix}_{now:%Y-%m-%d}.jsonl')
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


def _is_expiry_day(expiry, now: datetime) -> bool:
    """True when the chain's expiry is today: the series is expiring and is NOT the one the bot
    trades (it rolls to the next monthly that day). Accepts NSE's '27-Oct-2026' and ISO dates."""
    s = str(expiry or '')
    for fmt in ('%d-%b-%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(s[:11] if fmt == '%d-%b-%Y' else s[:10], fmt).date() == now.date()
        except ValueError:
            continue
    return False


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


class FyersChainSource:
    """Loader for ChainRecorder that reads the BROKER's option chain, for the series the bot trades.

    Why: NSE's public page returns only the nearest expiry, so on that series' expiry day the gate
    was reading a chain for a series the bot does not trade. Fyers lets us ask for an expiry.
    Cost: one request per snapshot (~77/day at 5 minutes) plus one discovery call a day, from the
    API budget under the label 'chain'. It has NO implied volatility (NSE's recorder keeps that).
    """

    def __init__(self, get_broker, c=cfg, today_fn=None):
        self.get_broker, self.c = get_broker, c
        self._today = today_fn or (lambda: datetime.now(IST).date())
        self._ts = {}                       # (year, month) of the traded expiry -> epoch string

    def _traded_expiry(self, b, today):
        e = getattr(b, '_expiry', None)
        if e and e[0] == today:
            return e[1]
        cands = bnf_broker.expiry_candidates(today, self.c)
        return cands[0] if cands else None

    @staticmethod
    def _parse_expiries(expiry_data) -> list:
        out = []
        for e in expiry_data or []:
            try:
                d = datetime.strptime(str(e.get('date')), '%d-%m-%Y').date()
                out.append((d, str(e.get('expiry'))))
            except Exception:
                continue
        return out

    def __call__(self):
        b = self.get_broker()
        if b is None or getattr(b, 'f', None) is None:
            raise RuntimeError('no Fyers client yet')
        today = self._today()
        exp = self._traded_expiry(b, today)
        if exp is None:
            raise RuntimeError('no tradable expiry')
        key = (exp.year, exp.month)
        n = int(self.c.CHAIN_STRIKES_EACH_SIDE)
        ts = self._ts.get(key)
        data, st = b.option_chain(ts or '', n)
        if data is None:
            raise RuntimeError(f'fyers chain: {st}')
        expiries = self._parse_expiries(data.get('expiryData'))
        match = next(((d, e) for d, e in expiries if (d.year, d.month) == key), None)
        if match is None:
            raise RuntimeError(f'traded expiry {key} not in Fyers expiryData')
        if ts is None:
            self._ts[key] = match[1]
            if expiries and (expiries[0][0].year, expiries[0][0].month) != key:
                # the discovery call returned the NEAREST series; ask for the traded one
                data, st = b.option_chain(match[1], n)
                if data is None:
                    raise RuntimeError(f'fyers chain: {st}')
        rows, spot = {}, None
        for r in data.get('optionsChain', []):
            ot = r.get('option_type')
            if ot not in ('CE', 'PE'):
                if spot is None and r.get('ltp'):
                    spot = float(r['ltp'])
                continue
            k = int(r.get('strike_price', 0))
            row = rows.setdefault(k, dict(strike=k, call_oi=0, call_iv=None, put_oi=0, put_iv=None))
            row['call_oi' if ot == 'CE' else 'put_oi'] = int(float(r.get('oi') or 0))
        if not rows or not spot:
            raise RuntimeError('fyers chain without strikes or spot')
        co, po = float(data.get('callOi') or 0), float(data.get('putOi') or 0)
        return dict(source='fyers', spot=spot, expiry=match[0].isoformat(),
                    all_expiries=[d.isoformat() for d, _ in expiries],
                    pcr=round(po / co, 3) if co else None, max_pain=None, atm_iv=None, iv_skew=None,
                    total_call_oi=co, total_put_oi=po,
                    df=[rows[k] for k in sorted(rows)])


class ChainFilePcrHistory(PcrHistory):
    """PCR history from the recorder's own daily files (`<prefix>_<date>.jsonl`). Used when the gate
    reads the Fyers chain: its PCR is a different quantity (the traded series, all strikes) from the
    NSE nearest-series PCR in oi_intraday.jsonl, so it needs its own history, which starts empty."""

    def __init__(self, log_dir: str, prefix: str):
        super().__init__(log_dir, '')
        self.dir, self.prefix = log_dir, prefix

    def _load(self):
        if time.time() - self._loaded < 600:
            return
        by = {}
        for p in sorted(glob.glob(os.path.join(self.dir, f'{self.prefix}_*.jsonl'))):
            try:
                with open(p, encoding='utf-8', errors='ignore') as fh:
                    for ln in fh:
                        try:
                            d = json.loads(ln)
                        except Exception:
                            continue
                        ts, pcr = str(d.get('ts') or ''), d.get('pcr')
                        if pcr is None or len(ts) < 16:
                            continue
                        by.setdefault(ts[:10], []).append((ts[11:16], float(pcr)))
            except Exception:
                continue
        for k in by:
            by[k].sort()
        self._by_day, self._loaded = by, time.time()
