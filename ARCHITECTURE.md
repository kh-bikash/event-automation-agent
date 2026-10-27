# Architecture

## Goal

Accept timestamped HMAC signed webhooks, queue events with idempotency keys, retry failures, and retain dead letters.

## Request path

HTTP webhook -> timestamp and HMAC verification -> SQLite insert or duplicate detection -> worker lease -> registered handler -> completed, backoff retry, or dead letter.

## Components

- `src/event_automation_agent/engine.py`: policy and state transitions for this project.

- `tests/test_engine.py`: deterministic behavior and failure tests.
- `.github/workflows/ci.yml`: install and test gate.
- `src/event_automation_agent/__main__.py`: HTTP service entry point.
- `Dockerfile`: non-root container image.

## State and failure behavior

The built-in `log` workflow is a safe runnable example. Register real workflow handlers in application code. Handlers must be idempotent because a crash or lease expiry can cause at least once delivery. Mount /data on persistent storage in Docker.

The application should propagate failed operations to its caller or queue so the caller can decide whether to retry. Input and handler output are treated as untrusted at the integration boundary.

## Operations checklist

1. Set secrets through a secret manager and rotate them regularly.
2. Put the service or integrating application behind TLS and authentication.
3. Persist SQLite files on durable storage and back them up; migrate to a managed database for high write volume or multiple replicas.
4. Run `pytest -q` in CI before deployment.
5. Define workload specific evaluation cases, latency objectives, and rollback criteria before serving real users.
