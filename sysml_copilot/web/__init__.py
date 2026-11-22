"""Local web workbench (`mbse serve`): model browser, diagrams, properties.

Standard library only. The browser talks to `POST /api/call` with
`{"op": ..., "args": {...}}`, i.e. exactly the operations the MCP server
exposes, so the UI and agents always see and do the same things.
Binds to 127.0.0.1 by default: there is no authentication.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..operations import ALL_OPERATIONS, ModelService, call

_HERE = os.path.dirname(__file__)
_MAX_BODY = 5 * 1024 * 1024


def make_handler(service: ModelService):
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "mbse"

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        def _send(self, status, body: bytes, ctype):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status, data):
            self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                with open(os.path.join(_HERE, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif self.path == "/api/ops":
                self._json(200, {"operations": list(ALL_OPERATIONS)})
            else:
                self._json(404, {"ok": False, "error": {"code": "not_found", "message": self.path}})

        def do_POST(self):
            if self.path != "/api/call":
                return self._json(404, {"ok": False, "error": {"code": "not_found", "message": self.path}})
            length = int(self.headers.get("Content-Length") or 0)
            if length > _MAX_BODY:
                return self._json(413, {"ok": False, "error": {"code": "too_large", "message": "body too large"}})
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                op, args = payload.get("op"), payload.get("args") or {}
            except (json.JSONDecodeError, AttributeError):
                return self._json(400, {"ok": False, "error": {"code": "bad_request", "message": "invalid JSON"}})
            with lock:
                result = call(service, op, args)
            self._json(200, result)

    return Handler


def serve(paths, host="127.0.0.1", port=8765, autosave=True):
    service = ModelService(paths, autosave=autosave)
    httpd = ThreadingHTTPServer((host, port), make_handler(service))
    print(f"MBSE workbench on http://{host}:{port}  (model: {', '.join(paths)}) - Ctrl+C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
