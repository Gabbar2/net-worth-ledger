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


def _parse_feed(source_name, xml_bytes):
    items = []
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return items
    for node in root.iter():
        if _strip_ns(node.tag) != "item":
            continue
        title, link, pub = None, None, None
        for child in node:
            tag = _strip_ns(child.tag)
            if tag == "title" and not title:
                title = (child.text or "").strip()
            elif tag == "link" and not link:
                link = (child.text or child.get("href") or "").strip()
            elif tag == "pubDate" and not pub:
                pub = (child.text or "").strip()
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

        all_items.sort(key=lambda i: i["published_on"] or 0, reverse=True)

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
