# Agent Marketplace

A working prototype of a marketplace for AI-agent work. A requester keeps its job plan private and advertises one bounded task at a time. Separate worker processes bid on tasks, take expiring leases and return source code. The requester tests each result independently, pays simulated credits once per accepted task, and reassigns work when a worker disappears. A local dashboard shows the plan, offers, selection reasons, failures, latency and cost.

A fresh real Meta run on November 4 completed the expense application with three accepted modules, two repaired rejections and recovery from an intentionally failed worker. It took 192.9 seconds and about $0.057 by the local guard. This is one same-Mac run, not a general reliability, multi-machine or cloud-protection result.

The marketplace itself handles public work only. Separately, one private task ran in an Azure confidential container on November 1. The current requester additionally requires an approved utility VM measurement and minimum firmware TCB before sealing data to the attested key; the shipped trust-policy template deliberately approves nothing. This hardened path has been tested with local synthetic evidence and awaits a separately approved live run.

[docs/implementation/STATUS.md](docs/implementation/STATUS.md) is the authority on current behaviour and evidence. Where any other document disagrees with it, trust STATUS. For the items below, read STATUS rather than assuming either way:

- whether the application is a reusable template or still fixed to the expense report
- whether local inference through Ollama has been run
- whether connections between machines use TLS

For the visual dashboard, component placement and personal Meta-key setup, follow the [laptop testing guide](docs/implementation/TRY_DEMO.md).

## Setup

Python 3.11 or later. The marketplace needs no third-party packages and no installation. From the repository root:

```sh
python3 -m unittest discover -s tests
```

The tests use loopback networking and start real worker processes. They need no credentials, paid calls, models, devices or cloud accounts. Tests of the attestation verifier need the `cryptography` package and are skipped without it. To include them:

```sh
python3 -m venv .venv && .venv/bin/pip install cryptography
.venv/bin/python -m unittest discover -s tests
```

## Run it for free

Start the interactive visual dashboard:

```sh
python3 -m agent_marketplace.studio --port 8765
```

Open `http://127.0.0.1:8765` and start a free rehearsal. Separate worker processes really bid, receive leases, deliver source and undergo independent verification. Model responses and credits are simulated and labeled. Click components to filter the backend trace; after acceptance, try public CSV input through the resulting application. The dashboard stays open for another run. No cloud resources or local GPU are needed. To enable explicit billable browser runs with your own configured key, add `--allow-live-meta` when starting the studio. See the runbook for scope and controls.

The fixture provider returns clearly labelled prewritten code. It is not AI inference, and it costs nothing.

```sh
python3 -m agent_marketplace.demo --failure --hold-seconds 300
```

Open the dashboard URL the command prints. Three workers enrol, the cheapest one exits, its lease expires and the remaining workers deliver three modules of a small expense-report application. Each module is accepted independently, and final integration is checked separately. Output goes to a new `.demo/` directory, which contains local credentials, so share selected reports rather than the whole directory.

To rehearse the full planning and repair path against a local simulator, also free and also not AI:

```sh
python3 -m agent_marketplace.demo --provider simulated-meta --failure --lease-seconds 2 --hold-seconds 300
```

[docs/implementation/RUNBOOK.md](docs/implementation/RUNBOOK.md) covers separate services and the native Swift client.

## Run it with your own Meta API key

This makes billable calls on your account. Put the key in `.env`, never in source or on a command line:

```sh
cp .env.example .env   # then set MODEL_API_KEY
python3 -m agent_marketplace.demo --provider meta --failure --lease-seconds 75 --hold-seconds 300
python3 -m agent_marketplace.budget   # spend so far
```

One measured run cost about $0.05 by the local guard. That is a single observation, and the guard's figure was not reconciled against Meta's billing console. The guard refuses calls above a $35 default cap and a $1 per-run cap, and configuration cannot exceed a $40 ceiling in the code. It is per machine, and it sees only calls made through this code. Its prices were copied from Meta's pricing page on 2026-11-01, so recheck them. Model availability depends on your own access.

Hosted calls receive public module specifications only. Any hosted provider sees what it is sent.

## Run the protected task with your own Azure subscription

This creates resources that bill you, about $0.37 per hour when measured, until you run `down`.

**The parameterised deployment files in this copy have not been deployed.** The evidence in STATUS came from the same structure with the original image names and policy. Treat these steps as a starting point, and expect to adjust them. No model weights or generated policy are included.

1. Install `cryptography` as shown under Setup, and install the Azure CLI with the `confcom` extension.
2. Create a resource group and an Azure Container Registry with the admin user enabled.
3. Build and push the two images. `protected_worker/Dockerfile` is the worker. `protected_worker/model/Dockerfile` is the model server, and it expects a Llama 3.2 3B GGUF file named `llama32-3b-q4.gguf` in that directory, which you supply yourself.
4. Generate a CCE policy for your images from `protected_worker/deploy/template.json` with `az confcom acipolicygen`, and save the base64 policy to a file. Exact options depend on your extension version.
5. Approve that policy. Replace the placeholder in `protected_worker/deploy/expected_host_data.txt` with the SHA-256 of the decoded policy:

   ```sh
   base64 -d < policy.b64 | shasum -a 256 | cut -d' ' -f1 > protected_worker/deploy/expected_host_data.txt
   ```

   Until you do, the verifier refuses every worker. Changing either image requires a new policy and a new hash.
6. Configure `protected_worker/deploy/trust_policy.json` with independently approved UVM measurements and per-product minimum firmware TCB values. Its schema is `version: 1`, `measurements` (nonzero 96-character lowercase hex strings), and `minimum_tcb` (Milan/Genoa product names mapped to `bootloader`, `tee`, `snp`, `microcode`, each 0–255 with a nonzero floor). The shipped empty template refuses all private execution. A policy hash alone is insufficient, and copying values from a worker response is not approval. Use authenticated UVM references/endorsements and a reviewed firmware policy. See the runbook for the checks and CLI overrides. The November 1 live run predates these mandatory checks; the hardened path has not been rerun on Azure.
7. Once cloud execution is approved, start the worker, then run the demo against the URL it prints. Allow about a minute for the model to load.

   ```sh
   export AZ_SUBSCRIPTION=YOUR_SUBSCRIPTION AZ_RESOURCE_GROUP=YOUR_GROUP AZ_CONTAINER_GROUP=YOUR_CONTAINER_GROUP
   export WORKER_IMAGE=YOUR_REGISTRY.azurecr.io/protected-worker:TAG
   export MODEL_IMAGE=YOUR_REGISTRY.azurecr.io/protected-model:TAG
   export CCE_POLICY_FILE=policy.b64
   scripts/protected_worker.sh up
   .venv/bin/python -m agent_marketplace.demo --provider meta --failure --lease-seconds 75 --protected-url URL --protected-trust-policy protected_worker/deploy/trust_policy.json --hold-seconds 300
   scripts/protected_worker.sh down
   ```

   The deployment `location` defaults to Central India. Confidential containers are not offered in every region, so override it in the template if needed.

## Limits

These are the limits of what has been shown.

**What the protection covers**

- The intended private boundary excludes the worker host and trusts AMD and an approved Microsoft utility VM. Private release now requires the mandatory trust-policy checks below; the hardened path has only been tested locally with synthetic evidence.
- Side channels and physical attacks are excluded.
- The verifier requires an approved utility VM measurement and a product-specific firmware floor (minimum TCB), alongside the container policy hash and key/nonce binding. Missing trust configuration refuses private release. These checks were added after the November 1 live run, so that run does not validate the hardened path.
- Protected status records attestation, encrypted submission, delivery and acceptance separately. A lost response after submission is shown as unknown delivery, never as proof that no data was released.
- Attestation says what is running. It says nothing about the quality of the output, or about the absence of bugs.
- Signing identifies who built software. It does not make software tamper-proof, and this project makes no such claim.

**What is not connected**

- The protected step is not yet in bidding, leases or credits. It is a fixed endpoint that runs beside the marketplace, so the marketplace as a whole does not offer private execution.
- All marketplace workers are ordinary processes with no confidentiality from whoever runs them.

**What the results do and do not show**

- The protected model scored 18/18 on a small synthetic set, and 10 of those rows were used for tuning. This is not a quality measure.
- Passing application tests shows the tested behaviour only. It does not establish data confidentiality.
- Credits are simulated. No real payment exists.
- This is not a consumer-device proof. A cloud confidential container says nothing about protecting work on phones or laptops, and no such path is demonstrated here.

Further limits are in [docs/implementation/STATUS.md](docs/implementation/STATUS.md). The generated-code interpreter, for example, is not a general sandbox.

## Reading the other documents

The October 29 design and threat-model documents are not included: they predate the Azure work and understate what now runs, so STATUS and this README carry the delivered behaviour and its limits. The documents under `docs/implementation/` are current references. "The user" in those documents is the project's authors. References such as C23, C25 or C30 point to a decision register that is not published.

## Models and licence

Project-owned code in this sanitized public demo is licensed under the [MIT License](LICENSE), including commercial use subject to its terms. Model weights, Meta API access and third-party components retain their separate licenses and agreements. The public-demo grant does not publish the private project or change licenses on material excluded from this repository. Any separate rights granted through the hackathon remain separate.

This repository contains no model weights. Llama 3.2 is Meta's model and is subject to Meta's licence terms, which you accept when you obtain it. The original deployment used an unofficial GGUF conversion. `muse-spark-1.3` is a hosted Meta model, used under your own API agreement.
