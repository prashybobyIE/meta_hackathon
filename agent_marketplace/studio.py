"""Interactive loopback dashboard over real marketplace/worker runs.

Free rehearsal is the default. Live model calls require an explicit startup opt-in
and the existing API key/spending guard. No cloud resources are provisioned here.
"""
from __future__ import annotations

import argparse
import copy
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
import time

from .budget import load_env_file
from .dashboard import DASHBOARD
from .demo import run_demo
from .protocol import ApiError, sha256_text
from .report import build_report
from .requester import private_json
from .restricted import RestrictedError
from .security import assess_execution
from .workload import default_workload


def component_resources(snapshot):
    """Known execution placement, not invented hardware-utilization telemetry."""
    studio = snapshot.get("studio") or {}
    mode = studio.get("mode", "simulated-meta")
    live = mode == "meta"
    model = (snapshot.get("plan") or {}).get("model") or "muse-spark-1.3"
    local = "This Mac"
    resources = {
        "requester": {"location": local, "runtime": "Python requester", "model": "None", "compute": "Local orchestration CPU", "notes": "Owns job readiness, assignments and accepted artifacts. Utilization is not measured."},
        "planner": {"location": "This Mac → Meta API" if live else local, "runtime": "Validated JSON task planner", "model": model if live else "Prewritten planning response", "compute": "Hosted inference" if live else "Local simulator", "notes": "Three frozen public contracts; required dependencies are validated before dispatch."},
        "market": {"location": local, "runtime": "Python HTTP + SQLite", "model": "None", "compute": "Local service and storage", "notes": "Worker registration, bids, eligibility, reservations and expiring leases."},
        "meta": {"location": "Meta hosted service" if live else local, "runtime": "Meta Model API" if live else "Local API simulator", "model": model if live else "Prewritten responses", "compute": "Remote model compute" if live else "Local fixture delivery", "notes": "Public planning, generation and repair inputs only. No local model GPU is used."},
        "verifier": {"location": local, "runtime": "Bounded Python AST interpreter", "model": "No model grader", "compute": "Local acceptance checks", "notes": "Artifact integrity and independent deterministic cases; no arbitrary exec or host-confidentiality claim."},
        "assembler": {"location": local, "runtime": "Accepted bounded modules", "model": "None", "compute": "Local application evaluation", "notes": "Final integration and public sample CSV use already accepted artifacts, without another model call."},
        "ledger": {"location": local, "runtime": "SQLite credit receipts", "model": "None", "compute": "Local durable storage", "notes": "Credits are simulated. API spending uses a separate shared local guard."},
    }
    workers = {w["worker_id"]: w for w in (snapshot.get("market") or {}).get("workers", [])}
    for worker_id in ("worker-a", "worker-b", "worker-fault"):
        worker = workers.get(worker_id) or {}
        enabled = worker_id != "worker-fault" or studio.get("failure_enabled", True)
        resources[worker_id] = {"location": local, "runtime": "Separate Python worker process" if enabled else "Disabled for this run",
                                "model": worker.get("model") or (model if live else "Prewritten responses"),
                                "compute": "Local agent → hosted Meta inference" if live else "Local agent → simulator",
                                "notes": "Each worker has its own identity, bid and lease. CPU/RAM utilization is not measured." if enabled else "No fault-worker process is launched when the failure scenario is off."}
    return resources


class RunManager:
    """One local run at a time; credentials and paths never enter observer state."""

    def __init__(self, workspace, *, allow_live_meta=False, runner=run_demo, paced=True):
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.allow_live_meta, self.runner, self.paced = allow_live_meta, runner, paced
        self.lock = threading.RLock()
        self.phase, self.mode, self.error = "idle", "simulated-meta", None
        self.run_id, self.run_path, self.requester = None, None, None
        self.thread, self.started_at = None, None
        self.failure_enabled = True
        self.cached = None
        self.stop_event = threading.Event()
        self.application_lock = threading.Lock()
        self.restored = False

    def start(self, body):
        if not isinstance(body, dict) or set(body) != {"provider", "failure"}:
            raise ApiError(400, "Choose a provider and a failure scenario")
        provider, failure = body["provider"], body["failure"]
        if provider not in ("simulated-meta", "meta") or type(failure) is not bool:
            raise ApiError(400, "Unsupported demo configuration")
        if provider == "meta" and not (self.allow_live_meta and os.environ.get("MODEL_API_KEY")):
            raise ApiError(403, "Live Meta is disabled; start the studio with --allow-live-meta and configure your key")
        with self.lock:
            if self.phase in ("starting", "running") or (self.thread and self.thread.is_alive()):
                raise ApiError(409, "A run is already active")
            if self.application_lock.locked():
                raise ApiError(409, "Wait for the current sample evaluation")
            self.phase, self.mode, self.error = "starting", provider, None
            self.restored = False
            self.failure_enabled, self.started_at = failure, time.time()
            self.run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
            self.run_path = self.workspace / self.run_id
            self.requester, self.cached = None, None
            self.stop_event = threading.Event()
            self.thread = threading.Thread(target=self._run, name="dashboard-run", daemon=True)
            self.thread.start()
            return {"started": True, "run_id": self.run_id}

    def _bind(self, requester):
        with self.lock:
            self.requester = requester

    def _ready(self, _):
        with self.lock:
            self.phase = "running"

    def _snapshot(self, snapshot):
        with self.lock:
            self.cached = copy.deepcopy(snapshot)
            private_json(self.run_path / "observer-status.json", {"snapshot": snapshot, "mode": self.mode,
                         "failure_enabled": self.failure_enabled, "started_at": self.started_at})

    def resume_completed(self, run_path):
        """Restore a completed local recording only after rechecking its artifacts."""
        path = Path(run_path).resolve()
        if not path.is_relative_to(self.workspace):
            raise ValueError("Recording must be in the studio workspace")
        saved = json.loads((path / "observer-status.json").read_text())
        snapshot = saved["snapshot"]
        workload = default_workload()
        if (snapshot.get("complete") is not True or not snapshot.get("integration", {}).get("passed")
                or saved.get("mode") not in ("meta", "simulated-meta")
                or len(snapshot.get("tasks", [])) != len(workload.tasks)
                or not all(t.get("accepted") is True for t in snapshot["tasks"])):
            raise ValueError("Recording has no completed accepted application")
        accepted = {a.get("kind"): a for a in snapshot.get("attempts", []) if a.get("status") == "accepted"}
        if set(accepted) != set(workload.tasks):
            raise ValueError("Recording has incomplete task acceptance")
        artifacts = path / "requester" / "artifacts"
        for kind, task in workload.tasks.items():
            source = (artifacts / task.filename).read_text()
            if not accepted[kind].get("report", {}).get("passed") or sha256_text(source) != accepted[kind].get("artifact_sha256"):
                raise ValueError("Recording artifact does not match acceptance")
        if build_report(artifacts, workload.integration["input"], workload) != workload.integration["expect"]:
            raise ValueError("Recording application no longer passes integration")
        with self.lock:
            if self.phase in ("starting", "running"):
                raise ValueError("Cannot resume while a run is active")
            self.cached, self.requester = snapshot, None
            self.mode, self.phase, self.error = saved["mode"], "complete", None
            self.run_path, self.run_id = path, path.name
            self.failure_enabled, self.started_at = bool(saved.get("failure_enabled")), saved.get("started_at")
            self.restored = True

    def _run(self):
        try:
            free = self.mode == "simulated-meta"
            self.runner(self.run_path, provider=self.mode, failure=self.failure_enabled,
                        timeout=300, quiet=True, lease_seconds=3 if free else 75,
                        execution_delay=0.9 if free and self.paced else 0.12,
                        bid_window=1.0 if self.paced else 0,
                        on_requester=self._bind, on_ready=self._ready,
                        on_snapshot=self._snapshot, stop_event=self.stop_event)
            with self.lock:
                self.phase = "complete"
        except Exception:
            # Never expose arbitrary exception text: it can contain paths or keys.
            with self.lock:
                self.phase, self.error = "failed", "Run stopped before completion. Review the component trace and retry."

    @staticmethod
    def _empty():
        return {"job_id": None, "complete": False, "workload": {"id": "expense-report", "title": "Expense report"},
                "tasks": [], "attempts": [], "plan": None, "budget": None, "protected": None,
                "events": [], "integration": None, "confidential_policy": assess_execution("confidential").to_dict(),
                "market": {"workers": [], "tasks": [], "bids": [], "attempts": [], "receipts": [], "events": [],
                           "credits": {"reserved": 0, "settled": 0}}}

    def _worker_events(self, run_path):
        entries = []
        if run_path:
            for name in ("worker-a", "worker-b", "worker-fault"):
                path = run_path / (name + ".log")
                try:
                    # Bounded local telemetry only; never return raw log text.
                    with path.open(encoding="utf-8") as stream:
                        for line in stream:
                            if len(line) > 8192:
                                continue
                            try:
                                entry = json.loads(line)
                            except (ValueError, UnicodeError):
                                continue
                            if isinstance(entry, dict) and entry.get("worker_id") == name:
                                entries.append(entry)
                except (OSError, UnicodeError):
                    pass
        return entries[-1000:]

    def status(self):
        # A new run can begin while a previous status request is being assembled.
        # Retry rather than label an old job with the next run's identity.
        for _ in range(2):
            snapshot = self._collect_status()
            if snapshot is not None:
                return snapshot
        with self.lock:
            snapshot = self._empty()
            snapshot["studio"] = self._studio_view()
        snapshot.update(trace=[], application_sample=default_workload().integration["input"])
        snapshot["resources"] = component_resources(snapshot)
        return snapshot

    def _studio_view(self):
        """Called only while holding the manager lock."""
        return {"phase": self.phase, "mode": self.mode, "can_start": self.phase not in ("starting", "running") and not (self.thread and self.thread.is_alive()) and not self.application_lock.locked(),
                "meta_available": bool(self.allow_live_meta and os.environ.get("MODEL_API_KEY")),
                "run_id": self.run_id, "error": self.error, "worker_count": 3 if self.failure_enabled else 2,
                "failure_enabled": self.failure_enabled, "started_at": self.started_at, "restored": self.restored}

    def _collect_status(self):
        # Never hold our lock across Requester.status(): it performs HTTP and takes
        # a different lock. The runner's final callback freezes the last snapshot.
        with self.lock:
            requester, run_path, run_id = self.requester, self.run_path, self.run_id
            phase, mode = self.phase, self.mode
            snapshot = copy.deepcopy(self.cached)
        if requester is not None and phase in ("starting", "running"):
            try:
                snapshot = requester.status()
                with self.lock:
                    if self.run_id == run_id and self.phase in ("starting", "running"):
                        self.cached = copy.deepcopy(snapshot)
            except (ApiError, OSError, RuntimeError):
                pass
        snapshot = copy.deepcopy(snapshot) if snapshot is not None else self._empty()
        from .trace import build_trace
        snapshot["trace"] = build_trace(snapshot, self._worker_events(run_path))
        budget = snapshot.get("budget")
        if budget:
            # This screen is shareable: show this run, never cumulative account use.
            run_cost = budget.get("run_used_usd", 0)
            snapshot["budget"] = {"used_usd": run_cost, "run_used_usd": run_cost,
                                  "run_cap_usd": budget.get("run_cap_usd"),
                                  "calls": sum(e.get("event") == "model_call_started" for e in snapshot["trace"])}
        with self.lock:
            if self.run_id != run_id or self.phase != phase:
                return None
            snapshot["studio"] = self._studio_view()
        snapshot["application_sample"] = default_workload().integration["input"]
        snapshot["resources"] = component_resources(snapshot)
        return snapshot

    def run_application(self, body):
        if not isinstance(body, dict) or set(body) != {"input"} or not isinstance(body["input"], str):
            raise ApiError(400, "Provide public expense CSV text")
        if len(body["input"].encode("utf-8")) > 64 * 1024:
            raise ApiError(413, "Public sample input exceeds 64 KiB")
        with self.lock:
            if self.phase != "complete" or not self.cached or not self.cached.get("complete"):
                raise ApiError(409, "Wait for independently accepted modules and final integration")
            artifacts = self.run_path / "requester" / "artifacts"
            run_id = self.run_id
            if not self.application_lock.acquire(blocking=False):
                raise ApiError(409, "A sample is already being evaluated")
        try:
            return {"report": build_report(artifacts, body["input"]), "run_id": run_id}
        except (RestrictedError, ValueError, TypeError, KeyError, IndexError, ArithmeticError, RecursionError):
            raise ApiError(400, "The accepted application rejected this sample; check the CSV columns and amounts") from None
        finally:
            self.application_lock.release()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)


def create_server(manager, port=8765):
    session = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, value, html=False):
            raw = value.encode() if html else json.dumps(value, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def handle_request(self, method):
            self.connection.settimeout(5)
            port_number = self.server.server_address[1]
            hosts = {f"127.0.0.1:{port_number}", f"localhost:{port_number}"}
            host = self.headers.get("Host", "")
            if host not in hosts:
                raise ApiError(403, "Use the local dashboard address")
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {"http://" + h for h in hosts}:
                raise ApiError(403, "Cross-origin requests are disabled")
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                raise ApiError(403, "Cross-site requests are disabled")
            if method == "GET" and self.path == "/":
                page = DASHBOARD.replace("</title>", f'</title><meta name="demo-session" content="{session}">', 1)
                return self.reply(200, page, html=True)
            if method == "GET" and self.path == "/health":
                return self.reply(200, {"ok": True, "role": "local-dashboard"})
            if method == "GET" and self.path == "/v1/status":
                return self.reply(200, manager.status())
            if method != "POST" or self.path not in ("/v1/runs", "/v1/application/run"):
                raise ApiError(404, "Unknown endpoint")
            if not hmac.compare_digest(self.headers.get("X-Demo-Session", ""), session):
                raise ApiError(403, "Dashboard session required")
            if self.headers.get("Transfer-Encoding"):
                raise ApiError(400, "Chunked request bodies are unsupported")
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise ApiError(415, "JSON body required")
            try:
                size = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                raise ApiError(400, "Invalid body length") from None
            if not 0 < size <= 128 * 1024:
                raise ApiError(413, "Body exceeds the demo limit")
            try:
                body = json.loads(self.rfile.read(size))
            except (ValueError, UnicodeError):
                raise ApiError(400, "Invalid JSON") from None
            if self.path == "/v1/runs":
                return self.reply(202, manager.start(body))
            return self.reply(200, manager.run_application(body))

        def safe(self, method):
            try:
                self.handle_request(method)
            except ApiError as exc:
                self.reply(exc.status, {"error": exc.message})
            except Exception:
                self.reply(500, {"error": "The local dashboard could not complete this request"})

        def do_GET(self):
            self.safe("GET")

        def do_POST(self):
            self.safe("POST")

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", type=Path, default=Path(".demo/studio"))
    parser.add_argument("--allow-live-meta", action="store_true", help="Enable explicit billable runs from the browser using the existing spend guard")
    parser.add_argument("--resume-completed-run", type=Path, help="Show a completed recording from this workspace after checking accepted artifacts; makes no model call")
    args = parser.parse_args()
    load_env_file()
    manager = RunManager(args.workspace, allow_live_meta=args.allow_live_meta)
    if args.resume_completed_run:
        try:
            manager.resume_completed(args.resume_completed_run)
        except (OSError, ValueError, KeyError, TypeError):
            raise SystemExit("The completed recording could not be independently restored") from None
    server = create_server(manager, args.port)
    print(json.dumps({"dashboard": f"http://127.0.0.1:{server.server_address[1]}", "live_meta_enabled": args.allow_live_meta}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        manager.close()
        server.server_close()


if __name__ == "__main__":
    main()
