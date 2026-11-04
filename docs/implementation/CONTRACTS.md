# Demo foundation contracts v1

Updated 2026-10-29. Implementation is now authorized by the user. These are provisional implementation choices, not hardware/security selections. Python 3.11+ standard library provides the first runnable network. Separate processes communicate over JSON HTTP on loopback by default. No external package installation is required. Confidential execution remains fail-closed until a real protected backend/verifier exists.

## Shared Python helpers (root-owned)

`protocol.py` exposes `ApiError(status, message)`, `json_request(method, url, body=None, token=None, timeout=5)`, `sha256_text(text)`, `canonical_hash(value)`, `new_id(prefix)`, `validate_identifier(value, field='id')`, `is_loopback_url(url)`. HTTP JSON response bodies are dictionaries. Successful list responses use a named array. Error response: `{"error": "message"}`. UTC timestamps are Unix seconds. Credits are positive integer demo units.

## Marketplace API

`MarketStore(db_path, requester_token, enrollment_token, clock=time.time)` owns SQLite state and exposes `snapshot()` for read-only demo metadata. `create_server(store, host='127.0.0.1', port=0)` returns a ThreadingHTTPServer (caller runs serve_forever). CLI: `python -m agent_marketplace.market --host ... --port ... --db ... --requester-token-file ... --enrollment-token-file ...`.

- GET `/health`: unauthenticated readiness only.
- GET `/v1/snapshot`: public demo metadata only; no tokens, input/source code, or private graph. Local dashboard can use this.
- POST `/v1/workers/register`, enrollment bearer: `{worker_id, capabilities:["python-module-v1"], provider, model, slots:1}` -> `{worker_id, worker_token}`. Registration conflicts cannot overwrite existing identity. Worker tokens are unique per worker, never exposed in snapshots.
- POST `/v1/workers/{id}/heartbeat`, worker bearer: `{}` -> `{ok:true}`. Worker live timeout 15 seconds.
- GET `/v1/tasks`, worker bearer: `{tasks:[descriptor...]}` listing OPEN tasks. No graph/contracts/source.
- POST `/v1/tasks`, requester bearer: descriptor `{task_id, capability:"python-module-v1", sensitivity:"public"|"confidential", max_credits:int, expires_at:float}` -> task. Confidential advertisements may exist but current workers cannot execute them.
- POST `/v1/tasks/{id}/bids`, worker bearer: `{credits:int}` -> bid `{bid_id, worker_id, task_id, credits}`. Capability/slots/liveness matter. Reject bids for confidential tasks in this unprotected implementation, regardless of self-claims.
- GET `/v1/tasks/{id}/bids`, requester bearer -> `{bids:[...]}` live eligible offers only.
- POST `/v1/tasks/{id}/assign`, requester bearer: `{bid_id, attempt_id, lease_seconds, requester_url, dispatch_token}` -> assignment. Reserves quote, unique attempt and capacity atomically; requester remains assignment authority. Dispatch token grants access only to PUBLIC attempts in this version. It is not a private-data release credential.
- GET `/v1/workers/{id}/assignments`, worker bearer -> `{assignments:[{task_id, attempt_id, lease_until, requester_url, dispatch_token}]}` current leases only.
- GET `/v1/tasks/{id}`, requester bearer -> `{task:descriptor+state, assignment:object|null}`. State: `open`, `assigned`, `accepted`; expired assignments reopen on store/API access. Fencing persists historical attempts.
- POST `/v1/tasks/{id}/accept`, requester bearer: `{attempt_id, artifact_sha256, report_sha256, receipt_id}` -> `{accepted:true, duplicate:bool, receipt:...}`. Settle quote exactly once only for current unexpired authorized attempt. Duplicate identical receipt is idempotent; conflicting receipts/stale attempts return 409. Receipt binds task, attempt, worker, quote and hashes.
- POST `/v1/tasks/{id}/reject`, requester bearer: `{attempt_id, reason}` -> `{ok:true}`. Releases reservation, fences attempt, reopens task. Keep public diagnostic reason bounded.

Snapshot keys: `workers`, `tasks`, `bids`, `attempts`, `receipts`, `events`, `credits` (`reserved`, `settled`). No full DAG, tokens or source text. Expiry releases reservations. HTTP has bounded JSON bodies, typed validation, role authorization and no tracebacks in responses. Agents may add safe fields, preserving the required ones.

## Requester dispatch/result API (root-owned)

Requester keeps full graph, contract, source and acceptance tests locally. It advertises ready descriptors only, collects bids, checks policy/budget/capacity and authorizes attempts. It pre-creates a unique dispatch token before assignment.

- GET `/v1/attempts/{attempt_id}`, attempt bearer -> `{task_id, attempt_id, sensitivity:"public", contract:{id, filename, function, description}, provider_policy:{allow_meta:bool}, deadline:float}`.
- POST `/v1/attempts/{attempt_id}/result`, attempt bearer -> submit `{task_id, attempt_id, filename, content, sha256, provider, model, elapsed_ms}`. Returns `{received:true}` or an error. Requester independently verifies content and current lease before issuing a marketplace receipt. Worker cannot mark acceptance.
- Attempt tokens are scoped, expire with lease and authorize only one attempt. Reject confidential payloads before any provider call. Result body maximum 128 KiB; module source maximum 32 KiB. No arbitrary shell commands or host file paths in these APIs.

## Worker API/CLI

`Worker(market_url, enrollment_token, worker_id, provider='fixture', bid_credits=10, poll_interval=0.25, model=None, **optional)` exposes `run(stop_event=None)` (register once, heartbeat, bid, poll assigned attempts, fetch public contract directly from requester, generate source, return result). Optional `on_assignment`/execution delay or crash behavior may support reproducible failure demonstration; document controls. CLI: `python -m agent_marketplace.worker --market ... --enrollment-token-file ... --id ... --provider fixture|meta|ollama`.

Provider interface: `generate_module(contract, provider_policy, sensitivity='public') -> {content, provider, model}`. Fixture provider is deterministic prewritten reference source and clearly labeled `fixture`, never claimed as AI inference. Optional live Meta adapter uses verified official API format, explicit key from env, bounded timeout and public-only input; never makes paid calls during development without configured credentials. Ollama/local adapter is optional. Do not execute returned arbitrary code on worker host. Unknown contract/provider is rejected. Only fixed public module contracts are enabled now.

Reference module contracts:

1. `expense-parser-v1`, `parser.py`, `parse_expenses(csv_text)`: CSV headers `date,merchant,amount,category`; return list of dicts preserving fields but amount is integer cents. Validate positive/negative decimal amounts with at most 2 decimal places, reject malformed amounts/rows. Python stdlib csv/decimal allowed.
2. `expense-categories-v1`, `categories.py`, `categorize_expenses(rows)`: return copied rows; use supplied nonblank category stripped/lowercase; otherwise merchant keywords `coffee|cafe|restaurant` -> `food`, `uber|train|bus` -> `transport`, all others `other`. Case insensitive. No input mutation.
3. `expense-summary-v1`, `summary.py`, `summarize_expenses(rows)`: return `{"total_cents":int,"by_category":dict[str,int],"count":int}` from integer-cent `amount` and category (default `other`). Reject invalid/bool amounts. No input mutation.

Parser/categories can be advertised independently; summary advertisement waits for both accepted artifacts to demonstrate requester-owned dependencies. These are provisional reference fixtures, not a final product choice.

## Security truth

All running workers are ordinary PUBLIC demo workers. No self-reported confidential flag, mock evidence, signed manifest, native probe, or passing tests grants host confidentiality. A real protected adapter must cover the approved node/model/tool loading path, channel key, evidence freshness, boot/debug/admin policy and private inference. Until then private input dispatch is refused. Local HTTP is loopback development transport; remote enrollment/dispatch must use authenticated TLS or an explicitly acknowledged public-data-only development override. Bind to loopback by default. Never log credentials.

Acceptance runs only the narrow fixed modules through an AST-restricted evaluator or a demonstrably isolated backend; a Python subprocess alone is not a security sandbox. Do not exec arbitrary LLM code on the developer host. Final integrated report is independently checked on public fixtures. Real payment is not implemented.
