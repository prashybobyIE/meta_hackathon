# Public-work marketplace service

Implemented 2026-10-29 against [contracts v1](CONTRACTS.md). Python 3.11+ standard library; no third-party dependencies.

The marketplace stores approved task descriptors, worker capability metadata, bids, leases, acceptance receipts, and simulated credits in SQLite. The requester retains the private task graph, contracts, source artifacts, and test reports. The service selects no dependencies and executes no submitted code. Requester authorization creates each assignment and acceptance receipt.

## Run

Create separate requester and enrollment token files with owner-only permissions. Supply their paths rather than credentials on the command line:

```sh
python3 -m agent_marketplace.market \
  --db .demo/market.sqlite3 \
  --requester-token-file .demo/requester.token \
  --enrollment-token-file .demo/enrollment.token
```

The default listener is `127.0.0.1:8765`. Use `--port 0` for an ephemeral development port. The database is created with owner-only permissions and contains live, public-attempt dispatch capabilities. SQLite worker credential hashes persist; plaintext worker credentials are returned only during registration. Reusing an already-enrolled worker ID is rejected rather than silently replacing its credential. Credential rotation/recovery is not implemented.

`--allow-insecure-public-network` explicitly permits a non-loopback HTTP listener and non-HTTPS public requester URLs. This switch does not add encryption or permit confidential tasks. It is only a public-data development option. Remote use otherwise requires a deliberate authenticated TLS deployment; this basic server does not terminate TLS itself.

## Python integration

```python
from agent_marketplace.market import MarketStore, create_server

store = MarketStore(db_path, requester_token, enrollment_token)
server = create_server(store, host="127.0.0.1", port=0)
# Run server.serve_forever() on a thread or as the main service loop.
# Stop with server.shutdown(); server.server_close(); store.close().
```

`store.handle(method, path, token=None, body=None)` is the direct state-machine interface; HTTP uses the same method. `store.snapshot()` gives the read-only public view. A clock callable may be supplied to `MarketStore(..., clock=...)` for deterministic lease tests.

POST task, bid, and assignment responses are top-level objects. GET task returns `{task, assignment}`. Worker assignment retrieval includes the direct requester URL and scoped dispatch token; requester task reads and public snapshots omit them. Credit totals are `{reserved, settled}`. Worker capability/provider/model metadata is self-reported and untrusted, suitable for placement hints only.

## Implemented invariants

- Enrollment, requester control, and per-worker credentials are separate authorization scopes. A worker cannot accept its own output or read another worker's dispatch capabilities.
- Unknown fields cannot enter stored descriptors. Content, full contracts, dependency graphs, arbitrary diagnostics, and secrets do not enter snapshots. Rejection diagnostics remain requester-side; the market stores a fixed reason.
- Assignment atomically checks open task state, deadline, worker liveness, advertised capability, capacity, quote budget, and a previously unused attempt ID. Worker liveness expires after 15 seconds; no claim of performance or protected execution is inferred.
- Lease expiry and explicit requester rejection release reservations and fence the old attempt. Task descriptors reopen; accepted tasks never reopen. Expiry is evaluated on API/store accesses, not by a background timer.
- Each accepted task creates one durable receipt binding its authorized attempt, worker, quote, artifact digest, and acceptance-report digest. An identical receipt retry is idempotent; a conflicting receipt or stale attempt returns 409. Final application success is outside this per-task reward gate.
- Confidential advertisements may be recorded as descriptors, but all current worker bids and assignments for them are refused. Worker claims, model names, capabilities, or a signed app cannot bypass this refusal.
- Bounded JSON requests/responses, duplicate-field and non-finite-value rejection, typed numeric validation, suppressed request logging, and loopback binding are implemented. The service never fetches an assigned requester URL.

## Limits

This is a public-data demonstration service, not a production Internet marketplace or protected runtime. It trusts the configured requester to issue acceptance. It provides neither hardware attestation nor fairness arbitration. There is no real money, global job-budget account, anonymous enrollment, TLS termination, credential rotation, or scalable API pagination. Demo quotas bound enrollment/task creation; snapshots contain only recent 100 tasks/bids/attempts/receipts/events, all enrolled workers up to the 128-worker quota, and complete credit totals. An oversized serialized snapshot is refused rather than truncated ambiguously. General Internet denial-of-service resistance requires a deployment layer.

SQLite preserves attempts/receipts and worker credential hashes across service restarts. A separately running requester must also preserve its own graph/artifacts/dispatch credentials to recover the full workflow. These are different state responsibilities.

## Verification

```sh
python3 -m unittest discover -s tests -p test_market.py -v
```

Twelve tests passed on October 29: local HTTP authorization and malformed-body handling; durable acceptance across restart; no duplicate payment; lease expiry/reassignment and stale fencing; explicit rejection/reservation release; worker identity scope; secret-free snapshots; confidential work refusal; capability/capacity/liveness/budget checks; concurrent assignment serialization; and unsafe request validation. Loopback HTTP tests required the execution sandbox's local-listener permission, which was granted for the test run. Passing these tests establishes the covered application behavior, not hostile-host confidentiality.
