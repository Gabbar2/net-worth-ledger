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
        status, body = common.okx_signed_get("/api/v5/account/balance")
        common.forward(self, status, body, "OKX")
