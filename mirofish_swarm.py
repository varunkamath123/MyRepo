"""
MiroFish Swarm — qualitative/news research layer for Kronos Futures Bot.
==========================================================================
Kronos reads only price shape. It has zero awareness of news, RBI policy,
FII/DII flows, or scheduled events. The Jul 7-8 2026 sessions exposed this
directly: both days saw >2% index moves that Kronos's forecast completely
missed, because the drivers were real-world events, not price patterns.

This script gathers real headlines across 5 angles, then asks Claude to
synthesize a directional lean (0=bearish .. 1=bullish) per instrument.
Runs TWICE daily, writing the same mirofish_scores.json each time (most
recent read wins):
  ~08:45 IST — pre-market read, feeds paper_trader.py --morning-scan
               (early exit on an overnight news flip against an open position)
  ~14:35 IST — close-time read, feeds paper_trader.py's entry veto gate

Angles researched:
  1. Global cues        — overnight Wall Street / Asian markets
  2. India macro         — RBI policy, inflation, GDP
  3. Banking sector       — PSU banks, NBFCs, credit growth (feeds BANKNIFTY)
  4. FII/DII flows        — institutional buying/selling pressure
  5. Scheduled events      — earnings, policy dates, index events this week

Run:
    python mirofish_swarm.py
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

OUT_FILE = Path(__file__).parent / "mirofish_scores.json"
MAX_HEADLINE_AGE_HOURS = 168     # 7 days — niche angles (banking, FII/DII) post less often
HEADLINES_PER_ANGLE    = 6

ANGLES = {
    "global_cues":      "Wall Street S&P 500 Nasdaq Asian markets today",
    "india_macro":       "RBI monetary policy India inflation India GDP",
    "banking_sector":     "Indian banking sector PSU bank NBFC Bank Nifty",
    "fii_dii_flows":       "FII DII flows India stock market today",
    "scheduled_events":     "Nifty Bank Nifty earnings results India this week",
}

_ENV_PATH = Path(__file__).parent / ".env"


def _load_env():
    """Minimal .env loader (avoids adding python-dotenv as a dependency)."""
    if not _ENV_PATH.exists():
        return
    for line in _ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


# ── News gathering (Google News RSS — free, no API key) ────────────────────

def fetch_headlines(query: str, max_items: int = HEADLINES_PER_ANGLE) -> list[dict]:
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception as e:
        log.warning("News fetch failed for %r: %s", query, e)
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        log.warning("RSS parse failed for %r: %s", query, e)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_HEADLINE_AGE_HOURS)
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        pub_date_raw = (item.findtext("pubDate") or "").strip()
        if not title:
            continue
        pub_dt = None
        if pub_date_raw:
            try:
                pub_dt = datetime.strptime(pub_date_raw, "%a, %d %b %Y %H:%M:%S %Z")
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        if pub_dt and pub_dt < cutoff:
            continue
        # Strip " - Source Name" suffix Google News appends to titles
        title = re.sub(r"\s*-\s*[^-]{2,40}$", "", title)
        items.append({"title": title, "published": pub_date_raw})
        if len(items) >= max_items:
            break
    return items


def gather_all_headlines() -> dict[str, list[dict]]:
    result = {}
    for angle, query in ANGLES.items():
        headlines = fetch_headlines(query)
        log.info("[%s] %d headlines", angle, len(headlines))
        result[angle] = headlines
    return result


# ── Claude synthesis ────────────────────────────────────────────────────────

_SYNTHESIS_PROMPT = """You are a market analyst synthesizing real-world news into a directional \
lean for Indian index futures trading. You will be given recent headlines grouped by category.

Your job: assess whether the news backdrop is bullish, bearish, or neutral for NIFTY and for \
BANKNIFTY over the next few trading days. Banking-sector news (category "banking_sector") \
should weigh more heavily on BANKNIFTY than on NIFTY. Global cues and macro news affect both \
roughly equally.

Each headline is prefixed with its publish date — weigh more recent headlines (last 1-2 days)
much more heavily than older ones (up to 7 days back); older items are context, not drivers.
If headlines are sparse, contradictory, or clearly stale/irrelevant, lean toward "neutral" with \
a score near 0.5 rather than overreacting to a single ambiguous item.

Respond with ONLY a JSON object, no other text, in exactly this shape:
{
  "NIFTY":     {"lean": "bullish|bearish|neutral", "score": 0.0-1.0, "reasons": ["short reason 1", "short reason 2"]},
  "BANKNIFTY": {"lean": "bullish|bearish|neutral", "score": 0.0-1.0, "reasons": ["short reason 1", "short reason 2"]}
}
score: 0.0 = strongly bearish, 0.5 = neutral, 1.0 = strongly bullish.
reasons: at most 2 short items (under 15 words each), citing the specific headline driving the lean.

HEADLINES:
__HEADLINES_BLOCK__
"""


def synthesize(headlines_by_angle: dict[str, list[dict]]) -> dict:
    blocks = []
    for angle, items in headlines_by_angle.items():
        if not items:
            blocks.append(f"[{angle}] (no recent headlines)")
            continue
        lines = "\n".join(f"  - ({h['published'] or 'date unknown'}) {h['title']}" for h in items)
        blocks.append(f"[{angle}]\n{lines}")
    headlines_block = "\n\n".join(blocks)

    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    prompt = _SYNTHESIS_PROMPT.replace("__HEADLINES_BLOCK__", headlines_block)
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        temperature=0.0,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()

    # Strip markdown code fences if present
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        log.error("Failed to parse Claude synthesis output: %s\nRaw: %s", e, text[:500])
        raise

    for inst in ("NIFTY", "BANKNIFTY"):
        if inst not in parsed or "score" not in parsed[inst]:
            raise ValueError(f"Synthesis output missing {inst}.score")

    return parsed


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    _load_env()
    if "ANTHROPIC_API_KEY" not in os.environ:
        log.error("ANTHROPIC_API_KEY not set (.env not found or missing key) — aborting")
        sys.exit(1)

    log.info("Gathering headlines across %d angles...", len(ANGLES))
    headlines = gather_all_headlines()

    total = sum(len(v) for v in headlines.values())
    if total == 0:
        log.warning("No headlines gathered at all — writing neutral fallback scores")
        result = {
            "NIFTY":     {"lean": "neutral", "score": 0.5, "reasons": ["no headlines available"]},
            "BANKNIFTY": {"lean": "neutral", "score": 0.5, "reasons": ["no headlines available"]},
        }
    else:
        log.info("Synthesizing %d total headlines via Claude...", total)
        result = synthesize(headlines)

    result["generated_at"] = datetime.now(IST).isoformat()
    result["date"] = datetime.now(IST).date().isoformat()
    result["headline_counts"] = {k: len(v) for k, v in headlines.items()}

    OUT_FILE.write_text(json.dumps(result, indent=2))
    log.info("Wrote %s", OUT_FILE)

    for inst in ("NIFTY", "BANKNIFTY"):
        r = result[inst]
        log.info("[%s] lean=%s score=%.2f  reasons=%s",
                 inst, r["lean"], r["score"], r["reasons"])


if __name__ == "__main__":
    main()
