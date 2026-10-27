"""Operator and webhook sender CLI for the event automation service."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time
import uuid
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


def _base_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("service URL must be HTTPS or loopback")
    return url.rstrip("/")


def _call(
    url: str, *, body: bytes | None = None, headers: dict | None = None
) -> object:
    request = Request(
        url,
        data=body,
        headers=headers or {},
        method="POST" if body is not None else "GET",
    )
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Event automation service client")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="Start the service")
    send = commands.add_parser("send", help="Submit a signed JSON webhook")
    send.add_argument("workflow", choices=["log", "forward"])
    send.add_argument("payload", help="JSON object")
    send.add_argument("--event-id")
    commands.add_parser("metrics")
    job = commands.add_parser("job")
    job.add_argument("event_id")
    requeue = commands.add_parser("requeue")
    requeue.add_argument("event_id")
    args = parser.parse_args(argv)
    if args.command == "serve":
        from .__main__ import main as serve_main

        serve_main()
        return 0
    base = _base_url(args.url)
    if args.command == "send":
        secret = os.environ.get("WEBHOOK_SECRET", "").encode()
        if len(secret) < 16:
            raise RuntimeError("Set WEBHOOK_SECRET to at least 16 bytes")
        payload = json.loads(args.payload)
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        event_id = args.event_id or uuid.uuid4().hex
        timestamp = int(time.time())
        body = json.dumps(payload, separators=(",", ":")).encode()
        signature = hmac.new(
            secret,
            str(timestamp).encode() + b"." + event_id.encode() + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        result = _call(
            base + "/webhooks/" + args.workflow,
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-Event-ID": event_id,
                "X-Timestamp": str(timestamp),
                "X-Signature-256": signature,
            },
        )
        result["event_id"] = event_id
    else:
        token = os.environ.get("ADMIN_TOKEN", "")
        if not token:
            raise RuntimeError("Set ADMIN_TOKEN for operator commands")
        headers = {"X-Admin-Token": token, "Content-Type": "application/json"}
        if args.command == "metrics":
            result = _call(base + "/metrics", headers=headers)
        elif args.command == "job":
            result = _call(
                base + "/jobs/" + quote(args.event_id, safe=""), headers=headers
            )
        else:
            result = _call(
                base + "/dead/requeue",
                body=json.dumps({"event_id": args.event_id}).encode(),
                headers=headers,
            )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
