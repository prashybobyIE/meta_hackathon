# Run the demo foundation

Run from the workspace root with Python 3.11+. Public runtime has no third-party dependencies; no installation step is required. Swift is optional for the native observation client. These commands process public reference inputs. The separate protected path uses Azure confidential containers and requires `cryptography`.

## One command

```sh
python3 -m agent_marketplace.demo --failure --hold-seconds 300
```

This creates a fresh ignored `.demo/` directory, a SQLite marketplace, requester HTTP server and three ordinary worker processes. The cheapest fault worker exits before fetching its contract. Its lease expires, a different worker receives the attempt, and independently accepted modules assemble into the report. The printed dashboard URL is live for five minutes; Ctrl-C closes the services. Omitting `--failure` runs two normal workers. Omitting `--hold-seconds` finishes and closes all processes immediately.

The workspace holds `summary.json`, a secret-free marketplace snapshot, worker logs, requester state, accepted module sources/reports, `requester/report.json` and `requester/report.html`. Role tokens and dispatch state are local credentials; do not share that entire directory. Share only selected public reports/snapshots. The default provider is a deterministic prewritten fixture, visibly labeled as such.

```sh
python3 -m agent_marketplace.report --artifacts .demo/YOUR_RUN/requester/artifacts --input .demo/YOUR_RUN/public-expenses.csv --output .demo/YOUR_RUN/rerun-report.html
```

## Separate services and machines

For manual testing in separate terminals, first create local role files:

```sh
python3 scripts/create_demo_credentials.py .demo/manual
```

Marketplace:

```sh
python3 -m agent_marketplace.market --port 8787 --db .demo/manual/market.sqlite3 --requester-token-file .demo/manual/requester.token --enrollment-token-file .demo/manual/enrollment.token
```

Requester:

```sh
python3 -m agent_marketplace.requester --market http://127.0.0.1:8787 --port 8770 --workspace .demo/manual/requester --requester-token-file .demo/manual/requester.token
```

Workers, using distinct IDs:

```sh
python3 -m agent_marketplace.worker --market http://127.0.0.1:8787 --enrollment-token-file .demo/manual/enrollment.token --id worker-a
python3 -m agent_marketplace.worker --market http://127.0.0.1:8787 --enrollment-token-file .demo/manual/enrollment.token --id worker-b
```

For actual separate machines, provide an existing authenticated HTTPS deployment/reverse proxy for the marketplace and requester. Configure requester `--advertise-url https://YOUR_REQUESTER_ORIGIN`, use the HTTPS market origin for all clients, and distribute enrollment credentials deliberately. Current development listeners remain loopback by default; this runbook does not provision a public service, certificates, or firewall rules. Plain remote HTTP requires explicit public-data development overrides on the relevant components and exposes bearer credentials; it is not the recommended network setup. Current tests establish separate-process behavior on one machine, not a tested multi-machine TLS deployment.

SQLite and requester state persist across service restart. Reuse the same role credentials and requester listening/advertised origin for active leases. Existing worker enrollment IDs cannot be overwritten. Worker `--state-file` saves an identity for resume, but resume across a real daemon restart has not been tested. The requester reconciles a saved valid result when remote acceptance committed before a local interruption.

Provider failures with status 408/425/429 or 5xx now cool that task's worker down for 2, 4, 8, 16, then at most 30 seconds. Another eligible worker gets preference before the usual offer score. The cooldown persists across requester restart; waiting consumes no attempts or reward credits, while every new assignment still counts toward the attempt limit. Permanent 400/401/402/403 failures remain excluded. This is requester-level recovery; generation is never automatically retried within one attempt.

## Optional real model providers

Meta generation requires `MODEL_API_KEY` in the worker environment and requester permission. Do not paste a key into source, a command argument, or this documentation. The provider uses the official `https://api.meta.ai/v1/chat/completions` endpoint and frozen public module specifications. Caller descriptions, fixtures and diagnostic content are not sent. See [worker details](WORKER.md) for checked sources and models.

```sh
python3 -m agent_marketplace.demo --provider meta --model muse-spark-1.3 --timeout 300 --hold-seconds 300
```

This command makes billable calls when a key is configured, about $0.05 per run on November 1. Put the key in `.env`. Each call passes the budget guard first; check it with `python3 -m agent_marketplace.budget`. Run hosted calls from one machine only. Generation is once per attempt, with at most 2 repairs per task. Simulated reward credits are not API prices. Repair prompts contain public test cases and the previous public source only; the restricted runtime has no unsafe execution fallback. Rehearse free with `--provider simulated-meta`, which is not AI.

To let another machine join with public data only, first create a development certificate covering the address workers will use. `openssl` is required, and the key is written to the gitignored `.demo/tls`:

```sh
python3 scripts/create_dev_tls_cert.py .demo/tls --host THIS_MACHINE_ADDRESS
python3 -m agent_marketplace.demo --remote-workers 1 --advertise-host THIS_MACHINE_ADDRESS --tls-cert .demo/tls/cert.pem --tls-key .demo/tls/key.pem
```

Copy `cert.pem` and `enrollment.token` to the other machine, never `key.pem`, and run the printed worker command, which uses `--ca-file cert.pem`. Standalone services take the same options: `--tls-cert` and `--tls-key` on the market and requester, `--ca-file` on the requester and workers, and an `https://` value for requester `--advertise-url`.

Without certificate options, `--remote-workers` still listens on all interfaces over unencrypted HTTP, which exposes bearer credentials on the network.

On a listener bound beyond loopback, `/v1/snapshot` and `/v1/status` need the requester bearer token unless the caller is on the same machine. `/health` stays public. Open the dashboard on the requester's own machine at the printed `127.0.0.1` address; a browser will warn about a self-signed certificate. A reverse proxy on the same machine makes all callers look local, so don't rely on this check behind one. NodeProbe has no token, so it reads the snapshot only from the same machine, and it needs OS-trusted certificates for HTTPS.

TLS protects credentials and metadata in transit from network observers once the client has verified the certificate. It does not protect data from the service operator or a worker host, and it does not make work confidential. This has been tested on one machine, including through its own network address, and not across two physical machines.

## Other workloads

`python3 -m agent_marketplace.demo --workload agent_marketplace/workloads/inventory_reorder/manifest.json` runs the inventory workload with the fixture provider. Add `--provider simulated-meta` to rehearse planning, rejection and repair without a key or spend, or `--provider meta` for live generation. The default is the expense report. `--protected-url` applies only to the expense report and is refused with `--workload`.

`python3 -m agent_marketplace.workload PATH` validates a manifest and prints its id and digest. For remote workers on a non-default workload, export the public contracts with `python3 -m agent_marketplace.workload PATH --public-out public-contracts.json`, copy that file to the worker machine, and add `--contracts-file public-contracts.json` to the worker command. Do not copy the manifest itself: it holds the hidden cases, and a worker refuses it. See [workloads](WORKLOADS.md).

## Local models and measurement

Local inference uses a loopback Ollama server and an explicitly named installed model. Install and run it by hand; the code does not install models or start the server:

```sh
brew install ollama
ollama serve            # Separate terminal; binds to loopback by default. Ctrl-C to stop.
ollama pull llama3.2:3b
python3 -m agent_marketplace.demo --provider ollama --model llama3.2:3b --timeout 280
```

Cloud-hosted Ollama tags are refused. Local runs cost no API credits, but `llama3.2:3b` is unreliable: 14 of 24 measured runs had all modules accepted, and the rest stop at the repair limit, which is a real result and not a harness failure.

To have one local worker and one Meta API worker bid in the same job (billable, through the budget guard):

```sh
python3 -m agent_marketplace.demo --workers ollama=llama3.2:3b,meta=muse-spark-1.3 --timeout 280
```

The first listed worker quotes lowest. `--workers` cannot be combined with `--failure`, `--single-worker` or `--model`. `--reference-plan` skips the Meta planner call and uses the fixed graph; in measured runs the planner serialized the modules, so workers only overlapped with this flag.

To measure worker configurations:

```sh
python3 scripts/measure_workers.py --configs local-1,local-2,local-3 --runs 8 --raw results.jsonl
python3 scripts/measure_workers.py --configs meta-ref-1,meta-ref-2 --runs 3 --allow-billable --stop-at-usd 1.0 --raw results.jsonl
python3 scripts/measure_workers.py --raw results.jsonl --render tables.md
```

Billable runs refuse to start without a key and an existing ledger; in a git worktree, set `META_BUDGET_LEDGER` to the main checkout's ledger. Results are in [the measurement record](../measurements/WORKER_SCALING_2026-11-01.md); they describe one Mac and do not measure distributed compute. Neither hosted nor local adapters provide host confidentiality.

## Protected private task

This project requires explicit user approval before starting cloud resources. The November 2 fixes and tests use local synthetic evidence only; no new cloud window is approved.

Deployment configuration contains no account-specific resource defaults or generated policy. Before an approved deployment, set `AZ_SUBSCRIPTION`, `AZ_RESOURCE_GROUP`, `AZ_CONTAINER_GROUP`, `WORKER_IMAGE`, `MODEL_IMAGE` and `CCE_POLICY_FILE`; generate and approve your own CCE policy and matching `expected_host_data.txt`. The helper passes credentials and policy through a temporary owner-only ARM parameters file, removes it on exit, and never includes those values in Azure CLI arguments. These configuration steps do not authorize starting resources.

Before an approved live run, complete `protected_worker/deploy/trust_policy.json` using independently trusted reference values. Its version-1 schema contains `measurements` (a nonempty allowlist of nonzero 96-character lowercase SHA-384 hex values) and `minimum_tcb` (product-specific floors for `bootloader`, `tee`, `snp`, `microcode`, each an integer 0–255). Only report version 5 (the inspected AMD ABI 1.58 layout) and Milan/Genoa TCB layouts are supported; other versions/layouts are refused. At least one component in each floor must be nonzero. The template's empty lists/maps intentionally refuse private execution. Do not approve values merely by copying a worker response or a previous run: use authenticated, reviewed UVM reference measurements/endorsements and firmware policy. The container policy hash in `expected_host_data.txt` is a separate required input; it does not identify the utility VM.

The demo validates these files before starting its services. Use `--protected-trust-policy PATH` and `--protected-policy PATH` to choose other local files. The verifier requires a matching measurement, a floor for the authenticated product, componentwise TCB compliance, and binding of reported TCB values to the signed VCEK certificate. The current synthetic tests exercise these decisions; the hardened path has not been rerun on Azure.

```sh
python3 -m venv .venv && .venv/bin/pip install cryptography
# Only after trust values have been approved and cloud execution is explicitly authorized:
scripts/protected_worker.sh up      # bills about $0.37/hour; prints the URL
.venv/bin/python -m agent_marketplace.demo --provider meta --failure --lease-seconds 75 --protected-url URL --protected-trust-policy protected_worker/deploy/trust_policy.json --hold-seconds 300
scripts/protected_worker.sh down
```

Wait about a minute after `up` for the model to load. Changing anything in the worker or model image requires a new image tag, regenerating the policy and updating `protected_worker/deploy/expected_host_data.txt`. `az confcom` crashed on the Homebrew CLI, so run it from the `mcr.microsoft.com/azure-cli` image with the Docker socket mounted.

Protected status separates attestation, submission, delivery and acceptance. Once the encrypted task POST begins, a timeout means delivery/execution is unknown; it never means data was withheld. A missing reply cannot earn acceptance. Legacy saved records without release state are also displayed as unknown. AMD root-service failures are recorded before release, and a failed optional refusal demonstration does not erase a completed task.

### Trust approval and proposed validation window, November 2

This is a proposal, not authorization to start resources. There are two approvals: the concrete evidence-backed trust policy, and the Azure execution window. The repository's empty policy remains unchanged.

Before approving trust, review the exact worker/model image contents and digests, their generated CCE policy hash, the Microsoft-authenticated UVM measurement/publisher/version, and a current AMD-backed firmware baseline for the selected product. The committed CCE policy decodes to the existing approved hash; that local consistency check does not validate a deployed guest. Any image change requires a regenerated policy and a new hash. Do not select VM/firmware values solely from a worker's response.

The remaining implementation/preparation is to obtain Microsoft's signed `reference-info-base64` UVM endorsement and verify its provenance, then match its endorsed measurement to the signed SNP report. The current worker returns raw SNP evidence/certificates but not that reference file, and the requester pins a measurement without implementing Microsoft endorsement verification. A public-evidence export/helper or independently verified reference package is therefore needed. [Microsoft CCF](https://ccf.dev/main/operations/platforms/snp.html) documents both the security-context files and `verify_uvm_attestation_and_endorsements`. Firmware floors should be checked against current AMD security advisories for the actual product/CPU, not chosen merely to pass the deployed report. Historical CCF example floors are reference material, not automatic approval.

If suitable authenticated reference data is unavailable offline, first obtain approval for an evidence-only discovery launch. No task input is sent during discovery. Inspect report format, product, signed UVM references and certificate/TCB before proposing concrete pins. An unsupported report layout is a stop condition; its parser must be reviewed and tested before task release. Once trust is approved, run synthetic categorization, reject wrong measurement/policy/TCB and stale/substituted evidence, verify encrypted return and independent acceptance, and confirm lost-response bookkeeping. Save sanitized outcomes rather than raw chip identifiers. Delete the compute group at completion or the window deadline, including on failure. These tests do not integrate the protected worker into bidding/settlement.

Public Microsoft retail-price API checked November 2, Central India, USD Consumption meters: confidential ACI vCPU `0.06237` per hour and memory `0.006853` per GB-hour. The committed group requests 5 vCPUs and 8 GB across worker, attestation sidecar and model. Estimated compute is `5 × 0.06237 + 8 × 0.006853 = 0.366674 USD/hour`: about $0.18 for 30 minutes, $0.37 for one hour, or $0.73 for two hours. Basic registry is `0.1666 USD/day` while retained, plus any separately billable excess storage/transfer. Reusing an existing registry may add no new registry charge for the validation, but its existing retention cost continues. These are retail estimates, not the subscription's exact invoice/credit balance.

Recommended future authorization: one group, at most 60 minutes total including startup/retries, only the previously approved subscription and existing project resources, synthetic inputs only, no hosted Meta calls, immediate compute deletion, and a conservative $2 spend authorization. One compute hour plus one full registry-day allowance is about $0.53 before taxes/transfer. The $2 authorization is an operational limit, not an Azure-enforced billing cutoff. Container deletion leaves the registry billable unless separately deleted with approval. No cloud window or trust values were approved by the cost inquiry.

Sources: [confidential ACI regional retail meters](https://prices.azure.com/api/retail/prices?%24filter=serviceName%20eq%20%27Container%20Instances%27%20and%20armRegionName%20eq%20%27centralindia%27%20and%20priceType%20eq%20%27Consumption%27), [Basic registry regional retail meters](https://prices.azure.com/api/retail/prices?%24filter=serviceName%20eq%20%27Container%20Registry%27%20and%20armRegionName%20eq%20%27centralindia%27%20and%20priceType%20eq%20%27Consumption%27), [API pricing semantics](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices), [Azure billing caveats](https://azure.microsoft.com/en-us/pricing/details/container-instances/), [AMD security guidance example](https://www.amd.com/en/resources/product-security/bulletin/amd-sb-3019.html). No Azure account/resource request was made to retrieve these public rates.

## Native networking and protected-platform research

```sh
python3 scripts/probe_platform.py
```

The read-only probe reports observations/unknowns and never authorizes private execution. Optional already-authorized Android queries are described in research. Build the Swift URLSession client using [its README](../../native/NodeProbe/README.md), then point it at the printed market origin. It observes health and public metadata; it is not a VM launcher or attestation verifier.

## Verification

```sh
python3 -m unittest discover -s tests -v
```

The suite includes real worker subprocesses and loopback servers. A restricted executor must permit loopback networking; approved local-network escalation was needed in Codex. No tests require credentials, paid calls, models, devices, or cloud accounts. Native tests run separately using Swift.

## Public export

`scripts/export_public.sh COMMIT OUTPUT_DIR` builds a history-free, allowlisted copy outside this repo and scans it for private names and credentials. It creates no remote and publishes nothing. A clean scan does not prove nothing sensitive remains, so read the export before publishing.
