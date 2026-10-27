from __future__ import annotations

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import EventAutomation


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    secret = os.environ.get("WEBHOOK_SECRET", "").encode()
    if len(secret) < 16:
        raise SystemExit("Set WEBHOOK_SECRET to at least 16 bytes")

    def log_event(payload: dict) -> None:
        logging.info("processed event keys=%s", sorted(payload))

    automation = EventAutomation(os.environ.get("APP_DB", "events.sqlite3"), secret, {"log": log_event})
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            if not automation.process_one():
                stop.wait(0.5)

    threading.Thread(target=worker, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/healthz":
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def do_POST(self):
            if self.path != "/webhooks/log":
                self.send_error(404)
                return
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1_000_000:
                self.send_error(413)
                return
            try:
                created = automation.submit(self.headers.get("X-Event-ID", ""), "log", self.rfile.read(size), self.headers.get("X-Signature-256", ""), int(self.headers.get("X-Timestamp", "0")))
            except PermissionError:
                self.send_error(401)
                return
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            self.send_response(202 if created else 200)
            self.end_headers()
            self.wfile.write(json.dumps({"accepted": created}).encode())

    server = ThreadingHTTPServer((os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8080"))), Handler)
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
