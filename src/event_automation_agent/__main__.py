from __future__ import annotations

import hmac
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

from .engine import EventAutomation, Workflow

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    secret = os.environ.get("WEBHOOK_SECRET", "").encode()
    if len(secret) < 16:
        raise SystemExit("Set WEBHOOK_SECRET to at least 16 bytes")
    admin_token = os.environ.get("ADMIN_TOKEN", "")
    if admin_token and len(admin_token) < 16:
        raise SystemExit("ADMIN_TOKEN must be at least 16 characters")

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
            if self.path == "/healthz":
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
