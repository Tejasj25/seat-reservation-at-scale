# Deployment evidence

- Public repository: https://github.com/Tejasj25/seat-reservation-at-scale
- Deploy link: https://render.com/deploy?repo=https://github.com/Tejasj25/seat-reservation-at-scale
- Public API URL: **pending Render authentication and deployment**.
- CI: https://github.com/Tejasj25/seat-reservation-at-scale/actions

| Check | Result |
|---|---|
| Regression suite | 2 tests passed, including concurrency, auth, validation, cancellation and declined-key retry |
| Final local burst | 20,000 requests, concurrency 200, 219.38 seconds |
| Hot-seat outcomes | 1 confirmation; 19,999 seat-taken declines; 0 5xx; 0 transport errors |
| Reconciliation | 109 valid in-flight snapshots; final available=1, held=0, confirmed=1, total=2 |
| Metrics | Final inventory gauges matched API state |
| Dependency outage | Readiness 503, liveness 200, recovered readiness 200 |
| Container build | [Passed clean-checkout build and tests](https://github.com/Tejasj25/seat-reservation-at-scale/actions/runs/37107593313) |
| Public cold start and burst | Pending deployment |
| 20,000 simultaneous requests | Not tested |
| Live public logs or recording | Pending deployment |

The local service uses Python 3.11 and isolated PostgreSQL 17.11 on Windows. The public deployment must be tested independently before submission. After deployment, replace this pending status with the actual service URL, `/metrics` URL, cold-start result, public burst report and live-log access/recording.
