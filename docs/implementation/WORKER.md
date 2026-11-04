# Public worker and model adapters

Implemented 2026-10-29 in `agent_marketplace/worker.py` and `providers.py`. This is ordinary host execution for public reference contracts. It provides no protected VM, remote hardware attestation, or host-confidentiality guarantee.

The node registers with an enrollment credential, receives a worker-specific token, sends heartbeats, bids on eligible public descriptors, and polls requester-authorized leases. It fetches the scoped contract directly from the requester, generates a source artifact, hashes it, and returns it to the requester. It never marks its own task accepted or settles rewards. It never executes generated Python.

## Run

```sh
python3 -m agent_marketplace.worker \
  --market http://127.0.0.1:8080 \
  --enrollment-token-file /path/to/enrollment-token \
  --id worker-one \
  --provider fixture
```

`fixture` is the deterministic prewritten reference baseline, labeled `prewritten-reference-v1`. It is not AI inference. This lets the network, verification, leases, recovery, and credit accounting be tested without credits or installed models.

Select `--provider meta --model muse-spark-1.3` for the optional hosted integration, with `MODEL_API_KEY` configured outside source control and requester `provider_policy.allow_meta` explicitly true. Select `--provider ollama --model <already-installed-model>` for a local model served at `http://127.0.0.1:11434`. The code does not install/pull a model. Both adapters have now run live: Meta on 2026-11-01, and Ollama 0.40.13 with `llama3.2:3b` on 2026-11-01 (see [measurements](../measurements/WORKER_SCALING_2026-11-01.md)). Model tags ending in `-cloud` or `:cloud` are refused because Ollama forwards them to its hosted service.

Local prompts add per-contract step hints (`_LOCAL_HINTS` in `providers.py`), sent only to local models. They restate the public contract and runtime limits as steps and contain no acceptance case, but they were adjusted while watching acceptance pass counts, and they are close to pseudocode. An accepted local module therefore shows that a small model can follow a recipe, not that it can solve the contract unaided. The local adapter reports token counts and no cost. It establishes no protection from the host.

`python -m agent_marketplace.demo --workers ollama=llama3.2:3b,meta=muse-spark-1.3` gives each worker its own provider in one run. Workers bid and go through acceptance as usual. Quotes rise by worker order (10, 12, 14 credits at full size), so the first listed worker is the cheapest. The Meta budget guard applies to the planner and to Meta workers only.

## Disclosure and transport boundaries

Only the three frozen public reference contract identities are supported. The provider checks ID, filename and function; caller descriptions, fixtures, extra fields and diagnostics are not sent to models. Prompts are reconstructed from a public allowlist. Private/confidential sensitivity is rejected before generation, even if a model/provider is local. This deliberately supports a narrower policy than generic redaction: no automatic redaction guarantee is claimed.

The Meta endpoint is fixed to `https://api.meta.ai/v1/chat/completions`, with a 30-second default timeout, bounded completion budget and a 32-KiB source limit. Non-success, malformed, incomplete and oversized completions fail; there is no fallback to a different provider or fixture. A failed generation is not automatically retried because that could incur duplicate hosted charges. Only artifact delivery retries, at most three times. The requester still independently checks the returned module.

Network endpoints must be loopback HTTP or HTTPS by default. `--allow-insecure-public-network` is an explicit public-data-only development override; enrollment/attempt credentials are exposed if HTTP is used. It is not a secure deployment configuration. Protocol redirects are disabled in the shared client. Tokens never enter worker event output. TLS validates server certificates through the standard library; enrollment credentials identify the caller. This is not hardware identity.

## Programmatic and failure controls

`Worker(market_url, enrollment_token, worker_id, provider='fixture', bid_credits=10, poll_interval=0.25, model=None, ...)` exposes `run(stop_event=None)` and `run_once(stop_event=None)`. `run` uses a background heartbeat during generation. `execution_delay` adds bounded delay; `crash_on_first_assignment=True` raises `WorkerCrash` before fetching the contract, causing CLI exit 17. `on_assignment` and `on_event` callbacks support the demo harness. Events include only explicit status/identity/hash fields.

Each seen attempt is processed once by that worker instance. The requester fences expired attempts and controls reassignment. Worker registration does not overwrite an existing identity; a restarted CLI should use a new ID unless a future durable credential/resume mechanism is added. No worker credential persistence is implemented here.

## Validation

`python3 -m unittest discover -s tests -p test_worker.py -v` covers real loopback HTTP registration/bids/dispatch/result delivery, attempt-token scope, private-dispatch refusal before provider use, expired lease refusal, bounded delivery retry without repeated generation, deterministic fixture labels, provider policy enforcement, allowlisted prompt construction, malformed/truncated/oversized/invalid-Unicode outputs, and explicit remote HTTP configuration. Transport regressions reject duplicate fields, nonfinite/exponent-overflow numbers, invalid UTF-8 and non-object documents, prevent authenticated redirects, and verify loopback proxy bypass. All 17 tests passed after shared protocol hardening. Tests use fake provider responses and public fixtures; no keys or paid inference are used.

## Primary API references

Checked 2026-10-29: [Meta quickstart](https://dev.meta.ai/docs/cookbook/quickstart-chat-completions), [Meta chat-completions parameters](https://dev.meta.ai/docs/protocols/chat-completions), and [Ollama generate endpoint](https://docs.ollama.com/api/generate). The Meta adapter uses the documented `messages`, `developer`, `max_completion_tokens`, `reasoning_effort`, and `choices[0].message.content` fields. API availability and model access must be rechecked before the live demonstration.
