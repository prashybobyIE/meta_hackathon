"""Record what one worker and several workers did on this machine, run by run.

Hosted Meta runs bill real money, so they need --allow-billable, a ledger that is not
accidentally empty, and they stop before --stop-at-usd of new spending. Every run gets a
fresh demo workspace. Records hold timings, tokens, costs and outcomes, never source,
prompts or credentials. Results are appended as JSON lines and rendered from that file.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shlex
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_marketplace.budget import BudgetLedger, load_env_file
from agent_marketplace.demo import run_demo
from agent_marketplace.requester import Requester

BILLABLE_PROVIDERS = ("meta",)
# Headroom kept for the next run: observed about $0.06, more when repairs happen.
NEXT_RUN_ALLOWANCE_USD = 0.20
DEFAULT_TIMEOUT_SECONDS = 280


def build_configs(hosted_provider="meta", hosted_model="muse-spark-1.3", local_provider="ollama", local_model="llama3.2:3b"):
    """Name -> run_demo options. 1 uses --single-worker, 2 the demo's default pair, 3 per-worker specs."""
    def family(prefix, provider, model):
        spec = (provider, model)
        return {
            f"{prefix}-1": {"provider": provider, "model": model, "single_worker": True},
            f"{prefix}-2": {"provider": provider, "model": model},
            f"{prefix}-3": {"worker_specs": [spec, spec, spec]},
        }
    configs = {**family("meta", hosted_provider, hosted_model), **family("local", local_provider, local_model)}
    # The Meta planner can serialize the graph, which leaves workers nothing to do at once.
    for count in (1, 2, 3):
        configs[f"meta-ref-{count}"] = {**configs[f"meta-{count}"], "use_planner": False}
    configs["mixed-2"] = {"worker_specs": [(local_provider, local_model), (hosted_provider, hosted_model)]}
    return configs


def providers_of(options):
    return [name for name, _ in options["worker_specs"]] if options.get("worker_specs") else [options["provider"]]


def is_billable(options):
    return any(name in BILLABLE_PROVIDERS for name in providers_of(options))


def worker_count(options):
    return len(options["worker_specs"]) if options.get("worker_specs") else (1 if options.get("single_worker") else 2)


def ledger_path(workspace, options):
    if "simulated-meta" in providers_of(options):
        return Path(workspace) / "simulated-ledger.json"
    return BudgetLedger.from_env().path


def ledger_usage(path, run_id):
    """Read-only. The ledger is replaced atomically, so a plain read sees a whole document."""
    usage = {"usd": 0.0, "calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    path = Path(path)
    if not path.exists():
        return usage
    for entry in json.loads(path.read_text())["entries"]:
        if entry.get("run") == run_id and entry.get("status") != "not_billed":
            usage["usd"] += entry["charged"]
            usage["calls"] += 1
            usage["prompt_tokens"] += entry.get("prompt_tokens", 0)
            usage["completion_tokens"] += entry.get("completion_tokens", 0)
    usage["usd"] = round(usage["usd"], 6)
    return usage


def attempt_records(workspace):
    """Metadata from requester state, which survives a run that stopped early. No source or tokens."""
    path = Path(workspace) / "requester" / "requester-state.json"
    if not path.exists():
        return [], None
    state = json.loads(path.read_text())
    records = []
    for record in state["attempts"].values():
        view = Requester._attempt_view(record)
        report = record.get("report") or {}
        usage = view.get("usage") or {}
        records.append({
            "kind": view["kind"], "worker_id": view["worker_id"], "status": view["status"], "provider": view["provider"], "model": view["model"],
            "repair_round": view["repair_round"], "elapsed_ms": view["elapsed_ms"], "cost_usd": view["cost_usd"], "failure": view["failure"],
            "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
            "cases_passed": report.get("cases_passed"), "cases_total": report.get("cases_total"),
            "offers": [offer["worker_id"] + "=" + offer["model"] for offer in view["offers"]],
        })
    return records, (state.get("plan") or {}).get("source")


def concurrency_facts(workspace):
    """How many generations overlapped, and whether the settled plan allowed them to."""
    path = Path(workspace) / "requester" / "requester-state.json"
    if not path.exists():
        return {"peak_concurrent": 0, "plan_width": None}
    state = json.loads(path.read_text())
    edges = []
    for record in state["attempts"].values():
        elapsed = (record.get("result") or {}).get("elapsed_ms")
        if elapsed is not None:
            edges += [(record["started_at"], 1), (record["started_at"] + elapsed / 1000, -1)]
    peak = current = 0
    for _, change in sorted(edges):
        current += change
        peak = max(peak, current)
    tasks = (state.get("plan") or {}).get("tasks") or {}
    # Width of the first layer: modules that are ready before anything has been accepted.
    width = sum(not task["depends_on"] for task in tasks.values()) if tasks else None
    return {"peak_concurrent": peak, "plan_width": width}


def measure_run(name, options, workspace, runner=run_demo, timeout=DEFAULT_TIMEOUT_SECONDS):
    """One fresh run. A run that stops early is a result to record, not an error to hide."""
    workspace = Path(workspace)
    record = {
        "config": name, "workers": worker_count(options), "providers": providers_of(options),
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "load_1m_at_start": round(os.getloadavg()[0], 2), "error": None,
    }
    started = time.monotonic()
    try:
        summary = runner(workspace, quiet=True, timeout=timeout, **options)
        record["outcome"] = "accepted" if summary.get("complete") else "failed"
    except (RuntimeError, TimeoutError, ValueError) as exc:
        record["outcome"] = "failed"
        record["error"] = str(exc)[:300]
    record["wall_seconds"] = round(time.monotonic() - started, 3)
    attempts, record["plan_source"] = attempt_records(workspace)
    record["attempts"] = attempts
    record.update(concurrency_facts(workspace))
    record["accepted_modules"] = sum(a["status"] == "accepted" for a in attempts)
    record["rejected_attempts"] = sum(a["status"] == "rejected" for a in attempts)
    record["generation_seconds"] = round(sum(a["elapsed_ms"] or 0 for a in attempts) / 1000, 3)
    record["attempt_tokens"] = sum((a["prompt_tokens"] or 0) + (a["completion_tokens"] or 0) for a in attempts)
    usage = ledger_usage(ledger_path(workspace, options), workspace.name)
    record["meta_usd"], record["meta_calls"] = usage["usd"], usage["calls"]
    record["meta_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    return record


def require_real_ledger(key_present):
    """A worktree has no .budget directory; a fresh default ledger would read as $0 spent."""
    if not key_present:
        raise SystemExit("MODEL_API_KEY is not set")
    path = BudgetLedger.from_env().path
    if not path.exists():
        raise SystemExit(f"Refusing billable runs: no ledger at {path}. Point META_BUDGET_LEDGER at the real ledger")


def measure(configs, names, runs, workspace_root, raw_path, *, allow_billable=False, stop_at_usd=2.0, runner=run_demo, command="", machine=None, timeout=DEFAULT_TIMEOUT_SECONDS):
    """Round-robin, so that drift in load and the network falls on every configuration alike."""
    billable = [name for name in names if is_billable(configs[name])]
    if billable and not allow_billable:
        raise SystemExit("These configurations bill the Meta API; pass --allow-billable: " + ", ".join(billable))
    if billable:
        require_real_ledger(bool(os.environ.get("MODEL_API_KEY")))
    start_used = BudgetLedger.from_env().status()["used_usd"] if billable else 0.0
    machine = machine or machine_info()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    workspace_root, raw_path = Path(workspace_root), Path(raw_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    records = []
    for index in range(runs):
        for name in names:
            options = configs[name]
            if is_billable(options):
                spent = BudgetLedger.from_env().status()["used_usd"] - start_used
                if spent + NEXT_RUN_ALLOWANCE_USD > stop_at_usd:
                    print(json.dumps({"skipped": name, "run": index + 1, "reason": f"spent ${spent:.4f} of ${stop_at_usd:.2f}"}), flush=True)
                    continue
            record = measure_run(name, options, workspace_root / f"{stamp}-{name}-{index + 1}", runner=runner, timeout=timeout)
            record.update(run=index + 1, command=command, machine=machine)
            records.append(record)
            with raw_path.open("a") as stream:
                stream.write(json.dumps(record) + "\n")
            print(json.dumps({k: record[k] for k in ("config", "run", "outcome", "wall_seconds", "generation_seconds", "accepted_modules", "rejected_attempts", "meta_usd", "error")}), flush=True)
    return records


def machine_info():
    """Model class and capacity only: no hostname, serial number or user name."""
    def sysctl(name):
        try:
            return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    memory = sysctl("hw.memsize")
    return {
        "cpu": sysctl("machdep.cpu.brand_string") or platform.processor(), "memory_gib": round(int(memory) / 2**30) if memory.isdigit() else None,
        "logical_cpus": os.cpu_count(), "os": platform.platform(terse=True), "python": platform.python_version(),
    }


def read_records(raw_path):
    return [json.loads(line) for line in Path(raw_path).read_text().splitlines() if line.strip()]


def _span(values, digits=1):
    values = [v for v in values if v is not None]
    if not values:
        return "n/a"
    low, high = round(min(values), digits), round(max(values), digits)
    return f"{low}" if low == high else f"{low} to {high}"


def summary_table(records):
    lines = ["| Config | Workers | Runs | All modules accepted | Wall s | Sum of attempt generation s | Peak concurrent | Rejected attempts | Meta USD (ledger) |", "|---|---|---|---|---|---|---|---|---|"]
    for name in dict.fromkeys(r["config"] for r in records):
        group = [r for r in records if r["config"] == name]
        accepted = [r for r in group if r["outcome"] == "accepted"]
        lines.append(f"| {name} | {group[0]['workers']} | {len(group)} | {len(accepted)} | {_span([r['wall_seconds'] for r in group])} | {_span([r['generation_seconds'] for r in group])} | {_span([r['peak_concurrent'] for r in group], 0)} | {_span([r['rejected_attempts'] for r in group], 0)} | {_span([r['meta_usd'] for r in group], 4)} |")
    return "\n".join(lines)


def per_run_table(records):
    lines = ["| Config | Run | Started (UTC) | Load 1m | Outcome | Wall s | Generation s | Peak concurrent | Plan width | Modules accepted | Rejected | Attempts | Attempt tokens | Meta calls | Meta tokens | Meta USD | Plan | Stop reason |", "|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|"]
    for r in records:
        reason = (r["error"] or "").replace("|", "/")
        lines.append(f"| {r['config']} | {r['run']} | {r['started_at'][:19]} | {r['load_1m_at_start']} | {r['outcome']} | {r['wall_seconds']} | {r['generation_seconds']} | {r['peak_concurrent']} | {r['plan_width']} | {r['accepted_modules']} | {r['rejected_attempts']} | {len(r['attempts'])} | {r['attempt_tokens']} | {r['meta_calls']} | {r['meta_tokens']} | {r['meta_usd']} | {r['plan_source']} | {reason} |")
    return "\n".join(lines)


def per_attempt_table(records):
    lines = ["| Config | Run | Module | Worker | Provider | Model | Repair round | Status | Generation ms | Tokens in/out | USD | Cases passed |", "|---|---|---|---|---|---|---:|---|---:|---:|---:|---:|"]
    for r in records:
        for a in r["attempts"]:
            tokens = f"{a['prompt_tokens']}/{a['completion_tokens']}" if a["prompt_tokens"] is not None else "n/a"
            cost = "n/a" if a["cost_usd"] is None else round(a["cost_usd"], 5)
            cases = "n/a" if a["cases_total"] is None else f"{a['cases_passed']}/{a['cases_total']}"
            lines.append(f"| {r['config']} | {r['run']} | {a['kind']} | {a['worker_id']} | {a['provider'] or 'n/a'} | {a['model'] or 'n/a'} | {a['repair_round']} | {a['status']} | {a['elapsed_ms'] if a['elapsed_ms'] is not None else 'n/a'} | {tokens} | {cost} | {cases} |")
    return "\n".join(lines)


def render_tables(records):
    commands = "\n".join(f"    {command}" for command in dict.fromkeys(r["command"] for r in records))
    machines = {json.dumps(r["machine"], sort_keys=True) for r in records}
    return "\n\n".join([
        "## Commands\n\n" + commands, "## Machine\n\n" + "\n".join(f"    {m}" for m in sorted(machines)),
        "## Ranges by configuration\n\n" + summary_table(records), "## Raw numbers per run\n\n" + per_run_table(records),
        "## Raw numbers per attempt\n\n" + per_attempt_table(records),
    ]) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--configs", default="meta-1,meta-2,meta-3,local-1,local-2,local-3", help="Comma-separated: meta-1,meta-2,meta-3,meta-ref-1,meta-ref-2,meta-ref-3,local-1,local-2,local-3,mixed-2")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--meta-model", default="muse-spark-1.3")
    p.add_argument("--local-model", default="llama3.2:3b")
    p.add_argument("--allow-billable", action="store_true", help="Permit configurations that call the paid Meta API")
    p.add_argument("--stop-at-usd", type=float, default=2.0, help="Skip billable runs before new spending could pass this")
    p.add_argument("--raw", type=Path, default=ROOT / "docs/measurements/worker_scaling_2026-11-01.jsonl", help="JSON lines, appended")
    p.add_argument("--workspace-root", type=Path, help="Directory for per-run workspaces. Default: a new temporary directory")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    p.add_argument("--render", type=Path, help="Write Markdown tables for the records in --raw to this file; runs nothing")
    args = p.parse_args(argv)
    if args.render:
        args.render.write_text(render_tables(read_records(args.raw)))
        return 0
    load_env_file()
    if args.meta_model.endswith("-contributor"):
        p.error("Contributor models permit training on content and are refused")
    configs = build_configs(hosted_model=args.meta_model, local_model=args.local_model)
    names = [name.strip() for name in args.configs.split(",")]
    unknown = [name for name in names if name not in configs]
    if unknown or args.runs < 1:
        p.error(f"Unknown configurations {unknown}, or fewer than one run")
    root = args.workspace_root or Path(tempfile.mkdtemp(prefix="measure-workers-"))
    measure(configs, names, args.runs, root, args.raw, allow_billable=args.allow_billable, stop_at_usd=args.stop_at_usd, command=shlex.join(["scripts/measure_workers.py", *sys.argv[1:]]).replace(str(root), "WORKSPACE_ROOT") if argv is None else "", timeout=args.timeout)
    print(json.dumps({"raw": str(args.raw), "workspaces": str(root)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
