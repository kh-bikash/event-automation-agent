# Operations runbook

## Deploy

1. Copy `.env.example` to `.env`. Generate separate random `WEBHOOK_SECRET` and `ADMIN_TOKEN` values. Set `WEBHOOK_FORWARD_URL` only to an HTTPS endpoint you own.
2. Run `docker compose up --build -d`. The included compose file binds localhost port 8080 and stores SQLite data on the `agent_data` volume.
3. Put a TLS reverse proxy in front of the service. Restrict `/metrics`, `/jobs/*`, and `/dead/requeue` to an operator network in addition to their token check.
4. Confirm `GET /healthz` returns 200 and submit a signed test event with a unique ID.

## Monitor

Poll authenticated `GET /metrics` for `queued`, `running`, `completed`, and `dead` counts. Alert if `dead` grows or `queued` does not drain. Each HTTP request and worker failure is also logged. Rotate the webhook secret with a coordinated sender cutover; the current version accepts one active secret.

## Replay a dead letter

Inspect the downstream system first. The worker uses at least once delivery, so the external effect may already have happened. Repair the downstream issue, confirm it deduplicates the event ID, then call `POST /dead/requeue` with `{"event_id":"..."}` and `X-Admin-Token`. `GET /jobs/{id}` shows the resulting status and checkpoint.

## Back up and recover

Stop writes or use SQLite's online backup API to copy the database consistently. Back up the Docker volume regularly, test restoration in an isolated environment, and retain the webhook secret in a secret manager. On restore, running jobs whose leases expire are reclaimed. The same external idempotency key must remain valid during replay.

## Scale limit

This deployment profile is one worker process with SQLite. Before running multiple replicas, move the queue to a shared transactional database or broker, validate lease timing, and load test the registered handlers.
