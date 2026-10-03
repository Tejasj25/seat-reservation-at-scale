# Design and operating notes

## Atomic decision

PostgreSQL is the system of record. Each mutation uses a single transaction; the connection context commits before an HTTP response is delivered. A seat is one row with primary key `(show_id, label)` and at most one `reservation_id`. A composite foreign key ensures its reservation belongs to the same show.

For reserve, acquire a transaction-scoped advisory lock for `(idempotency, user, key)`, inspect any previous reservation, then acquire a lock for `(user_show, user, show)`. Count that user's currently confirmed seats while holding the second lock. Lock requested seat rows with `ORDER BY label FOR UPDATE`. Check that all exist and are free, insert a reservation, assign every seat, and commit. A losing contender sees the winner's committed assignment after acquiring the row lock and receives 409. The same user's parallel reservations cannot pass the limit check independently because that user/show lock remains held through commit.

There is no global show lock; unrelated users booking disjoint seats can proceed concurrently. Transactions lock multi-seat requests in the same database ordering, avoiding reversed-seat deadlocks. Advisory locks use PostgreSQL's two-integer form: the first integer is a distinct namespace for idempotency versus user/show locks, and the second hashes JSON-encoded components. Hash collisions within a namespace add serialization without bypassing a lock; separate namespaces preserve the lock hierarchy. Like all lock-based implementations, consistent lock ordering must be maintained in future mutation paths.

Cancellation takes the same user/show lock before locking the reservation and its seat rows. It clears seats only where `reservation_id` still equals the cancelled reservation ID, and changes reservation status atomically. Repeated cancellation cannot clear a later reservation. There are no seat deletion or mutation endpoints outside these paths.

Show state uses one SELECT, so seat statuses and aggregated counts share one MVCC snapshot even during writes. Metrics use a read-only repeatable-read transaction so their multiple queries share a snapshot. Separate HTTP requests can observe different valid snapshots as inventory changes.

## Idempotency

Successful keys are stored on `reservations` with a unique `(user_id, idempotency_key)` constraint. The key lock makes in-flight retries wait for the first transaction rather than race on insertion. The normalized seats and show ID are the request fingerprint. Changed seats/show produce 409; a matching replay returns the same reservation ID and amount with HTTP 200, which makes initial 201 winners distinguishable in a burst. Body seat order does not matter. Keys from distinct users are independent. For requests to valid shows, reservation_keys also persists the first fingerprint even when a domain outcome declines the booking. Reusing a declined key with different seats or another show is rejected; retrying its original seats can reevaluate availability. Validation failures and nonexistent shows do not bind a key.

A crash before commit leaves neither reservation nor assignment. A crash after commit but before the client receives the response is resolved by replay. Cancelled keys stay retained; retry returns the original ID and current cancelled state. There is no actual charging integration. Adding payments requires a transactional outbox, a payment-provider idempotency key and an explicit state machine; a database commit alone cannot provide atomicity with an external payment system.

## Lifecycle and partitions

Use immediate confirmation plus explicit owner-only cancellation. There is no expiry scheduler and no held state; held count is always zero. All-or-nothing bundles make retry and cancellation behavior unambiguous.

Choose consistency over availability when PostgreSQL is unavailable. Readiness returns 503 after its dependency timeout, writes fail closed, and no local inventory fallback exists. Liveness only checks the process. The requirement of zero 5xx is a healthy-system load acceptance criterion, not a claim that database outages can produce correct successful bookings. Dependency failures are honest 503 responses. A client with an ambiguous result retries the same key.

## Capacity and observability

An async connection pool bounds concurrent PostgreSQL work to 20 connections by default. Pool waits allow a burst to queue for up to 120 seconds; that is not an unlimited throughput promise. Reverse-proxy request deadlines, connection limits, memory, database tier and the client load generator all constrain the deployed system. Validate the exact public endpoint. The prepared load script defaults to 20,000 total requests at concurrency 500 and can be explicitly set to 20,000 simultaneous requests. The final local 20,000-request run at concurrency 200 passed with exactly one winner, 19,999 seat-taken declines, zero 5xx/transport errors, and 109 valid snapshots in 219.38 seconds. This is loopback Windows evidence, not a public-deployment capacity result.

Confirmation/cancellation counters derive from persisted reservations; decline/replay events are appended in the same transaction as the outcome decision. This avoids one contended global counter row and makes metrics survive restarts. Counts represent committed decisions, not proof the client received a response. The event table grows indefinitely in this exercise; production needs partitioning/retention plus durable aggregation before pruning.

At 2am, page for readiness failures, elevated 5xx/transport failures, sustained latency/timeouts, database saturation, and nonzero inventory reconciliation errors. Seat-taken declines during a sale are expected; a spike alone is not a page. Investigate idempotency conflicts as a client correctness issue. Follow a request ID through structured logs. The provided endpoint exposes the assignment's required metrics; external HTTP latency/error monitoring and an installed Prometheus/alerting stack are future work, not delivered infrastructure.

## AI usage and verification status

The user supplied the assignment and requested a new GitHub repository and deployment URL. Codex selected Python/FastAPI/PostgreSQL, designed the transaction and lock protocol, and authored the API, SQL schema, container configuration, Render Blueprint, load checks, CI and these notes. The user has not yet independently reviewed or demonstrated the implementation. Do not present the generated design as unaided work.

The sandbox initially failed with disk-space and initialization errors. Commands subsequently ran under the normal Windows account. Dependencies installed, Python compilation and OpenAPI generation passed, Docker Compose configuration validated, and the initial real PostgreSQL concurrency suite and additional authentication/validation checks passed. Docker itself remained unresponsive, so local tests use isolated PostgreSQL 17 binaries inside the ignored .tools directory. The final 20,000-request run passed, both regression tests passed, and stopping/restarting the isolated database produced readiness 503, liveness 200, and recovered readiness 200. An earlier large run was not accepted because its observation connection failed. The load generator was changed to share its TLS context, avoid a shared contended worker pool, and report observation errors explicitly; subsequent full runs passed. See evidence/ for the final report, tested source commit and local request logs. Git history has been recorded incrementally. The public GitHub repository has been created at https://github.com/Tejasj25/seat-reservation-at-scale; hosted deployment and public load verification remain pending. Official Psycopg pooling, PostgreSQL Windows download and Render Blueprint documentation were consulted during preparation.

## Next work

Deploy, measure the public endpoint and capture live logs, including a cold-start test. Then add schema migrations with versioning, database backups/recovery drills, identity-provider integration, an outbox for payment work, deployment resource sizing, load admission control with documented client behavior, outcome retention, scrape protection if required, and managed alerts. Practice extending the code and explaining its transaction interleavings before the interview.
