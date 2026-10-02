# Public crypto market news, aggregated server-side from a handful of
# outlets' free RSS feeds and returned as plain JSON.
#
# Unlike the Binance/OKX routes, this one needs no API keys and no access
# token -- it's just republishing public headlines, so there's nothing here
# worth locking down. That also means the dashboard's news card works for
# anyone who opens this deployment's URL, even before any exchange keys are
# configured.

from http.server import BaseHTTPRequestHandler
import sys
import os
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.exists(os.path.join(_p, "_common.py")):
    _parent = os.path.dirname(_p)
    if _parent == _p:
        break
    _p = _parent
sys.path.insert(0, _p)
import _common as common

FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
    ("Bitcoin Magazine", "https://bitcoinmagazine.com/feed"),
]


def _strip_ns(tag):
    return tag.split("}", 1)[-1] if "}" in tag else tag


# ---------------------------------------------------------------------------
# Rough, keyword-based "what kind of impact might this have" classifier.
# This is a heuristic, not a real analysis -- there's no AI model in this
# proxy to actually read and reason about each article, just three word
# lists run against the headline (and short summary, when the feed has
# one). Good enough for a quick visual signal, not a trading tool.
#   now    -> language that typically means the market already reacted
#   future -> language about things that could move price later
#   other  -> background/explainer coverage, or the fallback when neither
#             of the above matched anything
# A headline can match more than one list, which is intentional.
# ---------------------------------------------------------------------------

IMPACT_NOW_WORDS = [
    "crash", "crashes", "crashed", "plunge", "plunges", "plunged", "plummet",
    "plummets", "plummeted", "surge", "surges", "surged", "soar", "soars",
    "soared", "spike", "spikes", "spiked", "rally", "rallies", "rallied",
    "all-time high", "all time high", "record high", "record low", "ath",
    "hack", "hacked", "hacker", "exploit", "exploited", "breach", "breached",
    "liquidation", "liquidated", "liquidations", "sell-off", "selloff",
    "sells off", "dump", "dumped", "dumping", "pump", "pumped", "lawsuit",
    "sues", "sued", "suing", "charged", "charges", "indicted", "ban",
    "banned", "halt", "halted", "delist", "delisted", "delisting",
    "bankruptcy", "bankrupt", "insolvency", "insolvent", "collapse",
    "collapsed", "collapses", "drops", "dropped", "falls", "fell", "tumbles",
    "tumbled", "whale moves", "margin call", "flash crash", "outage", "down",
    "frozen", "freezes", "seized", "seizure", "fraud", "scam", "ponzi",
    "arrest", "arrested", "fined", "penalty", "etf approved", "etf rejected",
    "rate cut", "rate hike", "fomc", "fed decision", "cpi report",
]
IMPACT_FUTURE_WORDS = [
    "upgrade", "upgrades", "mainnet", "roadmap", "launch", "launches",
    "launching", "to launch", "partnership", "partners with", "integration",
    "integrates", "adoption", "regulation", "regulatory", "bill",
    "legislation", "lawmakers", "cbdc", "halving", "hard fork", "soft fork",
    "testnet", "lists", "listing", "institutional", "custody", "pilot",
    "proposal", "proposes", "framework", "policy", "license", "licence",
    "files for", "filing", "plans to", "expected to", "will launch",
    "development", "upcoming", "next year", "long-term", "long term",
]
IMPACT_OTHER_WORDS = [
    "explainer", "explained", "guide", "opinion", "interview", "analysis",
    "roundup", "recap", "overview", "what is", "how to", "everything you",
    "deep dive", "explains",
]


def _classify_impact(text):
    t = (text or "").lower()

    def _any(words):
        return any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", t) for w in words)

    now = _any(IMPACT_NOW_WORDS)
    future = _any(IMPACT_FUTURE_WORDS)
    other = _any(IMPACT_OTHER_WORDS) or (not now and not future)
    return {"now": now, "future": future, "other": other}


def _parse_feed(source_name, xml_bytes):
    items = []
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return items
    for node in root.iter():
        if _strip_ns(node.tag) != "item":
            continue
        title, link, pub, summary = None, None, None, None
        for child in node:
            tag = _strip_ns(child.tag)
            if tag == "title" and not title:
                title = (child.text or "").strip()
            elif tag == "link" and not link:
                link = (child.text or child.get("href") or "").strip()
            elif tag == "pubDate" and not pub:
                pub = (child.text or "").strip()
            elif tag in ("description", "summary") and not summary:
                summary = (child.text or "").strip()
        if not title or not link:
            continue
        published_on = None
        if pub:
            try:
                published_on = int(parsedate_to_datetime(pub).timestamp())
            except Exception:
                published_on = None
        items.append({
            "title": title,
            "url": link,
            "source": source_name,
            "published_on": published_on,
            "impact": _classify_impact(title + " " + (summary or "")),
        })
    return items


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        common.send_options(self)

    def do_GET(self):
        all_items = []
        for name, url in FEEDS:
            try:
                status, body = common.http_get(url)
                if status == 200:
                    all_items.extend(_parse_feed(name, body))
            except Exception:
                continue

        # newest first; anything without a parseable date sinks to the bottom
        all_items.sort(key=lambda i: i["published_on"] or 0, reverse=True)

        # de-dupe near-identical headlines picked up by more than one outlet
        seen = set()
        deduped = []
        for it in all_items:
            key = it["title"].strip().lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(it)

        if not deduped:
            return common.send_error(self, 502, "All upstream news feeds failed to respond — try again shortly.")

        common.send_json(self, 200, {"items": deduped[:24]})
