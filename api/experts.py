# "Selective crypto experts" feed — aggregates recent public activity from a
# hand-picked list of accounts across three source types:
#   - Instagram, via a two-tier chain per account:
#       1. The Graph API's `business_discovery` field, using YOUR OWN
#          Instagram Business account's access token. Free, no per-account
#          consent needed, but only works for accounts that are Business/
#          Creator type AND only if this app has Advanced Access from Meta's
#          App Review -- on Standard Access it silently refuses anything
#          that isn't an account with a role on this same Meta app.
#       2. A fallback to a third-party scraper (same pattern/config shape as
#          the "Gridly" project's IG_THIRDPARTY_* setup): a generic
#          POST-a-templated-body-to-a-templated-path config, by default
#          pointed at Apify's Instagram Scraper actor. This doesn't go
#          through Meta's permission system at all, so it works regardless
#          of Graph API access level -- but it's a paid, third-party service
#          (billed per Apify run) and noticeably slower (a real scrape, not
#          an instant API call), which is why each account's result count is
#          kept small and this route's Vercel function timeout is raised.
#   - YouTube, via each channel's free public uploads RSS feed (no API key).
#   - Blogs/newsletters, via their free public RSS feeds (no API key).
#
# Unlike Binance/OKX, this route needs no PROXY_ACCESS_TOKEN -- everything it
# returns is already public. Secrets used here (set as Vercel Environment
# Variables, never sent to the browser):
#   IG_ACCESS_TOKEN, IG_BUSINESS_ID          -- Graph API tier (optional)
#   IG_THIRDPARTY_BASE_URL                   -- e.g. https://api.apify.com
#   IG_THIRDPARTY_KEY_HEADER                 -- e.g. authorization
#   IG_THIRDPARTY_KEY                        -- e.g. Bearer apify_api_...
#   IG_THIRDPARTY_METHOD                     -- e.g. POST
#   IG_THIRDPARTY_MEDIA_PATH                 -- e.g. /v2/acts/apify~instagram-scraper/run-sync-get-dataset-items
#   IG_THIRDPARTY_MEDIA_BODY                 -- a JSON template; "{username}"
#                                                and "{limit}" get substituted
#                                                in before the request is sent
# If the Graph API vars are absent, that tier is just skipped. If the
# IG_THIRDPARTY_* vars are absent, that fallback is just skipped -- so this
# route degrades gracefully no matter which (if either) tier is configured.

from http.server import BaseHTTPRequestHandler
import sys
import os
import json
import urllib.parse
import urllib.request
import urllib.error
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
APIFY_RESULTS_PER_ACCOUNT = 2  # kept small -- each one costs Apify run time/credits


def _strip_ns(tag):
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _http_request(method, url, headers=None, body_bytes=None, timeout=55):
    merged = dict(common.BROWSER_HEADERS)
    merged.update(headers or {})
    req = urllib.request.Request(url, data=body_bytes, headers=merged, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 599, json.dumps({"error": str(e)}).encode()


def _parse_timestamp(value):
    if not value:
        return None
    try:
        return int(datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%S%z").timestamp())
    except Exception:
        pass
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except Exception:
        pass
    try:
        return int(float(value))  # already a unix timestamp
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Instagram tier 1: the Graph API's business_discovery field
# ---------------------------------------------------------------------------

def _fetch_instagram_via_graph(username):
    """Returns (items, failure_reason). failure_reason is None on success
    (even a successful "zero posts" result), or a short string explaining
    why this tier didn't produce anything, so the caller knows to fall
    back to the third-party tier."""
    token = common.env("IG_ACCESS_TOKEN")
    biz_id = common.env("IG_BUSINESS_ID")
    if not token or not biz_id:
        return None, "Graph API not configured (missing IG_ACCESS_TOKEN / IG_BUSINESS_ID)"

    fields = ("business_discovery.username({username}){{"
              "username,profile_picture_url,"
              "media.limit(3){{caption,media_url,permalink,timestamp,media_type,thumbnail_url}}"
              "}}")
    url = (f"https://graph.facebook.com/{GRAPH_VERSION}/{biz_id}"
           f"?fields={urllib.parse.quote(fields.format(username=username))}"
           f"&access_token={urllib.parse.quote(token)}")
    try:
        status, body = common.http_get(url)
        data = json.loads(body)
    except Exception:
        return None, "couldn't reach Instagram's Graph API"

    if "error" in data:
        msg = (data.get("error") or {}).get("message", "unknown error")
        return None, msg

    discovery = data.get("business_discovery")
    if not discovery:
        return None, "no data returned (account may not be Business/Creator type)"

    items = []
    for m in (discovery.get("media") or {}).get("data", []):
        caption = (m.get("caption") or "").strip()
        media_type = (m.get("media_type") or "post").lower()
        title = caption if caption else f"New {media_type} from @{username}"
        items.append({
            "source_type": "instagram",
            "expert": f"@{username}",
            "title": title[:280],
            "url": m.get("permalink"),
            "thumbnail": m.get("thumbnail_url") or m.get("media_url"),
            "published_on": _parse_timestamp(m.get("timestamp")),
        })
    return items, None


# ---------------------------------------------------------------------------
# Instagram tier 2: a generic third-party scraper fallback, configured the
# same way as the "Gridly" project's IG_THIRDPARTY_* setup (by default,
# Apify's Instagram Scraper actor, called via its run-sync-get-dataset-items
# endpoint). This is a paid service and a real scrape rather than an instant
# API call, so it's noticeably slower -- that's expected, not a bug.
# ---------------------------------------------------------------------------

def _apify_configured():
    return bool(common.env("IG_THIRDPARTY_BASE_URL") and common.env("IG_THIRDPARTY_KEY")
                and common.env("IG_THIRDPARTY_MEDIA_PATH") and common.env("IG_THIRDPARTY_MEDIA_BODY"))


def _fetch_instagram_via_thirdparty(username):
    base = common.env("IG_THIRDPARTY_BASE_URL").rstrip("/")
    key_header = common.env("IG_THIRDPARTY_KEY_HEADER") or "authorization"
    key_val = common.env("IG_THIRDPARTY_KEY")
    method = (common.env("IG_THIRDPARTY_METHOD") or "POST").upper()
    path = common.env("IG_THIRDPARTY_MEDIA_PATH")
    body_tpl = common.env("IG_THIRDPARTY_MEDIA_BODY")

    body_str = body_tpl.replace("{username}", username).replace("{limit}", str(APIFY_RESULTS_PER_ACCOUNT))
    try:
        json.loads(body_str)  # confirm substitution kept it valid JSON
    except Exception:
        return None, "IG_THIRDPARTY_MEDIA_BODY isn't valid JSON once {username}/{limit} are filled in"

    url = base + path
    headers = {key_header: key_val, "Content-Type": "application/json"}
    status, resp_body = _http_request(method, url, headers=headers, body_bytes=body_str.encode())
    if status != 200:
        snippet = resp_body.decode("utf-8", "replace")[:160].strip()
        return None, f"third-party API returned HTTP {status}: {snippet}"

    try:
        data = json.loads(resp_body)
    except Exception:
        return None, "third-party API returned a non-JSON response"

    rows = data if isinstance(data, list) else (data.get("items") or data.get("data") or [])
    items = []
    for post in rows[:APIFY_RESULTS_PER_ACCOUNT]:
        if not isinstance(post, dict):
            continue
        permalink = post.get("url") or post.get("permalink") or post.get("postUrl")
        if not permalink:
            continue
        caption = (post.get("caption") or "").strip()
        title = caption if caption else f"New post from @{username}"
        thumb = post.get("displayUrl") or post.get("thumbnailUrl") or post.get("imageUrl")
        ts = post.get("timestamp") or post.get("takenAtTimestamp") or post.get("takenAt")
        items.append({
            "source_type": "instagram",
            "expert": f"@{username}",
            "title": title[:280],
            "url": permalink,
            "thumbnail": thumb,
            "published_on": _parse_timestamp(ts),
        })
    return items, None


def _fetch_instagram(notes):
    items = []
    apify_ready = _apify_configured()

    for username in INSTAGRAM_USERNAMES:
        graph_items, graph_fail = _fetch_instagram_via_graph(username)
        if graph_items is not None:
            items.extend(graph_items)
            continue

        if not apify_ready:
            notes.append(f"Instagram @{username}: {graph_fail}, and no third-party fallback is "
                          f"configured (IG_THIRDPARTY_* environment variables).")
            continue

        tp_items, tp_fail = _fetch_instagram_via_thirdparty(username)
        if tp_items is not None:
            items.extend(tp_items)
        else:
            notes.append(f"Instagram @{username}: {tp_fail}")

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
