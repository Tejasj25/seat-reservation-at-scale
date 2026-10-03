# Deployment evidence

Public repository: https://github.com/Tejasj25/seat-reservation-at-scale

Status: source published; public hosting still pending.

| Deliverable | Evidence |
|---|---|
| Public GitHub repository | Created; incremental history retained |
| Public API URL | Not yet deployed; Render authentication required |
| Readiness after cold start | Local startup passed; public cold start pending |
| Live metrics URL | Pending deployment; route `/metrics` |
| Public logs or screen recording | Not yet captured |
| 20,000-request local burst | Prior revision passed: one winner, 19,999 declines, zero errors; final revision recheck running |
| 20,000 simultaneous requests | Not yet tested |
| Dependency outage | Readiness 503, liveness 200, recovery 200 |
| Container build | Local Docker daemon unresponsive; GitHub Actions verification pending |

The local service uses isolated PostgreSQL 17 on Windows. Regression tests passed against a real HTTP service and database. Final benchmark details will be saved under `evidence/`. Public deployment must be tested independently before submission.
