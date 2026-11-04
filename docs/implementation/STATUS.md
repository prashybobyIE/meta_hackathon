# Demo foundation implementation status

Updated: 2026-11-02. Live Meta inference has run end to end on public workers, and one private task ran on an Azure confidential container on November 1. The protected release gate is now hardened and locally regression-tested; a new live run still requires approved trust values and explicit cloud approval.

## November 4 interactive workflow studio

Recording refinement: components now have distinct light/dark accents, a sticky top workflow/current-phase strip and local/hosted runtime labels. Component selection shows its resource placement, model/runtime and linked trace. `status.resources` describes known execution placement, not invented utilization. Existing complete runs can be restored with `--resume-completed-run PATH` after checking artifact hashes and final integration; inspection makes no model call and the screen is explicitly labeled as a recorded run.

Two live Meta runs were performed through the dashboard in Edge. The first completed generation/repair/integration in 77.633 seconds at USD 0.058069, with two rejected outputs repaired; its invalid model plan was visibly rejected in favor of the reference graph. The prompt/input was then corrected to expose required build-dependency minimums explicitly. The second accepted Meta's plan, used two local worker processes, repaired one rejected artifact, independently accepted all three modules and passed integration in **79.737 seconds**, at **USD 0.053391** by the guard. It settled **22 simulated credits**. Both runs together used USD 0.111460 by the ledger, not reconciled billing-console figures. Public specifications/repair diagnostics only; no Azure worker ran.

The refined backend/UI passed **242 tests without skips in 81.360 seconds**. Tests include resource placement, concurrent work/verification, final-delivery semantics and restored-artifact refusal. Presentation-only trace readability/responsive adjustments were checked separately. A brittle rehearsal assertion expecting a fixed credit total was replaced with actual receipt-sum/idempotency invariants: quotes and delivery order can legitimately change the settled total.

`demo_video/` now contains a **180-second, 1920×1080, 30fps** editable Remotion preview with ten scenes, component/resource focus and the sanitized actual 73-event trace. It labels replay/explanatory holds and includes a clean completed-dashboard proof image. Typecheck, lint, evidence/timeline validation and still-image QA passed. The user supplied a 163.2-second direct recording. Local speech-model word timestamps checked the ten paragraph boundaries; the original performance was aligned with silence between phases and normalized to approximately −16 LUFS, without a speed change. The prepared soundtrack is exactly 180.000 seconds and the browser preview plays it without an error. Audio, local transcripts and model weights remain ignored; original Voice Memos metadata was removed from the prepared audio. The final MP4 has not been exported. No speech service was charged, voice cloned, subscription upgraded, or video published. The separate public GitHub copy is updated only from the reviewed sanitized export.

The video now includes component placement throughout the working application, text instructions for free rehearsal/local personal Meta-key setup, and 41 synchronized English subtitle phrases with SRT/VTT sidecars. The testing guide is `docs/implementation/TRY_DEMO.md`; the revised local preview runs on port3002. No new voice generation or additional API spending was needed.

Read-only Azure inventory on November 4 found **11 resources in the approved subscription**, including one container registry, AI-service accounts/project, a key vault and monitoring/log resources. There were **no ACI container groups**. This is an inventory finding, not approval to start or delete anything; no cloud resource state was changed.

The local branch adds `python3 -m agent_marketplace.studio --port 8765`: a persistent browser dashboard with real multi-worker run controls, animated component connections, clickable component trace filters, task plans, bid/winner explanations, Meta-call stages, verifier case counts and accepted-app CSV interaction. Free mode calls a local prewritten-response simulator through the real worker protocol; it is not AI inference. Live Meta requires `--allow-live-meta` at startup plus configured credentials and the existing spend guard. No Azure resource is provisioned or needed by the studio. Use the laptop testing guide to run this dashboard from a revision containing studio.py.

Structured traces merge marketplace/requester observations and explicitly labeled worker reports. The public screen shows only current-run model accounting, not unrelated cumulative account totals. Browser actions require a session capability, local Host/Origin and bounded allowed request fields; application interaction uses accepted source in the bounded interpreter. Run generations and sample responses are bound so previous-run results are not labeled as new work. Planning is single-flight and does not block status polling. Failed setup restores model-ledger environment and closes unstarted listeners without deadlock.

All **235 Python tests passed**, without skips, in **80.439 seconds**, including real worker/control-plane rehearsals and new trace/authentication/race/cleanup regressions. Later presentation-only adjustments were checked in the actual browser and targeted UI/protected-view tests. Browser verification exercised component selection, Meta trace filtering, real intentional failure/reassignment, repaired rejection, three accepted modules and final integration. Public sample input was then evaluated through accepted artifacts: two rows produced 5.50 units without another model call. Development checks used no paid API or cloud resource; the revised planning prompt has not been measured against live Meta yet.

The planner still works within the three frozen public module contracts. It now distinguishes code-generation dependencies from runtime data flow in its prompt, but arbitrary source-derived decomposition remains future work. Protected/hardware and physical multi-machine limitations below remain unchanged.

November 4 readiness check: all 210 Python tests passed again, without skips, in 68.767 seconds; a free simulated crash/repair run and a separate run from the sanitized licensed export completed with three accepted modules and passing integration. No marketplace runtime code changed, no paid inference was called and no cloud resource started. See the submission-readiness record for release/video/form work and the publication hold. These checks do not change the protected-path limitations below.

November 4 release update: original PR 1 and privacy follow-up PR 2 are merged. All 213 tests passed after the confidentiality fixes. A fresh real Meta public-work run passed in 192.909 seconds at USD 0.056978 by the guard. The sanitized public copy is [`prashybobyIE/meta_hackathon`](https://github.com/prashybobyIE/meta_hackathon), initial commit `5521d68`: all 79 published paths and hashes match the reviewed export, its source-tree scan passed, and its own free crash/repair rehearsal passed in 2.971 seconds. The user explicitly chose to keep the account's GitHub commit attribution, including its configured email. See the private publication review for the source-versus-metadata distinction. Video is deferred in favor of aligning on the requested interactive visual dashboard and backend trace. No cloud resource was started.

Review note, 2026-11-02: the three findings below have been fixed in code with local regression tests. Private release now requires independently approved VM measurements and firmware floors; the empty shipped trust policy refuses execution. The historical live run demonstrates hardware-signed evidence and encrypted task processing, but predates these mandatory checks. It does not validate the hardened path against real Azure evidence.

## Protected execution, November 1

Backend: confidential containers on Azure Container Instances, Central India, Genoa hardware (C30). Run `.demo/live-3`: the Mac fetched AMD's chain from AMD, verified the VCEK chain and report signature, checked non-debug policy, approved policy hash, nonce and key binding, then sealed 18 synthetic rows with X25519 and AES-GCM. Llama 3.2 3B in the group categorised them in 8 s on CPU, and the Mac scored 18/18 against its own answer key. Operator tests: `az container exec` was denied by policy, and logs were empty. A live worker with a valid AMD signature but an unapproved policy was refused.

Limits of the November 1 live run (the November 2 code changes are recorded below):
- AMD and Microsoft, including its utility VM, remain trusted. Side channels and physical attacks are excluded.
- The utility VM measurement is reported but not pinned, and there is no minimum TCB check. Different hosts reported different TCB values.
- The protected step is a fixed endpoint: it does not bid, take a lease or settle credits, so it is not yet part of the marketplace.
- The policy was generated from locally built images, and the model comes from an unofficial GGUF conversion on Hugging Face.
- 18/18 is on a small synthetic set and is not a quality measure. Attestation says nothing about output quality.
- Transport is plain HTTP; task data is sealed end to end, while metadata and evidence are visible.

Commands and required trust configuration are in the [runbook](RUNBOOK.md#protected-private-task). No new cloud window is approved. The worker bills while up, and the current CLI refuses the empty trust-policy template before starting its services.

## Rehearsal without Azure, November 1

Run from merged code, `.demo/rehearsal-180813`, on one Mac. Protected step and second Mac were not part of this rehearsal.

| Run | Result | Meta cost by the guard |
| --- | --- | --- |
| Expense report, `--provider meta --failure` | Complete: crash expired, 1 rejection repaired, 3 modules, integration passed, 124 s | $0.052 |
| Inventory reorder, `--provider meta` | Complete: 2 rejections repaired, 2 modules, integration passed, 38 s. First real model output for a module that consumes a dependency | $0.041 |
| Mixed, `--workers ollama=llama3.2:3b,meta=muse-spark-1.3` | Complete: 3 modules, integration passed, 30 s | $0.021 |
| Inventory over TLS with a separate worker process through this Mac's own network address, fixture provider | Complete, integration passed. Not AI output | $0 |

Five further sequential runs of each live workload the same day: expense report with `--failure` completed 4 of 5 (the other stopped on four consecutive provider 503s after the lease expiry, with no module generated) for $0.33 in total, and inventory reorder completed 5 of 5 for $0.11, all by the guard. See [reliability](../measurements/RELIABILITY_2026-11-01.md); 5 runs each is a small sample.

## Workload template, local models and measurement, November 1

**Workload template, first version.** Applications are defined by a validated JSON manifest: tasks, dependencies, public specs, entry points, public and hidden acceptance cases, price limits and an integration check. See [workloads](WORKLOADS.md). The expense report runs from a manifest and remains the default. Inventory reorder is a two-task chain whose `reorder` module receives the accepted `catalog` artifact. Hidden cases are withheld from workers and prompts but are readable by anyone with the repository. Accepted source from one worker's model enters the next worker's prompt for tasks that declare `consumes`, which is a new prompt-injection surface limited by independent acceptance and the budget guard. It is not a general application generator.

**Local models.** Ollama 0.40.13 with `llama3.2:3b` has run live on an M3 Pro, alone and bidding against a Meta API worker through normal acceptance. Local modules are accepted only with local-only step hints close to pseudocode, tuned while watching pass counts, and only 14 of 24 local-only runs completed.

**Measurement.** Recorded in [worker scaling](../measurements/WORKER_SCALING_2026-11-01.md). The Meta planner made `categories` depend on `parser` in all 18 measured planner runs, so extra workers overlapped only with `--reference-plan`. On that fixed graph, two Meta workers took 25.7 to 32.4 s against 39.7 to 65.6 s for one (5 runs each), which fits two requests waiting on the network and is not distributed compute. Three workers added nothing, because at most two modules are ready at once. Local wall times overlap across 1, 2 and 3 workers. Multi-machine scaling is not measured.

**Remote connections.** Beyond loopback, `/v1/status` and `/v1/snapshot` need the requester token or a loopback caller, and optional TLS is available. See the [runbook](RUNBOOK.md).

## Live evidence, November 1

Run `.demo/live-2` (`--provider meta --failure`, `muse-spark-1.3`, about 126 s, $0.05 by the guard): Spark's plan passed validation; the crashing worker's lease expired and the task was reassigned; a real rejection (`strip` on `None`) was repaired and accepted; 3 modules accepted and integration passed. One run is not a reliability measure. Meta console billing has not been reconciled against the guard.

The budget guard reserves worst-case cost before each call, refuses above a $35 default cap and a $1 per-run cap, and cannot be configured above the $40 ceiling (C25). It is per machine and sees only calls made through this code. Use `python3 -m agent_marketplace.budget`. Earlier ledger entries from the first live run overstate cost because reasoning was double-counted before the fix.

## Implemented and exercised

| Component | Behavior |
| --- | --- |
| Marketplace | SQLite-backed metadata, scoped authentication, capability/liveness/capacity filtering, bids, atomic assignment, expiring leases, stale-attempt fencing, restart persistence |
| Requester | Full graph and readiness remain local; scoped direct public contract delivery, independent acceptance, budget checks, per-task receipt authorization, restart reconciliation, final assembly |
| Worker | Separate OS process; enroll/heartbeat/bid/poll, fetch authorized public contract, generate source, deliver artifact; no execution of model source on worker host |
| Providers | Fixture baseline; Meta adapter and Ollama adapter exercised live, including one worker of each in a job; loopback simulator (`simulated-meta`, no AI) for free rehearsal |
| Planner | Spark proposes dependencies and complexity by structured output; code validates; complexity sets each task's maximum price; invalid or failed plans fall back visibly |
| Repair | Rejection feedback from public cases and the previous source goes to the next attempt, at most 2 repairs per task; hidden cases are withheld |
| Market | Quotes scale with task size; requester records every offer, score and selection reason; optional bid window |
| Acceptance | Narrow AST interpreter with allowed APIs and execution/value/CSV/Decimal limits. No Python exec/eval or generated-code subprocess. Rejects unsupported syntax, host interfaces, incorrect artifacts and mutation |
| Accounting | Simulated reservation and one reward per accepted task; rejected/expired attempts receive no completion reward. Final-app success is separate |
| Demonstration | Two healthy workers, optional intentionally crashing third worker, lease recovery, three accepted modules and final public expense report |
| Observer | Local dashboard with plan, workers, offers, selection reasons, model, latency, tokens, cost, spend against the guard, refused private dispatch and final result. Remote strings rendered as text |
| Native client | Compiled Swift Foundation/URLSession observer for market health/metadata and read-only ordinary Hypervisor availability query |
| Research | Primary-source comparison and proof routes for AVF/pKVM/Gunyah, CCA, confidential VMs and Nitro |

The reference app is a provisional fixture: CSV parser and categorizer can be built independently; summary becomes ready after both are accepted. Workers supply module source. The requester interprets each against independent cases, then runs the accepted modules together. The offer policy provisionally spreads accepted modules across eligible workers, then chooses the cheaper quote. This is not a measured speed/cost optimization.

## Actual limits

- All marketplace workers are ordinary public-work processes, and marketplace dispatch of confidential work is refused. The only protected execution is the fixed Azure step described above, outside bidding, leases and credits.
- A read-only Hypervisor probe or compiled native binary does not grant protection. This executor denied the Mac capability query; its result remains unknown.
- The AST interpreter supports only the small reference Python subset. It is not a general-purpose or formally audited sandbox. Broader arbitrary-code tasks require a separately supported isolation/runtime backend.
- Default fixture output is prewritten code, not AI inference. Model tool calling is not implemented.
- The template is a first version: the expense report renderer, complexity prices and bid policy stay fixed, a task can consume at most 2 others, and runtime rules still mention csv, io and decimal for every workload.
- `--remote-workers` is tested through this Mac's own network address, not a second physical machine. It uses plain HTTP that exposes credentials unless `--tls-cert` and `--tls-key` are supplied; use it for public data only either way. Beyond loopback, status and snapshot need the requester token or a loopback caller, and a same-host reverse proxy defeats the loopback check. TLS covers credentials and metadata in transit only.
- Worker `--state-file` resumes an identity. Resume across a real daemon restart is covered only by the file being written, not a restart test.
- Requester receipt authority is trusted for this prototype. Disputes, malicious receipt withholding, real payment, global requester authentication and anonymous enrollment remain unresolved.

## Validation

November 2: all 210 Python tests passed under `.venv` in 69.7 seconds, including 48 new regressions for the review fixes, integration and export compatibility. These include signed unapproved-VM/old-TCB refusal before POST, an approved synthetic signed guest with real X25519/AES-GCM exchange, lost replies and corrupt responses, dashboard JavaScript states, CLI policy gating, deterministic transient failover/cooldowns/restart, and existing budget/lease/receipt checks. Local networking permission was enabled; no tests were skipped. Attestation/protected-flow test modules require `cryptography`. The hardened code has not been run against live Azure hardware or the Meta API. The history-free export and its scanner passed; its two internal scanner tests stay private with the internal export tool.

Historical November 1 validation: 162 Python tests, 3 Swift tests and a Swift-to-real-market smoke passed. Swift was unchanged in this fix round; it was not rerun. The client uses `/health` and `/v1/snapshot` on loopback with no token. The suite also exercises real process/HTTP integration, intentional failure/reassignment, marketplace restart, requester crash after remote acceptance, ambiguous assignment response recovery, atomic capacity reservations, stale/duplicate receipts, authorization, metadata redaction, provider disclosure gates and bounded runtime rejection. See [runbook](RUNBOOK.md) for commands.

These application tests do not establish live host confidentiality. The historical protected evidence above predates the mandatory measurement/TCB appraisal, so a new live claim requires an approved trust policy and a separately approved hardware validation. The current test run is recorded in the root README and discussion log.

## Next implementation boundaries

1. Independently approve the UVM measurement allowlist and product-specific TCB floors, then validate the hardened gate on real evidence in a separately approved cloud window.
2. Bring the protected worker into bidding, leases and settlement.
3. Validate a real second Mac.
4. Stop the Meta planner serializing independent modules, so concurrency appears without `--reference-plan`.

Shared interfaces and ownership: [contracts](CONTRACTS.md). Service details: [market](MARKET.md), [worker/providers](WORKER.md). Research: protected execution paths.

## Independent review, November 2

Scope: initial foundation `6503dd9` through current `dda89e2`, with a clean starting working tree. These are review findings and recommendations, not new user decisions. Application code was not changed. All 162 Python tests passed under `.venv` with local networking enabled, in 67.8 seconds. The history-free public export and its scanner passed for `dda89e2`; nothing was published. No paid inference, cloud resource, or hardware deployment was used for this review, and the November 1 live runs were not rerun.

1. **P1 — Require trusted guest identity before private release.** `snp.verify_evidence` checks a VM measurement only when its optional `expected_measurement` argument is supplied; `protected.run_private_task` never supplies it. The worker also does not deliver Microsoft's UVM measurement endorsements. HOST_DATA is supplied by the hypervisor at launch, so a matching policy digest alone does not establish that the approved guest enforces that policy. A different SNP guest can name the same digest and bind its own channel key. A local test using the existing synthetic certificate/report helper accepted an all-zero measurement and TCB; supplying a different explicit measurement pin refused that evidence. This demonstrates the missing appraisal, not a forged AMD signature or a live hardware attack. Require a trusted measurement allowlist or verify Microsoft UVM endorsements/compliance and the binding to the report, plus a defined minimum TCB policy. Do not approve values merely because the endpoint supplied them.

2. **P1 — Preserve whether data was released when a response is lost.** `protected.attested_step` catches every protocol error as `verified:false`, even if it occurs on the POST after successful verification and sealing. The dashboard then says `REFUSED: no private data was released`. A local mock confirmed that an encrypted task body was submitted before a simulated 503 produced this record. Preserve separate attestation, release, delivery and acceptance states; once submission begins, an ambiguous result must say that delivery/execution is unknown. Additional robustness gap: fetching AMD roots is outside the exception handler, so an endorsement-service error escapes and tears down the demo rather than producing a recorded unavailable state.

3. **P2 — Back off and change worker after transient provider failures.** `Requester.tick` penalizes acceptance rejections and excludes selected permanent failures, but provider 503s do neither. The cheapest worker wins immediately again until the task attempt limit is exhausted. An in-process test returned 503 five times and selected `worker-a` on every attempt while `worker-b` remained available. This agrees with the recorded reliability failure. Use bounded cooldown/backoff and a transient-failure selection penalty or another eligible worker, without weakening the spend cap or stale-attempt fencing.

Architecture assessment: the requester-owned graph, public contract projection, bounded independent acceptance, leases, single settlement and honest measurement records are solid foundations. The protected worker still runs after the public app has already assembled; it has no market bid, lease or receipt. Integrating one protected task into that lifecycle and completing an actual second-machine TLS run would strengthen the central product demonstration more than another workload or model. The Meta planner currently models unnecessary execution dependencies between code-generation tasks; use the reference graph for a predictable concurrent demo or constrain its planning semantics. The protected 18-row outcome and local-model measurements remain small/tuned samples, not general model-quality or distributed-compute evidence.

Primary sources rechecked November 2: [AMD SEV-SNP firmware ABI](https://www.amd.com/content/dam/amd/en/documents/developer/56860.pdf) defines HOST_DATA as hypervisor-provided launch data; [Azure ACI attestation](https://learn.microsoft.com/en-us/azure/container-instances/confidential-containers-attestation-concepts) distinguishes compliant UVM identity from policy identity; [Microsoft CCF SNP documentation](https://ccf.dev/main/operations/platforms/snp.html) documents UVM measurement endorsements and minimum TCB policies. The consequence for this verifier is the reviewer's inference from those trust requirements and the inspected code.

## November 2 fixes

Three implementation agents worked in isolated worktrees from the saved review contracts. The root reviewed and integrated their commits, added CLI/real-synthetic-evidence and requester-event integration coverage, and updated the documentation. No cloud resources were started and no paid model calls were made.

- **Attestation:** a local version-1 trust policy must approve the VM measurement and a product-specific minimum TCB. Floors are compared component by component and bound to the AMD-signed VCEK SPL extensions, with certificate validity, strict bounded evidence parsing, nonce, channel-key, signature, non-debug and container-policy checks. The template approves nothing. Only report version 5 from the inspected AMD ABI 1.58 layout and Milan/Genoa TCB layouts are supported; other versions/products refuse. Historical raw report/version was not saved, so compatibility of the November 1 Azure report cannot be confirmed offline. Microsoft endorsement verification is not implemented; trusted measurements/floors must be independently approved, never copied from an endpoint as implicit approval.
- **Release bookkeeping:** verification, encrypted submission, authenticated delivery and independent acceptance have separate states. Once POST starts, errors retain `submitted/unknown`; invalid authenticated responses remain unaccepted. Root-service outages are recorded before release, legacy records without release state remain unknown, and optional refusal-check errors cannot erase a completed task. Dashboard text and requester events follow these states. No private payload enters general metadata.
- **Transient recovery:** 408/425/429/5xx put the task's worker on persistent exponential cooldowns of 2, 4, 8, 16, then at most 30 seconds. Another eligible worker is preferred. Cooling waits do not consume attempts or rewards and do not trigger the no-offer timeout. Attempt ceilings, credit limits, stale-result fencing and single settlement remain enforced.
- **Export compatibility:** AMD certificate OIDs are no longer mistaken for partial IPv4 addresses. Regression tests retain detection of actual public addresses, including next to an OID or sentence-ending punctuation. The internal scanner fixtures remain private along with the internal tool. The final export/scanner passed without publishing anything.

Local tests use synthetic signed certificates/reports, real X25519/AES-GCM payload exchange, mocked failures, deterministic clocks and real local HTTP/worker processes. They establish the covered code behavior, not live hardware confidentiality. See the [runbook](RUNBOOK.md) for approved-policy configuration and tests.
