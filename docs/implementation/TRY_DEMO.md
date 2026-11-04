# Try the public-work demo on your laptop

Use a source revision that contains `agent_marketplace/studio.py` and this guide. The latest dashboard/video changes are being prepared locally; an older public repository revision may not include them yet. Run the commands from the repository root.

You need macOS or Linux and Python 3.11+. The Python dashboard/runtime uses the standard library. The free rehearsal needs no API key, local model, GPU, Azure deployment or Node.js installation. Node.js is only needed to edit the separate Remotion video project.

## What runs where

| Component | Placement in this MVP | Purpose |
| --- | --- | --- |
| Dashboard | Your laptop's browser at `http://127.0.0.1:8765` | Start a job, inspect components/traces, try the accepted expense app |
| Requester and planner orchestration | Local Python on your laptop | Own the graph, validate the plan, decide readiness and authorize attempts |
| Marketplace | Local Python HTTP service and SQLite | Enrollment, bids, reservations and expiring leases |
| Workers A and B | Two separate Python processes on the same laptop | Bid, fetch an assigned public contract, obtain source and deliver an artifact |
| Verifier and application assembly | Requester-side bounded Python interpreter on your laptop | Check artifacts independently and run accepted modules together |
| Simulated credit ledger | Local SQLite state | Settle each accepted task once; no real payment |
| Meta inference, in live mode | Meta's hosted API | Propose a bounded plan and generate/repair public module source |
| Model responses, in free rehearsal | Local API simulator | Return prewritten responses; no AI inference or paid call |

These are separate roles and processes on **one physical device**. The dashboard does not deploy remote workers. Ordinary workers do not protect private data from their host. Azure confidential execution, physical multi-machine validation and arbitrary application decomposition are outside this walkthrough.

## Run a free rehearsal first

```sh
python3 --version
python3 -m agent_marketplace.studio --port 8765
```

Open `http://127.0.0.1:8765` on that laptop. Keep **Free rehearsal · simulated Meta** selected and click **Start rehearsal**. To match the recorded two-worker layout, turn **Worker failure demo** off before starting. Turning it on adds an intentionally failing worker so you can watch its lease expire and the task move to another worker.

The job advertises three fixed expense-app contracts: parser, categorisation and summary. Real local processes perform enrollment, bidding, assignment, delivery and deterministic acceptance. Model output is prewritten in rehearsal, and reward credits are always simulated. Select a coloured component to see its resource placement and filtered backend trace.

Wait for final integration to pass. Then edit the public CSV in the working-application panel and click **Run application →**. This uses already accepted modules locally and makes no new model call. For example:

```csv
date,merchant,amount,category
2026-11-04,Coffee,2.50,
2026-11-04,Train,3.00,
```

That input totals **5.50 units**. The prepared four-transaction example totals **40.25 units**. API timing, repairs, accepted worker and settled demo-credit total can vary in live mode; they are not fixed expected outcomes.

## Connect your own Meta API key

Use a key from your own Meta API account with access to the configured model. This build uses **`muse-spark-1.3`** through **`https://api.meta.ai/v1/chat/completions`**. The dashboard does not accept interchangeable API keys for arbitrary providers. Account access and provider billing remain your responsibility.

1. Stop the running studio with Ctrl-C.
2. If you do not already have a local `.env`, create one from the example:

   ```sh
   cp -n .env.example .env
   chmod 600 .env
   ```

3. Open `.env` in your editor and replace `REPLACE_ME` on its `MODEL_API_KEY` line with your key. Keep it in that local ignored file. Do not put the key in a browser field, screenshot, source file, shared trace, command argument or Git commit.
4. Start the studio with live access enabled:

   ```sh
   python3 -m agent_marketplace.studio --port 8765 --allow-live-meta
   ```

5. Reopen the local dashboard, choose **Live Meta API · billable**, then click **Start live Meta run**. Starting the server alone does not make a model call. If live mode is unavailable, check that the startup flag and local `MODEL_API_KEY` are present, then restart. An inherited shell `MODEL_API_KEY` takes precedence over `.env`.

The requester calls Meta to propose dependencies/complexity for the three prepared public contracts. Code validates and freezes that plan. Each winning local worker calls Meta for generation and, when needed, bounded repair. Public specifications and public repair feedback can reach Meta; hidden acceptance cases and private inputs are not part of this workflow. A Meta API error or rejected artifact appears in the trace; do not treat the animation as proof of acceptance.

### Spending controls and local state

The local guard defaults to **USD 1 per run** and **USD 35 cumulative**, with a code ceiling of USD 40. You can lower the supported limits in `.env`:

```dotenv
META_BUDGET_USD=5
META_BUDGET_RUN_USD=1
```

Only `MODEL_API_KEY`, `META_BUDGET_USD` and `META_BUDGET_RUN_USD` are loaded from `.env`. Keep the existing `.budget/` ledger; deleting it loses local accounting. If reusing a key from another checkout/worktree, set `META_BUDGET_LEDGER` in the shell environment to that existing absolute ledger path rather than starting fresh accounting. It is not an additional `.env` key. Use one machine for hosted calls: this ledger cannot see external account usage, and the provider console is the billing authority.

Inspect the local guard privately with:

```sh
python3 -m agent_marketplace.budget
```

Generated `.demo/` state can contain role credentials even though the dashboard projects a public trace. Keep `.env`, `.budget/` and `.demo/` out of shared copies. The dashboard binds to loopback; this command does not make it a public service. No additional cloud resources are required for either mode.

## Optional local regression check

```sh
python3 -m unittest discover -s tests -p test_studio.py -v
```

These tests use local networking and fixture responses, not a paid Meta call. The full protected-verifier suite additionally needs `cryptography`; passing it does not establish host confidentiality. See [the runbook](RUNBOOK.md) for separate-service commands, optional local Ollama support and the distinct protected-execution path.
