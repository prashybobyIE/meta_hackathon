"""Launch a complete public protocol demonstration with real worker processes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time

from .budget import BudgetLedger, load_env_file
from .protocol import ApiError, json_request
from .transport import client_tls_context, server_tls_context
from .market import MarketStore, create_server as market_server
from .planner import plan_with_meta
from .providers import MetaProvider, SimulatedMetaProvider
from .requester import Requester, create_server as requester_server, private_json
from .security import assess_execution
from .simulated_meta import create_server as simulated_server
from .workload import resolve_workload


WORKER_PROVIDERS = ("fixture", "meta", "ollama", "simulated-meta")
MAX_LOCAL_WORKERS = 8


def write_token(path, token):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(token + "\n")


def parse_worker_specs(text):
    """Turn 'ollama=llama3.2:3b,meta=muse-spark-1.3' into one (provider, model) per worker."""
    specs = []
    for item in text.split(","):
        name, _, node_model = item.strip().partition("=")
        if name not in WORKER_PROVIDERS or (name in ("meta", "ollama") and not node_model):
            raise ValueError(f"Worker spec needs a provider from {', '.join(WORKER_PROVIDERS)} and, for a real model, =MODEL")
        specs.append((name, node_model or None))
    if len(specs) > MAX_LOCAL_WORKERS:
        raise ValueError(f"At most {MAX_LOCAL_WORKERS} workers")
    return specs


def run_demo(workspace, *, failure=False, provider="fixture", model=None, timeout=45,
             hold_seconds=0, on_ready=None, quiet=False, lease_seconds=None, advertise_host=None, remote_workers=0, single_worker=False, protected_runner=None,
             tls_cert=None, tls_key=None, ca_file=None, workload=None, worker_specs=None, use_planner=True):
    workload = resolve_workload(workload)
    tls_context = server_tls_context(tls_cert, tls_key)
    # A self-signed certificate is its own trust anchor unless a CA file is named.
    ca_file = Path(ca_file or tls_cert).resolve() if tls_context else None
    client_context = client_tls_context(ca_file)
    workspace = Path(workspace).resolve()
    kinds = {provider}
    if worker_specs:
        # Each worker runs its own provider; the job-level settings below follow from the set.
        if failure or single_worker or model:
            raise ValueError("Per-worker providers cannot be combined with --failure, --single-worker or --model")
        kinds = {name for name, _ in worker_specs}
        if {"meta", "simulated-meta"} <= kinds:
            raise ValueError("A run cannot mix the real and simulated Meta providers")
        provider = next(iter(kinds)) if len(kinds) == 1 else "mixed"
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (workspace / "market.sqlite3").exists():
        raise ValueError("Use a fresh workspace for each demo; standalone services support persistence/restart")
    requester_token, enrollment_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    req_file, enroll_file = workspace / "requester.token", workspace / "enrollment.token"
    write_token(req_file, requester_token)
    write_token(enroll_file, enrollment_token)
    # Workers get only the public projection, and only for a workload they do not ship with.
    contracts_file = None
    if not workload.is_default:
        contracts_file = workspace / "public-contracts.json"
        private_json(contracts_file, workload.public_contracts())
    remote = remote_workers > 0
    if remote and not advertise_host:
        raise ValueError("Remote workers need --advertise-host, an address they can reach")
    insecure = remote and tls_context is None
    store = MarketStore(workspace / "market.sqlite3", requester_token, enrollment_token, allow_insecure_public_network=insecure)
    bind = "0.0.0.0" if remote else "127.0.0.1"  # All interfaces only on explicit request.
    market = market_server(store, bind, tls_context=tls_context)
    market_port = market.server_address[1]
    scheme = "https" if tls_context else "http"
    # With TLS, local parties use the advertised name so they verify the same certificate identity.
    market_url = f"{scheme}://{advertise_host if remote and tls_context else '127.0.0.1'}:{market_port}"
    lease = lease_seconds or (1.0 if failure and provider == "fixture" else (4.0 if provider == "fixture" else 120.0))
    hosted_provider = next((name for name in ("meta", "simulated-meta") if name in kinds), None)
    hosted = hosted_provider is not None
    child_env = dict(os.environ, META_BUDGET_RUN=workspace.name)
    servers, planner, budget_status = [market], None, None
    if hosted_provider == "simulated-meta":
        # Separate ledger: rehearsals never touch the real spending record.
        simulator = simulated_server(workload)
        servers.append(simulator)
        child_env.update(SIMULATED_META_URL=f"http://127.0.0.1:{simulator.server_address[1]}", META_BUDGET_LEDGER=str(workspace / "simulated-ledger.json"))
    if hosted:
        previous_env = {k: os.environ.get(k) for k in ("META_BUDGET_RUN", "SIMULATED_META_URL", "META_BUDGET_LEDGER")}
        os.environ.update({k: child_env[k] for k in previous_env if k in child_env})
        ledger = BudgetLedger.from_env()
        budget_status = ledger.status
        first_model = model.split(",")[0] if model else None
        if worker_specs:
            first_model = next(node_model for name, node_model in worker_specs if name == hosted_provider)
        planner_provider = (SimulatedMetaProvider if hosted_provider == "simulated-meta" else MetaProvider)(model=first_model, ledger=ledger)
        planner = lambda: plan_with_meta(planner_provider, workload)
    requester = Requester(market_url, requester_token, workspace / "requester", lease_seconds=lease, allow_meta=hosted, planner=planner if use_planner else None, budget_status=budget_status, allow_insecure_public_network=insecure, ca_file=ca_file, workload=workload)
    requester_http = None if remote else requester_server(requester, tls_context=tls_context)
    if remote:
        # The requester port must be known before it can be advertised to other machines.
        import socket
        with socket.socket() as probe:
            probe.bind((bind, 0))
            requester_port = probe.getsockname()[1]
        requester_http = requester_server(requester, host=bind, port=requester_port, advertise_url=f"{scheme}://{advertise_host}:{requester_port}", tls_context=tls_context)
    servers.append(requester_http)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    processes, log_handles = [], []
    try:
        for thread in threads:
            thread.start()
        if tls_context:
            try:
                json_request("GET", market_url + "/health", ssl_context=client_context)
            except ApiError as exc:
                raise RuntimeError(f"Could not reach {market_url} over TLS: {exc.message}. The certificate must cover this address and chain to the CA file.") from None
        nodes = [("worker-a", 10)] if single_worker else [("worker-a", 10), ("worker-b", 12)]
        if failure:
            nodes.insert(0, ("worker-fault", 1))
        if worker_specs:
            nodes = [(f"worker-{chr(97 + index)}", 10 + 2 * index) for index in range(len(worker_specs))]
        models = model.split(",") if model else [None]
        for index, (worker_id, price) in enumerate(nodes):
            node_provider, node_model = worker_specs[index] if worker_specs else (provider, models[index % len(models)])
            log = (workspace / (worker_id + ".log")).open("w")
            os.chmod(log.name, 0o600)
            log_handles.append(log)
            command = [sys.executable, "-m", "agent_marketplace.worker", "--market", market_url,
                       "--enrollment-token-file", str(enroll_file), "--id", worker_id,
                       "--provider", node_provider, "--bid-credits", str(price), "--poll-interval", "0.05",
                       "--execution-delay", "0.12"]
            if node_model:
                command += ["--model", node_model]
            if ca_file:
                command += ["--ca-file", str(ca_file)]
            if contracts_file:
                command += ["--contracts-file", str(contracts_file)]
            if worker_id == "worker-fault":
                command += ["--crash-on-first-assignment"]
            processes.append(subprocess.Popen(command, stdout=log, stderr=log, cwd=Path(__file__).resolve().parents[1], env=child_env))
        ready = {"market": market_url, "dashboard": f"{scheme}://127.0.0.1:{requester_http.server_address[1]}", "workspace": str(workspace), "provider": provider, "host_confidentiality_verified": False}
        if not quiet:
            print(json.dumps(ready), flush=True)
        if remote and not quiet:
            if tls_context:
                print(f"Copy enrollment.token and {Path(ca_file).name} (never the private key) to each other Mac, then run there (public data only):", flush=True)
                print(f"python3 -m agent_marketplace.worker --market https://{advertise_host}:{market_port} --ca-file {Path(ca_file).name} --enrollment-token-file enrollment.token --id WORKER_NAME --provider fixture --state-file .demo/worker-state.json" + (" --contracts-file public-contracts.json" if contracts_file else ""), flush=True)
            else:
                print("Copy enrollment.token to each other Mac, then run there (public data only; HTTP is not encrypted):", flush=True)
                print(f"python3 -m agent_marketplace.worker --market http://{advertise_host}:{market_port} --enrollment-token-file enrollment.token --id WORKER_NAME --provider fixture --state-file .demo/worker-state.json --allow-insecure-public-network" + (" --contracts-file public-contracts.json" if contracts_file else ""), flush=True)
        if on_ready:
            on_ready(ready)
        until = time.monotonic() + timeout
        # All workers enroll before readiness is advertised, making failure deterministic.
        while len(store.snapshot()["workers"]) < len(nodes) + remote_workers:
            if time.monotonic() >= until:
                raise TimeoutError("Workers failed to enroll; see worker logs")
            time.sleep(0.05)
        requester.tick(schedule=False)
        task_ids = [t["task_id"] for t in requester.state["tasks"].values() if t["advertised"]]
        while any(len(requester.market("GET", "/v1/tasks/" + task_id + "/bids")["bids"]) < len(nodes) + remote_workers for task_id in task_ids):
            if time.monotonic() >= until:
                raise TimeoutError("Workers failed to bid")
            time.sleep(0.05)
        while not requester.tick():
            if time.monotonic() >= until:
                raise TimeoutError("Demo timed out; inspect local state and worker logs")
            time.sleep(0.05)
        if protected_runner:
            requester.record_protected(protected_runner())
        snapshot = store.snapshot()
        receipts = snapshot["receipts"]
        if len(receipts) != len(workload.tasks) or snapshot["credits"]["reserved"] != 0:
            raise RuntimeError("Settlement invariant failed")
        if failure and not any(a["status"] == "expired" and a["worker_id"] == "worker-fault" for a in snapshot["attempts"]):
            raise RuntimeError("Failure scenario did not exercise lease expiry")
        summary = {
            "complete": True, "workload": workload.id, "provider": provider, "fixture_is_ai_inference": False, "real_ai_inference": bool(kinds & {"meta", "ollama"}),
            "protected": requester.state.get("protected"),
            "plan_source": requester.state["plan"]["source"], "plan_rejected_reason": requester.state["plan"].get("rejected_reason"),
            "rejected_attempts": sum(a["status"] == "rejected" for a in snapshot["attempts"]),
            "attempt_timings": [{k: v for k, v in Requester._attempt_view(a).items() if k in ("kind", "worker_id", "status", "provider", "model", "elapsed_ms", "usage", "cost_usd", "repair_round", "diagnostic")} for a in requester.state["attempts"].values()],
            "budget": budget_status() if budget_status else None,
            "workers_used": sorted({r["worker_id"] for r in receipts}),
            "workers": [{k: w[k] for k in ("worker_id", "provider", "model")} for w in snapshot["workers"]],
            "accepted_tasks": len(receipts), "attempts": len(snapshot["attempts"]),
            "expired_attempts": sum(a["status"] == "expired" for a in snapshot["attempts"]),
            "credits": snapshot["credits"], "integration": requester.state["integration"],
            "confidential_policy": assess_execution("confidential", {"secure": True}).to_dict(),
            "report_html": str(requester.workspace / "report.html"),
            "dashboard": ready["dashboard"], "market": market_url,
            "elapsed_seconds": round(time.time() - requester.state["started_at"], 3),
        }
        private_json(workspace / "summary.json", summary)
        private_json(workspace / "market-snapshot.json", snapshot)
        integration_input = workload.integration["input"]
        (workspace / workload.integration["input_filename"]).write_text(integration_input if isinstance(integration_input, str) else json.dumps(integration_input))
        if not quiet:
            print(json.dumps(summary, indent=2), flush=True)
        if hold_seconds:
            if not quiet:
                print(f"Dashboard remains available for {hold_seconds:g} seconds. Press Ctrl-C to stop.", flush=True)
            end = time.monotonic() + hold_seconds
            while time.monotonic() < end:
                time.sleep(min(0.25, end - time.monotonic()))
        return summary
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for log in log_handles:
            log.close()
        for server in reversed(servers):
            server.shutdown()
            server.server_close()
        for thread in threads:
            if thread.is_alive():
                thread.join(timeout=5)
        store.close()
        if hosted:
            for key, value in previous_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workspace", type=Path)
    p.add_argument("--failure", action="store_true", help="Cheapest worker exits, then lease expires and work is reassigned")
    p.add_argument("--provider", choices=WORKER_PROVIDERS, default="fixture")
    p.add_argument("--model", help="Model name, or comma-separated names assigned to workers in turn")
    p.add_argument("--timeout", type=float, default=300)
    p.add_argument("--lease-seconds", type=float, help="Override the attempt lease")
    p.add_argument("--single-worker", action="store_true", help="One-worker baseline")
    p.add_argument("--reference-plan", action="store_true", help="Skip the Meta planner call and use the fixed reference graph")
    p.add_argument("--workers", help="One provider per worker, overriding --provider: ollama=llama3.2:3b,meta=muse-spark-1.3")
    p.add_argument("--protected-url", help="Attested confidential worker for the private task; needs .venv for cryptography")
    p.add_argument("--protected-policy", type=Path, default=Path("protected_worker/deploy/expected_host_data.txt"), help="File holding the approved policy hash")
    p.add_argument("--protected-trust-policy", type=Path, default=Path("protected_worker/deploy/trust_policy.json"), help="Requester-approved VM measurements and product-specific minimum TCB; the empty template refuses private execution")
    p.add_argument("--advertise-host", help="Address other machines use to reach this one")
    p.add_argument("--remote-workers", type=int, default=0, help="Wait for this many workers from other machines")
    p.add_argument("--tls-cert", type=Path, help="Serve market and requester over HTTPS with this certificate chain; it must cover --advertise-host")
    p.add_argument("--tls-key", type=Path, help="Private key for --tls-cert")
    p.add_argument("--ca-file", type=Path, help="CA that local parties trust for the listeners; defaults to --tls-cert")
    p.add_argument("--hold-seconds", type=float, default=0)
    p.add_argument("--workload", type=Path, help="Workload manifest (default: expense report)")
    args = p.parse_args()
    if args.timeout <= 0 or args.hold_seconds < 0:
        p.error("Timeout must be positive and hold duration nonnegative")
    load_env_file()
    try:
        worker_specs = parse_worker_specs(args.workers) if args.workers else None
    except ValueError as exc:
        p.error(str(exc))
    if (args.provider == "meta" or any(name == "meta" for name, _ in worker_specs or [])) and not os.environ.get("MODEL_API_KEY"):
        p.error("MODEL_API_KEY is not set. Put it in .env (see .env.example), or rehearse with --provider simulated-meta")
    protected_runner = None
    if args.protected_url and args.workload:
        p.error("--protected-url is defined for the default expense workload only")
    if args.protected_url:
        try:
            from .protected import attested_step
            from .snp import AttestationRefused, load_trust_policy
        except ImportError:
            p.error("Protected execution needs cryptography; use the configured .venv interpreter")
        try:
            approved = args.protected_policy.read_text().strip()
            if len(approved) != 64 or any(c not in "0123456789abcdef" for c in approved) or approved == "0" * 64:
                raise ValueError("The approved container policy hash must be 64 lowercase hex characters and cannot be all zero")
            trust_policy = load_trust_policy(args.protected_trust_policy)
        except (OSError, ValueError, AttestationRefused) as exc:
            p.error(f"Protected execution configuration refused: {exc}")
        protected_runner = lambda: attested_step(args.protected_url, approved, trust_policy=trust_policy)
    workspace = args.workspace or Path(".demo") / (time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3))
    try:
        run_demo(workspace, failure=args.failure, provider=args.provider, model=args.model, timeout=args.timeout, hold_seconds=args.hold_seconds, lease_seconds=args.lease_seconds, advertise_host=args.advertise_host, remote_workers=args.remote_workers, single_worker=args.single_worker, protected_runner=protected_runner, tls_cert=args.tls_cert, tls_key=args.tls_key, ca_file=args.ca_file, workload=args.workload, worker_specs=worker_specs, use_planner=not args.reference_plan)
    except KeyboardInterrupt:
        pass
    except (ValueError, TimeoutError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
