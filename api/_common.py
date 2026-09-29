"""
Shared logic for the cloud version of the Binance/OKX proxy.

This is the same signing/aggregation logic as the local binance-okx-proxy.py
script, ported to run as Vercel serverless functions instead of a script on
your own machine. The big difference: your API keys live in this project's
Vercel Environment Variables (encrypted at rest, never in this repo's code
or in the browser) instead of a local proxy-config.json file, and every
request must carry a shared access token so a stranger who finds your
Vercel URL can't call your exchange keys.

Vercel ignores any file in /api that starts with an underscore, so this
file is never turned into a route of its own — it's only ever imported by
the actual route files sitting next to it.
"""

import json
import os
import time
import hmac
import hashlib
import base64
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone


def env(name, default=""):
    return os.environ.get(name, default) or default


def binance_configured():
    return bool(env("BINANCE_API_KEY") and env("BINANCE_API_SECRET"))


def okx_configured():
    return bool(env("OKX_API_KEY") and env("OKX_API_SECRET") and env("OKX_PASSPHRASE"))


def access_token_configured():
    return bool(env("PROXY_ACCESS_TOKEN"))


def check_auth(handler):
    """Returns True if the request is authorized. A missing
    PROXY_ACCESS_TOKEN env var fails closed (nothing works) rather than
    open, so a forgotten setup step can't accidentally leave this public."""
    expected = env("PROXY_ACCESS_TOKEN")
    if not expected:
        return False
    got = handler.headers.get("X-Auth-Token", "")
    return hmac.compare_digest(got, expected)


BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def http_get(url, headers=None):
    merged = dict(BROWSER_HEADERS)
    merged.update(headers or {})
    req = urllib.request.Request(url, headers=merged)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 599, json.dumps({"error": str(e)}).encode()


def binance_time_offset():
    """Cold starts mean this cache rarely helps much here (unlike the
    long-running local proxy), but it's harmless and saves a round trip
    on warm invocations."""
    try:
        status, body = http_get("https://api.binance.com/api/v3/time")
        data = json.loads(body)
        server_time = data.get("serverTime")
        if server_time:
            return server_time - int(time.time() * 1000)
    except Exception:
        pass
    return 0


def binance_signed_get(path, extra_params=None):
    params = dict(extra_params or {})
    params["timestamp"] = int(time.time() * 1000) + binance_time_offset()
    params.setdefault("recvWindow", 5000)
    query = urllib.parse.urlencode(params)
    signature = hmac.new(env("BINANCE_API_SECRET").encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"https://api.binance.com{path}?{query}&signature={signature}"
    return http_get(url, headers={"X-MBX-APIKEY": env("BINANCE_API_KEY")})


def binance_public_get(path):
    return http_get(f"https://api.binance.com{path}")


def okx_signed_get(path):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    prehash = ts + "GET" + path
    sign = base64.b64encode(
        hmac.new(env("OKX_API_SECRET").encode(), prehash.encode(), hashlib.sha256).digest()
    ).decode()
    headers = {
        "OK-ACCESS-KEY": env("OKX_API_KEY"),
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": env("OKX_PASSPHRASE"),
        "Content-Type": "application/json",
    }
    return http_get(f"https://www.okx.com{path}", headers=headers)


# ---------------------------------------------------------------------------
# Entry-price (cost basis) estimation from trade history — identical logic
# to the local proxy's version.
# ---------------------------------------------------------------------------

STABLE_ASSETS = {"USDT", "USDC", "FDUSD", "BUSD", "DAI", "TUSD", "USD"}
FIAT_ASSETS = {"AED", "PKR", "EUR", "GBP", "INR", "SAR", "EGP", "TRY", "JPY", "CNY", "AUD", "CAD"}
BINANCE_QUOTE_CANDIDATES = ["USDT", "FDUSD", "USDC", "BUSD", "BTC", "BNB", "ETH"]
OKX_QUOTE_CANDIDATES = ["USDT", "USDC", "BTC", "ETH"]


def _usd_rate_for_quote(quote):
    if quote in ("USDT", "USDC", "FDUSD", "BUSD", "DAI", "TUSD", "USD"):
        return 1.0
    status, body = binance_public_get(f"/api/v3/ticker/price?symbol={quote}USDT")
    try:
        data = json.loads(body)
        rate = float(data.get("price", 0))
        return rate if rate > 0 else None
    except Exception:
        return None


def binance_my_trades(symbol):
    status, body = binance_signed_get("/api/v3/myTrades", {"symbol": symbol, "limit": 1000})
    if status != 200:
        try:
            err = json.loads(body)
            msg = err.get("msg") or str(err)
        except Exception:
            msg = f"HTTP {status}"
        return None, f"{symbol}: {msg}"
    try:
        data = json.loads(body)
    except Exception:
        return None, f"{symbol}: invalid response from Binance"
    if not isinstance(data, list):
        return None, f"{symbol}: unexpected response shape"
    if not data:
        return None, f"{symbol}: no trade history on this account"
    return data, None


def binance_entry_price(asset):
    asset = asset.upper()
    if asset in STABLE_ASSETS:
        return {"entryPrice": 1, "currency": "USD", "quote": "USD", "fills": 0, "note": "stablecoin"}
    if asset in FIAT_ASSETS:
        return {"entryPrice": None, "reason": f"{asset} is a fiat currency balance, not a trade — "
                                               f"there's no 'entry price' to look up for it.", "attempts": []}
    attempts = []
    for quote in BINANCE_QUOTE_CANDIDATES:
        if quote == asset:
            continue
        symbol = f"{asset}{quote}"
        trades, diag = binance_my_trades(symbol)
        if trades is None:
            attempts.append(diag)
            continue
        buys = [t for t in trades if t.get("isBuyer")]
        if not buys:
            attempts.append(f"{symbol}: {len(trades)} trade(s) found but all were sells, not buys")
            continue
        total_qty = sum(float(t["qty"]) for t in buys)
        total_cost = sum(float(t["qty"]) * float(t["price"]) for t in buys)
        if total_qty <= 0:
            attempts.append(f"{symbol}: buy fills summed to zero quantity")
            continue
        avg_price = total_cost / total_qty
        rate = _usd_rate_for_quote(quote)
        if rate is None:
            attempts.append(f"{symbol}: found {len(buys)} buy(s) but couldn't convert {quote} to USD")
            continue
        return {"entryPrice": avg_price * rate, "currency": "USD", "quote": quote, "fills": len(buys)}
    return {
        "entryPrice": None,
        "reason": f"No buy trades found on Binance for {asset} — checked "
                  f"{', '.join(f'{asset}{q}' for q in BINANCE_QUOTE_CANDIDATES if q != asset)}. "
                  f"If you deposited/transferred {asset} into this account rather than buying it "
                  f"here, Binance has no trade record of your cost, so it can't be auto-filled.",
        "attempts": attempts,
    }


def okx_fills(inst_id):
    status, body = okx_signed_get(f"/api/v5/trade/fills?instType=SPOT&instId={inst_id}&limit=100")
    try:
        data = json.loads(body)
    except Exception:
        return None, f"{inst_id}: invalid response from OKX (status {status})"
    if data.get("code") != "0":
        return None, f"{inst_id}: {data.get('msg') or ('code ' + str(data.get('code')))}"
    rows = data.get("data", [])
    if not rows:
        return None, f"{inst_id}: no fills in the last ~3 months"
    return rows, None


def okx_entry_price(asset):
    asset = asset.upper()
    if asset in STABLE_ASSETS:
        return {"entryPrice": 1, "currency": "USD", "quote": "USD", "fills": 0, "note": "stablecoin"}
    if asset in FIAT_ASSETS:
        return {"entryPrice": None, "reason": f"{asset} is a fiat currency balance, not a trade — "
                                               f"there's no 'entry price' to look up for it.", "attempts": []}
    attempts = []
    for quote in OKX_QUOTE_CANDIDATES:
        if quote == asset:
            continue
        inst_id = f"{asset}-{quote}"
        fills, diag = okx_fills(inst_id)
        if fills is None:
            attempts.append(diag)
            continue
        buys = [f for f in fills if f.get("side") == "buy"]
        if not buys:
            attempts.append(f"{inst_id}: {len(fills)} fill(s) found but all were sells, not buys")
            continue
        total_qty = sum(float(f["fillSz"]) for f in buys)
        total_cost = sum(float(f["fillSz"]) * float(f["fillPx"]) for f in buys)
        if total_qty <= 0:
            attempts.append(f"{inst_id}: buy fills summed to zero quantity")
            continue
        avg_price = total_cost / total_qty
        rate = _usd_rate_for_quote(quote)
        if rate is None:
            attempts.append(f"{inst_id}: found {len(buys)} buy(s) but couldn't convert {quote} to USD")
            continue
        return {"entryPrice": avg_price * rate, "currency": "USD", "quote": quote, "fills": len(buys)}
    return {
        "entryPrice": None,
        "reason": f"No buy fills found on OKX for {asset} — checked "
                  f"{', '.join(f'{asset}-{q}' for q in OKX_QUOTE_CANDIDATES if q != asset)} "
                  f"(OKX only returns roughly the last 3 months of fills here). If you deposited/"
                  f"transferred {asset} into this account rather than buying it here, OKX has no "
                  f"trade record of your cost, so it can't be auto-filled.",
        "attempts": attempts,
    }


# ---------------------------------------------------------------------------
# HTTP response helpers shared by every route file
# ---------------------------------------------------------------------------

def send_json(handler, status, payload):
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "*")
    handler.send_header("Access-Control-Allow-Private-Network", "true")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def send_error(handler, status, message):
    send_json(handler, status, {"error": message})


def send_options(handler):
    handler.send_response(204)
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "*")
    handler.send_header("Access-Control-Allow-Private-Network", "true")
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def forward(handler, status, body, exchange):
    try:
        json.loads(body)
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        handler.send_header("Access-Control-Allow-Headers", "*")
        handler.send_header("Access-Control-Allow-Private-Network", "true")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    except Exception:
        snippet = body.decode("utf-8", "replace")[:200].strip()
        send_error(handler, 502, f"{exchange} returned a non-JSON response (likely blocked the "
                                  f"request rather than an API error) — status {status}: {snippet}")


def require_auth_or_401(handler):
    """Call at the top of every route's do_GET. Returns True if the caller
    should
