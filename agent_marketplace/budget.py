"""Local hard spending guard for hosted Meta calls.

Every call reserves its worst-case cost first and is refused if that could cross
a limit. The user's ceiling is a code constant: configuration can only lower it.
The ledger is per machine and sees only calls made through this code, so the Meta
console remains the source of truth. Prompts and completions are never recorded.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile
import time
import uuid

from .protocol import ApiError

HARD_CEILING_USD = 40.0  # Set by the user 2026-11-01. Do not raise without their instruction.
DEFAULT_CAP_USD = 35.0   # Margin for usage this ledger cannot see.
DEFAULT_RUN_CAP_USD = 1.0
ROOT = Path(__file__).resolve().parents[1]
ALLOWED_ENV = {"MODEL_API_KEY", "META_BUDGET_USD", "META_BUDGET_RUN_USD"}

# USD per 1M input/output tokens, from dev.meta.ai/docs/pricing-rate-limits, checked 2026-11-01.
PRICES = {
    "muse-spark-1.3": (1.25, 4.25), "muse-spark-1.2": (1.25, 4.25), "muse-spark-1.1": (1.25, 4.25),
    "muse-wonder-1.0": (4.00, 20.00),
}


def load_env_file(path=None):
    """Load allowlisted keys from .env without overriding the environment."""
    path = Path(path) if path else ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.strip().partition("=")
        value = value.strip().strip("'\"")
        if separator and name in ALLOWED_ENV and value and value != "REPLACE_ME":
            os.environ.setdefault(name, value)


def _limit(name, default, ceiling):
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        value = default
    return min(value, ceiling) if math.isfinite(value) and value >= 0 else min(default, ceiling)


class BudgetLedger:
    def __init__(self, path, cap_usd=DEFAULT_CAP_USD, run_cap_usd=DEFAULT_RUN_CAP_USD, run_id="adhoc"):
        self.path = Path(path)
        self.cap = min(float(cap_usd), HARD_CEILING_USD)
        self.run_cap = min(float(run_cap_usd), self.cap)
        self.run_id = run_id

    @classmethod
    def from_env(cls):
        cap = _limit("META_BUDGET_USD", DEFAULT_CAP_USD, HARD_CEILING_USD)
        return cls(os.environ.get("META_BUDGET_LEDGER") or ROOT / ".budget" / "meta-usage.json", cap,
                   _limit("META_BUDGET_RUN_USD", DEFAULT_RUN_CAP_USD, cap), os.environ.get("META_BUDGET_RUN") or "adhoc")

    @contextlib.contextmanager
    def _locked(self, write=True):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(str(self.path) + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = {"entries": []}
            if self.path.exists():
                try:
                    state = json.loads(self.path.read_text())
                    if not isinstance(state.get("entries"), list):
                        raise ValueError()
                except (ValueError, OSError):
                    # An unreadable ledger means unknown spend, so refuse rather than reset it.
                    raise ApiError(402, "Meta budget ledger is unreadable; refusing hosted calls") from None
            yield state
            if write:
                fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".ledger-")
                with os.fdopen(fd, "w") as stream:
                    json.dump(state, stream, indent=1)
                os.replace(name, self.path)

    @staticmethod
    def worst_case(model, prompt_bytes, max_completion_tokens):
        if model not in PRICES:
            raise ApiError(402, "Model has no known price; refusing hosted call")
        input_price, output_price = PRICES[model]
        return (prompt_bytes * input_price + max_completion_tokens * output_price) / 1_000_000

    def reserve(self, model, prompt_bytes, max_completion_tokens):
        worst = self.worst_case(model, prompt_bytes, max_completion_tokens)
        with self._locked() as state:
            total = sum(e["charged"] for e in state["entries"])
            run = sum(e["charged"] for e in state["entries"] if e["run"] == self.run_id)
            if total + worst > self.cap:
                raise ApiError(402, f"Meta budget guard: ${total:.4f} used, cap ${self.cap:.2f}; call refused")
            if run + worst > self.run_cap:
                raise ApiError(402, f"Meta budget guard: run used ${run:.4f}, run cap ${self.run_cap:.2f}; call refused")
            entry = {"id": uuid.uuid4().hex, "time": time.time(), "run": self.run_id, "model": model, "charged": worst, "status": "reserved"}
            state["entries"].append(entry)
            return entry["id"]

    def _update(self, entry_id, **fields):
        with self._locked() as state:
            for entry in state["entries"]:
                if entry["id"] == entry_id:
                    entry.update(fields)
                    return entry
        raise ApiError(500, "Unknown budget entry")

    def release(self, entry_id):
        """Only for a definite refusal. Timeouts stay charged because they may have been billed."""
        self._update(entry_id, charged=0.0, status="not_billed")

    def settle(self, entry_id, usage):
        """Record actual cost. Missing usage keeps the worst-case charge."""
        usage = usage if isinstance(usage, dict) else {}
        details = usage.get("completion_tokens_details")
        reasoning = details.get("reasoning_tokens", 0) if isinstance(details, dict) else 0
        prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
        with self._locked() as state:
            entry = next((e for e in state["entries"] if e["id"] == entry_id), None)
            if entry is None:
                raise ApiError(500, "Unknown budget entry")
            if type(prompt) is int and type(completion) is int and type(reasoning) is int and min(prompt, completion, reasoning) >= 0:
                input_price, output_price = PRICES[entry["model"]]
                # completion_tokens includes reasoning (observed in live usage). If a response ever
                # reports more reasoning than completion, count both. No cached discount is applied.
                billable = completion if reasoning <= completion else completion + reasoning
                entry.update(charged=(prompt * input_price + billable * output_price) / 1_000_000, status="settled", prompt_tokens=prompt, completion_tokens=completion, reasoning_tokens=reasoning)
            else:
                entry["status"] = "settled_worst_case"
            return entry["charged"]

    def status(self):
        with self._locked(write=False) as state:
            entries = state["entries"]
            used = sum(e["charged"] for e in entries)
            return {"used_usd": round(used, 6), "cap_usd": self.cap, "hard_ceiling_usd": HARD_CEILING_USD, "remaining_usd": round(self.cap - used, 6), "run_id": self.run_id, "run_used_usd": round(sum(e["charged"] for e in entries if e["run"] == self.run_id), 6), "run_cap_usd": self.run_cap, "calls": len(entries)}


def main():
    argparse.ArgumentParser(description="Show local Meta spending against the guard").parse_args()
    load_env_file()
    print(json.dumps(BudgetLedger.from_env().status(), indent=2))


if __name__ == "__main__":
    main()
