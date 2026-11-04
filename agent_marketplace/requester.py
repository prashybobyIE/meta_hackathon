"""Requester-owned graph, dispatch, independent verification and assembly."""
from __future__ import annotations

import argparse
import hmac
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
from urllib.parse import urlparse

from .protocol import ApiError, CAPABILITY, MAX_BODY_BYTES, canonical_hash, decode_json, json_request, new_id, sha256_text
from .planner import COMPLEXITY_CREDITS, reference_plan
from .security import assess_execution, require_public_work, validate_transport_url
from .transport import caller_is_loopback, client_tls_context, http_listener, is_loopback_host, server_tls_context
from .verification import verify_artifact
from .workload import default_workload, resolve_workload

PROVIDER_BACKOFF_BASE_SECONDS = 2.0
PROVIDER_BACKOFF_MAX_SECONDS = 30.0


def provider_failure_is_transient(status):
    return status in (408, 425, 429) or 500 <= status <= 599


def private_json(path: Path, value):
    """Atomic requester state; credentials stay in requester-owned local files."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Requester:
    def __init__(self, market_url, requester_token, workspace, *, lease_seconds=4,
                 total_budget=None, allow_meta=False, max_attempts=5, on_event=None, planner=None,
                 max_repairs=2, bid_window=0.0, offer_wait=60.0, budget_status=None,
                 allow_insecure_public_network=False, ca_file=None, workload=None):
        self.workload = resolve_workload(workload)
        self.allow_insecure_public_network = bool(allow_insecure_public_network)
        self.ssl_context = client_tls_context(ca_file)
        self.market_url = validate_transport_url(market_url, self.allow_insecure_public_network)
        self.planner, self.max_repairs, self.bid_window, self.offer_wait = planner, max_repairs, bid_window, offer_wait
        self.budget_status = budget_status
        self.token = requester_token
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.workspace / "requester-state.json"
        self.lock = threading.RLock()
        self.lease_seconds = float(lease_seconds)
        if not 0.1 <= self.lease_seconds <= 3600:
            raise ValueError("Invalid lease duration")
        total_budget = self.workload.budget_credits if total_budget is None else total_budget
        if type(total_budget) is not int or total_budget < 1:
            raise ValueError("Invalid job budget")
        self.total_budget = total_budget
        self.allow_meta = bool(allow_meta)
        self.max_attempts = max_attempts
        self.on_event = on_event
        self.requester_url = None
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
            if self.state.get("version") != 1:
                raise ValueError("Unsupported requester state")
            if self.state.get("workload", default_workload().identity) != self.workload.identity:
                raise ValueError("Requester state belongs to a different workload manifest")
        else:
            job_id = new_id("job")
            self.state = {
                "version": 1, "job_id": job_id, "started_at": time.time(), "workload": self.workload.identity,
                "tasks": {kind: {"task_id": job_id + "_" + kind, "advertised": False, "accepted": False, "attempts": 0, "failed_workers": []} for kind in self.workload.tasks},
                "attempts": {}, "events": [], "complete": False,
            }
            self.state["confidential_policy"] = assess_execution("confidential", {"secure": True}).to_dict()
            self.emit("confidential_dispatch_refused")
            self.save()

    def plan(self):
        """Settle the graph once, before anything is advertised. Survives restart."""
        if not self.state.get("plan"):
            self.state["plan"] = self.planner() if self.planner else reference_plan(workload=self.workload)
            self.emit("plan_settled", source=self.state["plan"]["source"])
            self.save()
        return self.state["plan"]

    def record_protected(self, record):
        """Metadata only. This step is separate from market bidding and credit settlement."""
        with self.lock:
            self.state["protected"] = record
            release, delivery = record.get("release_state"), record.get("delivery_state")
            if release == "not_released" and delivery == "not_submitted":
                event = "protected_task_not_submitted" if record.get("verified") is True else "attestation_refused"
            elif release == "submitted" and delivery == "response_received" and record.get("verified") is True:
                event = "protected_task_accepted" if record.get("accepted") is True else "protected_task_rejected"
            else:
                # Includes ambiguous delivery and legacy records without release state.
                event = "protected_delivery_unknown"
            self.emit(event, release_state=release or "unknown", delivery_state=delivery or "unknown", reason=record.get("reason"))
            self.save()

    def save(self):
        private_json(self.path, self.state)

    def emit(self, name, **fields):
        entry = {"time": time.time(), "event": name, **fields}
        self.state["events"].append(entry)
        self.state["events"] = self.state["events"][-1000:]
        if self.on_event:
            self.on_event(entry)

    def market(self, method, path, body=None):
        return json_request(method, self.market_url + path, body, token=self.token, ssl_context=self.ssl_context)

    def current(self, task_id):
        return self.market("GET", "/v1/tasks/" + task_id)

    def committed_credits(self):
        return sum(self.state["attempts"][task["accepted_attempt"]]["credits"] for task in self.state["tasks"].values() if task["accepted"])

    def reserved_credits(self):
        total = 0
        for task in self.state["tasks"].values():
            if task["accepted"]:
                continue
            record = self.state["attempts"].get(task.get("current_attempt"))
            if record and record.get("status") in ("pending", "assigned") and record["lease_until"] > time.time():
                total += record["credits"]
        return total

    def attempt(self, attempt_id, token):
        with self.lock:
            record = self.state["attempts"].get(attempt_id)
            if not record or not isinstance(token, str) or not hmac.compare_digest(record["token"], token):
                raise ApiError(401, "Invalid attempt credential")
            require_public_work(record["sensitivity"])
            if time.time() >= record["lease_until"]:
                raise ApiError(409, "Attempt expired")
            task = self.current(record["task_id"])
            assignment = task.get("assignment")
            if task["task"]["state"] != "assigned" or not assignment or assignment["attempt_id"] != attempt_id:
                raise ApiError(409, "Attempt is no longer authorized")
            return record

    def accepted_dependencies(self, kind):
        """Accepted artifacts this task consumes, by name. Refuses until every one is accepted."""
        delivered = {}
        for name in self.workload.tasks[kind].consumes:
            task = self.state["tasks"][name]
            if not task["accepted"]:
                raise ApiError(409, "Dependency artifact is not accepted yet")
            result = self.state["attempts"][task["accepted_attempt"]]["result"]
            contract = self.workload.contracts[name]
            delivered[name] = {"contract_id": contract["id"], "filename": contract["filename"], "function": contract["function"], "sha256": result["sha256"], "content": result["content"]}
        return delivered

    def dispatch(self, attempt_id, token):
        with self.lock:
            record = self.attempt(attempt_id, token)
            dependencies = self.accepted_dependencies(record["kind"])
            dispatch = {
                "task_id": record["task_id"], "attempt_id": attempt_id,
                "sensitivity": "public", "contract": self.workload.contracts[record["kind"]],
                "provider_policy": {"allow_meta": self.allow_meta}, "deadline": record["lease_until"],
            }
            if dependencies:
                dispatch["dependencies"] = dependencies
            repair = self.state["tasks"][record["kind"]].get("repair")
            if repair:
                dispatch["repair"] = repair
            return dispatch

    def report_failure(self, attempt_id, token, body):
        with self.lock:
            record = self.attempt(attempt_id, token)
            if not isinstance(body, dict) or set(body) != {"status"} or type(body["status"]) is not int or not 100 <= body["status"] <= 599:
                raise ApiError(400, "Invalid failure report")
            if not record.get("result") and not record.get("failure"):
                record["failure"] = body["status"]
                self.save()
            return {"received": True}

    def receive(self, attempt_id, token, body):
        with self.lock:
            record = self.attempt(attempt_id, token)
            if not isinstance(body, dict) or set(body) - {"task_id", "attempt_id", "filename", "content", "sha256", "provider", "model", "elapsed_ms", "usage", "cost_usd"}:
                raise ApiError(400, "Invalid artifact envelope")
            contract = self.workload.contracts[record["kind"]]
            content = body.get("content")
            if body.get("task_id") != record["task_id"] or body.get("attempt_id") != attempt_id or body.get("filename") != contract["filename"]:
                raise ApiError(400, "Artifact does not match attempt")
            if not isinstance(content, str) or len(content.encode()) > 32768 or body.get("sha256") != sha256_text(content):
                raise ApiError(400, "Invalid artifact source or digest")
            if not all(isinstance(body.get(key), str) and 0 < len(body[key]) <= 100 for key in ("provider", "model")):
                raise ApiError(400, "Missing model/provider provenance")
            if type(body.get("elapsed_ms")) not in (int, float) or not 0 <= body["elapsed_ms"] <= 3600000:
                raise ApiError(400, "Invalid elapsed time")
            usage = body.get("usage", {})
            if not isinstance(usage, dict) or len(usage) > 8 or any(type(v) is not int for v in usage.values()):
                raise ApiError(400, "Invalid usage")
            if "cost_usd" in body and type(body["cost_usd"]) not in (int, float):
                raise ApiError(400, "Invalid reported cost")
            if record.get("result"):
                if canonical_hash(record["result"]) != canonical_hash(body):
                    raise ApiError(409, "Conflicting duplicate artifact")
                return {"received": True, "duplicate": True}
            record["result"] = body
            self.emit("artifact_received", task_id=record["task_id"], attempt_id=attempt_id, artifact_sha256=body["sha256"])
            self.save()
            return {"received": True}

    def tick(self, schedule=True):
        with self.lock:
            if self.state["complete"]:
                return True
            if not self.requester_url:
                raise ValueError("Start requester server before scheduling")
            plan = self.plan()
            accepted = {kind for kind, task in self.state["tasks"].items() if task["accepted"]}
            for kind, task in self.state["tasks"].items():
                task_id = task["task_id"]
                if task["accepted"]:
                    continue
                if not task["advertised"]:
                    if not set(plan["tasks"][kind]["depends_on"]) <= accepted:
                        continue
                    descriptor = {"task_id": task_id, "capability": CAPABILITY, "sensitivity": "public", "max_credits": COMPLEXITY_CREDITS[plan["tasks"][kind]["complexity"]], "expires_at": time.time() + 3600}
                    try:
                        self.market("POST", "/v1/tasks", descriptor)
                    except ApiError as exc:
                        if exc.status != 409:
                            raise
                    task["advertised"] = True
                    task["advertised_at"] = time.time()
                    self.emit("ready_task_advertised", task_id=task_id, kind=kind)
                    self.save()
                if not schedule:
                    continue
                status = self.current(task_id)
                if status["task"]["state"] == "accepted":
                    # Reconcile after a crash between remote receipt and local commit.
                    attempt_id = status["assignment"]["attempt_id"] if status.get("assignment") else task.get("current_attempt")
                    record = self.state["attempts"].get(attempt_id)
                    if not record or not record.get("report", {}).get("passed") or not record.get("result"):
                        raise RuntimeError("Accepted marketplace state has no local verified artifact")
                    self._commit(kind, record)
                    accepted.add(kind)
                    continue
                if status["task"]["state"] == "assigned":
                    assignment = status["assignment"]
                    record = self.state["attempts"].get(assignment["attempt_id"])
                    if not record:
                        raise RuntimeError("Authorized attempt has no prepared requester dispatch state")
                    record["status"] = "assigned"
                    record["lease_until"] = assignment["lease_until"]
                    task["current_attempt"] = assignment["attempt_id"]
                    self.save()
                    if record.get("failure") and not record.get("result"):
                        self.market("POST", "/v1/tasks/" + task_id + "/reject", {"attempt_id": assignment["attempt_id"], "reason": "provider_failed"})
                        record["status"] = "failed"
                        task.pop("current_attempt", None)
                        # 400/401/402/403 will repeat on this worker, so it gets no more attempts.
                        if record["failure"] in (400, 401, 402, 403):
                            task["failed_workers"].append(record["worker_id"])
                        elif provider_failure_is_transient(record["failure"]):
                            backoff = task.setdefault("provider_backoff", {})
                            failures = backoff.get(record["worker_id"], {}).get("failures", 0) + 1
                            delay = min(PROVIDER_BACKOFF_MAX_SECONDS, PROVIDER_BACKOFF_BASE_SECONDS * 2 ** min(failures - 1, 4))
                            backoff[record["worker_id"]] = {"failures": failures, "retry_at": time.time() + delay, "status": record["failure"]}
                            self.emit("provider_cooldown", task_id=task_id, worker_id=record["worker_id"], status=record["failure"], retry_after_seconds=delay)
                        task["last_failure"] = f"{record['worker_id']} provider status {record['failure']}"
                        self.emit("attempt_failed", task_id=task_id, attempt_id=assignment["attempt_id"], worker_id=record["worker_id"], status=record["failure"])
                        self.save()
                        continue
                    if not record.get("result"):
                        continue
                    report = verify_artifact(kind, record["result"], self.workload, self.accepted_dependencies(kind))
                    record["report"] = report
                    self.save()
                    if report["passed"]:
                        receipt = {
                            "attempt_id": assignment["attempt_id"], "artifact_sha256": record["result"]["sha256"],
                            "report_sha256": canonical_hash(report), "receipt_id": record["receipt_id"],
                        }
                        try:
                            self.market("POST", "/v1/tasks/" + task_id + "/accept", receipt)
                        except ApiError as exc:
                            if exc.status != 409:
                                raise
                            continue
                        self._commit(kind, record)
                        accepted.add(kind)
                    else:
                        self.market("POST", "/v1/tasks/" + task_id + "/reject", {"attempt_id": assignment["attempt_id"], "reason": "independent_acceptance_failed"})
                        record["status"] = "rejected"
                        task.pop("current_attempt", None)
                        task["rejections"] = task.get("rejections", 0) + 1
                        task.setdefault("rejected_by", {})
                        task["rejected_by"][record["worker_id"]] = task["rejected_by"].get(record["worker_id"], 0) + 1
                        if report.get("diagnostic"):
                            task["repair"] = {"previous_source": record["result"]["content"], "diagnostic": report["diagnostic"]}
                        self.emit("artifact_rejected", task_id=task_id, attempt_id=assignment["attempt_id"], worker_id=record["worker_id"], reason=report.get("diagnostic") or report.get("reason"))
                        self.save()
                        if task["rejections"] > self.max_repairs:
                            raise RuntimeError(f"Module {kind} failed acceptance after {self.max_repairs} repairs; stopping to limit cost")
                    self.save()
                    continue
                # Lease expired/rejected: fence it locally too, and choose another offer.
                previous = task.pop("current_attempt", None)
                if previous:
                    record = self.state["attempts"].get(previous)
                    if record:
                        record["status"] = "expired"
                        task["failed_workers"].append(record["worker_id"])
                    self.emit("attempt_expired_or_reopened", task_id=task_id, attempt_id=previous)
                if task["attempts"] >= self.max_attempts:
                    raise RuntimeError("Task attempt limit exhausted")
                if time.time() < task.get("advertised_at", 0) + self.bid_window:
                    continue
                listed = self.market("GET", "/v1/tasks/" + task_id + "/bids")["bids"]
                usable = [b for b in listed if b["worker_id"] not in task["failed_workers"]]
                backoff = task.get("provider_backoff", {})
                now = time.time()
                offers = [b for b in usable if backoff.get(b["worker_id"], {}).get("retry_at", 0) <= now]
                if not offers:
                    if usable:
                        # Eligible workers are deliberately cooling. This is not a lack of
                        # offers, and repeated polls must not consume more attempts or credit.
                        task.pop("no_offers_since", None)
                        self.save()
                        continue
                    if listed and task.get("last_failure"):
                        raise RuntimeError(f"No usable workers remain for {kind}; last failure: {task['last_failure']}")
                    task.setdefault("no_offers_since", time.time())
                    if time.time() - task["no_offers_since"] > self.offer_wait:
                        raise RuntimeError(f"No offers for {kind} after {self.offer_wait:g} seconds")
                    self.save()
                    continue
                task.pop("no_offers_since", None)
                delivered = {}
                for completed in self.state["tasks"].values():
                    if completed["accepted"]:
                        worker_id = self.state["attempts"][completed["accepted_attempt"]]["worker_id"]
                        delivered[worker_id] = delivered.get(worker_id, 0) + 1
                # Provisional policy: quote, plus penalties for load already delivered and for
                # rejections on this task. It is transparent, not a measured speed or cost optimum.
                models = {w["worker_id"]: w["provider"] + " / " + w["model"] for w in self.market("GET", "/v1/snapshot")["workers"]}
                scored = []
                for b in offers:
                    load, rejected = delivered.get(b["worker_id"], 0), task.get("rejected_by", {}).get(b["worker_id"], 0)
                    scored.append({"worker_id": b["worker_id"], "credits": b["credits"], "load_penalty": 5 * load, "rejection_penalty": 20 * rejected, "score": b["credits"] + 5 * load + 20 * rejected, "transient_failures": backoff.get(b["worker_id"], {}).get("failures", 0), "model": models.get(b["worker_id"], "unknown")})
                # Give an unfailed worker a chance before returning to a failing provider,
                # even after its cooldown expires. Otherwise retain the existing offer score.
                scored.sort(key=lambda o: (o["transient_failures"], o["score"], o["worker_id"]))
                bid = next(b for b in offers if b["worker_id"] == scored[0]["worker_id"])
                winner = scored[0]
                selection = f"{winner['worker_id']} has {winner['transient_failures']} transient failure(s), then scored {winner['score']} ({winner['credits']} quote + {winner['load_penalty']} load + {winner['rejection_penalty']} rejection), preferred of {len(scored)} offer(s)"
                committed = self.committed_credits()
                reserved = self.reserved_credits()
                if committed + reserved + bid["credits"] > self.total_budget:
                    raise RuntimeError("Job reward budget exhausted")
                attempt_id = new_id("attempt")
                dispatch_token = secrets.token_urlsafe(32)
                record = {"kind": kind, "task_id": task_id, "attempt_id": attempt_id, "worker_id": bid["worker_id"], "credits": bid["credits"], "token": dispatch_token, "lease_until": time.time() + self.lease_seconds, "sensitivity": "public", "receipt_id": new_id("receipt"), "status": "pending", "offers": scored, "selection": selection, "repair_round": task.get("rejections", 0) if task.get("repair") else 0, "started_at": time.time()}
                self.state["attempts"][attempt_id] = record
                task["attempts"] += 1
                task["current_attempt"] = attempt_id
                self.save()  # Dispatch state exists before worker observes assignment.
                try:
                    assigned = self.market("POST", "/v1/tasks/" + task_id + "/assign", {"bid_id": bid["bid_id"], "attempt_id": attempt_id, "lease_seconds": self.lease_seconds, "requester_url": self.requester_url, "dispatch_token": dispatch_token})
                except ApiError as exc:
                    if exc.status >= 500:
                        # A transport failure may follow a committed assignment.
                        # Keep the prepared token and reconcile on the next tick.
                        self.emit("assignment_outcome_pending", task_id=task_id, attempt_id=attempt_id)
                        self.save()
                        continue
                    record["status"] = "not_assigned"
                    task.pop("current_attempt", None)
                    task["attempts"] -= 1
                    self.save()
                    if exc.status == 409:
                        continue
                    raise
                record["lease_until"] = assigned["lease_until"]
                record["status"] = "assigned"
                self.emit("attempt_authorized", task_id=task_id, attempt_id=attempt_id, worker_id=bid["worker_id"], credits=bid["credits"], reason=selection)
                self.save()
            if all(task["accepted"] for task in self.state["tasks"].values()):
                self.assemble()
                return True
            return False

    def _commit(self, kind, record):
        task = self.state["tasks"][kind]
        if task["accepted"]:
            return
        target = self.workspace / "artifacts" / self.workload.contracts[kind]["filename"]
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_text(record["result"]["content"])
        os.chmod(target, 0o600)
        private_json(target.with_suffix(".acceptance.json"), record["report"])
        task["accepted"] = True
        task.pop("provider_backoff", None)
        record["status"] = "accepted"
        task["accepted_attempt"] = record["attempt_id"]
        self.emit("task_accepted_and_paid", task_id=task["task_id"], attempt_id=record["attempt_id"], worker_id=record["worker_id"], credits=record["credits"])
        self.save()

    def assemble(self):
        from .report import build_report, render_report
        report = build_report(self.workspace / "artifacts", self.workload.integration["input"], self.workload)
        if report != self.workload.integration["expect"]:
            raise RuntimeError("Final integration suite failed; accepted task receipts remain valid")
        private_json(self.workspace / "report.json", report)
        (self.workspace / "report.html").write_text(render_report(self.workload, report))
        self.state["complete"] = True
        self.state["integration"] = {"passed": True, "report": report, "runtime": "bounded-python-subset-v1"}
        self.emit("application_integration_passed")
        self.save()

    @staticmethod
    def _attempt_view(record):
        """Metadata only: never the dispatch token or artifact source."""
        result = record.get("result") or {}
        report = record.get("report") or {}
        return {
            "kind": record.get("kind"), "worker_id": record.get("worker_id"), "status": record.get("status"), "credits": record.get("credits"),
            "offers": record.get("offers", []), "selection": record.get("selection"), "repair_round": record.get("repair_round", 0),
            "provider": result.get("provider"), "model": result.get("model"), "elapsed_ms": result.get("elapsed_ms"),
            "usage": result.get("usage"), "cost_usd": result.get("cost_usd"), "failure": record.get("failure"),
            "diagnostic": report.get("diagnostic") or report.get("reason") if not report.get("passed") else None,
        }

    def status(self):
        with self.lock:
            return {
                "job_id": self.state["job_id"], "complete": self.state["complete"],
                "workload": {"id": self.workload.id, "title": self.workload.title},
                "execution_policy": assess_execution("public").to_dict(),
                "tasks": [{"kind": k, "task_id": t["task_id"], "accepted": t["accepted"], "attempts": t["attempts"], "rejections": t.get("rejections", 0)} for k, t in self.state["tasks"].items()],
                "plan": self.state.get("plan"),
                "attempts": [self._attempt_view(a) for a in self.state["attempts"].values()],
                "confidential_policy": self.state.get("confidential_policy") or assess_execution("confidential", {"secure": True}).to_dict(),
                "budget": self.budget_status() if self.budget_status else None,
                "protected": self.state.get("protected"),
                "events": self.state["events"][-100:], "integration": self.state.get("integration"),
                "market": self.market("GET", "/v1/snapshot"),
            }


def create_server(requester, host="127.0.0.1", port=0, advertise_url=None, tls_context=None):
    from .dashboard import DASHBOARD
    # Beyond loopback, status needs the requester credential unless the caller is local.
    restrict_status = not is_loopback_host(host)
    if tls_context is not None and advertise_url and urlparse(advertise_url).scheme != "https":
        raise ValueError("A TLS requester must advertise an HTTPS URL")
    if restrict_status:
        if tls_context is None and not requester.allow_insecure_public_network:
            raise ValueError("Non-loopback requester HTTP requires TLS or the explicit public-data override")
        if not advertise_url:
            raise ValueError("A non-loopback requester needs an advertised URL that remote workers can reach")
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, value, html=False):
            data = value.encode() if html else json.dumps(value, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def handle_request(self, method):
            if self.headers.get("Origin"):
                raise ApiError(403, "Cross-origin requests are disabled")
            path = urlparse(self.path).path
            if method == "GET" and path == "/":
                return self.respond(200, DASHBOARD, html=True)
            if method == "GET" and path == "/health":
                return self.respond(200, {"ok": True, "role": "requester"})
            if method == "GET" and path == "/v1/status":
                if restrict_status and not caller_is_loopback(self.client_address):
                    credential = self.headers.get("Authorization", "")
                    if not hmac.compare_digest(credential.encode("latin-1", "replace"), ("Bearer " + requester.token).encode("latin-1", "replace")):
                        raise ApiError(401, "Unauthorized")
                return self.respond(200, requester.status())
            parts = path.strip("/").split("/")
            if len(parts) not in (3, 4) or parts[:2] != ["v1", "attempts"]:
                raise ApiError(404, "Unknown endpoint")
            credential = self.headers.get("Authorization", "")
            token = credential[7:] if credential.startswith("Bearer ") else None
            if method == "GET" and len(parts) == 3:
                return self.respond(200, requester.dispatch(parts[2], token))
            if method == "POST" and len(parts) == 4 and parts[3] in ("result", "failure"):
                if self.headers.get("Transfer-Encoding"):
                    raise ApiError(400, "Chunked request bodies unsupported")
                try:
                    length = int(self.headers.get("Content-Length", "-1"))
                except ValueError:
                    raise ApiError(400, "Invalid content length") from None
                if not 0 <= length <= MAX_BODY_BYTES:
                    raise ApiError(413, "Invalid body size")
                body = decode_json(self.rfile.read(length))
                handler = requester.receive if parts[3] == "result" else requester.report_failure
                return self.respond(200, handler(parts[2], token, body))
            raise ApiError(404, "Unknown endpoint")

        def do_GET(self):
            self.safe_handle("GET")

        def do_POST(self):
            self.safe_handle("POST")

        def safe_handle(self, method):
            self.connection.settimeout(5)
            try:
                self.handle_request(method)
            except ApiError as exc:
                self.respond(exc.status, {"error": exc.message})
            except (ValueError, TypeError, UnicodeError, RecursionError):
                self.respond(400, {"error": "Invalid request"})
            except Exception:
                self.respond(500, {"error": "Request failed"})
    server = http_listener((host, port), Handler, tls_context)
    server.daemon_threads = True
    requester.requester_url = validate_transport_url(advertise_url, requester.allow_insecure_public_network) if advertise_url else f"{'https' if tls_context else 'http'}://127.0.0.1:{server.server_address[1]}"
    parsed = urlparse(requester.requester_url)
    if parsed.path not in ("", "/"):
        server.server_close()
        raise ValueError("Advertised URL must be a service origin without path")
    return server


def main():
    p = argparse.ArgumentParser(description="Requester-owned public reference application")
    p.add_argument("--market", required=True)
    p.add_argument("--requester-token-file", type=Path, required=True)
    p.add_argument("--workspace", type=Path, default=Path(".demo/requester"))
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--advertise-url", help="HTTPS origin remote public workers use to reach this requester")
    p.add_argument("--lease-seconds", type=float, default=60)
    p.add_argument("--allow-meta", action="store_true")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--bid-window", type=float, default=3, help="Seconds to collect offers before choosing")
    p.add_argument("--allow-insecure-public-network", action="store_true", help="Public data only: HTTP without transport confidentiality")
    p.add_argument("--tls-cert", type=Path, help="Serve HTTPS with this certificate chain")
    p.add_argument("--tls-key", type=Path, help="Private key for --tls-cert")
    p.add_argument("--ca-file", type=Path, help="Trust only this CA or self-signed certificate for an HTTPS market")
    p.add_argument("--timeout", type=float, default=300)
    p.add_argument("--workload", type=Path, help="Workload manifest (default: expense report)")
    args = p.parse_args()
    try:
        tls_context = server_tls_context(args.tls_cert, args.tls_key)
        client_tls_context(args.ca_file)
    except ValueError as exc:
        p.error(str(exc))
    requester = Requester(args.market, args.requester_token_file.read_text().strip(), args.workspace, lease_seconds=args.lease_seconds, allow_meta=args.allow_meta, bid_window=args.bid_window, allow_insecure_public_network=args.allow_insecure_public_network, ca_file=args.ca_file, workload=args.workload)
    server = create_server(requester, host=args.host, port=args.port, advertise_url=args.advertise_url, tls_context=tls_context)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(json.dumps({"dashboard": requester.requester_url, "mode": "ordinary-public-development"}))
    try:
        until = time.monotonic() + args.timeout
        while time.monotonic() < until:
            if requester.tick():
                print(json.dumps({"complete": True, "report": str(requester.workspace / "report.html")}))
                return
            time.sleep(0.1)
        raise SystemExit("Requester timed out")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
