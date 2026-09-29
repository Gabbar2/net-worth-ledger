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
        if not common.binance_configured():
            return common.send_error(self, 400, "BINANCE_API_KEY / BINANCE_API_SECRET aren't set "
                                                  "in this Vercel project's environment variables.")
        status, body = common.binance_signed_get("/api/v3/account")
        common.forward(self, status, body, "Binance")
