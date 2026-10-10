# "Xpert's Tweets" card -- recent tweets from a hand-picked list of X
# (Twitter) accounts, completely separate from the Instagram/YouTube/blog
# "Expert's card" in experts.py.
#
# ScrapingDog was the first provider tried here, but it turned out to have
# no endpoint that returns a user's recent tweets at all -- only two X
# endpoints exist on it: one that fetches a single tweet's full content
# given its exact URL, and a profile-stats endpoint that returns follower
# counts/bio/etc. but no tweet text, dates, or engagement beyond a
# `pinned_tweet_ids` list. Neither can build a "latest posts" feed. So this
# route uses twitterapi.io's "Get User Last Tweets" endpoint instead:
#   GET https://api.twitterapi.io/twitter/user/last_tweets?userName=<handle>
#
# twitterapi.io is pay-as-you-go (new accounts get a small free trial
# credit, then roughly $0.15 per 1,000 tweets returned -- about 15 credits
# per tweet out of 100,000 credits = $1), not a flat recurring free tier.
# That's why this card's refresh is manual-only, same spirit as the
# Expert's card's Apify-backed Instagram fetch -- nothing here ever runs
# on page load or on opening the card, only when you press the card's own
# ⟳ button.
#
# Secret used here (set as a Vercel Environment Variable, never sent to
# the browser):
#   TWITTERAPI_IO_KEY   -- your twitterapi.io API key
#
# If that var is absent, every request below just returns an empty list
# with a note explaining why, instead of failing outright.

from http.server import BaseHTTPRequestHandler
import sys
import os
import json
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, wait

_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.exists(os.path.join(_p, "_common.py")):
    _parent = os.path.dirname(_p)
    if _parent == _p:
        break
    _p = _parent
sys.path.insert(0, _p)
import _common as common

# ---------------------------------------------------------------------------
# The hand-picked list of X/Twitter handles (no "@", just the handle). Edit
# this to add/remove accounts -- no other code changes needed. Starts
# empty on purpose: unlike the Instagram/YouTube lists in experts.py, I
# don't know which X accounts you actually want tracked here, and I'd
# rather this card tell you plainly to fill the list in than show you
# placeholder/guessed accounts as if they were your picks.
# ---------------------------------------------------------------------------
TWITTER_USERNAMES = [
    "waqarzaka",
]

TWEETS_PER_ACCOUNT = 3
OVERALL_BUDGET_SECONDS = 20


def _twitterapi_configured():
    return bool(common.env("TWITTERAPI_IO_KEY"))


def _parse_created_at(value):
    if not value:
        return None
    # twitterapi.io's format, e.g. "Tue Dec 10 07:00:30 +0000 2024"
    try:
        return int(datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y").timestamp())
    except Exception:
        pass
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _fetch_tweets_for_account(username):
    api_key = common.env("TWITTERAPI_IO_KEY")
    url = ("https://api.twitterapi.io/twitter/user/last_tweets?"
           + urllib.parse.urlencode({"userName": username}))
    headers = dict(common.BROWSER_HEADERS)
    headers["X-API-Key"] = api_key
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status, body = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read()
    except Exception as e:
        return [], f"@{username}: couldn't reach twitterapi.io ({e})"

    if status != 200:
        snippet = body.decode("utf-8", "replace")[:160].strip()
        return [], f"@{username}: twitterapi.io returned HTTP {status}: {snippet}"

    try:
        data = json.loads(body)
    except Exception:
        return [], f"@{username}: twitterapi.io returned a non-JSON response"

    if data.get("status") == "error":
        return [], f"@{username}: {data.get('message') or 'twitterapi.io reported an error'}"

    raw_tweets = data.get("tweets") or []
    # twitterapi.io returned a 200 with status != "error" but an empty
    # `tweets` array -- this is NOT a code bug, it's twitterapi.io telling
    # us (quietly) it found nothing for this handle. The two common causes:
    # the handle is misspelled/doesn't exist, or it does exist but
    # twitterapi.io resolved it to a user with zero tweets visible to it
    # (suspended, protected/private, or brand-new with no posts). Surface
    # this explicitly instead of silently returning nothing, which is what
    # was happening before and made this indistinguishable from "it's
    # working, there's just nothing new."
    if not raw_tweets:
        return [], (f"@{username}: twitterapi.io returned 0 tweets for this handle — double-check "
                     f"the spelling in TWITTER_USERNAMES, and that the account exists, isn't "
                     f"suspended, and isn't set to private.")

    items = []
    dropped = 0
    for t in raw_tweets[:TWEETS_PER_ACCOUNT]:
        if not isinstance(t, dict):
            dropped += 1
            continue
        text = (t.get("text") or "").strip()
        tweet_id = t.get("id") or ""
        url = t.get("url") or (f"https://x.com/{username}/status/{tweet_id}" if tweet_id else None)
        if not url:
            dropped += 1
            continue
        items.append({
            "source_type": "twitter",
            "expert": f"@{username}",
            "title": text[:280] if text else f"New post from @{username}",
            "url": url,
            "thumbnail": None,  # twitterapi.io's last_tweets response documents no media field
            "published_on": _parse_created_at(t.get("createdAt")),
        })

    if not items and dropped:
        return [], (f"@{username}: twitterapi.io returned {dropped} tweet(s) but none had a "
                     f"usable id/url field — twitterapi.io's response shape may have changed; "
                     f"this needs the parsing in tweets.py adjusted.")
    return items, None


def _fetch_all(notes):
    if not TWITTER_USERNAMES:
        notes.append("No X/Twitter accounts configured yet — edit TWITTER_USERNAMES in "
                      "api/tweets.py (no other code changes needed).")
        return []
    if not _twitterapi_configured():
        notes.append("twitterapi.io isn't configured — set TWITTERAPI_IO_KEY in Vercel's "
                      "Environment Variables to turn this card on.")
        return []

    all_items = []
    with ThreadPoolExecutor(max_workers=max(1, len(TWITTER_USERNAMES))) as pool:
        future_to_name = {pool.submit(_fetch_tweets_for_account, u): u for u in TWITTER_USERNAMES}
        done, not_done = wait(future_to_name.keys(), timeout=OVERALL_BUDGET_SECONDS)
        for fut in done:
            name = future_to_name[fut]
            try:
                items, note = fut.result()
                all_items.extend(items)
                if note:
                    notes.append(note)
            except Exception as e:
                notes.append(f"@{name}: {e}")
        for fut in not_done:
            name = future_to_name[fut]
            notes.append(f"@{name}: still running when this request's time budget ran out — "
                         f"try ⟳ again.")
            fut.cancel()
    return all_items


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        common.send_options(self)

    def do_GET(self):
        notes = []
        all_items = _fetch_all(notes)
        all_items.sort(key=lambda i: i["published_on"] or 0, reverse=True)
        common.send_json(self, 200, {"items": all_items[:30], "notes": notes})
