# Reliability runs, expense report with failure and inventory reorder

Date: 2026-11-01. Ten sequential runs of the demo on one Mac against the Meta API with `muse-spark-1.3`, five per workload. Raw records are in `reliability_2026-11-01.jsonl`, one JSON line per run. Per-run workspaces were not kept in the repository.

## What this measures

Whether a full run of each live workload completes on repeated tries, how long it takes and what the budget guard charges. It is a small sample from one afternoon and does not measure throughput, scaling or anything about confidentiality.

- **A, expense report with the failure scenario.** Three workers, of which the cheapest exits on its first assignment so that its lease (75 s here) expires and the task is reassigned. The Meta planner proposes the graph. Complete means all 3 modules accepted and the integration check passed.
- **B, inventory reorder.** Two workers, two tasks: `reorder` consumes the accepted `catalog` artifact. The Meta planner proposes the graph. Complete means both modules accepted and the integration check passed.

Each run used a fresh ignored `.demo/` workspace created by the demo, no `--hold-seconds`, and the default limits: at most 2 repairs per task, at most 5 attempts per task, the guard's $1 per-run cap.

## Results

| Series | Runs | Complete | Wall s, min / median / max | Rejections repaired | Lease expiry after crash | Planner graph | Meta USD by the guard, total |
|---|---:|---:|---|---:|---|---|---|
| A, expense with failure | 5 | 4 | 81.3 / 117.7 / 125.5 (completed runs 115.9 to 125.5) | 3 | 5 of 5 | `categories` after `parser` in 5 of 5 | 0.3321 |
| B, inventory reorder | 5 | 5 | 18.0 / 21.8 / 31.1 | 0 | not applicable | `reorder` after `catalog` in 5 of 5 | 0.1128 |

Total for the ten runs: $0.4449 by the guard. The per-workspace ledger entries sum to the same figure within rounding.

### A, expense report with failure

    python -m agent_marketplace.demo --provider meta --model muse-spark-1.3 --failure --lease-seconds 75 --timeout 300

| Run | Complete | Modules accepted | Rejections repaired | Crash and lease expiry | Wall s | Meta USD (guard) | Planner: `categories` depends on `parser` |
|---|---|---:|---:|---|---:|---:|---|
| 1 | no, stopped: "Task attempt limit exhausted" | 0 of 3 | 0 | yes | 81.3 | 0.1509 | yes |
| 2 | yes | 3 of 3 | 1 | yes | 125.5 | 0.0493 | yes |
| 3 | yes | 3 of 3 | 1 | yes | 119.4 | 0.0472 | yes |
| 4 | yes | 3 of 3 | 0 | yes | 117.7 | 0.0406 | yes |
| 5 | yes | 3 of 3 | 1 | yes | 115.9 | 0.0441 | yes |

Run 1: the crashing worker's lease expired as intended and `parser` was reassigned to the next cheapest worker, whose four Meta requests all came back as status 503 within 1.3 s (the logs record only the status, which here can be an HTTP 503 from the API or a network error the client maps to 503). The requester re-authorized the same worker immediately after each failure, reached the 5-attempt limit on `parser` with 1 expiry and 4 provider failures, and stopped with no module generated. The four failed calls stay charged at their reserved worst case of $0.0368 each because they may have been billed, so this run is the most expensive of the ten, about $0.147 of its $0.151. Runs 2 to 5 that followed within minutes saw no provider failure.

Runs 2, 3 and 5: the first `categories` attempt was rejected with the same diagnostic each time, `Attribute strip is not available on NoneType in the reference runtime`, from one worker, then accepted on the first repair. Run 4 had no rejection. All 12 generated modules that were accepted passed every public and hidden case on acceptance (`parser` 11 of 11, `categories` 2 of 2, `summary` 5 of 5).

Wall time in the completed runs is mostly the 75 s lease wait on the crashed attempt plus about 40 s of sequential generation: with `categories` depending on `parser`, modules became ready one at a time.

### B, inventory reorder

    python -m agent_marketplace.demo --workload agent_marketplace/workloads/inventory_reorder/manifest.json --provider meta --model muse-spark-1.3 --timeout 300

| Run | Complete | Modules accepted | Rejections repaired | Wall s | Meta USD (guard) | Planner: `reorder` depends on `catalog` |
|---|---|---:|---:|---:|---:|---|
| 1 | yes | 2 of 2 | 0 | 18.0 | 0.0178 | yes |
| 2 | yes | 2 of 2 | 0 | 21.5 | 0.0222 | yes |
| 3 | yes | 2 of 2 | 0 | 21.8 | 0.0226 | yes |
| 4 | yes | 2 of 2 | 0 | 22.5 | 0.0234 | yes |
| 5 | yes | 2 of 2 | 0 | 31.1 | 0.0267 | yes |

No rejection in 10 attempts; every accepted module passed all cases (`catalog` 10 of 10, `reorder` 8 of 8). The `reorder` dependency on `catalog` is required by the manifest's `consumes`, so here the planner's choice confirms the contract rather than adding to it. Per-module generation took 5.6 to 14.4 s. In 4 of 5 runs the two modules went to different workers.

## What the numbers show

1. **Nine of ten runs completed, and the one that did not stopped on provider availability, not on model output.** The expense workload completed 4 of 5 and inventory 5 of 5. Model-side rejections were all repaired within one round.
2. **The failure scenario behaved the same in all five runs.** The cheapest worker crashed, its lease expired after 75 s and the task was reassigned every time.
3. **The same `categories` rejection recurred in 3 of 4 completed expense runs,** a `strip` call on a `None` value, which the one repair round fixed each time. It is the same diagnostic recorded for the earlier single live run.
4. **The planner made `categories` depend on `parser` in all 5 expense runs,** as in the 18 planner runs of the worker-scaling record. The inventory graph it proposed matches the manifest's required chain.
5. **A burst of provider 503s is the costliest outcome seen,** because failed calls stay charged at the reserved worst case and the requester retries the same worker at once with no delay. Four failures cost about three completed runs. The worker that received the 503s was not marked failed for the task, so the other healthy worker never received an attempt.
6. **Cost per completed run was $0.041 to $0.049 for the expense workload and $0.018 to $0.027 for inventory,** consistent with the "about $0.05 per run" figure in the runbook for the expense report.

## Limitations

- One Mac, one day, one model and one account, over about 15 minutes of wall clock. 5 runs per workload is a small sample; no statistical test was applied.
- Meta USD comes from the local budget guard's ledger, computed from reported tokens and list prices, and has not been reconciled with the Meta console. The four 503 calls are charged at their reserved worst case and may have been billed at nothing.
- The Mac was shared with other agents' work. One-minute load at the start of each run was 7.1 to 9.6 on 12 logical CPUs and is recorded per run.
- Wall seconds were measured around the demo process by the driver and include starting worker processes and enrollment; the demo's own `elapsed_seconds` is 0.6 to 0.7 s lower in each completed run.
- Series A includes a fixed 75 s lease wait in every run, so its wall time is not comparable with series B or with runs without the failure scenario.
- The runs were sequential, so the network and the API saw one job at a time. Nothing here is a scaling or throughput figure.
- These runs use the Meta provider only; the protected step and a second machine were not involved. Neither acceptance nor integration establishes confidentiality from the host.

## Machine

    {"cpu": "Apple M3 Pro", "logical_cpus": 12, "memory_gib": 36, "os": "macOS-27.0.1", "python": "3.14.4"}
