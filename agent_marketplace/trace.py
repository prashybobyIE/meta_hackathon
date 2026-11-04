"""Safe, stable dashboard trace projections of observed protocol events.

This module never forwards log text, request bodies, credentials, source or remote
error messages. Worker telemetry is explicitly labeled as a worker report; only
requester verification/market receipts establish acceptance.
"""
from __future__ import annotations

import hashlib
import json
import math
import re

_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_MODEL = re.compile(r"[A-Za-z0-9_.:/-]{1,100}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PROVIDERS = {"meta", "simulated-meta", "fixture", "ollama"}
_REQUESTER_EVENTS = {
    "confidential_dispatch_refused": ("requester", "Private dispatch refused on ordinary workers"),
    "plan_started": ("planner", "Planning the public module graph"),
    "plan_settled": ("planner", "Task graph validated and frozen"),
    "plan_failed": ("planner", "Planning failed; no task graph was authorized"),
    "ready_task_advertised": ("requester", "Ready task advertised to the marketplace"),
    "attempt_authorized": ("requester", "Winning offer authorized and lease reserved"),
    "assignment_outcome_pending": ("requester", "Assignment response unavailable; reconciling outcome"),
    "artifact_received": ("requester", "Worker artifact received for independent verification"),
    "verification_started": ("verifier", "Independent artifact verification started"),
    "verification_completed": ("verifier", "Independent artifact verification completed"),
    "artifact_rejected": ("verifier", "Artifact rejected; repair or reassignment required"),
    "provider_cooldown": ("requester", "Provider temporarily cooled down; another worker is preferred"),
    "attempt_failed": ("requester", "Worker attempt failed"),
    "attempt_expired_or_reopened": ("requester", "Lease ended; task reopened for another attempt"),
    "task_accepted_and_paid": ("ledger", "Verified artifact accepted; simulated credits settled once"),
    "application_integration_started": ("assembler", "Assembling accepted modules and checking the application"),
    "application_integration_passed": ("assembler", "Final application integration passed"),
    "application_integration_failed": ("assembler", "Final application integration failed"),
    "attestation_refused": ("requester", "Protected task refused before private input release"),
    "protected_task_not_submitted": ("requester", "Protected task was not submitted"),
    "protected_task_accepted": ("verifier", "Protected task response independently accepted"),
    "protected_task_rejected": ("verifier", "Protected task response rejected"),
    "protected_delivery_unknown": ("requester", "Protected input submitted or state ambiguous; delivery unknown"),
}
_MARKET_EVENTS = {
    "worker_registered": "Worker enrolled in the marketplace",
    "task_advertised": "Task advertisement recorded",
    "bid_received": "Worker bid recorded",
    "attempt_assigned": "Winning worker assigned an expiring lease",
    "lease_expired": "Worker lease expired; reserved credits released",
    "attempt_rejected": "Attempt rejected; reserved credits released",
    "task_accepted": "Acceptance receipt recorded; simulated credits settled",
}
_WORKER_EVENTS = {
    "worker_registered": "Worker reports successful enrollment",
    "worker_resumed": "Worker resumed its existing identity",
    "bid_submitted": "Worker submitted its offer",
    "attempt_started": "Worker fetched the assigned public contract",
    "dependency_artifact_received": "Worker received an accepted dependency artifact",
    "artifact_delivered": "Worker reports artifact delivery; requester acceptance is still required",
    "intentional_worker_crash": "Induced demo failure: worker exited before fetching its contract",
    "attempt_failed": "Worker reports a failed attempt",
    "heartbeat_unavailable": "Worker heartbeat temporarily unavailable",
    "worker_poll_failed": "Worker polling failed",
}
_CALL_EVENTS = {"meta_call_started", "meta_call_completed", "meta_call_failed", "model_call_started", "model_call_completed", "model_call_failed"}


def _finite(value):
    return type(value) in (int, float) and -1e100 <= value <= 1e100 and math.isfinite(value)


def _safe_metadata(raw):
    result = {}
    for key in ("job_id", "task_id", "kind", "attempt_id", "worker_id", "dependency"):
        if isinstance(raw.get(key), str) and _IDENTIFIER.fullmatch(raw[key]):
            result[key] = raw[key]
    if raw.get("provider") in _PROVIDERS:
        result["provider"] = raw["provider"]
    if isinstance(raw.get("model"), str) and _MODEL.fullmatch(raw["model"]):
        result["model"] = raw["model"]
    if raw.get("operation") in ("planning", "decomposition", "generation", "repair", "review"):
        result["operation"] = raw["operation"]
    if raw.get("phase") in ("started", "completed", "failed"):
        result["phase"] = raw["phase"]
    for key in ("credits", "cases_passed", "cases_total", "task_count", "status", "consecutive_failures"):
        if type(raw.get(key)) is int and 0 <= raw[key] <= 1000000:
            result[key] = raw[key]
    for key in ("elapsed_ms", "cost_usd", "retry_after_seconds"):
        if _finite(raw.get(key)) and raw[key] >= 0:
            result[key] = raw[key]
    if type(raw.get("passed")) is bool:
        result["passed"] = raw["passed"]
    if type(raw.get("assisted")) is bool:
        result["assisted"] = raw["assisted"]
    if isinstance(raw.get("artifact_sha256"), str) and _DIGEST.fullmatch(raw["artifact_sha256"]):
        result["artifact_sha256"] = raw["artifact_sha256"]
    if raw.get("source") in ("reference", "reference-fallback", "meta", "simulated-meta"):
        result["source"] = raw["source"]
    usage = raw.get("usage")
    if isinstance(usage, dict):
        result["usage"] = {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                           if type(usage.get(key)) is int and usage[key] >= 0}
    return result


def build_trace(status, worker_events=()):
    """Merge bounded safe metadata into time-ordered, stable trace entries.

    Caller supplies requester status plus optional parsed local worker JSONL
    objects. Repeated polls of the same records produce identical event IDs.
    Existing traces are ignored to avoid recursive duplication.
    """
    if not isinstance(status, dict):
        return []
    tasks = {t["task_id"]: t.get("kind") for t in status.get("tasks", [])
             if isinstance(t, dict) and isinstance(t.get("task_id"), str)}
    market = status.get("market") if isinstance(status.get("market"), dict) else {}
    bids = {(b.get("task_id"), b.get("worker_id")): b for b in market.get("bids", []) if isinstance(b, dict)}
    attempts = {a.get("attempt_id"): a for a in market.get("attempts", []) if isinstance(a, dict)}
    trace = []

    def add(raw, origin):
        if not isinstance(raw, dict) or not _finite(raw.get("time")) or raw["time"] < 0:
            return
        event = raw.get("kind") if origin == "market" else raw.get("event")
        if not isinstance(event, str):
            return
        metadata = _safe_metadata(raw)
        if origin == "market":
            # Here 'kind' is the event name, not a workload task kind.
            metadata.pop("kind", None)
        task_kind = tasks.get(metadata.get("task_id"))
        if isinstance(task_kind, str) and _IDENTIFIER.fullmatch(task_kind):
            metadata["kind"] = task_kind
        if isinstance(status.get("job_id"), str) and _IDENTIFIER.fullmatch(status["job_id"]):
            metadata["job_id"] = status["job_id"]
        if event in _CALL_EVENTS:
            phase = event.rsplit("_", 1)[-1]
            metadata["phase"] = phase
            metadata.setdefault("operation", "planning" if event.startswith("meta_call_") else "generation")
            provider = metadata.get("provider")
            component = "meta" if provider in ("meta", "simulated-meta") else metadata.get("worker_id", "planner")
            label = "Meta API" if provider == "meta" else "Simulated Meta API" if provider == "simulated-meta" else "Model provider"
            message = f"{label} {metadata['operation']} call {phase}"
            metadata["caller_component"] = metadata.get("worker_id", "planner")
        elif origin == "requester" and event in _REQUESTER_EVENTS:
            component, message = _REQUESTER_EVENTS[event]
        elif origin == "market" and event in _MARKET_EVENTS:
            component, message = "market", _MARKET_EVENTS[event]
            bid = bids.get((metadata.get("task_id"), metadata.get("worker_id")), {})
            attempt = attempts.get(metadata.get("attempt_id"), {})
            quote = attempt.get("credits", bid.get("credits"))
            if type(quote) is int and quote >= 0:
                metadata["credits"] = quote
        elif origin == "worker" and event in _WORKER_EVENTS:
            component, message = metadata.get("worker_id", "requester"), _WORKER_EVENTS[event]
        else:
            return
        if event == "verification_completed":
            verdict = "passed" if metadata.get("passed") else "failed"
            message = f"Independent verification {verdict} ({metadata.get('cases_passed', 0)}/{metadata.get('cases_total', 0)} cases)"
        identity = {"origin": origin, "time": raw["time"], "event": event, **metadata}
        if type(raw.get("sequence")) is int:
            identity["sequence"] = raw["sequence"]
        stable = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()[:20]
        trace.append({"id": f"{origin}-{stable}", "time": raw["time"], "component": component,
                      "event": event, "message": message, "origin": "worker-report" if origin == "worker" else "observed",
                      **metadata})

    for event in status.get("events", [])[-1000:]:
        add(event, "requester")
    for event in market.get("events", [])[-1000:]:
        add(event, "market")
    for event in list(worker_events)[-2000:]:
        add(event, "worker")
    unique = {entry["id"]: entry for entry in trace}
    return sorted(unique.values(), key=lambda e: (e["time"], e["id"]))[-3000:]
