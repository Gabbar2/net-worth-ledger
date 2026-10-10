# Optional alternate Instagram source for the Expert's card, via Bright
# Data's Instagram Profiles dataset. This exists purely as a manual
# fallback for when Apify's monthly free credit (used by experts.py's
# normal ⟳ refresh) runs out -- it does NOT replace anything: the main
# Graph API → Apify chain in experts.py is untouched and keeps working
# exactly as before. This route is only ever called when you tap the
# Expert's card's separate "BD" button.
#
# Bright Data's dataset API is asynchronous: a request first *triggers* a
# collection job and gets back a snapshot_id, then you poll that job's
# progress, then you download the finished snapshot once it's ready. A
# full run can take anywhere from a few seconds to over a minute --
# comfortably past Vercel's hard 10-second function cap on the Hobby plan
# -- so this route does NOT sit and block waiting for the job to finish.
# Instead:
#   - A tap with no ?resume= triggers a brand-new (billable) job, polls it
#     briefly, and either returns results immediately (if Bright Data
#     happens to finish fast) or returns right away with a "still
#     processing" note plus that job's snapshot_id.
#   - The dashboard remembers that snapshot_id and sends it back as
#     ?resume=<snapshot_id> the next time you tap the same button, so a
#     second tap resumes checking the SAME job instead of starting a new
#     one and paying for it twice.
#
# One honest caveat: Bright Data's own docs don't publish the exact field
# names inside a profile record's embedded "posts" list, so the field
# names read below (url/caption/thumbnail/date, plus a few common
# synonyms of each) are a best-effort, defensive guess. If this route
# comes back with "profile fetched but had no posts data" for every
# account even once Bright Data is properly configured and billing, the
# real field names differ from what's guessed here -- paste the actual
# JSON Bright Data returned and the parsing below can be corrected.
#
# Secrets used here (set as Vercel Environment Variables, never sent to
# the browser):
#   BRIGHTDATA_API_KEY        -- your Bright Data API key
#   BRIGHTDATA_IG_DATASET_ID  -- optional override; defaults to Bright
#                                 Data's public Instagram Profiles dataset
#                                 ID below
#
# If BRIGHTDATA_API_KEY is absent, every request just returns an empty
# list with a note explaining why, instead of failing outright.

from http.server import BaseHTTPRequestHandler
import sys
import os
import json
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.exists(os.path.join(_p, "_common.py")):
    _parent = os.path.dirname(_p)
    if _parent == _p:
        break
    _p = _parent
sys.path.insert(0, _p)
import _common as common

# Keep this in sync with INSTAGRAM_USERNAMES in experts.py by hand --
# duplicated here (rather than imported) so this file has no import-time
# dependency on experts.py and can be edited/deployed on its own.
INSTAGRAM_USERNAMES = [
    "kashifoncrypto",
    "inspiredanalyst",
    "cryptowalaindia",
    "greygold__",
    "waqarzaka",
]

DEFAULT_DATASET_ID = "gd_l1vikfch901nx3by4"  # Bright Data's Instagram Profiles dataset
POSTS_PER_ACCOUNT = 3
POLL_ATTEMPTS = 2          # how many times THIS single request checks before giving up
POLL_INTERVAL_SECONDS = 3  # spacing between those checks, kept small to stay under Vercel's timeout


def _brightdata_configured():
    return bool(common.env("BRIGHTDATA_API_KEY"))


def _dataset_id():
    return common.env("BRIGHTDATA_IG_DATASET_ID") or DEFAULT_DATASET_ID


def _bd_headers():
    return {"Authorization": f"Bearer {common.env('BRIGHTDATA_API_KEY')}",
            "Content-Type": "application/json"}


def _http_request(method, url, headers=None, body_bytes=None, timeout=15):
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


def _trigger_job():
    body = json.dumps({
        "input": [{"url": f"https://www.instagram.com/{u}"} for u in INSTAGRAM_USERNAMES]
    }).encode()
    url = ("https://api.brightdata.com/datasets/v3/trigger?"
           + urllib.parse.urlencode({"dataset_id": _dataset_id(), "include_errors": "true"}))
    status, body_resp = _http_request("POST", url, headers=_bd_headers(), body_bytes=body)
    if status != 200:
        snippet = body_resp.decode("utf-8", "replace")[:160].strip()
        return None, f"Bright Data rejected the trigger request (HTTP {status}): {snippet}"
    try:
        data = json.loads(body_resp)
    except Exception:
        return None, "Bright Data's trigger response wasn't valid JSON"
    snap = data.get("snapshot_id")
    if not snap:
        return None, "Bright Data's trigger response had no snapshot_id"
    return snap, None


def _check_progress(snapshot_id):
    url = f"https://api.brightdata.com/datasets/v3/progress/{snapshot_id}"
    status, body = _http_request("GET", url, headers=_bd_headers())
    try:
        data = json.loads(body)
    except Exception:
        return "failed", "Bright Data's progress response wasn't valid JSON"
    return data.get("status"), data.get("error_message")


def _download_snapshot(snapshot_id):
    url = f"https://api.brightdata.com/datasets/v3/snapshot/{snapshot_id}?format=json"
    status, body = _http_request("GET", url, headers=_bd_headers(), timeout=20)
    if status != 200:
        snippet = body.decode("utf-8", "replace")[:160].strip()
        return None, f"Bright Data returned HTTP {status} fetching the finished snapshot: {snippet}"
    try:
        data = json.loads(body)
    except Exception:
        return None, "Bright Data's snapshot response wasn't valid JSON"
    return (data if isinstance(data, list) else []), None


def _parse_ts(value):
    if not value:
        return None
    try:
        return int(float(value))
    except Exception:
        pass
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _items_from_profiles(rows, notes):
    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        username = (row.get("account") or row.get("username") or row.get("user_name") or "unknown")
        username = str(username).lstrip("@")
        posts = row.get("posts") or row.get("top_posts") or row.get("recent_posts") or []
        if not posts:
            notes.append(f"Instagram (Bright Data) @{username}: profile fetched but it had no "
                         f"posts data in this dataset's response.")
            continue
        for p in posts[:POSTS_PER_ACCOUNT]:
            if not isinstance(p, dict):
                continue
            permalink = p.get("url") or p.get("post_url") or p.get("permalink")
            if not permalink and p.get("shortcode"):
                permalink = f"https://www.instagram.com/p/{p['shortcode']}/"
            if not permalink:
                continue
            caption = (p.get("caption") or p.get("description") or p.get("text") or "").strip()
            thumb = (p.get("thumbnail") or p.get("thumbnail_url") or p.get("display_url")
                     or p.get("image_url") or p.get("photos"))
            if isinstance(thumb, list):
                thumb = thumb[0] if thumb else None
            ts = p.get("date_posted") or p.get("timestamp") or p.get("taken_at")
            items.append({
                "source_type": "instagram",
                "expert": f"@{username}",
                "title": caption[:280] if caption else f"New post from @{username}",
                "url": permalink,
                "thumbnail": thumb,
                "published_on": _parse_ts(ts),
            })
    return items


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        common.send_options(self)

    def do_GET(self):
        notes = []

        if not _brightdata_configured():
            common.send_json(self, 200, {
                "items": [],
                "notes": ["Bright Data isn't configured — set BRIGHTDATA_API_KEY in Vercel's "
                          "Environment Variables to turn this on. This is optional; the main ⟳ "
                          "button keeps working via Apify either way."],
                "pending_snapshot_id": None,
            })
            return

        qs = common.parse_query(self)
        resume_id = (qs.get("resume") or [None])[0]

        if resume_id:
            snapshot_id, trigger_err = resume_id, None
        else:
            snapshot_id, trigger_err = _trigger_job()

        if trigger_err:
            common.send_json(self, 200, {"items": [], "notes": [trigger_err], "pending_snapshot_id": None})
            return

        status = None
        err = None
        for attempt in range(POLL_ATTEMPTS):
            status, err = _check_progress(snapshot_id)
            if status == "ready":
                break
            if status in ("failed", "canceled"):
                notes.append(f"Bright Data job {status}: {err or 'no error message given'}")
                common.send_json(self, 200, {"items": [], "notes": notes, "pending_snapshot_id": None})
                return
            if attempt < POLL_ATTEMPTS - 1:
                time.sleep(POLL_INTERVAL_SECONDS)

        if status != "ready":
            common.send_json(self, 200, {
                "items": [],
                "notes": ["Bright Data is still scraping — this can take under a minute. Tap this "
                          "button again shortly; it'll pick up the same job instead of starting a "
                          "new one."],
                "pending_snapshot_id": snapshot_id,
            })
            return

        rows, dl_err = _download_snapshot(snapshot_id)
        if dl_err:
            common.send_json(self, 200, {"items": [], "notes": [dl_err], "pending_snapshot_id": None})
            return

        items = _items_from_profiles(rows, notes)
        items.sort(key=lambda i: i["published_on"] or 0, reverse=True)
        common.send_json(self, 200, {"items": items[:30], "notes": notes, "pending_snapshot_id": None})
