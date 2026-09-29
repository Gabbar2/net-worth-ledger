from http.server import BaseHTTPRequestHandler
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
