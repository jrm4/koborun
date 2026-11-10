#!/usr/bin/env python3
"""Loopback mock of the KoboldCpp HTTP API used by koboldctl's smoke test.

Implements just enough of the real endpoints (same paths and JSON shapes as
koboldcpp.py) to exercise every subcommand: status fields, admin model list,
hot-swap via /api/admin/reload_config with a simulated load delay, and abort.

Run standalone:  python3 tests/mock_koboldcpp.py [port]
"""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ADMIN_PASSWORD = "admin-secret"
PASSWORD = "user-secret"
LOAD_DELAY = 1.5

STATE = {
    "model": "tiny-test-model-Q4_K_M.gguf",
    "pending_target": None,
    "ready_at": 0.0,
    "aborted": False,
    "gens": 42,
}
LOCK = threading.Lock()

ADMIR_FILES = ["tiny-test-model-Q4_K_M.gguf", "big-test-model-Q8_0.gguf", "chat.kcpps",
               "initial_model", "unload_model"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, admin_only=False):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        token = header[len("Bearer "):].strip()
        if admin_only:
            return token == ADMIN_PASSWORD
        return token in (PASSWORD, ADMIN_PASSWORD)

    def _apply_pending(self):
        with LOCK:
            if STATE["pending_target"] and time.time() >= STATE["ready_at"]:
                target = STATE["pending_target"]
                STATE["pending_target"] = None
                if target == "unload_model":
                    STATE["model"] = "inactive"
                else:
                    STATE["model"] = target

    def do_GET(self):
        self._apply_pending()
        path = self.path.split("?")[0]
        if path == "/api/v1/model":
            if not self._authorized():
                self._send({"result": "koboldcpp/protected-model"})
            else:
                with LOCK:
                    self._send({"result": STATE["model"]})
        elif path == "/api/extra/version":
            self._send({"result": "KoboldCpp", "version": "1.99.0-mock", "protected": True,
                        "llm": STATE["model"] != "inactive", "admin": 2, "router": False,
                        "vision": False, "audio": False})
        elif path == "/api/extra/perf":
            with LOCK:
                busy = STATE["pending_target"] is not None
                self._send({
                    "last_process_speed": 512.3, "last_eval_speed": 42.7,
                    "last_token_count": 12, "last_input_count": 130,
                    "queue": 1 if busy else 0, "idle": 0 if busy else 1,
                    "uptime": 3661, "idletime": 5, "total_gens": STATE["gens"],
                    "stop_reason": "eos_token" if not STATE["aborted"] else "abort",
                })
        elif path == "/api/v1/config/max_context_length":
            self._send({"value": 8192})
        elif path == "/api/v1/config/max_length":
            self._send({"value": 256})
        elif path == "/api/admin/list_options":
            if not self._authorized(admin_only=True):
                self._send([])
            else:
                self._send(ADMIR_FILES)
        elif path == "/v1/models":
            with LOCK:
                self._send({"object": "list", "data": [{"id": STATE["model"], "object": "model"}]})
        else:
            self._send({"detail": "not found"}, code=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            body = {}
        path = self.path.split("?")[0]
        if path == "/api/admin/reload_config":
            if not self._authorized(admin_only=True):
                self._send({"success": False})
                return
            target = body.get("filename", "")
            if target in ADMIR_FILES:
                with LOCK:
                    STATE["pending_target"] = target
                    STATE["ready_at"] = time.time() + LOAD_DELAY
                self._send({"success": True})
            else:
                self._send({"success": False})
        elif path == "/api/extra/abort":
            if not self._authorized():
                self._send({"success": "false", "done": "false"}, code=401)
                return
            with LOCK:
                STATE["aborted"] = True
            self._send({"success": "true", "done": "true"})
        else:
            self._send({"detail": "not found"}, code=404)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 15001
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("mock koboldcpp on http://127.0.0.1:%d" % port, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
