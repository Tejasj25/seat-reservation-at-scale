# Seat Reservation at Scale

JSON API using FastAPI and PostgreSQL. A reservation immediately confirms all requested seats or confirms none. Cancellation returns those seats to inventory. All money is integer paise. There is no payment-provider integration; `amount_paise` is the recorded booking amount.

**Current delivery status:** [public repository](https://github.com/Tejasj25/seat-reservation-at-scale) created with incremental history. Both regression tests passed. The final local burst passed 20,000 requests at concurrency 200: one confirmation, 19,999 seat-taken declines, zero 5xx/transport errors, and 109 valid snapshots in 219.38 seconds. Database-outage and recovery checks passed. [GitHub Actions](https://github.com/Tejasj25/seat-reservation-at-scale/actions/runs/37107593313) also passed a clean-checkout Docker build and the full regression/outage checks. See [deployment status](DEPLOYMENT.md). The public service still requires Render sign-in and deployment; no live URL is claimed.

## Run from a clean checkout

Requires Docker with Compose v2.

```sh
cp .env.example .env
docker compose up --build -d --wait
curl http://localhost:8000/health/ready
```

On PowerShell use `Copy-Item .env.example .env` for the first command. Local API: <http://localhost:8000>. Interactive API documentation: <http://localhost:8000/docs>.

Compose persists PostgreSQL in a named volume. `docker compose down` preserves data. The example credentials are for local development; production uses independently generated secrets. Startup waits for PostgreSQL and applies the initial idempotent schema under a migration lock. The API runs as an unprivileged container user.

## Authentication and API

Admin requests use `Authorization: Bearer <ADMIN_TOKEN>`. Admins can mint user tokens through `POST /auth/tokens` with `{"user_id":"alice"}`; the response contains `access_token`. User tokens expire after 24 hours. Mint a different user token for each test identity. The server verifies JWT signature, issuer, audience and expiration. The signing key stays on the server.

```sh
# ADMIN_TOKEN must match .env or the deployment environment.
export ADMIN_TOKEN=local-admin-token-change-me-32-characters
curl -s http://localhost:8000/auth/tokens \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"user_id":"alice"}'

curl -s http://localhost:8000/shows \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"friday-night","seats":["A1","A2","A3"],"price_paise":25000,"per_user_limit":4}'

curl -s http://localhost:8000/shows/SHOW_ID/reserve \
  -H 'Authorization: Bearer USER_TOKEN' -H 'Content-Type: application/json' \
  -d '{"seats":["A1"],"idempotency_key":"alice-order-1"}'

curl -s http://localhost:8000/shows/SHOW_ID
curl -s -X POST http://localhost:8000/reservations/RESERVATION_ID/cancel \
  -H 'Authorization: Bearer USER_TOKEN'
```

| Route | Authentication | Behavior |
|---|---|---|
| `POST /auth/tokens` | Admin | Issue a signed user token |
| `POST /shows` | Admin | 201, show and all seats; default limit 4 |
| `POST /shows/{id}/reserve` | User | 201 for new reservation, 200 for replay |
| `POST /reservations/{id}/cancel` | Owner | 200, repeatable cancellation |
| `GET /shows/{id}` | Public | Per-seat states and consistent counts |
| `GET /health/live` | Public | Process liveness |
| `GET /health/ready` | Public | Checks PostgreSQL and schema; 503 if unavailable |
| `GET /metrics` | Public | Prometheus exposition from committed database state |

Reserve accepts `Idempotency-Key` as an alternative to the body key. If both are supplied they must match. Keys are scoped to the authenticated user across all shows. Seat order is normalized, so `["A1","A2"]` and `["A2","A1"]` are the same request. Duplicate labels are invalid (422). For a valid show, a key is permanently bound to its first normalized seat request, including domain declines. Using it for different seats or another show returns 409. A declined request can retry the same seats after conditions change; a successful key always returns its original reservation.

The request body's `user_id`, if supplied, is ignored. Identity always comes from the verified token. Only admins can mint tokens; the admin credential must not be shared with ordinary buyers.

Conflicts return `{"error":"seat_taken"}`, `per_user_limit`, or `idempotency_conflict` with 409. Unknown seat labels return `unknown_seat` with 404. A nonexistent show/reservation returns 404. If multiple reasons apply, validation/idempotency is checked first, then the user limit, then seats. A normal race never uses exceptions to represent a losing request.

Cancellation is explicit; there are no expiring holds. Show `held` is always zero. Cancelling the same reservation again does nothing, including after another user rebooks its seats. A replay after cancellation returns the original reservation ID with its current `cancelled` status, never a new booking. Rebooking requires a new key.

## One-command live burst

Install Python 3.11+ dependencies once:

```sh
python -m pip install -r requirements.lock
export ADMIN_TOKEN=YOUR_DEPLOYMENT_ADMIN_TOKEN
python scripts/burst.py https://YOUR-LIVE-URL --requests 20000 --concurrency 500
```

PowerShell environment variable syntax: `$env:ADMIN_TOKEN = 'YOUR_DEPLOYMENT_ADMIN_TOKEN'`.

The command first checks concurrent per-user limits, retries, conflicting keys, identity spoofing, owner-only cancellation, cancellation replay, rebooking, and reversed-order multi-seat races. Then it mints 20,000 distinct user tokens and sends 20,000 hot-seat requests with up to 500 in flight. Token setup is excluded from burst timing. Set `--concurrency 20000` to attempt 20,000 simultaneously; this also requires sufficient client sockets, proxy capacity, and server resources. A bounded run is not evidence of sustaining 20,000 simultaneous connections.

It prints outcome distribution (including unexpected statuses, transport errors and 5xx), p50/p99 request latency, reconciliation counts, and the number of snapshots checked during the storm. Any observation error also fails the run. Worker connections are independent and share a TLS context to avoid client-pool contention and repeated certificate initialization. It fails unless exactly one hot-seat request returns 201, every other one returns `seat_taken`, metrics match final inventory, and every sampled snapshot reconciles. Add `--output result.json` to save a successful report. Tests create new shows and retain their data for inspection.

Run the shorter real-database suite with `python -m pytest -q` against the running Compose stack, with `ADMIN_TOKEN` set. CI builds the container, runs this suite, stops PostgreSQL to verify readiness fails while liveness remains healthy, then checks recovery. CI uploads structured logs as an artifact.

## Deployment

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Tejasj25/seat-reservation-at-scale)

`render.yaml` is a Render Blueprint defining a Docker web service, PostgreSQL, generated admin/signing secrets, and `/health/ready` health checks. See [Render's Blueprint reference](https://render.com/docs/blueprint-spec) for the deployment configuration format.

1. Open the Deploy to Render link above and sign in to your hosting account.
2. Review and apply the services described in `render.yaml`.
3. Copy `ADMIN_TOKEN` from the web service's environment into your local shell. Do not commit it.
4. Verify `/health/ready`, run the burst against the assigned public service URL, and retain its JSON output.
5. Record the actual repository URL, live URL, load evidence and log recording links in `DEPLOYMENT.md`.

The free plans are a starting configuration, not a capacity guarantee. Inspect current provider limits and database retention before submission. Keep the service and database active through evaluation; benchmark the actual deployed instance, including a cold start. If the provider cannot sustain the target burst, provision appropriate resources and rerun before claiming readiness.

## Metrics and logs

`GET /metrics` is public, and all inventory gauges come from a single repeatable-read PostgreSQL snapshot:

- `reservations_confirmed_total`: lifetime committed reservations (includes subsequently cancelled ones).
- `reservations_cancelled_total`: lifetime cancelled reservations.
- `reservations_declined_total{reason="..."}`: `seat_taken`, `per_user_limit`, `idempotency_conflict`, `unknown_seat`, and `idempotent_replay`.
- `seats_available`, `seats_confirmed`, `seats_held`, `seats_total`, each labeled by `show_id`.

Replay is a successful HTTP 200, but appears under the assignment's requested `idempotent_replay` outcome label. Counters count reservations, while gauges count seats; a two-seat reservation increments the confirmation counter once. Cancellation reduces the confirmed seat gauge but never decreases the lifetime confirmation counter. Metrics survive process restarts and work across multiple application instances. Scrape one service target for these shared database totals; summing replicas would double-count them.

API responses carry `X-Request-ID`. Each completed request logs one JSON event containing request ID, method, route template, status and duration. Supplied request IDs are restricted to safe characters and 64 characters. Tokens and bodies are not logged. Dependency errors log only the exception class. Startup/server messages may additionally appear as ordinary Uvicorn text.

Local logs: `docker compose logs -f api`. Hosted logs: Render service dashboard. Do not expose admin dashboard credentials to make logs public; provide a short screen recording of a live burst and attach/link it in `DEPLOYMENT.md` if public log access is unavailable.
