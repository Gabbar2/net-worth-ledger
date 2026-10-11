# Two small, related read-only endpoints live in this one file on purpose:
# the dashboard's "how much is left" checks AND its cross-device tap
# counter. They were originally two separate files, but Vercel's Hobby
# plan caps a deployment at 12 serverless functions total, and this
# project was already at that ceiling -- so these were merged into one
# file (one function) instead of adding a 13th. Functionally nothing
# about either feature changed; this is purely about staying under that
# per-file-is-a-function limit.
#
# ---------------------------------------------------------------------------
# PART 1: live provider balance/credit check
#   ?provider=twitterapi  -> twitterapi.io's own "my info" endpoint
#   ?provider=brightdata  -> Bright Data's own "customer balance" endpoint
#
# Powers the small tooltip you get by hovering (desktop) or long-pressing
# (mobile) the "BD" button on the Expert's card and the ⟳ button on the
# Xpert's Tweets card. This is a read-only balance lookup; it never
# triggers a scrape and never spends any of that balance itself.
#
# Honest caveat: neither provider's balance API appears to include
# promotional/bonus/free-trial credit in these numbers -- just the
# recharge/paid balance -- so a number here can look lower (even "0")
# compared to what the provider's own dashboard shows while you're still
# mostly running on bonus credit. Treat this as a rough signal, not an
# exact countdown, and the dashboard says so in the tooltip text itself.
#
# Secrets used here are the SAME ones already set for the other routes --
# this part adds no new environment variables:
#   TWITTERAPI_IO_KEY, BRIGHTDATA_API_KEY
#
# ---------------------------------------------------------------------------
# PART 2: synced monthly tap counter
#   ?key=<name>&action=get    -> read the current month's count (no change)
#   ?key=<name>&action=bump   -> increment the current month's count by one
#                                 and return the new total
#
# This is what makes the little badge on the "BD" button and the Xpert's
# Tweets card's ⟳ button show the SAME number whether you check the
# dashboard on your phone or your laptop, instead of each device keeping
# its own separate local tally.
#
# Storage: a Vercel KV store (built on Upstash Redis), reached over its
# plain REST API -- no Redis client library needed, just plain HTTP calls,
# same style as every other route in this proxy.
#
# SETUP (one-time, from the Vercel dashboard -- no code change needed):
#   1. Open your project in the Vercel dashboard → Storage tab → Create
#      Database → KV (Upstash). Any region/free tier is fine for this.
#   2. On the "Connect to a project" step, connect it to THIS project.
#      Vercel automatically adds KV_REST_API_URL and KV_REST_API_TOKEN as
#      Environment Variables for you -- no manual copy/pasting of keys.
#   3. Redeploy (or push any small change) so the new env vars are picked
#      up. That's it -- no other file needs to change.
#
# Until that's done, every counter call here returns {"count": null,
# "error": "..."} instead of failing outright, and the dashboard falls
# back to each device's own local tap count exactly as it did before this
# feature existed -- so skipping this setup doesn't break anything, it
# just means the badge stays per-device.
#
# Also works with a standalone Upstash Redis database (not created
# through Vercel's own Storage tab) if its REST URL/token are set as
# UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN instead.

from http.server import BaseHTTPRequestHandler
import sys
import os
import json
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.exists(os.path.join(_p, "_common.py")):
    _parent = os.path.dirname(_p)
    if _parent == _p:
        break
    _p = _parent
sys.path.insert(0, _p)
import _common as common


# ---------------------------------------------------------------------------
# Part 1: balance/credit check
# ---------------------------------------------------------------------------

def _get(url, headers):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 599, json.dumps({"error": str(e)}).encode()


def _twitterapi_usage():
    key = common.env("TWITTERAPI_IO_KEY")
    if not key:
        return {"error": "twitterapi.io isn't configured (TWITTERAPI_IO_KEY not set)."}
    status, body = _get("https://api.twitterapi.io/oapi/my/info", {"X-API-Key": key})
    if status != 200:
        snippet = body.decode("utf-8", "replace")[:160].strip()
        return {"error": f"twitterapi.io returned HTTP {status}: {snippet}"}
    try:
        data = json.loads(body)
    except Exception:
        return {"error": "twitterapi.io returned a non-JSON response"}
    return {"recharge_credits": data.get("recharge_credits")}


def _brightdata_usage():
    key = common.env("BRIGHTDATA_API_KEY")
    if not key:
        return {"error": "Bright Data isn't configured (BRIGHTDATA_API_KEY not set)."}
    status, body = _get("https://api.brightdata.com/customer/balance",
                         {"Authorization": f"Bearer {key}"})
    if status != 200:
        snippet = body.decode("utf-8", "replace")[:160].strip()
        return {"error": f"Bright Data returned HTTP {status}: {snippet}"}
    try:
        data = json.loads(body)
    except Exception:
        return {"error": "Bright Data returned a non-JSON response"}
    return {"balance": data.get("balance"), "credit": data.get("credit")}


# ---------------------------------------------------------------------------
# Part 2: synced monthly tap counter
# ---------------------------------------------------------------------------

def _kv_url():
    return common.env("KV_REST_API_URL") or common.env("UPSTASH_REDIS_REST_URL")


def _kv_token():
    return common.env("KV_REST_API_TOKEN") or common.env("UPSTASH_REDIS_REST_TOKEN")


def _kv_configured():
    return bool(_kv_url() and _kv_token())


def _kv_call(parts):
    # Upstash's REST API takes a Redis command as path segments, e.g.
    # GET https://<url>/incr/<key> == Redis command INCR <key>.
    url = _kv_url().rstrip("/") + "/" + "/".join(urllib.parse.quote(str(p), safe="") for p in parts)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_kv_token()}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 599, json.dumps({"error": str(e)}).encode()


def _month_key(key):
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    safe_key = "".join(ch for ch in key.strip().lower() if ch.isalnum() or ch in "-_") or "unnamed"
    return f"nwl:count:{safe_key}:{month}"


_NOT_CONFIGURED_MSG = (
    "Synced counter isn't set up yet — add a Vercel KV store and connect it "
    "to this project (Vercel dashboard → Storage → Create Database → KV) to "
    "make this badge the same on every device. Showing this device's own "
    "count instead for now."
)


def _get_count(key):
    if not _kv_configured():
        return {"count": None, "error": _NOT_CONFIGURED_MSG}
    status, body = _kv_call(["get", _month_key(key)])
    if status != 200:
        snippet = body.decode("utf-8", "replace")[:160].strip()
        return {"count": None, "error": f"Counter store returned HTTP {status}: {snippet}"}
    try:
        data = json.loads(body)
    except Exception:
        return {"count": None, "error": "Counter store returned a non-JSON response"}
    raw = data.get("result")
    try:
        count = int(raw) if raw is not None else 0
    except Exception:
        count = 0
    return {"count": count}


def _bump_count(key):
    if not _kv_configured():
        return {"count": None, "error": _NOT_CONFIGURED_MSG}
    redis_key = _month_key(key)
    status, body = _kv_call(["incr", redis_key])
    if status != 200:
        snippet = body.decode("utf-8", "replace")[:160].strip()
        return {"count": None, "error": f"Counter store returned HTTP {status}: {snippet}"}
    try:
        data = json.loads(body)
        new_val = int(data.get("result"))
    except Exception:
        return {"count": None, "error": "Counter store returned a non-JSON response"}
    if new_val == 1:
        # First tap of the month for this key — set it to expire in ~40
        # days so old months' keys don't pile up in the store forever.
        # Not critical if this silently fails, so it isn't surfaced as an
        # error either way.
        try:
            _kv_call(["expire", redis_key, "3456000"])
        except Exception:
            pass
    return {"count": new_val}


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        common.send_options(self)

    def do_GET(self):
        qs = common.parse_query(self)
        provider = (qs.get("provider") or [""])[0]
        key = (qs.get("key") or [""])[0].strip()

        if provider == "twitterapi":
            common.send_json(self, 200, _twitterapi_usage())
        elif provider == "brightdata":
            common.send_json(self, 200, _brightdata_usage())
        elif key:
            action = (qs.get("action") or ["get"])[0]
            if action == "bump":
                common.send_json(self, 200, _bump_count(key))
            else:
                common.send_json(self, 200, _get_count(key))
        else:
            common.send_json(self, 400, {
                "error": "pass ?provider=twitterapi|brightdata for a balance check, "
                         "or ?key=<name>&action=get|bump for the tap counter"
            })
