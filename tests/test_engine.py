import hashlib
import hmac
import time

import pytest

from event_automation_agent.engine import EventAutomation, Workflow


def test_signature_idempotency_and_dead_letter(tmp_path):
    secret = b"a very long secret key"
    calls = []

    def broken(payload):
        calls.append(payload)
        raise RuntimeError("downstream unavailable")

    agent = EventAutomation(
        tmp_path / "events.db", secret, {"sync": broken}, max_attempts=2
    )
    body = b'{"id":1}'
    timestamp = int(time.time())
    signature = hmac.new(
        secret, str(timestamp).encode() + b".e1." + body, hashlib.sha256
    ).hexdigest()
    with pytest.raises(PermissionError):
        agent.submit("e1", "sync", body, "wrong", timestamp)
    with pytest.raises(PermissionError):
        agent.submit("e1", "sync", body, signature, timestamp - 301)
    assert agent.submit("e1", "sync", body, signature, timestamp)
    assert not agent.submit("e1", "sync", body, signature, timestamp)
    assert agent.process_one(now=10**12)
    assert agent.process_one(now=10**12 + 3)
    assert agent.counts()["dead"] == 1 and len(calls) == 2
    assert agent.requeue_dead("e1")


def test_workflow_checkpoints_each_step(tmp_path):
    secret = b"another long secret"
    calls = []
    workflow = Workflow(
        (lambda _: calls.append("first"), lambda _: calls.append("second"))
    )
    agent = EventAutomation(tmp_path / "events.db", secret, {"flow": workflow})
    body = b"{}"
    timestamp = int(time.time())
    signature = hmac.new(
        secret, str(timestamp).encode() + b".e2." + body, hashlib.sha256
    ).hexdigest()
    agent.submit("e2", "flow", body, signature, timestamp)
    assert agent.process_one() and agent.job("e2")[:2] == ("queued", 1)
    assert agent.process_one() and agent.job("e2")[:2] == ("completed", 2)
    assert calls == ["first", "second"]
