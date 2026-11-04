# Worker scaling measurement

Date: 2026-11-01. Machine, commands, ranges and raw numbers are below. The same records are in `worker_scaling_2026-11-01.jsonl`. Per-run workspaces were not kept in the repository.

## What this measures

**This is not a measurement of distributed compute.** Every worker ran as a separate process on one Mac.

- Workers on the Meta API spend almost all their time waiting on the network. Parallel workers on one Mac can wait at the same time, so a shorter wall time here says nothing about added computing power.
- Workers on a local model share one Ollama server and the same CPU and GPU. Parallel local runs contend for them and may be slower.

Samples are small and the Mac was busy with other work, so the results are ranges and are not claims of speedup.

## Results

| Config | Workers | Graph | Runs | All modules accepted | Wall s, all runs | Peak concurrent generations | Generation s per attempt, median (range) | Rejected / all attempts | Meta USD per run |
|---|---:|---|---:|---:|---|---:|---|---:|---|
| meta-1 | 1 | Meta planner | 5 | 5 | 42.5 to 65.9 | 1 | 10.8 (7.0 to 23.4) | 7 / 23 | 0.0427 to 0.0937 |
| meta-2 | 2 | Meta planner | 5 | 4 | 43.6 to 72.7 | 1 | 10.1 (5.5 to 23.0) | 11 / 23 | 0.0449 to 0.0743 |
| meta-3 | 3 | Meta planner | 5 | 5 | 45.4 to 68.0 | 1 | 11.4 (7.0 to 23.5) | 6 / 21 | 0.0434 to 0.0703 |
| meta-ref-1 | 1 | Fixed reference | 5 | 5 | 39.7 to 65.6 | 1 | 10.6 (5.9 to 21.2) | 9 / 24 | 0.0404 to 0.0717 |
| meta-ref-2 | 2 | Fixed reference | 5 | 5 | 25.7 to 32.4 | 2 | 9.4 (5.6 to 17.0) | 7 / 22 | 0.0409 to 0.0508 |
| meta-ref-3 | 3 | Fixed reference | 5 | 5 | 25.8 to 31.9 | 2 | 9.0 (1.9 to 17.9) | 5 / 20 | 0.0316 to 0.0482 |
| local-1 | 1 | Fixed reference | 8 | 5 | 10.2 to 19.7 | 1 | 3.2 (2.4 to 6.0) | 13 / 33 | 0 |
| local-2 | 2 | Fixed reference | 8 | 3 | 10.4 to 31.4 | 2 | 3.5 (2.4 to 8.5) | 23 / 41 | 0 |
| local-3 | 3 | Fixed reference | 8 | 6 | 9.6 to 23.7 | 2 | 3.6 (2.4 to 8.3) | 13 / 35 | 0 |
| mixed-2 | 1 local, 1 Meta | Meta planner | 3 | 3 | 28.8 to 41.2 | 1 | 3.6 (2.5 to 17.6) | 5 / 14 | 0.0195 to 0.0332 |

The attempt count includes the one attempt that ended in a provider failure (meta-1 run 4), which has no generation time.

## What the numbers show

1. **With the Meta planner, extra workers had nothing to do at the same time.** In all 18 measured planner runs the planner made `categories` depend on `parser`, which is a stricter graph than the contract requires. Modules then became ready one at a time, and peak concurrency was 1 with any number of workers. The wall ranges of meta-1, meta-2 and meta-3 overlap almost completely. These runs show routing across workers and do not test parallel generation.
2. **On the fixed reference graph, two Meta workers finished in a lower range than one.** The graph lets `parser` and `categories` run together. meta-ref-2 took 25.7 to 32.4 s and meta-ref-1 took 39.7 to 65.6 s, and the ranges do not overlap in these 5 runs each. The likely cause is the waiting described above: two requests were outstanding at once. Rejections also differ between runs and affect these times, so this is a small sample and not a speedup figure.
3. **A third worker showed no benefit over two.** The reference graph has at most two modules ready at once, and peak concurrency stayed at 2. meta-ref-3 took 25.8 to 31.9 s and local-3 overlaps local-2.
4. **Local runs showed no clear wall time change with more workers.** The ranges overlap: 10.2 to 19.7 s, 10.4 to 31.4 s and 9.6 to 23.7 s. Median attempt time rose slightly from 3.2 s to 3.5 and 3.6 s, and the slowest attempt rose from 6.0 s to 8.5 and 8.3 s. That fits contention, but these samples are too small to establish it. Runs that stop early are not comparable with complete runs, and only 7 of the 24 local runs were complete with no rejection (wall times: one worker 10.2, 10.4, 12.6 s; two workers 10.4 s; three workers 9.6, 10.0, 12.8 s).
5. **Local generation was faster per attempt but much less reliable.** Only 14 of 24 local runs had all modules accepted, against 14 of 15 for meta-1 to meta-3 and 15 of 15 for meta-ref-1 to meta-ref-3. The differences between local-1, local-2 and local-3 (5, 3 and 6 of 8) are within what model randomness could produce here. They should not be read as an effect of worker count.
6. **Meta modules were also rejected often.** 45 of 133 Meta-only attempts were rejected, and one run (meta-2 run 5) stopped after `parser` failed acceptance 3 times. The rejected attempts' cost is included.

## Local model

Ollama 0.40.13 from Homebrew, `llama3.2:3b`, on loopback, with `OLLAMA_NO_CLOUD=1` and other server settings left at their defaults. `llama3.1:8b` was not pulled because the 3B model produced accepted modules for every contract.

Before any hints, the existing adapter ran correctly but all its modules failed: 0 of 15 one-shot generations were accepted, and a full `--provider ollama` run stopped after 2 repairs. The adapter now sends step hints and a temperature of 0.2 to local models only. With the final prompt, one-shot acceptance was 22 of 30 (parser 8 of 10, categories 9 of 10, summary 5 of 10).

Limits of that result:

- The hints are close to pseudocode. An accepted local module shows that this model can follow a recipe, not that it can solve the contract from the specification alone.
- The hints were adjusted over several rounds while watching pass counts that include hidden cases. They contain no acceptance case, and no hidden case details were shown or sent, but the 22 of 30 is optimistic because the prompt was tuned against the same checks. Acceptance itself was not changed.
- Temperature and hints differ from the Meta path, so this is not a like-for-like model comparison.

## Mixed run

One worker on `llama3.2:3b` and one on `muse-spark-1.3` in the same job:

    python -m agent_marketplace.demo --workers ollama=llama3.2:3b,meta=muse-spark-1.3 --timeout 280

The first live run completed in 31.1 s and $0.020942 with the Meta plan. Offers from both workers were recorded on all three first attempts. Accepted: `parser` from the local worker, `categories` from the local worker after a rejected Meta attempt, and `summary` from the Meta worker. This run is not in the tables. The 3 mixed-2 runs that are in the tables also completed, with 5 modules accepted from the local worker and 4 from the Meta worker. Local rejections were 4 and Meta rejections 1.

## Limitations

- 3 to 8 runs per configuration. No statistical test was applied.
- The Mac was shared with other agents' work, including test suites. The one-minute load at the start of runs ranged from 4.9 to 24.6 on 12 logical CPUs and is recorded per run. Later batches saw less load, so comparing meta-1 to meta-3 with meta-ref-1 to meta-ref-3 is less reliable than comparing within a batch. Each batch was run round-robin to spread drift across its configurations.
- Configurations with the Meta planner include its call in wall time and cost, and those without do not.
- The Meta API's latency varied and was not controlled. Everything depended on one network path and one account.
- Wall time includes starting worker processes and enrollment, and a demo-only 0.12 s delay per attempt.
- Cold start was not measured separately. The model had probably unloaded during the roughly 15-minute Meta batch, so local-1 run 1 may include a model load. All local workers shared one Ollama server with default parallelism settings.
- Costs come from the local budget ledger, computed from reported tokens and list prices checked on 2026-11-01. The Meta console remains the source of truth. One Meta 503 (meta-1 run 4) stays charged at its reserved worst case of $0.038, because it may have been billed. That is why that run has the highest cost. Local cost excludes electricity and hardware.
- Peak concurrency is estimated from the time the requester authorized each attempt plus the generation time the worker reported.
- This uses one reference job with three small modules on public data. It does not predict behaviour for other work, other machines or a real network between workers. Neither the Meta nor the local path provides confidentiality from the host.

## Commands

    scripts/measure_workers.py --configs meta-1,meta-2,meta-3 --runs 5 --allow-billable --stop-at-usd 2.0 --workspace-root WORKSPACE_ROOT
    scripts/measure_workers.py --configs local-1,local-2,local-3 --runs 8 --workspace-root WORKSPACE_ROOT
    scripts/measure_workers.py --configs mixed-2 --runs 3 --allow-billable --stop-at-usd 0.5 --workspace-root WORKSPACE_ROOT
    scripts/measure_workers.py --configs meta-ref-1,meta-ref-2,meta-ref-3 --runs 5 --allow-billable --stop-at-usd 1.0 --workspace-root WORKSPACE_ROOT

## Machine

    {"cpu": "Apple M3 Pro", "logical_cpus": 12, "memory_gib": 36, "os": "macOS-27.0.1", "python": "3.14.4"}

## Ranges by configuration

| Config | Workers | Runs | All modules accepted | Wall s | Sum of attempt generation s | Peak concurrent | Rejected attempts | Meta USD (ledger) |
|---|---|---|---|---|---|---|---|---|
| meta-1 | 1 | 5 | 5 | 42.5 to 65.9 | 37.4 to 61.1 | 1 | 0 to 2 | 0.0427 to 0.0937 |
| meta-2 | 2 | 5 | 4 | 43.6 to 72.7 | 38.9 to 67.8 | 1 | 1 to 3 | 0.0449 to 0.0743 |
| meta-3 | 3 | 5 | 5 | 45.4 to 68.0 | 39.1 to 62.0 | 1 | 0 to 2 | 0.0434 to 0.0703 |
| local-1 | 1 | 8 | 5 | 10.2 to 19.7 | 9.0 to 18.0 | 1 | 0 to 3 | 0.0 |
| local-2 | 2 | 8 | 3 | 10.4 to 31.4 | 12.2 to 37.5 | 2 | 0 to 5 | 0.0 |
| local-3 | 3 | 8 | 6 | 9.6 to 23.7 | 12.1 to 28.1 | 2 | 0 to 5 | 0.0 |
| mixed-2 | 2 | 3 | 3 | 28.8 to 41.2 | 23.3 to 37.2 | 1 | 1 to 2 | 0.0195 to 0.0332 |
| meta-ref-1 | 1 | 5 | 5 | 39.7 to 65.6 | 38.3 to 64.1 | 1 | 0 to 3 | 0.0404 to 0.0717 |
| meta-ref-2 | 2 | 5 | 5 | 25.7 to 32.4 | 41.0 to 45.1 | 2 | 1 to 2 | 0.0409 to 0.0508 |
| meta-ref-3 | 3 | 5 | 5 | 25.8 to 31.9 | 29.9 to 45.0 | 2 | 0 to 2 | 0.0316 to 0.0482 |

## Raw numbers per run

| Config | Run | Started (UTC) | Load 1m | Outcome | Wall s | Generation s | Peak concurrent | Plan width | Modules accepted | Rejected | Attempts | Attempt tokens | Meta calls | Meta tokens | Meta USD | Plan | Stop reason |
|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| meta-1 | 1 | 2026-11-01T11:59:02 | 24.61 | accepted | 65.941 | 61.061 | 1 | 1 | 3 | 2 | 5 | 16890 | 6 | 18081 | 0.068669 | meta |  |
| meta-2 | 1 | 2026-11-01T12:00:08 | 13.06 | accepted | 46.74 | 41.624 | 1 | 1 | 3 | 2 | 5 | 11725 | 6 | 12830 | 0.047657 | meta |  |
| meta-3 | 1 | 2026-11-01T12:00:55 | 10.92 | accepted | 45.439 | 39.126 | 1 | 1 | 3 | 0 | 3 | 9837 | 4 | 11105 | 0.043404 | meta |  |
| meta-1 | 2 | 2026-11-01T12:01:40 | 11.14 | accepted | 60.813 | 55.051 | 1 | 1 | 3 | 2 | 5 | 15757 | 6 | 16852 | 0.063011 | meta |  |
| meta-2 | 2 | 2026-11-01T12:02:41 | 11.73 | accepted | 72.701 | 67.787 | 1 | 1 | 3 | 2 | 5 | 16704 | 6 | 17738 | 0.067014 | meta |  |
| meta-3 | 2 | 2026-11-01T12:03:54 | 8.19 | accepted | 60.981 | 54.802 | 1 | 1 | 3 | 1 | 4 | 14514 | 5 | 15810 | 0.061741 | meta |  |
| meta-1 | 3 | 2026-11-01T12:04:55 | 7.66 | accepted | 42.506 | 37.392 | 1 | 1 | 3 | 0 | 3 | 9562 | 4 | 10928 | 0.042652 | meta |  |
| meta-2 | 3 | 2026-11-01T12:05:37 | 7.05 | accepted | 70.061 | 63.813 | 1 | 1 | 3 | 3 | 6 | 18471 | 7 | 19855 | 0.074298 | meta |  |
| meta-3 | 3 | 2026-11-01T12:06:47 | 9.78 | accepted | 68.014 | 62.049 | 1 | 1 | 3 | 2 | 5 | 17027 | 6 | 18456 | 0.070287 | meta |  |
| meta-1 | 4 | 2026-11-01T12:07:55 | 13.0 | accepted | 57.615 | 52.081 | 1 | 1 | 3 | 1 | 5 | 13189 | 6 | 14337 | 0.093669 | meta |  |
| meta-2 | 4 | 2026-11-01T12:08:53 | 11.86 | accepted | 45.796 | 40.485 | 1 | 1 | 3 | 1 | 4 | 11296 | 5 | 12470 | 0.047682 | meta |  |
| meta-3 | 4 | 2026-11-01T12:09:39 | 11.41 | accepted | 48.4 | 43.136 | 1 | 1 | 3 | 1 | 4 | 12392 | 5 | 13551 | 0.052018 | meta |  |
| meta-1 | 5 | 2026-11-01T12:10:27 | 10.66 | accepted | 55.876 | 49.429 | 1 | 1 | 3 | 2 | 5 | 14389 | 6 | 15797 | 0.060018 | meta |  |
| meta-2 | 5 | 2026-11-01T12:11:23 | 14.23 | failed | 43.568 | 38.883 | 1 | 1 | 0 | 3 | 3 | 11229 | 4 | 12365 | 0.044943 | meta | Module parser failed acceptance after 2 repairs; stopping to limit cost |
| meta-3 | 5 | 2026-11-01T12:12:07 | 17.42 | accepted | 57.268 | 52.217 | 1 | 1 | 3 | 2 | 5 | 14196 | 6 | 15333 | 0.057209 | meta |  |
| local-1 | 1 | 2026-11-01T12:13:20 | 11.56 | accepted | 12.622 | 11.498 | 1 | 2 | 3 | 0 | 3 | 1841 | 0 | 0 | 0.0 | reference |  |
| local-2 | 1 | 2026-11-01T12:13:33 | 11.17 | failed | 15.246 | 16.816 | 2 | 2 | 2 | 3 | 5 | 3416 | 0 | 0 | 0.0 | reference | Module summary failed acceptance after 2 repairs; stopping to limit cost |
| local-3 | 1 | 2026-11-01T12:13:48 | 10.37 | failed | 23.726 | 28.052 | 2 | 2 | 2 | 5 | 7 | 5247 | 0 | 0 | 0.0 | reference | Module summary failed acceptance after 2 repairs; stopping to limit cost |
| local-1 | 2 | 2026-11-01T12:14:12 | 9.94 | accepted | 10.201 | 9.031 | 1 | 2 | 3 | 0 | 3 | 1840 | 0 | 0 | 0.0 | reference |  |
| local-2 | 2 | 2026-11-01T12:14:22 | 10.18 | accepted | 16.086 | 22.106 | 2 | 2 | 3 | 1 | 4 | 2803 | 0 | 0 | 0.0 | reference |  |
| local-3 | 2 | 2026-11-01T12:14:38 | 11.86 | accepted | 18.83 | 20.156 | 2 | 2 | 3 | 2 | 5 | 3723 | 0 | 0 | 0.0 | reference |  |
| local-1 | 3 | 2026-11-01T12:14:57 | 12.12 | accepted | 19.226 | 17.712 | 1 | 2 | 3 | 2 | 5 | 3763 | 0 | 0 | 0.0 | reference |  |
| local-2 | 3 | 2026-11-01T12:15:16 | 10.5 | failed | 23.539 | 27.701 | 2 | 2 | 2 | 5 | 7 | 5289 | 0 | 0 | 0.0 | reference | Module summary failed acceptance after 2 repairs; stopping to limit cost |
| local-3 | 3 | 2026-11-01T12:15:39 | 10.66 | failed | 15.861 | 18.14 | 2 | 2 | 2 | 3 | 5 | 3434 | 0 | 0 | 0.0 | reference | Module summary failed acceptance after 2 repairs; stopping to limit cost |
| local-1 | 4 | 2026-11-01T12:15:55 | 9.48 | accepted | 16.915 | 15.367 | 1 | 2 | 3 | 2 | 5 | 3866 | 0 | 0 | 0.0 | reference |  |
| local-2 | 4 | 2026-11-01T12:16:12 | 8.32 | accepted | 14.363 | 16.719 | 2 | 2 | 3 | 1 | 4 | 2966 | 0 | 0 | 0.0 | reference |  |
| local-3 | 4 | 2026-11-01T12:16:27 | 8.09 | accepted | 9.631 | 12.104 | 2 | 2 | 3 | 0 | 3 | 1841 | 0 | 0 | 0.0 | reference |  |
| local-1 | 5 | 2026-11-01T12:16:36 | 7.99 | failed | 14.117 | 12.892 | 1 | 2 | 1 | 3 | 4 | 2923 | 0 | 0 | 0.0 | reference | Module categories failed acceptance after 2 repairs; stopping to limit cost |
| local-2 | 5 | 2026-11-01T12:16:50 | 7.81 | failed | 24.236 | 29.125 | 2 | 2 | 2 | 5 | 7 | 5328 | 0 | 0 | 0.0 | reference | Module summary failed acceptance after 2 repairs; stopping to limit cost |
| local-3 | 5 | 2026-11-01T12:17:15 | 7.9 | accepted | 13.176 | 15.704 | 2 | 2 | 3 | 1 | 4 | 2616 | 0 | 0 | 0.0 | reference |  |
| local-1 | 6 | 2026-11-01T12:17:28 | 7.7 | accepted | 10.405 | 9.286 | 1 | 2 | 3 | 0 | 3 | 1841 | 0 | 0 | 0.0 | reference |  |
| local-2 | 6 | 2026-11-01T12:17:38 | 7.82 | accepted | 10.426 | 12.234 | 2 | 2 | 3 | 0 | 3 | 1841 | 0 | 0 | 0.0 | reference |  |
| local-3 | 6 | 2026-11-01T12:17:49 | 8.23 | accepted | 9.992 | 12.412 | 2 | 2 | 3 | 0 | 3 | 1840 | 0 | 0 | 0.0 | reference |  |
| local-1 | 7 | 2026-11-01T12:17:59 | 7.81 | failed | 16.346 | 14.901 | 1 | 2 | 2 | 3 | 5 | 3219 | 0 | 0 | 0.0 | reference | Module summary failed acceptance after 2 repairs; stopping to limit cost |
| local-2 | 7 | 2026-11-01T12:18:15 | 8.06 | failed | 14.066 | 16.359 | 2 | 2 | 1 | 3 | 4 | 3393 | 0 | 0 | 0.0 | reference | Module categories failed acceptance after 2 repairs; stopping to limit cost |
| local-3 | 7 | 2026-11-01T12:18:29 | 7.73 | accepted | 18.951 | 23.936 | 2 | 2 | 3 | 2 | 5 | 3723 | 0 | 0 | 0.0 | reference |  |
| local-1 | 8 | 2026-11-01T12:18:48 | 7.84 | failed | 19.727 | 18.028 | 1 | 2 | 2 | 3 | 5 | 3412 | 0 | 0 | 0.0 | reference | Module summary failed acceptance after 2 repairs; stopping to limit cost |
| local-2 | 8 | 2026-11-01T12:19:08 | 10.5 | failed | 31.384 | 37.542 | 2 | 2 | 2 | 5 | 7 | 5348 | 0 | 0 | 0.0 | reference | Module summary failed acceptance after 2 repairs; stopping to limit cost |
| local-3 | 8 | 2026-11-01T12:19:39 | 15.58 | accepted | 12.833 | 16.353 | 2 | 2 | 3 | 0 | 3 | 1848 | 0 | 0 | 0.0 | reference |  |
| mixed-2 | 1 | 2026-11-01T12:20:02 | 17.34 | accepted | 31.125 | 25.383 | 1 | 1 | 3 | 2 | 5 | 6606 | 3 | 5786 | 0.019488 | meta |  |
| mixed-2 | 2 | 2026-11-01T12:20:34 | 13.21 | accepted | 28.759 | 23.312 | 1 | 1 | 3 | 1 | 4 | 5969 | 2 | 5387 | 0.020045 | meta |  |
| mixed-2 | 3 | 2026-11-01T12:21:02 | 10.88 | accepted | 41.23 | 37.171 | 1 | 1 | 3 | 2 | 5 | 9732 | 3 | 8673 | 0.033173 | meta |  |
| meta-ref-1 | 1 | 2026-11-01T12:23:43 | 11.57 | accepted | 62.586 | 61.065 | 1 | 2 | 3 | 2 | 5 | 15713 | 5 | 15713 | 0.058992 | reference |  |
| meta-ref-2 | 1 | 2026-11-01T12:24:45 | 7.5 | accepted | 30.296 | 44.185 | 2 | 2 | 3 | 2 | 5 | 13371 | 5 | 13371 | 0.049534 | reference |  |
| meta-ref-3 | 1 | 2026-11-01T12:25:16 | 6.29 | accepted | 25.832 | 29.873 | 2 | 2 | 3 | 0 | 3 | 8027 | 3 | 8027 | 0.03161 | reference |  |
| meta-ref-1 | 2 | 2026-11-01T12:25:42 | 6.04 | accepted | 39.662 | 38.275 | 1 | 2 | 3 | 0 | 3 | 10102 | 3 | 10102 | 0.040428 | reference |  |
| meta-ref-2 | 2 | 2026-11-01T12:26:21 | 6.57 | accepted | 29.898 | 45.071 | 2 | 2 | 3 | 1 | 4 | 12507 | 4 | 12507 | 0.049123 | reference |  |
| meta-ref-3 | 2 | 2026-11-01T12:26:51 | 6.13 | accepted | 28.818 | 43.759 | 2 | 2 | 3 | 2 | 5 | 12573 | 5 | 12573 | 0.047534 | reference |  |
| meta-ref-1 | 3 | 2026-11-01T12:27:20 | 5.63 | accepted | 52.701 | 51.1 | 1 | 2 | 3 | 2 | 5 | 14495 | 5 | 14495 | 0.054959 | reference |  |
| meta-ref-2 | 3 | 2026-11-01T12:28:13 | 5.56 | accepted | 30.868 | 42.813 | 2 | 2 | 3 | 1 | 4 | 11351 | 4 | 11351 | 0.044057 | reference |  |
| meta-ref-3 | 3 | 2026-11-01T12:28:44 | 5.26 | accepted | 31.94 | 45.009 | 2 | 2 | 3 | 1 | 4 | 12032 | 4 | 12032 | 0.046984 | reference |  |
| meta-ref-1 | 4 | 2026-11-01T12:29:16 | 5.33 | accepted | 65.632 | 64.071 | 1 | 2 | 3 | 2 | 5 | 18393 | 5 | 18393 | 0.070331 | reference |  |
| meta-ref-2 | 4 | 2026-11-01T12:30:21 | 4.93 | accepted | 25.717 | 40.966 | 2 | 2 | 3 | 1 | 4 | 10570 | 4 | 10570 | 0.040861 | reference |  |
| meta-ref-3 | 4 | 2026-11-01T12:30:47 | 5.32 | accepted | 29.596 | 43.806 | 2 | 2 | 3 | 1 | 4 | 12443 | 4 | 12443 | 0.048185 | reference |  |
| meta-ref-1 | 5 | 2026-11-01T12:31:16 | 5.11 | accepted | 65.167 | 63.413 | 1 | 2 | 3 | 3 | 6 | 18964 | 6 | 18964 | 0.071693 | reference |  |
| meta-ref-2 | 5 | 2026-11-01T12:32:22 | 5.65 | accepted | 32.411 | 44.897 | 2 | 2 | 3 | 2 | 5 | 13300 | 5 | 13300 | 0.050765 | reference |  |
| meta-ref-3 | 5 | 2026-11-01T12:32:54 | 6.0 | accepted | 27.052 | 43.164 | 2 | 2 | 3 | 1 | 4 | 11674 | 4 | 11674 | 0.045534 | reference |  |

## Raw numbers per attempt

| Config | Run | Module | Worker | Provider | Model | Repair round | Status | Generation ms | Tokens in/out | USD | Cases passed |
|---|---|---|---|---|---|---:|---|---:|---:|---:|---:|
| meta-1 | 1 | parser | worker-a | meta | muse-spark-1.3 | 0 | rejected | 23390 | 293/5681 | 0.02451 | 0/11 |
| meta-1 | 1 | parser | worker-a | meta | muse-spark-1.3 | 1 | accepted | 9095 | 968/2350 | 0.0112 | 11/11 |
| meta-1 | 1 | categories | worker-a | meta | muse-spark-1.3 | 0 | rejected | 9208 | 274/2122 | 0.00936 | 0/2 |
| meta-1 | 1 | categories | worker-a | meta | muse-spark-1.3 | 1 | accepted | 7326 | 493/1717 | 0.00791 | 2/2 |
| meta-1 | 1 | summary | worker-a | meta | muse-spark-1.3 | 0 | accepted | 12042 | 268/2724 | 0.01191 | 5/5 |
| meta-2 | 1 | parser | worker-a | meta | muse-spark-1.3 | 0 | accepted | 13196 | 293/3173 | 0.01385 | 11/11 |
| meta-2 | 1 | categories | worker-b | meta | muse-spark-1.3 | 0 | rejected | 5820 | 274/1623 | 0.00724 | 0/2 |
| meta-2 | 1 | categories | worker-a | meta | muse-spark-1.3 | 1 | rejected | 7391 | 532/1774 | 0.0082 | 0/2 |
| meta-2 | 1 | categories | worker-b | meta | muse-spark-1.3 | 2 | accepted | 5503 | 494/1097 | 0.00528 | 2/2 |
| meta-2 | 1 | summary | worker-a | meta | muse-spark-1.3 | 0 | accepted | 9714 | 268/2197 | 0.00967 | 5/5 |
| meta-3 | 1 | parser | worker-a | meta | muse-spark-1.3 | 0 | accepted | 19262 | 293/4342 | 0.01882 | 11/11 |
| meta-3 | 1 | categories | worker-b | meta | muse-spark-1.3 | 0 | accepted | 7997 | 274/1910 | 0.00846 | 2/2 |
| meta-3 | 1 | summary | worker-c | meta | muse-spark-1.3 | 0 | accepted | 11867 | 268/2750 | 0.01202 | 5/5 |
| meta-1 | 2 | parser | worker-a | meta | muse-spark-1.3 | 0 | rejected | 17670 | 293/4524 | 0.01959 | 0/11 |
| meta-1 | 2 | parser | worker-a | meta | muse-spark-1.3 | 1 | accepted | 10656 | 1018/2460 | 0.01173 | 11/11 |
| meta-1 | 2 | categories | worker-a | meta | muse-spark-1.3 | 0 | rejected | 6967 | 274/1964 | 0.00869 | 0/2 |
| meta-1 | 2 | categories | worker-a | meta | muse-spark-1.3 | 1 | accepted | 12011 | 588/2762 | 0.01247 | 2/2 |
| meta-1 | 2 | summary | worker-a | meta | muse-spark-1.3 | 0 | accepted | 7747 | 268/1606 | 0.00716 | 5/5 |
| meta-2 | 2 | parser | worker-a | meta | muse-spark-1.3 | 0 | rejected | 22987 | 293/4750 | 0.02055 | 0/11 |
| meta-2 | 2 | parser | worker-b | meta | muse-spark-1.3 | 1 | accepted | 17066 | 1030/3771 | 0.01731 | 11/11 |
| meta-2 | 2 | categories | worker-a | meta | muse-spark-1.3 | 0 | rejected | 8155 | 274/1744 | 0.00775 | 0/2 |
| meta-2 | 2 | categories | worker-b | meta | muse-spark-1.3 | 1 | accepted | 8176 | 497/1614 | 0.00748 | 2/2 |
| meta-2 | 2 | summary | worker-b | meta | muse-spark-1.3 | 0 | accepted | 11403 | 268/2463 | 0.0108 | 5/5 |
| meta-3 | 2 | parser | worker-a | meta | muse-spark-1.3 | 0 | accepted | 19371 | 293/4516 | 0.01956 | 11/11 |
| meta-3 | 2 | categories | worker-b | meta | muse-spark-1.3 | 0 | rejected | 12550 | 274/2928 | 0.01279 | 0/2 |
| meta-3 | 2 | categories | worker-c | meta | muse-spark-1.3 | 1 | accepted | 11496 | 553/2694 | 0.01214 | 2/2 |
| meta-3 | 2 | summary | worker-a | meta | muse-spark-1.3 | 0 | accepted | 11385 | 268/2988 | 0.01303 | 5/5 |
| meta-1 | 3 | parser | worker-a | meta | muse-spark-1.3 | 0 | accepted | 20490 | 293/4641 | 0.02009 | 11/11 |
| meta-1 | 3 | categories | worker-a | meta | muse-spark-1.3 | 0 | accepted | 7858 | 274/1799 | 0.00799 | 2/2 |
| meta-1 | 3 | summary | worker-a | meta | muse-spark-1.3 | 0 | accepted | 9044 | 268/2287 | 0.01005 | 5/5 |
| meta-2 | 3 | parser | worker-a | meta | muse-spark-1.3 | 0 | rejected | 15691 | 293/3988 | 0.01732 | 0/11 |
| meta-2 | 3 | parser | worker-b | meta | muse-spark-1.3 | 1 | accepted | 10218 | 939/2308 | 0.01098 | 11/11 |
| meta-2 | 3 | categories | worker-a | meta | muse-spark-1.3 | 0 | rejected | 9273 | 274/2380 | 0.01046 | 0/2 |
| meta-2 | 3 | categories | worker-b | meta | muse-spark-1.3 | 1 | accepted | 8297 | 618/2116 | 0.00977 | 2/2 |
| meta-2 | 3 | summary | worker-a | meta | muse-spark-1.3 | 0 | rejected | 10439 | 268/2614 | 0.01144 | 0/5 |
| meta-2 | 3 | summary | worker-b | meta | muse-spark-1.3 | 1 | accepted | 9895 | 541/2132 | 0.00974 | 5/5 |
| meta-3 | 3 | parser | worker-a | meta | muse-spark-1.3 | 0 | rejected | 23458 | 293/5639 | 0.02433 | 0/11 |
| meta-3 | 3 | parser | worker-b | meta | muse-spark-1.3 | 1 | accepted | 10051 | 878/2272 | 0.01075 | 11/11 |
| meta-3 | 3 | categories | worker-a | meta | muse-spark-1.3 | 0 | rejected | 7113 | 274/1591 | 0.0071 | 0/2 |
| meta-3 | 3 | categories | worker-c | meta | muse-spark-1.3 | 1 | accepted | 12489 | 575/3084 | 0.01383 | 2/2 |
| meta-3 | 3 | summary | worker-a | meta | muse-spark-1.3 | 0 | accepted | 8938 | 268/2153 | 0.00949 | 5/5 |
| meta-1 | 4 | parser | worker-a | meta | muse-spark-1.3 | 0 | accepted | 17593 | 293/4288 | 0.01859 | 11/11 |
| meta-1 | 4 | categories | worker-a | meta | muse-spark-1.3 | 0 | rejected | 12497 | 274/2747 | 0.01202 | 0/2 |
| meta-1 | 4 | categories | worker-a | n/a | n/a | 1 | failed | n/a | n/a | n/a | n/a |
| meta-1 | 4 | categories | worker-a | meta | muse-spark-1.3 | 1 | accepted | 11000 | 504/2421 | 0.01092 | 2/2 |
| meta-1 | 4 | summary | worker-a | meta | muse-spark-1.3 | 0 | accepted | 10991 | 268/2394 | 0.01051 | 5/5 |
| meta-2 | 4 | parser | worker-a | meta | muse-spark-1.3 | 0 | accepted | 12495 | 293/3005 | 0.01314 | 11/11 |
| meta-2 | 4 | categories | worker-b | meta | muse-spark-1.3 | 0 | rejected | 7926 | 274/1856 | 0.00823 | 0/2 |
| meta-2 | 4 | categories | worker-a | meta | muse-spark-1.3 | 1 | accepted | 10127 | 508/2576 | 0.01158 | 2/2 |
| meta-2 | 4 | summary | worker-b | meta | muse-spark-1.3 | 0 | accepted | 9937 | 268/2516 | 0.01103 | 5/5 |
| meta-3 | 4 | parser | worker-a | meta | muse-spark-1.3 | 0 | accepted | 14064 | 293/3888 | 0.01689 | 11/11 |
| meta-3 | 4 | categories | worker-b | meta | muse-spark-1.3 | 0 | rejected | 11231 | 274/2717 | 0.01189 | 0/2 |
| meta-3 | 4 | categories | worker-c | meta | muse-spark-1.3 | 1 | accepted | 9722 | 594/2440 | 0.01111 | 2/2 |
| meta-3 | 4 | summary | worker-b | meta | muse-spark-1.3 | 0 | accepted | 8119 | 268/1918 | 0.00849 | 5/5 |
| meta-1 | 5 | parser | worker-a | meta | muse-spark-1.3 | 0 | rejected | 14695 | 293/4084 | 0.01772 | 0/11 |
| meta-1 | 5 | parser | worker-a | meta | muse-spark-1.3 | 1 | accepted | 11897 | 620/2957 | 0.01334 | 11/11 |
| meta-1 | 5 | categories | worker-a | meta | muse-spark-1.3 | 0 | rejected | 7019 | 274/1559 | 0.00697 | 0/2 |
| meta-1 | 5 | categories | worker-a | meta | muse-spark-1.3 | 1 | accepted | 7752 | 489/2011 | 0.00916 | 2/2 |
| meta-1 | 5 | summary | worker-a | meta | muse-spark-1.3 | 0 | accepted | 8066 | 268/1834 | 0.00813 | 5/5 |
| meta-2 | 5 | parser | worker-a | meta | muse-spark-1.3 | 0 | rejected | 15796 | 293/3654 | 0.0159 | 0/11 |
| meta-2 | 5 | parser | worker-b | meta | muse-spark-1.3 | 1 | rejected | 11363 | 916/2579 | 0.01211 | 3/11 |
| meta-2 | 5 | parser | worker-a | meta | muse-spark-1.3 | 2 | rejected | 11724 | 898/2889 | 0.0134 | 3/11 |
| meta-3 | 5 | parser | worker-a | meta | muse-spark-1.3 | 0 | rejected | 17109 | 293/3932 | 0.01708 | 0/11 |
| meta-3 | 5 | parser | worker-b | meta | muse-spark-1.3 | 1 | accepted | 8478 | 862/1720 | 0.00839 | 11/11 |
| meta-3 | 5 | categories | worker-c | meta | muse-spark-1.3 | 0 | rejected | 7025 | 274/1674 | 0.00746 | 0/2 |
| meta-3 | 5 | categories | worker-a | meta | muse-spark-1.3 | 1 | accepted | 11983 | 526/2712 | 0.01218 | 2/2 |
| meta-3 | 5 | summary | worker-c | meta | muse-spark-1.3 | 0 | accepted | 7622 | 268/1935 | 0.00856 | 5/5 |
| local-1 | 1 | parser | worker-a | ollama | llama3.2:3b | 0 | accepted | 5989 | 530/194 | n/a | 11/11 |
| local-1 | 1 | categories | worker-a | ollama | llama3.2:3b | 0 | accepted | 2913 | 452/142 | n/a | 2/2 |
| local-1 | 1 | summary | worker-a | ollama | llama3.2:3b | 0 | accepted | 2596 | 395/128 | n/a | 5/5 |
| local-2 | 1 | parser | worker-a | ollama | llama3.2:3b | 0 | accepted | 6377 | 530/194 | n/a | 11/11 |
| local-2 | 1 | categories | worker-b | ollama | llama3.2:3b | 0 | accepted | 2818 | 452/151 | n/a | 2/2 |
| local-2 | 1 | summary | worker-a | ollama | llama3.2:3b | 0 | rejected | 2412 | 395/126 | n/a | 0/5 |
| local-2 | 1 | summary | worker-b | ollama | llama3.2:3b | 1 | rejected | 2824 | 658/126 | n/a | 0/5 |
| local-2 | 1 | summary | worker-a | ollama | llama3.2:3b | 2 | rejected | 2385 | 658/126 | n/a | 0/5 |
| local-3 | 1 | parser | worker-a | ollama | llama3.2:3b | 0 | rejected | 3582 | 530/194 | n/a | 9/11 |
| local-3 | 1 | categories | worker-b | ollama | llama3.2:3b | 0 | accepted | 6042 | 452/142 | n/a | 2/2 |
| local-3 | 1 | parser | worker-c | ollama | llama3.2:3b | 1 | rejected | 6497 | 756/195 | n/a | 8/11 |
| local-3 | 1 | parser | worker-b | ollama | llama3.2:3b | 2 | accepted | 4206 | 757/199 | n/a | 11/11 |
| local-3 | 1 | summary | worker-a | ollama | llama3.2:3b | 0 | rejected | 2528 | 395/123 | n/a | 0/5 |
| local-3 | 1 | summary | worker-c | ollama | llama3.2:3b | 1 | rejected | 2836 | 629/123 | n/a | 0/5 |
| local-3 | 1 | summary | worker-b | ollama | llama3.2:3b | 2 | rejected | 2361 | 629/123 | n/a | 0/5 |
| local-1 | 2 | parser | worker-a | ollama | llama3.2:3b | 0 | accepted | 3838 | 530/194 | n/a | 11/11 |
| local-1 | 2 | categories | worker-a | ollama | llama3.2:3b | 0 | accepted | 2602 | 452/141 | n/a | 2/2 |
| local-1 | 2 | summary | worker-a | ollama | llama3.2:3b | 0 | accepted | 2591 | 395/128 | n/a | 5/5 |
| local-2 | 2 | parser | worker-a | ollama | llama3.2:3b | 0 | rejected | 4169 | 530/197 | n/a | 9/11 |
| local-2 | 2 | categories | worker-b | ollama | llama3.2:3b | 0 | accepted | 7408 | 452/141 | n/a | 2/2 |
| local-2 | 2 | parser | worker-a | ollama | llama3.2:3b | 1 | accepted | 7915 | 759/202 | n/a | 11/11 |
| local-2 | 2 | summary | worker-a | ollama | llama3.2:3b | 0 | accepted | 2614 | 395/127 | n/a | 5/5 |
| local-3 | 2 | parser | worker-a | ollama | llama3.2:3b | 0 | rejected | 6810 | 530/189 | n/a | 9/11 |
| local-3 | 2 | categories | worker-b | ollama | llama3.2:3b | 0 | accepted | 2729 | 452/142 | n/a | 2/2 |
| local-3 | 2 | parser | worker-c | ollama | llama3.2:3b | 1 | rejected | 4127 | 751/190 | n/a | 8/11 |
| local-3 | 2 | parser | worker-b | ollama | llama3.2:3b | 2 | accepted | 4092 | 752/194 | n/a | 11/11 |
| local-3 | 2 | summary | worker-b | ollama | llama3.2:3b | 0 | accepted | 2398 | 395/128 | n/a | 5/5 |
| local-1 | 3 | parser | worker-a | ollama | llama3.2:3b | 0 | rejected | 3912 | 530/197 | n/a | 9/11 |
| local-1 | 3 | categories | worker-a | ollama | llama3.2:3b | 0 | accepted | 2938 | 452/142 | n/a | 2/2 |
| local-1 | 3 | parser | worker-a | ollama | llama3.2:3b | 1 | rejected | 4353 | 759/198 | n/a | 8/11 |
| local-1 | 3 | parser | worker-a | ollama | llama3.2:3b | 2 | accepted | 4136 | 760/202 | n/a | 11/11 |
| local-1 | 3 | summary | worker-a | ollama | llama3.2:3b | 0 | accepted | 2373 | 395/128 | n/a | 5/5 |
| local-2 | 3 | parser | worker-a | ollama | llama3.2:3b | 0 | rejected | 3517 | 530/189 | n/a | 9/11 |
| local-2 | 3 | categories | worker-b | ollama | llama3.2:3b | 0 | accepted | 5988 | 452/142 | n/a | 2/2 |
| local-2 | 3 | parser | worker-a | ollama | llama3.2:3b | 1 | rejected | 6361 | 751/190 | n/a | 8/11 |
| local-2 | 3 | parser | worker-b | ollama | llama3.2:3b | 2 | accepted | 4003 | 752/194 | n/a | 11/11 |
| local-2 | 3 | summary | worker-b | ollama | llama3.2:3b | 0 | rejected | 2485 | 395/126 | n/a | 0/5 |
| local-2 | 3 | summary | worker-a | ollama | llama3.2:3b | 1 | rejected | 2878 | 658/126 | n/a | 0/5 |
| local-2 | 3 | summary | worker-a | ollama | llama3.2:3b | 2 | rejected | 2469 | 658/126 | n/a | 0/5 |
| local-3 | 3 | parser | worker-a | ollama | llama3.2:3b | 0 | accepted | 3707 | 530/202 | n/a | 11/11 |
| local-3 | 3 | categories | worker-b | ollama | llama3.2:3b | 0 | accepted | 6597 | 452/156 | n/a | 2/2 |
| local-3 | 3 | summary | worker-b | ollama | llama3.2:3b | 0 | rejected | 2599 | 395/127 | n/a | 0/5 |
| local-3 | 3 | summary | worker-c | ollama | llama3.2:3b | 1 | rejected | 2852 | 659/127 | n/a | 0/5 |
| local-3 | 3 | summary | worker-a | ollama | llama3.2:3b | 2 | rejected | 2385 | 659/127 | n/a | 0/5 |
| local-1 | 4 | parser | worker-a | ollama | llama3.2:3b | 0 | accepted | 3885 | 530/202 | n/a | 11/11 |
| local-1 | 4 | categories | worker-a | ollama | llama3.2:3b | 0 | rejected | 2464 | 452/128 | n/a | 0/2 |
| local-1 | 4 | categories | worker-a | ollama | llama3.2:3b | 1 | rejected | 3166 | 884/128 | n/a | 0/2 |
| local-1 | 4 | categories | worker-a | ollama | llama3.2:3b | 2 | accepted | 3225 | 884/135 | n/a | 2/2 |
| local-1 | 4 | summary | worker-a | ollama | llama3.2:3b | 0 | accepted | 2627 | 395/128 | n/a | 5/5 |
| local-2 | 4 | parser | worker-a | ollama | llama3.2:3b | 0 | accepted | 3624 | 530/194 | n/a | 11/11 |
| local-2 | 4 | categories | worker-b | ollama | llama3.2:3b | 0 | rejected | 6891 | 452/184 | n/a | 0/2 |
| local-2 | 4 | categories | worker-a | ollama | llama3.2:3b | 1 | accepted | 3486 | 928/142 | n/a | 2/2 |
| local-2 | 4 | summary | worker-b | ollama | llama3.2:3b | 0 | accepted | 2718 | 395/141 | n/a | 5/5 |
| local-3 | 4 | parser | worker-a | ollama | llama3.2:3b | 0 | accepted | 3564 | 530/194 | n/a | 11/11 |
| local-3 | 4 | categories | worker-b | ollama | llama3.2:3b | 0 | accepted | 6152 | 452/142 | n/a | 2/2 |
| local-3 | 4 | summary | worker-b | ollama | llama3.2:3b | 0 | accepted | 2388 | 395/128 | n/a | 5/5 |
| local-1 | 5 | parser | worker-a | ollama | llama3.2:3b | 0 | accepted | 3646 | 530/199 | n/a | 11/11 |
| local-1 | 5 | categories | worker-a | ollama | llama3.2:3b | 0 | rejected | 2771 | 452/150 | n/a | 0/2 |
| local-1 | 5 | categories | worker-a | ollama | llama3.2:3b | 1 | rejected | 3253 | 641/153 | n/a | 0/2 |
| local-1 | 5 | categories | worker-a | ollama | llama3.2:3b | 2 | rejected | 3222 | 645/153 | n/a | 0/2 |
| local-2 | 5 | parser | worker-a | ollama | llama3.2:3b | 0 | rejected | 3666 | 530/194 | n/a | 9/11 |
| local-2 | 5 | categories | worker-b | ollama | llama3.2:3b | 0 | accepted | 6597 | 452/142 | n/a | 2/2 |
| local-2 | 5 | parser | worker-a | ollama | llama3.2:3b | 1 | rejected | 6860 | 756/195 | n/a | 8/11 |
| local-2 | 5 | parser | worker-b | ollama | llama3.2:3b | 2 | accepted | 4144 | 757/199 | n/a | 11/11 |
| local-2 | 5 | summary | worker-a | ollama | llama3.2:3b | 0 | rejected | 2410 | 395/127 | n/a | 0/5 |
| local-2 | 5 | summary | worker-b | ollama | llama3.2:3b | 1 | rejected | 2911 | 659/127 | n/a | 0/5 |
| local-2 | 5 | summary | worker-a | ollama | llama3.2:3b | 2 | rejected | 2537 | 659/136 | n/a | 0/5 |
| local-3 | 5 | parser | worker-a | ollama | llama3.2:3b | 0 | accepted | 3693 | 530/194 | n/a | 11/11 |
| local-3 | 5 | categories | worker-b | ollama | llama3.2:3b | 0 | rejected | 6295 | 452/141 | n/a | 0/2 |
| local-3 | 5 | categories | worker-c | ollama | llama3.2:3b | 1 | accepted | 3131 | 635/142 | n/a | 2/2 |
| local-3 | 5 | summary | worker-c | ollama | llama3.2:3b | 0 | accepted | 2585 | 395/127 | n/a | 5/5 |
| local-1 | 6 | parser | worker-a | ollama | llama3.2:3b | 0 | accepted | 3807 | 530/194 | n/a | 11/11 |
| local-1 | 6 | categories | worker-a | ollama | llama3.2:3b | 0 | accepted | 2777 | 452/142 | n/a | 2/2 |
| local-1 | 6 | summary | worker-a | ollama | llama3.2:3b | 0 | accepted | 2702 | 395/128 | n/a | 5/5 |
| local-2 | 6 | parser | worker-a | ollama | llama3.2:3b | 0 | accepted | 6861 | 530/194 | n/a | 11/11 |
| local-2 | 6 | categories | worker-b | ollama | llama3.2:3b | 0 | accepted | 3009 | 452/142 | n/a | 2/2 |
| local-2 | 6 | summary | worker-a | ollama | llama3.2:3b | 0 | accepted | 2364 | 395/128 | n/a | 5/5 |
| local-3 | 6 | parser | worker-a | ollama | llama3.2:3b | 0 | accepted | 3634 | 530/194 | n/a | 11/11 |
| local-3 | 6 | categories | worker-b | ollama | llama3.2:3b | 0 | accepted | 6182 | 452/142 | n/a | 2/2 |
| local-3 | 6 | summary | worker-c | ollama | llama3.2:3b | 0 | accepted | 2596 | 395/127 | n/a | 5/5 |
| local-1 | 7 | parser | worker-a | ollama | llama3.2:3b | 0 | accepted | 3824 | 530/194 | n/a | 11/11 |
| local-1 | 7 | categories | worker-a | ollama | llama3.2:3b | 0 | accepted | 2863 | 452/142 | n/a | 2/2 |
| local-1 | 7 | summary | worker-a | ollama | llama3.2:3b | 0 | rejected | 2447 | 395/116 | n/a | 0/5 |
| local-1 | 7 | summary | worker-a | ollama | llama3.2:3b | 1 | rejected | 2845 | 549/136 | n/a | 0/5 |
| local-1 | 7 | summary | worker-a | ollama | llama3.2:3b | 2 | rejected | 2922 | 569/136 | n/a | 0/5 |
| local-2 | 7 | parser | worker-a | ollama | llama3.2:3b | 0 | accepted | 3536 | 530/194 | n/a | 11/11 |
| local-2 | 7 | categories | worker-b | ollama | llama3.2:3b | 0 | rejected | 5996 | 452/141 | n/a | 0/2 |
| local-2 | 7 | categories | worker-a | ollama | llama3.2:3b | 1 | rejected | 3434 | 897/141 | n/a | 0/2 |
| local-2 | 7 | categories | worker-b | ollama | llama3.2:3b | 2 | rejected | 3393 | 897/141 | n/a | 0/2 |
| local-3 | 7 | parser | worker-a | ollama | llama3.2:3b | 0 | rejected | 3586 | 530/189 | n/a | 9/11 |
| local-3 | 7 | categories | worker-b | ollama | llama3.2:3b | 0 | accepted | 6337 | 452/142 | n/a | 2/2 |
| local-3 | 7 | parser | worker-c | ollama | llama3.2:3b | 1 | rejected | 6947 | 751/190 | n/a | 8/11 |
| local-3 | 7 | parser | worker-b | ollama | llama3.2:3b | 2 | accepted | 4391 | 752/194 | n/a | 11/11 |
| local-3 | 7 | summary | worker-a | ollama | llama3.2:3b | 0 | accepted | 2675 | 395/128 | n/a | 5/5 |
| local-1 | 8 | parser | worker-a | ollama | llama3.2:3b | 0 | accepted | 4380 | 530/194 | n/a | 11/11 |
| local-1 | 8 | categories | worker-a | ollama | llama3.2:3b | 0 | accepted | 3570 | 452/142 | n/a | 2/2 |
| local-1 | 8 | summary | worker-a | ollama | llama3.2:3b | 0 | rejected | 3173 | 395/127 | n/a | 0/5 |
| local-1 | 8 | summary | worker-a | ollama | llama3.2:3b | 1 | rejected | 3691 | 659/127 | n/a | 0/5 |
| local-1 | 8 | summary | worker-a | ollama | llama3.2:3b | 2 | rejected | 3214 | 659/127 | n/a | 0/5 |
| local-2 | 8 | parser | worker-a | ollama | llama3.2:3b | 0 | rejected | 4958 | 530/201 | n/a | 9/11 |
| local-2 | 8 | categories | worker-b | ollama | llama3.2:3b | 0 | accepted | 8249 | 452/141 | n/a | 2/2 |
| local-2 | 8 | parser | worker-a | ollama | llama3.2:3b | 1 | rejected | 8456 | 763/202 | n/a | 8/11 |
| local-2 | 8 | parser | worker-b | ollama | llama3.2:3b | 2 | accepted | 5650 | 764/206 | n/a | 11/11 |
| local-2 | 8 | summary | worker-b | ollama | llama3.2:3b | 0 | rejected | 3359 | 395/126 | n/a | 0/5 |
| local-2 | 8 | summary | worker-a | ollama | llama3.2:3b | 1 | rejected | 3617 | 658/126 | n/a | 0/5 |
| local-2 | 8 | summary | worker-a | ollama | llama3.2:3b | 2 | rejected | 3253 | 658/126 | n/a | 0/5 |
| local-3 | 8 | parser | worker-a | ollama | llama3.2:3b | 0 | accepted | 4829 | 530/194 | n/a | 11/11 |
| local-3 | 8 | categories | worker-b | ollama | llama3.2:3b | 0 | accepted | 8270 | 452/149 | n/a | 2/2 |
| local-3 | 8 | summary | worker-c | ollama | llama3.2:3b | 0 | accepted | 3254 | 395/128 | n/a | 5/5 |
| mixed-2 | 1 | parser | worker-a | ollama | llama3.2:3b | 0 | rejected | 4703 | 530/187 | n/a | 0/11 |
| mixed-2 | 1 | parser | worker-b | meta | muse-spark-1.3 | 1 | accepted | 8480 | 525/2162 | 0.00984 | 11/11 |
| mixed-2 | 1 | categories | worker-a | ollama | llama3.2:3b | 0 | rejected | 3465 | 452/182 | n/a | 0/2 |
| mixed-2 | 1 | categories | worker-b | meta | muse-spark-1.3 | 1 | accepted | 6056 | 747/1298 | 0.00645 | 2/2 |
| mixed-2 | 1 | summary | worker-a | ollama | llama3.2:3b | 0 | accepted | 2679 | 395/128 | n/a | 5/5 |
| mixed-2 | 2 | parser | worker-a | ollama | llama3.2:3b | 0 | rejected | 3541 | 530/189 | n/a | 9/11 |
| mixed-2 | 2 | parser | worker-b | meta | muse-spark-1.3 | 1 | accepted | 14602 | 521/3613 | 0.01601 | 11/11 |
| mixed-2 | 2 | categories | worker-a | ollama | llama3.2:3b | 0 | accepted | 2680 | 452/142 | n/a | 2/2 |
| mixed-2 | 2 | summary | worker-a | ollama | llama3.2:3b | 0 | accepted | 2489 | 395/127 | n/a | 5/5 |
| mixed-2 | 3 | parser | worker-a | ollama | llama3.2:3b | 0 | rejected | 3699 | 530/194 | n/a | 9/11 |
| mixed-2 | 3 | parser | worker-b | meta | muse-spark-1.3 | 1 | accepted | 17582 | 526/4367 | 0.01922 | 11/11 |
| mixed-2 | 3 | categories | worker-b | meta | muse-spark-1.3 | 0 | rejected | 10145 | 274/2487 | 0.01091 | 0/2 |
| mixed-2 | 3 | categories | worker-a | ollama | llama3.2:3b | 1 | accepted | 3269 | 688/143 | n/a | 2/2 |
| mixed-2 | 3 | summary | worker-a | ollama | llama3.2:3b | 0 | accepted | 2476 | 395/128 | n/a | 5/5 |
| meta-ref-1 | 1 | parser | worker-a | meta | muse-spark-1.3 | 0 | rejected | 19868 | 293/4422 | 0.01916 | 0/11 |
| meta-ref-1 | 1 | categories | worker-a | meta | muse-spark-1.3 | 0 | rejected | 7171 | 274/1731 | 0.0077 | 0/2 |
| meta-ref-1 | 1 | parser | worker-a | meta | muse-spark-1.3 | 1 | accepted | 13528 | 1210/2598 | 0.01255 | 11/11 |
| meta-ref-1 | 1 | categories | worker-a | meta | muse-spark-1.3 | 1 | accepted | 7494 | 551/1797 | 0.00833 | 2/2 |
| meta-ref-1 | 1 | summary | worker-a | meta | muse-spark-1.3 | 0 | accepted | 13004 | 268/2569 | 0.01125 | 5/5 |
| meta-ref-2 | 1 | parser | worker-a | meta | muse-spark-1.3 | 0 | rejected | 12887 | 293/3267 | 0.01425 | 0/11 |
| meta-ref-2 | 1 | categories | worker-b | meta | muse-spark-1.3 | 0 | rejected | 6765 | 274/1748 | 0.00777 | 0/2 |
| meta-ref-2 | 1 | categories | worker-b | meta | muse-spark-1.3 | 1 | accepted | 8645 | 624/2087 | 0.00965 | 2/2 |
| meta-ref-2 | 1 | parser | worker-a | meta | muse-spark-1.3 | 1 | accepted | 6338 | 972/1692 | 0.00841 | 11/11 |
| meta-ref-2 | 1 | summary | worker-a | meta | muse-spark-1.3 | 0 | accepted | 9550 | 268/2146 | 0.00946 | 5/5 |
| meta-ref-3 | 1 | parser | worker-a | meta | muse-spark-1.3 | 0 | accepted | 16247 | 293/3718 | 0.01617 | 11/11 |
| meta-ref-3 | 1 | categories | worker-b | meta | muse-spark-1.3 | 0 | accepted | 5262 | 274/1520 | 0.0068 | 2/2 |
| meta-ref-3 | 1 | summary | worker-c | meta | muse-spark-1.3 | 0 | accepted | 8364 | 268/1954 | 0.00864 | 5/5 |
| meta-ref-1 | 2 | parser | worker-a | meta | muse-spark-1.3 | 0 | accepted | 13579 | 293/3129 | 0.01366 | 11/11 |
| meta-ref-1 | 2 | categories | worker-a | meta | muse-spark-1.3 | 0 | accepted | 9463 | 274/2321 | 0.01021 | 2/2 |
| meta-ref-1 | 2 | summary | worker-a | meta | muse-spark-1.3 | 0 | accepted | 15233 | 268/3817 | 0.01656 | 5/5 |
| meta-ref-2 | 2 | parser | worker-a | meta | muse-spark-1.3 | 0 | accepted | 16993 | 293/4603 | 0.01993 | 11/11 |
| meta-ref-2 | 2 | categories | worker-b | meta | muse-spark-1.3 | 0 | rejected | 7974 | 274/1882 | 0.00834 | 0/2 |
| meta-ref-2 | 2 | categories | worker-b | meta | muse-spark-1.3 | 1 | accepted | 8313 | 509/1875 | 0.0086 | 2/2 |
| meta-ref-2 | 2 | summary | worker-a | meta | muse-spark-1.3 | 0 | accepted | 11791 | 268/2803 | 0.01225 | 5/5 |
| meta-ref-3 | 2 | parser | worker-a | meta | muse-spark-1.3 | 0 | accepted | 16391 | 293/4167 | 0.01808 | 11/11 |
| meta-ref-3 | 2 | categories | worker-b | meta | muse-spark-1.3 | 0 | rejected | 10736 | 274/2268 | 0.00998 | 0/2 |
| meta-ref-3 | 2 | categories | worker-c | meta | muse-spark-1.3 | 1 | accepted | 9552 | 534/2388 | 0.01082 | 2/2 |
| meta-ref-3 | 2 | summary | worker-a | meta | muse-spark-1.3 | 0 | rejected | 1853 | 268/379 | 0.00195 | 0/5 |
| meta-ref-3 | 2 | summary | worker-b | meta | muse-spark-1.3 | 1 | accepted | 5227 | 598/1404 | 0.00671 | 5/5 |
| meta-ref-1 | 3 | parser | worker-a | meta | muse-spark-1.3 | 0 | accepted | 12253 | 293/3208 | 0.014 | 11/11 |
| meta-ref-1 | 3 | categories | worker-a | meta | muse-spark-1.3 | 0 | rejected | 7093 | 274/1734 | 0.00771 | 0/2 |
| meta-ref-1 | 3 | categories | worker-a | meta | muse-spark-1.3 | 1 | accepted | 10310 | 513/2345 | 0.01061 | 2/2 |
| meta-ref-1 | 3 | summary | worker-a | meta | muse-spark-1.3 | 0 | rejected | 15553 | 268/3499 | 0.01521 | 0/5 |
| meta-ref-1 | 3 | summary | worker-a | meta | muse-spark-1.3 | 1 | accepted | 5891 | 867/1494 | 0.00743 | 5/5 |
| meta-ref-2 | 3 | parser | worker-a | meta | muse-spark-1.3 | 0 | accepted | 13348 | 293/3456 | 0.01505 | 11/11 |
| meta-ref-2 | 3 | categories | worker-b | meta | muse-spark-1.3 | 0 | rejected | 9585 | 274/2181 | 0.00961 | 0/2 |
| meta-ref-2 | 3 | categories | worker-b | meta | muse-spark-1.3 | 1 | accepted | 9158 | 560/2134 | 0.00977 | 2/2 |
| meta-ref-2 | 3 | summary | worker-a | meta | muse-spark-1.3 | 0 | accepted | 10722 | 268/2185 | 0.00962 | 5/5 |
| meta-ref-3 | 3 | parser | worker-a | meta | muse-spark-1.3 | 0 | accepted | 17935 | 293/4370 | 0.01894 | 11/11 |
| meta-ref-3 | 3 | categories | worker-b | meta | muse-spark-1.3 | 0 | rejected | 7953 | 274/1896 | 0.0084 | 0/2 |
| meta-ref-3 | 3 | categories | worker-c | meta | muse-spark-1.3 | 1 | accepted | 6186 | 549/1421 | 0.00673 | 2/2 |
| meta-ref-3 | 3 | summary | worker-b | meta | muse-spark-1.3 | 0 | accepted | 12935 | 268/2961 | 0.01292 | 5/5 |
| meta-ref-1 | 4 | parser | worker-a | meta | muse-spark-1.3 | 0 | rejected | 21244 | 293/5280 | 0.02281 | 0/11 |
| meta-ref-1 | 4 | categories | worker-a | meta | muse-spark-1.3 | 0 | rejected | 8678 | 274/2039 | 0.00901 | 0/2 |
| meta-ref-1 | 4 | parser | worker-a | meta | muse-spark-1.3 | 1 | accepted | 15300 | 1171/4018 | 0.01854 | 11/11 |
| meta-ref-1 | 4 | categories | worker-a | meta | muse-spark-1.3 | 1 | accepted | 7921 | 607/1868 | 0.0087 | 2/2 |
| meta-ref-1 | 4 | summary | worker-a | meta | muse-spark-1.3 | 0 | accepted | 10928 | 268/2575 | 0.01128 | 5/5 |
| meta-ref-2 | 4 | parser | worker-a | meta | muse-spark-1.3 | 0 | accepted | 16621 | 293/3465 | 0.01509 | 11/11 |
| meta-ref-2 | 4 | categories | worker-b | meta | muse-spark-1.3 | 0 | rejected | 7915 | 274/1742 | 0.00775 | 0/2 |
| meta-ref-2 | 4 | categories | worker-b | meta | muse-spark-1.3 | 1 | accepted | 9638 | 519/2440 | 0.01102 | 2/2 |
| meta-ref-2 | 4 | summary | worker-a | meta | muse-spark-1.3 | 0 | accepted | 6792 | 268/1569 | 0.007 | 5/5 |
| meta-ref-3 | 4 | parser | worker-a | meta | muse-spark-1.3 | 0 | accepted | 15591 | 293/3918 | 0.01702 | 11/11 |
| meta-ref-3 | 4 | categories | worker-b | meta | muse-spark-1.3 | 0 | rejected | 9048 | 274/2371 | 0.01042 | 0/2 |
| meta-ref-3 | 4 | categories | worker-c | meta | muse-spark-1.3 | 1 | accepted | 11901 | 731/2523 | 0.01164 | 2/2 |
| meta-ref-3 | 4 | summary | worker-b | meta | muse-spark-1.3 | 0 | accepted | 7266 | 268/2065 | 0.00911 | 5/5 |
| meta-ref-1 | 5 | parser | worker-a | meta | muse-spark-1.3 | 0 | rejected | 19350 | 293/4766 | 0.02062 | 0/11 |
| meta-ref-1 | 5 | categories | worker-a | meta | muse-spark-1.3 | 0 | rejected | 7233 | 274/2011 | 0.00889 | 0/2 |
| meta-ref-1 | 5 | parser | worker-a | meta | muse-spark-1.3 | 1 | accepted | 6423 | 788/1650 | 0.008 | 11/11 |
| meta-ref-1 | 5 | categories | worker-a | meta | muse-spark-1.3 | 1 | accepted | 8814 | 604/2134 | 0.00982 | 2/2 |
| meta-ref-1 | 5 | summary | worker-a | meta | muse-spark-1.3 | 0 | rejected | 13535 | 268/3563 | 0.01548 | 0/5 |
| meta-ref-1 | 5 | summary | worker-a | meta | muse-spark-1.3 | 1 | accepted | 8058 | 741/1872 | 0.00888 | 5/5 |
| meta-ref-2 | 5 | parser | worker-a | meta | muse-spark-1.3 | 0 | accepted | 15768 | 293/4043 | 0.01755 | 11/11 |
| meta-ref-2 | 5 | categories | worker-b | meta | muse-spark-1.3 | 0 | rejected | 7409 | 274/1826 | 0.0081 | 0/2 |
| meta-ref-2 | 5 | categories | worker-b | meta | muse-spark-1.3 | 1 | accepted | 6370 | 531/1730 | 0.00802 | 2/2 |
| meta-ref-2 | 5 | summary | worker-a | meta | muse-spark-1.3 | 0 | rejected | 9754 | 268/2554 | 0.01119 | 0/5 |
| meta-ref-2 | 5 | summary | worker-b | meta | muse-spark-1.3 | 1 | accepted | 5596 | 554/1227 | 0.00591 | 5/5 |
| meta-ref-3 | 5 | parser | worker-a | meta | muse-spark-1.3 | 0 | accepted | 17395 | 293/4028 | 0.01749 | 11/11 |
| meta-ref-3 | 5 | categories | worker-b | meta | muse-spark-1.3 | 0 | rejected | 8737 | 274/2038 | 0.009 | 0/2 |
| meta-ref-3 | 5 | categories | worker-c | meta | muse-spark-1.3 | 1 | accepted | 9004 | 525/2403 | 0.01087 | 2/2 |
| meta-ref-3 | 5 | summary | worker-b | meta | muse-spark-1.3 | 0 | accepted | 8028 | 268/1845 | 0.00818 | 5/5 |
