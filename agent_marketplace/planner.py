"""Requester-side planning: Meta proposes a public graph, deterministic code decides.

Only frozen public contract text is sent. Model output cannot add tasks, drop a
required dependency or change a contract; an invalid plan falls back to the workload's
declared graph and the rejection is reported.
"""
from __future__ import annotations

import json
import time

from .protocol import ApiError
from .workload import COMPLEXITY_CREDITS, default_workload, resolve_workload

REFERENCE_COMPLEXITY = {kind: task.complexity for kind, task in default_workload().tasks.items()}


def plan_schema(workload=None):
    kinds = sorted(resolve_workload(workload).tasks)
    return {
        "type": "object", "additionalProperties": False, "required": ["tasks"],
        "properties": {"tasks": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["kind", "depends_on", "complexity", "rationale"],
            "properties": {
                "kind": {"type": "string", "enum": kinds},
                "depends_on": {"type": "array", "items": {"type": "string", "enum": kinds}},
                "complexity": {"type": "string", "enum": sorted(COMPLEXITY_CREDITS)},
                "rationale": {"type": "string"},
            }}}},
    }


PLAN_SCHEMA = plan_schema()


def reference_plan(source="reference", reason=None, workload=None):
    workload = resolve_workload(workload)
    tasks = {k: {"depends_on": list(t.depends_on), "complexity": t.complexity, "rationale": "Fixed reference graph"} for k, t in workload.tasks.items()}
    plan = {"source": source, "tasks": tasks}
    if reason:
        plan["rejected_reason"] = reason
    return plan


def validate_plan(raw, workload=None):
    """Return normalized tasks or raise ValueError with a reason safe to display."""
    declared = resolve_workload(workload).tasks
    if not isinstance(raw, dict) or not isinstance(raw.get("tasks"), list):
        raise ValueError("Plan is not an object with a task list")
    tasks = {}
    for item in raw["tasks"]:
        if not isinstance(item, dict):
            raise ValueError("Plan task is not an object")
        kind, deps = item.get("kind"), item.get("depends_on")
        if kind not in declared or kind in tasks:
            raise ValueError("Plan has an unknown or duplicate task")
        if not isinstance(deps, list) or any(d not in declared or d == kind for d in deps):
            raise ValueError(f"Plan has invalid dependencies for {kind}")
        if item.get("complexity") not in COMPLEXITY_CREDITS:
            raise ValueError(f"Plan has invalid complexity for {kind}")
        if not set(declared[kind].depends_on) <= set(deps):
            raise ValueError(f"Plan drops a required dependency of {kind}")
        tasks[kind] = {"depends_on": sorted(set(deps)), "complexity": item["complexity"], "rationale": str(item.get("rationale", ""))[:300]}
    if set(tasks) != set(declared):
        raise ValueError("Plan does not cover exactly the contracted modules")
    done = set()
    while len(done) < len(tasks):
        ready = {k for k, t in tasks.items() if k not in done and set(t["depends_on"]) <= done}
        if not ready:
            raise ValueError("Plan contains a dependency cycle")
        done |= ready
    return tasks


def plan_with_meta(provider, workload=None, on_event=None):
    """Ask once. Provider failure or an invalid plan falls back and reports why."""
    workload = resolve_workload(workload)
    contracts = {k: {"function": c["function"], "description": c["description"],
                     "required_build_dependencies": list(workload.tasks[k].depends_on),
                     "consumes_artifacts": list(workload.tasks[k].consumes)}
                 for k, c in workload.contracts.items()}
    payload = {
        "model": provider.model,
        "messages": [
            {"role": "developer", "content": workload.planning_brief + "\nPlan CODE-GENERATION work, not runtime data flow. Each task's required_build_dependencies is a hard minimum for its depends_on list: include EVERY listed dependency even if its implementation could otherwise be written independently. Those workflow constraints are mandatory. Frozen function/input/output interfaces are already provided. Apart from those required edges, two modules can be implemented in parallel even when the application later pipes data from one to the other. Do not add another dependency merely because one function's output is another function's runtime input; add one only if generating the module genuinely requires an accepted upstream artifact. Explain the build rationale briefly."},
            {"role": "user", "content": json.dumps(contracts)},
        ],
        "max_completion_tokens": provider.max_completion_tokens,
        "reasoning_effort": "low",
        "response_format": {"type": "json_schema", "json_schema": {"name": "task_plan", "schema": plan_schema(workload), "strict": True}},
    }
    metadata = {"provider": provider.name, "model": provider.model, "operation": "planning", "caller_component": "planner"}
    started = time.monotonic()
    if on_event:
        on_event("model_call_started", **metadata)
    try:
        response, cost = provider.complete(payload)
    except ApiError as exc:
        if on_event:
            on_event("model_call_failed", **metadata, status=exc.status, elapsed_ms=round((time.monotonic() - started) * 1000))
        return reference_plan("reference-fallback", f"Planner call failed: {exc.message}", workload)
    if on_event:
        on_event("model_call_completed", **metadata, cost_usd=cost, elapsed_ms=round((time.monotonic() - started) * 1000))
    try:
        tasks = validate_plan(json.loads(response["choices"][0]["message"]["content"]), workload)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        return reference_plan("reference-fallback", f"Plan rejected: {exc}"[:300], workload)
    return {"source": provider.name, "model": response.get("model", provider.model), "cost_usd": cost, "tasks": tasks}
