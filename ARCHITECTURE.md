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
3. Mount the SQLite queue on durable storage, back it up, and monitor dead letters. Use a shared queue/database before multiple replicas.
4. Run `pytest -q` in CI before deployment.
5. Define workload specific evaluation cases, latency objectives, and rollback criteria before serving real users.

## Ingress, queue, and recovery

The HTTP ingress limits request size and checks a five minute timestamp window. HMAC-SHA256 signs `timestamp.event_id.raw_body`, binding the replay key and body. `jobs.id` is unique, so resubmitting the same event is idempotent. The SQLite worker claims due jobs under `BEGIN IMMEDIATE`, sets a lease, and executes one workflow step. `step_index` checkpoints successful steps. Failed steps use exponential backoff until `max_attempts`, then enter `dead`; `requeue_dead` supports operator replay.

`Workflow` can contain several application handlers. The service registers `log` and, when configured, `forward` to a fixed HTTPS endpoint with optional bearer token. `/metrics` requires `ADMIN_TOKEN`. The delivery model is at least once: a crash after an external side effect but before the checkpoint can replay that step. Handlers must use `event_id` or another stable idempotency key at the downstream boundary. Mount `/data` on durable storage and keep one worker process per SQLite database unless the lease and handler timing are validated for the deployment.

## Product interfaces

`event-agent serve` runs the webhook API and worker. `event-agent send` signs the raw JSON body with the configured webhook secret; `metrics`, `job`, and `requeue` use the separate admin token. The operator browser at `/` can submit an event, inspect queue counts and job status, and requeue a dead letter. Its `/ui/submit` route requires the admin token and signs the event inside the server before entering the same durable queue. The browser page never embeds either secret. Keep operator endpoints behind TLS and network restrictions when exposed beyond loopback.
