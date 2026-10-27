from __future__ import annotations

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

from .engine import EventAutomation, Workflow


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    secret = os.environ.get("WEBHOOK_SECRET", "").encode()
    if len(secret) < 16:
        raise SystemExit("Set WEBHOOK_SECRET to at least 16 bytes")

    def log_event(payload: dict) -> None:
        logging.info("processed event keys=%s", sorted(payload))

    workflows = {"log": log_event}
    forward_url = os.environ.get("WEBHOOK_FORWARD_URL")
    if forward_url:
        if not forward_url.startswith("https://"):
            raise SystemExit("WEBHOOK_FORWARD_URL must use HTTPS")
        def forward_event(payload: dict) -> None:
            headers = {"Content-Type": "application/json", "Idempotency-Key": payload["_event_id"]}
            bearer = os.environ.get("WEBHOOK_FORWARD_TOKEN")
            if bearer:
                headers["Authorization"] = "Bearer " + bearer
            request = Request(forward_url, data=json.dumps(payload).encode(), headers=headers, method="POST")
            with urlopen(request, timeout=15) as response:
                if response.status >= 300:
                    raise RuntimeError("forward failed with HTTP " + str(response.status))
        workflows["forward"] = Workflow((log_event, forward_event))
    automation = EventAutomation(os.environ.get("APP_DB", "events.sqlite3"), secret, workflows)
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            if not automation.process_one():
                stop.wait(0.5)

    threading.Thread(target=worker, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/healthz":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            elif self.path == "/metrics" and self.headers.get("X-Admin-Token") == os.environ.get("ADMIN_TOKEN", "") and os.environ.get("ADMIN_TOKEN"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(automation.counts()).encode())
            else:
                self.send_error(404)

        def do_POST(self):
            workflow = self.path.removeprefix("/webhooks/")
            if not self.path.startswith("/webhooks/") or workflow not in workflows:
                self.send_error(404)
                return
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1_000_000:
                self.send_error(413)
                return
            try:
                created = automation.submit(self.headers.get("X-Event-ID", ""), workflow, self.rfile.read(size), self.headers.get("X-Signature-256", ""), int(self.headers.get("X-Timestamp", "0")))
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
