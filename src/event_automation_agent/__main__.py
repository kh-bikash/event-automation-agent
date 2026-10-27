from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

from .engine import EventAutomation, Workflow
from .ui import PAGE

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    secret = os.environ.get("WEBHOOK_SECRET", "").encode()
    if len(secret) < 16 or secret.startswith(b"replace-"):
        raise SystemExit("Set a non-example WEBHOOK_SECRET of at least 16 bytes")
    admin_token = os.environ.get("ADMIN_TOKEN", "")
    if admin_token and (len(admin_token) < 16 or admin_token.startswith("replace-")):
        raise SystemExit("Set a non-example ADMIN_TOKEN of at least 16 characters")

    def log_event(payload: dict) -> None:
        logger.info("processed event keys=%s", sorted(payload))

    workflows = {"log": log_event}
    forward_url = os.environ.get("WEBHOOK_FORWARD_URL")
    if forward_url:
        if not forward_url.startswith("https://"):
            raise SystemExit("WEBHOOK_FORWARD_URL must use HTTPS")

        def forward_event(payload: dict) -> None:
            headers = {
                "Content-Type": "application/json",
                "Idempotency-Key": payload["_event_id"],
            }
            bearer = os.environ.get("WEBHOOK_FORWARD_TOKEN")
            if bearer:
                headers["Authorization"] = "Bearer " + bearer
            request = Request(
                forward_url,
                data=json.dumps(payload).encode(),
                headers=headers,
                method="POST",
            )
            with urlopen(request, timeout=15) as response:
                if response.status >= 300:
                    raise RuntimeError(
                        "forward failed with HTTP " + str(response.status)
                    )

        workflows["forward"] = Workflow((log_event, forward_event))
    automation = EventAutomation(
        os.environ.get("APP_DB", "events.sqlite3"), secret, workflows
    )
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            if not automation.process_one():
                stop.wait(0.5)

    threading.Thread(target=worker, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def admin_authorized(self) -> bool:
            return bool(admin_token) and hmac.compare_digest(
                self.headers.get("X-Admin-Token", ""), admin_token
            )

        def do_GET(self):
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(PAGE)))
                self.end_headers()
                self.wfile.write(PAGE)
            elif self.path == "/healthz":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            elif self.path == "/metrics" and self.admin_authorized():
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(automation.counts()).encode())
            elif self.path.startswith("/jobs/") and self.admin_authorized():
                job = automation.job(self.path.removeprefix("/jobs/"))
                if job is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {"status": job[0], "step_index": job[1], "attempts": job[2]}
                    ).encode()
                )
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path == "/ui/submit":
                if not self.admin_authorized():
                    self.send_error(401)
                    return
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1_000_000:
                    self.send_error(413)
                    return
                try:
                    data = json.loads(self.rfile.read(size))
                    event_id, workflow, payload = (
                        data["event_id"],
                        data["workflow"],
                        data["payload"],
                    )
                    if not isinstance(event_id, str) or not isinstance(payload, dict):
                        raise TypeError("invalid event")
                    body = json.dumps(payload, separators=(",", ":")).encode()
                    timestamp = int(time.time())
                    signed = (
                        str(timestamp).encode() + b"." + event_id.encode() + b"." + body
                    )
                    signature = hmac.new(secret, signed, hashlib.sha256).hexdigest()
                    created = automation.submit(
                        event_id, workflow, body, signature, timestamp
                    )
                except (ValueError, KeyError, TypeError, PermissionError):
                    self.send_error(400)
                    return
                self.send_response(202 if created else 200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps({"accepted": created, "event_id": event_id}).encode()
                )
                return
            if self.path == "/dead/requeue":
                if not self.admin_authorized():
                    self.send_error(401)
                    return
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1000:
                    self.send_error(413)
                    return
                try:
                    event_id = str(json.loads(self.rfile.read(size))["event_id"])
                except (ValueError, KeyError, TypeError):
                    self.send_error(400)
                    return
                requeued = automation.requeue_dead(event_id)
                self.send_response(200 if requeued else 404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"requeued": requeued}).encode())
                return
            workflow = self.path.removeprefix("/webhooks/")
            if not self.path.startswith("/webhooks/") or workflow not in workflows:
                self.send_error(404)
                return
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1_000_000:
                self.send_error(413)
                return
            try:
                created = automation.submit(
                    self.headers.get("X-Event-ID", ""),
                    workflow,
                    self.rfile.read(size),
                    self.headers.get("X-Signature-256", ""),
                    int(self.headers.get("X-Timestamp", "0")),
                )
            except PermissionError:
                self.send_error(401)
                return
            except (ValueError, TypeError, json.JSONDecodeError):
                self.send_error(400)
                return
            self.send_response(202 if created else 200)
            self.end_headers()
            self.wfile.write(json.dumps({"accepted": created}).encode())

    server = ThreadingHTTPServer(
        (os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8080"))),
        Handler,
    )
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
