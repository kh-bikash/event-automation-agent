import json
import os
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

import pytest

from event_automation_agent.cli import main


def test_cli_and_browser_against_running_service(tmp_path, monkeypatch, capsys):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    secret = "a-long-webhook-secret"
    admin = "a-long-admin-token"
    env = os.environ.copy()
    env.update(
        {
            "WEBHOOK_SECRET": secret,
            "ADMIN_TOKEN": admin,
            "APP_DB": str(tmp_path / "events.sqlite3"),
            "HOST": "127.0.0.1",
            "PORT": str(port),
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "event_automation_agent"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(50):
            try:
                with urlopen(base + "/healthz", timeout=1) as response:
                    if response.status == 200:
                        break
            except URLError:
                time.sleep(0.1)
        else:
            raise AssertionError("service did not start")
        with urlopen(base + "/") as response:
            assert b"Event Automation" in response.read()
        monkeypatch.setenv("WEBHOOK_SECRET", secret)
        monkeypatch.setenv("ADMIN_TOKEN", admin)
        assert (
            main(["--url", base, "send", "log", '{"kind":"test"}', "--event-id", "e1"])
            == 0
        )
        assert json.loads(capsys.readouterr().out)["event_id"] == "e1"
        assert main(["--url", base, "job", "e1"]) == 0
        assert json.loads(capsys.readouterr().out)["status"] in {
            "queued",
            "running",
            "completed",
        }
        ui_submit = Request(
            base + "/ui/submit",
            data=json.dumps(
                {"event_id": "e2", "workflow": "log", "payload": {"kind": "browser"}}
            ).encode(),
            headers={"Content-Type": "application/json", "X-Admin-Token": admin},
            method="POST",
        )
        with urlopen(ui_submit) as response:
            assert json.load(response)["accepted"] is True
        assert main(["--url", base, "metrics"]) == 0
        assert sum(json.loads(capsys.readouterr().out).values()) == 2
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_service_rejects_example_webhook_secret(monkeypatch):
    from event_automation_agent.__main__ import main as serve_main

    monkeypatch.setenv("WEBHOOK_SECRET", "replace-with-a-long-random-secret")
    with pytest.raises(SystemExit, match="non-example"):
        serve_main()
