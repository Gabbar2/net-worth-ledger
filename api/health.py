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
        common.send_json(self, 200, {
            "status": "ok",
            "binance_configured": common.binance_configured(),
            "okx_configured": common.okx_configured(),
            "access_token_configured": common.access_token_configured(),
        })
