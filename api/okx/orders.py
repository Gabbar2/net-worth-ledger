# Merged "OKX open orders" + "OKX order history" into one file/one
# function (previously two separate files: orders/pending.py and
# orders/history.py). This is purely to free up a serverless-function slot
# -- Vercel's Hobby plan caps a deployment at 12 functions total, and this
# project needed the extra one for the new usage.py (live balance/credit
# checks + the cross-device tap counter). Nothing about what either
# endpoint returns has changed.
#
#   ?type=pending  -> OKX's open/pending orders (same as the old
#                      orders/pending.py)
#   ?type=history  -> OKX's recent order history (same as the old
#                      orders/history.py)
#
# The dashboard's existing calls to /okx/orders/pending and
# /okx/orders/history keep working unchanged -- vercel.json now rewrites
# those two paths to this one file with the right ?type=, so no frontend
# changes were needed for this merge.

from http.server import BaseHTTPRequestHandler
import sys
import os
_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.exists(os.path.join(_p, "_common.py")):
    _parent = os.path.dirname(_p)
    if _parent == _p:
        break
    _p = _parent
sys.path.insert(0, _p)
import _common as common


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        common.send_options(self)

    def do_GET(self):
        if common.require_auth_or_401(self):
            return
        if not common.okx_configured():
            return common.send_error(self, 400, "OKX_API_KEY / OKX_API_SECRET / OKX_PASSPHRASE "
                                                  "aren't set in this Vercel project's environment variables.")
        qs = common.parse_query(self)
        kind = (qs.get("type") or [""])[0]
        if kind == "pending":
            status, body = common.okx_signed_get("/api/v5/trade/orders-pending?instType=SPOT")
        elif kind == "history":
            status, body = common.okx_signed_get("/api/v5/trade/orders-history?instType=SPOT&limit=100")
        else:
            return common.send_error(self, 400, "pass ?type=pending or ?type=history")
        common.forward(self, status, body, "OKX")
