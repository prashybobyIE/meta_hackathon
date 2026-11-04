"""Ordinary public-work node: enrollment, bids, leases and source delivery.

This daemon does not execute returned source and does not provide a protected VM.
Only loopback HTTP or authenticated HTTPS endpoints are accepted by default.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlparse

from .budget import load_env_file
from .protocol import ApiError, CAPABILITY, TLS_VERIFY_FAILED, is_loopback_url, json_request, new_id, sha256_text, validate_identifier
from .providers import MAX_SOURCE_BYTES, get_provider, register_public_contracts, validate_public_contract
from .transport import client_tls_context
from .workload import load_public_contracts


class WorkerCrash(RuntimeError):
    """Intentional demo failure before fetching a public attempt's contract."""


def validate_endpoint(url, allow_insecure_public_network=False):
    if not isinstance(url, str) or len(url) > 2048:
        raise ApiError(400, "Invalid endpoint URL")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ApiError(400, "Endpoint must be an HTTP(S) base URL without credentials")
    try:
        parsed.port
    except ValueError:
        raise ApiError(400, "Invalid endpoint port") from None
    if parsed.scheme == "http" and not is_loopback_url(url) and not allow_insecure_public_network:
        raise ApiError(403, "Non-loopback HTTP requires an explicit public-data-only override")
    return url.rstrip("/")


class Worker:
    def __init__(self, market_url, enrollment_token, worker_id, provider="fixture", bid_credits=10,
                 poll_interval=0.25, model=None, *, allow_insecure_public_network=False,
                 execution_delay=0, crash_on_first_assignment=False, on_assignment=None,
                 on_event=None, max_failures=12, provider_options=None, clock=time.time, state_file=None, ca_file=None):
        self.market_url = validate_endpoint(market_url, allow_insecure_public_network)
        self.allow_insecure_public_network = bool(allow_insecure_public_network)
        self.ssl_context = client_tls_context(ca_file)
        self.worker_id = validate_identifier(worker_id, "worker_id")
        if not isinstance(enrollment_token, str) or not 1 <= len(enrollment_token) <= 4096 or any(c in enrollment_token for c in "\r\n"):
            raise ApiError(400, "Invalid enrollment credential")
        if type(bid_credits) is not int or not 1 <= bid_credits <= 1000000:
            raise ApiError(400, "Bid must be a positive bounded integer")
        if not isinstance(poll_interval, (int, float)) or not math.isfinite(poll_interval) or not 0.01 <= poll_interval <= 30:
            raise ApiError(400, "Invalid poll interval")
        if not isinstance(execution_delay, (int, float)) or not math.isfinite(execution_delay) or not 0 <= execution_delay <= 300:
            raise ApiError(400, "Invalid execution delay")
        self._enrollment_token = enrollment_token
        self.worker_token = None
        self.state_file = Path(state_file) if state_file else None
        self.provider = get_provider(provider, model=model, **(provider_options or {}))
        self.bid_credits = bid_credits
        self.poll_interval = float(poll_interval)
        self.execution_delay = float(execution_delay)
        self.crash_on_first_assignment = bool(crash_on_first_assignment)
        self.on_assignment, self.on_event = on_assignment, on_event
        self.max_failures = max(1, min(int(max_failures), 100))
        self.clock = clock
        self._seen_attempts = set()
        self._bid_tasks = set()
        self._crash_triggered = False
        self.last_error = None
        self._heartbeat_stop = threading.Event()

    def _emit(self, event, **fields):
        # Explicit fields only: never forward a remote error, payload or token.
        entry = {"event": event, "worker_id": self.worker_id, **fields}
        if self.on_event:
            self.on_event(entry)

    def _request(self, method, url, body=None, token=None):
        return json_request(method, url, body, token=token, ssl_context=self.ssl_context)

    def register(self):
        if self.worker_token:
            return
        if self.state_file and self.state_file.exists():
            saved = json.loads(self.state_file.read_text())
            if saved.get("worker_id") != self.worker_id:
                raise ApiError(400, "Worker state file belongs to another worker identity")
            self.worker_token = saved["worker_token"]
            self._emit("worker_resumed")
            return
        response = self._request("POST", self.market_url + "/v1/workers/register", {
            "worker_id": self.worker_id,
            "capabilities": [CAPABILITY],
            "provider": self.provider.name,
            "model": self.provider.model,
            "slots": 1,
        }, token=self._enrollment_token)
        token = response.get("worker_token")
        if response.get("worker_id") != self.worker_id or not isinstance(token, str) or not 1 <= len(token) <= 4096 or any(c in token for c in "\r\n"):
            raise ApiError(502, "Invalid worker registration response")
        self.worker_token = token
        self._enrollment_token = ""
        if self.state_file:
            self.state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(self.state_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump({"worker_id": self.worker_id, "worker_token": token}, stream)
        self._emit("worker_registered", provider=self.provider.name, model=self.provider.model, execution_mode="ordinary-public")

    def heartbeat(self):
        if self.worker_token:
            self._request("POST", f"{self.market_url}/v1/workers/{self.worker_id}/heartbeat", {}, token=self.worker_token)

    def _heartbeat_loop(self):
        while not self._heartbeat_stop.wait(2):
            try:
                self.heartbeat()
            except ApiError as exc:
                self._emit("heartbeat_unavailable", status=exc.status)

    def _sleep(self, seconds, stop_event=None):
        if stop_event:
            return stop_event.wait(seconds)
        time.sleep(seconds)
        return False

    def run_once(self, stop_event=None):
        self.register()
        self.heartbeat()
        tasks = self._request("GET", self.market_url + "/v1/tasks", token=self.worker_token).get("tasks")
        if not isinstance(tasks, list) or len(tasks) > 10000:
            raise ApiError(502, "Invalid task listing")
        for task in tasks:
            if not isinstance(task, dict) or task.get("sensitivity") != "public" or task.get("capability") != CAPABILITY:
                continue
            task_id = validate_identifier(task.get("task_id"), "task_id")
            max_credits = task.get("max_credits")
            if task_id in self._bid_tasks or type(max_credits) is not int:
                continue
            # The worker's rate scales with the size the task advertises (20 credits is full size).
            quote = max(1, math.ceil(self.bid_credits * max_credits / 20))
            if quote > max_credits:
                continue
            try:
                self._request("POST", f"{self.market_url}/v1/tasks/{task_id}/bids", {"credits": quote}, token=self.worker_token)
                self._bid_tasks.add(task_id)
                self._emit("bid_submitted", task_id=task_id, credits=quote)
            except ApiError as exc:
                if exc.status not in (403, 404, 409):
                    raise
        response = self._request("GET", f"{self.market_url}/v1/workers/{self.worker_id}/assignments", token=self.worker_token)
        assignments = response.get("assignments")
        if not isinstance(assignments, list) or len(assignments) > 100:
            raise ApiError(502, "Invalid assignment listing")
        processed = 0
        for assignment in assignments:
            if stop_event and stop_event.is_set():
                break
            if not isinstance(assignment, dict):
                raise ApiError(502, "Invalid assignment")
            attempt_id = validate_identifier(assignment.get("attempt_id"), "attempt_id")
            if attempt_id in self._seen_attempts:
                continue
            self._seen_attempts.add(attempt_id)
            if self.crash_on_first_assignment and not self._crash_triggered:
                self._crash_triggered = True
                self._emit("intentional_worker_crash", attempt_id=attempt_id)
                raise WorkerCrash("Intentional worker failure for lease recovery demonstration")
            if self.on_assignment:
                self.on_assignment(assignment)
            try:
                self.execute_assignment(assignment, stop_event=stop_event)
                processed += 1
            except ApiError as exc:
                self.last_error = {"status": exc.status, "attempt_id": attempt_id}
                self._emit("attempt_failed", attempt_id=attempt_id, status=exc.status)
        return processed

    def execute_assignment(self, assignment, stop_event=None):
        attempt_id = validate_identifier(assignment.get("attempt_id"), "attempt_id")
        task_id = validate_identifier(assignment.get("task_id"), "task_id")
        lease_until = assignment.get("lease_until")
        if type(lease_until) not in (int, float) or not math.isfinite(lease_until) or lease_until <= self.clock():
            raise ApiError(409, "Assignment lease has expired")
        endpoint = validate_endpoint(assignment.get("requester_url"), self.allow_insecure_public_network)
        token = assignment.get("dispatch_token")
        if not isinstance(token, str) or not 1 <= len(token) <= 4096 or any(c in token for c in "\r\n"):
            raise ApiError(400, "Invalid attempt credential")
        attempt_url = f"{endpoint}/v1/attempts/{attempt_id}"
        dispatch = self._request("GET", attempt_url, token=token)
        if dispatch.get("task_id") != task_id or dispatch.get("attempt_id") != attempt_id:
            raise ApiError(409, "Dispatch does not match assigned attempt")
        sensitivity = dispatch.get("sensitivity")
        contract = dispatch.get("contract")
        dependencies = dispatch.get("dependencies")
        validate_public_contract(contract, sensitivity, dependencies)
        policy = dispatch.get("provider_policy")
        if not isinstance(policy, dict):
            raise ApiError(400, "Missing provider policy")
        deadline = dispatch.get("deadline")
        if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= self.clock():
            raise ApiError(409, "Dispatch has expired")
        self._emit("attempt_started", task_id=task_id, attempt_id=attempt_id, provider=self.provider.name, model=self.provider.model)
        for name, item in (dependencies or {}).items():
            self._emit("dependency_artifact_received", task_id=task_id, attempt_id=attempt_id, dependency=name, artifact_sha256=item["sha256"])
        started = time.monotonic()
        if self._sleep(self.execution_delay, stop_event):
            raise ApiError(409, "Worker stopped before execution")
        if min(lease_until, deadline) <= self.clock():
            raise ApiError(409, "Attempt expired before generation")
        repair = dispatch.get("repair")
        try:
            generated = self.provider.generate_module(contract, policy, sensitivity, repair=repair, **({"dependencies": dependencies} if dependencies else {}))
        except ApiError as exc:
            # Report now so the lease is not wasted waiting for an artifact that will never come.
            try:
                self._request("POST", attempt_url + "/failure", {"status": exc.status}, token=token)
            except ApiError:
                pass
            raise
        content = generated.get("content")
        if not isinstance(content, str) or not content or len(content.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ApiError(413, "Invalid generated module size")
        if min(lease_until, deadline) <= self.clock():
            raise ApiError(409, "Attempt expired before submission")
        if stop_event and stop_event.is_set():
            raise ApiError(409, "Worker stopped before submission")
        result = {
            "task_id": task_id, "attempt_id": attempt_id,
            "filename": contract["filename"], "content": content,
            "sha256": sha256_text(content),
            "provider": generated.get("provider", self.provider.name), "model": generated.get("model", self.provider.model),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }
        if isinstance(generated.get("usage"), dict):
            result["usage"] = generated["usage"]
        if type(generated.get("cost_usd")) in (int, float):
            result["cost_usd"] = generated["cost_usd"]
        # Retry only artifact delivery; never regenerate an API completion.
        for retry in range(3):
            if min(lease_until, deadline) <= self.clock():
                raise ApiError(409, "Attempt expired during delivery")
            try:
                self._request("POST", attempt_url + "/result", result, token=token)
                self._emit("artifact_delivered", task_id=task_id, attempt_id=attempt_id, artifact_sha256=result["sha256"])
                return result
            except ApiError as exc:
                if exc.status < 500 or retry == 2:
                    raise
                if self._sleep(0.1 * (2 ** retry), stop_event):
                    raise ApiError(409, "Worker stopped during delivery")

    def run(self, stop_event=None):
        stop_event = stop_event or threading.Event()
        self.register()
        self._heartbeat_stop.clear()
        heartbeats = threading.Thread(target=self._heartbeat_loop, name=f"heartbeat-{self.worker_id}", daemon=True)
        heartbeats.start()
        failures = 0
        try:
            while not stop_event.is_set():
                try:
                    self.run_once(stop_event)
                    failures = 0
                except ApiError as exc:
                    failures += 1
                    self.last_error = {"status": exc.status}
                    self._emit("worker_poll_failed", status=exc.status, consecutive_failures=failures)
                    if exc.status in (401, 403, TLS_VERIFY_FAILED) or failures >= self.max_failures:
                        raise
                self._sleep(min(self.poll_interval * (2 ** min(failures, 4)), 3), stop_event)
        finally:
            self._heartbeat_stop.set()
            heartbeats.join(timeout=6)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Public-work node; no protected execution is claimed")
    parser.add_argument("--market", required=True)
    parser.add_argument("--enrollment-token-file", type=Path, required=True)
    parser.add_argument("--id", default=None)
    parser.add_argument("--provider", choices=("fixture", "meta", "ollama", "simulated-meta"), default="fixture")
    parser.add_argument("--provider-timeout", type=float, default=60)
    parser.add_argument("--state-file", type=Path, help="Keep the worker credential here (0600) so a restart resumes this identity")
    parser.add_argument("--contracts-file", type=Path, help="Public contracts exported from a workload manifest; a full manifest is refused")
    parser.add_argument("--model")
    parser.add_argument("--bid-credits", type=int, default=10)
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument("--execution-delay", type=float, default=0)
    parser.add_argument("--crash-on-first-assignment", action="store_true")
    parser.add_argument("--allow-insecure-public-network", action="store_true", help="Explicit public-data-only development override; enrollment credentials are exposed over HTTP")
    parser.add_argument("--ca-file", type=Path, help="Trust only this CA or self-signed certificate for the HTTPS market and requester")
    args = parser.parse_args(argv)
    load_env_file()
    try:
        if args.contracts_file:
            register_public_contracts(load_public_contracts(args.contracts_file))
        token = args.enrollment_token_file.read_text(encoding="utf-8").strip()
        worker = Worker(args.market, token, args.id or new_id("worker"), provider=args.provider,
                        model=args.model, bid_credits=args.bid_credits, poll_interval=args.poll_interval,
                        execution_delay=args.execution_delay, crash_on_first_assignment=args.crash_on_first_assignment,
                        allow_insecure_public_network=args.allow_insecure_public_network, state_file=args.state_file, ca_file=args.ca_file,
                        provider_options=None if args.provider == "fixture" else {"timeout": args.provider_timeout},
                        on_event=lambda entry: print(json.dumps(entry), flush=True))
        worker.run()
    except KeyboardInterrupt:
        return 0
    except WorkerCrash:
        return 17
    except (ApiError, OSError, ValueError) as exc:
        reason = "TLS certificate verification failed; check --ca-file and that the certificate covers this host" if getattr(exc, "status", None) == TLS_VERIFY_FAILED else "Configuration, policy or protocol failure"
        print(json.dumps({"event": "worker_stopped", "reason": reason}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
