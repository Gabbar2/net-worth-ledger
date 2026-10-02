# "Selective crypto experts" feed — aggregates recent public activity from a
# hand-picked list of accounts across three source types:
#   - Instagram (Business/Creator accounts only) via the Graph API's
#     `business_discovery` field, using YOUR OWN Instagram Business account's
#     access token to look up someone else's PUBLIC posts. No per-account
#     login or consent from them is needed, but it only works for accounts
#     set to Business or Creator — a personal account is invisible to this
#     lookup.
#   - YouTube, via each channel's free public uploads RSS feed (no API key).
#   - Blogs/newsletters, via their free public RSS feeds (no API key).
#
# Unlike Binance/OKX, this route needs no PROXY_ACCESS_TOKEN -- everything it
# returns is already public. The Instagram piece does need two secrets
# though (IG_ACCESS_TOKEN, IG_BUSINESS_ID), set as Vercel Environment
# Variables -- never hand these to the browser.

from http.server import BaseHTTPRequestHandler
import sys
import os
import json
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.exists(os.path.join(_p, "_common.py")):
    _parent = os.path.dirname(_p)
    if _parent == _p:
        break
    _p = _parent
sys.path.insert(0, _p)
import _common as common

# ---------------------------------------------------------------------------
# The hand-picked expert list. Edit these three lists to add/remove people --
# no other code changes needed.
# ---------------------------------------------------------------------------

INSTAGRAM_USERNAMES = [
    "kashifoncrypto",
    "inspiredanalyst",
    "cryptowalaindia",
    "greygold__",
    "waqarzaka",
]

# (display name, YouTube channel ID -- the UC... string, not the @handle)
YOUTUBE_CHANNELS = [
    ("Waqar Zaka", "UCNc2cFy7_fRlcD0x-ddMFlQ"),
    ("Ilm e Aalim", "UCRBTa1JiHjYSDsrZQiGf73w"),
]

# (display name, RSS/Atom feed URL)
BLOG_FEEDS = [
    ("Bankless", "https://bankless.com/feed"),
    ("Glassnode Research", "https://research.glassnode.com/rss/"),
    ("Lyn Alden", "https://www.lynalden.com/feed/"),
]

GRAPH_VERSION = "v21.0"


def _strip_ns(tag):
    return tag.split("}", 1)[-1] if "}" in tag else tag


# ---------------------------------------------------------------------------
# Instagram, via business_discovery
# ---------------------------------------------------------------------------

def _fetch_instagram(notes):
    items = []
    token = common.env("IG_ACCESS_TOKEN")
    biz_id = common.env("IG_BUSINESS_ID")
    if not token or not biz_id:
        notes.append("Instagram isn't configured on this deployment (missing IG_ACCESS_TOKEN / "
                      "IG_BUSINESS_ID environment variables) -- skipping it for now.")
        return items

    fields = ("business_discovery.username({username}){{"
              "username,profile_picture_url,"
              "media.limit(3){{caption,media_url,permalink,timestamp,media_type,thumbnail_url}}"
              "}}")
    for username in INSTAGRAM_USERNAMES:
        url = (f"https://graph.facebook.com/{GRAPH_VERSION}/{biz_id}"
               f"?fields={urllib.parse.quote(fields.format(username=username))}"
               f"&access_token={urllib.parse.quote(token)}")
        try:
            status, body = common.http_get(url)
            data = json.loads(body)
        except Exception:
            notes.append(f"Instagram @{username}: couldn't reach Instagram's API.")
            continue

        if "error" in data:
            msg = (data.get("error") or {}).get("message", "unknown error")
            notes.append(f"Instagram @{username}: {msg}")
            continue

        discovery = data.get("business_discovery")
        if not discovery:
            notes.append(f"Instagram @{username}: no data returned (account may not be Business/"
                          f"Creator type, or the username is wrong).")
            continue

        media_items = (discovery.get("media") or {}).get("data", [])
        for m in media_items:
            ts = m.get("timestamp")
            published_on = None
            if ts:
                try:
                    published_on = int(datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").timestamp())
                except Exception:
                    published_on = None
            caption = (m.get("caption") or "").strip()
            media_type = (m.get("media_type") or "post").lower()
            title = caption if caption else f"New {media_type} from @{username}"
            items.append({
                "source_type": "instagram",
                "expert": f"@{username}",
                "title": title[:280],
                "url": m.get("permalink"),
                "thumbnail": m.get("thumbnail_url") or m.get("media_url"),
                "published_on": published_on,
            })
    return items


# ---------------------------------------------------------------------------
# YouTube, via each channel's free uploads RSS feed
# ---------------------------------------------------------------------------

def _parse_youtube_feed(display_name, xml_bytes):
    items = []
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return items
    for entry in root:
        if _strip_ns(entry.tag) != "entry":
            continue
        title, link, published, thumb = None, None, None, None
        for child in entry:
            tag = _strip_ns(child.tag)
            if tag == "title":
                title = (child.text or "").strip()
            elif tag == "link" and not link:
                link = child.get("href")
            elif tag == "published":
                published = (child.text or "").strip()
            elif tag == "group":
                for sub in child:
                    if _strip_ns(sub.tag) == "thumbnail" and not thumb:
                        thumb = sub.get("url")
        if not title or not link:
            continue
        published_on = None
        if published:
            try:
                published_on = int(datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp())
            except Exception:
                published_on = None
        items.append({
            "source_type": "youtube",
            "expert": display_name,
            "title": title,
            "url": link,
            "thumbnail": thumb,
            "published_on": published_on,
        })
    return items


def _fetch_youtube(notes):
    items = []
    for display_name, channel_id in YOUTUBE_CHANNELS:
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        try:
            status, body = common.http_get(url)
            if status != 200:
                notes.append(f"YouTube ({display_name}): HTTP {status}")
                continue
            items.extend(_parse_youtube_feed(display_name, body))
        except Exception:
            notes.append(f"YouTube ({display_name}): couldn't load this channel's feed.")
    return items


# ---------------------------------------------------------------------------
# Blogs/newsletters, via standard RSS
# ---------------------------------------------------------------------------

def _parse_blog_feed(display_name, xml_bytes):
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
            "source_type": "blog",
            "expert": display_name,
            "title": title,
            "url": link,
            "thumbnail": None,
            "published_on": published_on,
        })
    return items


def _fetch_blogs(notes):
    items = []
    for display_name, url in BLOG_FEEDS:
        try:
            status, body = common.http_get(url)
            if status != 200:
                notes.append(f"{display_name}: HTTP {status}")
                continue
            items.extend(_parse_blog_feed(display_name, body))
        except Exception:
            notes.append(f"{display_name}: couldn't load this feed.")
    return items


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        common.send_options(self)

    def do_GET(self):
        notes = []
        all_items = []
        all_items.extend(_fetch_instagram(notes))
        all_items.extend(_fetch_youtube(notes))
        all_items.extend(_fetch_blogs(notes))

        all_items.sort(key=lambda i: i["published_on"] or 0, reverse=True)

        seen = set()
        deduped = []
        for it in all_items:
            key = (it["expert"], (it["title"] or "").strip().lower())
            if key in seen:
                continue
            seen.add(key)
            deduped.append(it)

        common.send_json(self, 200, {"items": deduped[:30], "notes": notes})
