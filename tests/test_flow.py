import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from agent_marketplace.demo import run_demo
from agent_marketplace.planner import plan_with_meta, reference_plan, validate_plan
from agent_marketplace.protocol import ApiError, sha256_text
from agent_marketplace.providers import _user_prompt, PUBLIC_CONTRACTS, FixtureProvider
from agent_marketplace.reference import CONTRACTS, EXPECTED_REPORT
from agent_marketplace.restricted import RestrictedError, RestrictedModule
from agent_marketplace.verification import verify_artifact


def artifact(kind, source):
    return {"content": source, "filename": CONTRACTS[kind]["filename"], "sha256": sha256_text(source)}


def good_plan():
    plan = reference_plan()["tasks"]
    return {"tasks": [{"kind": k, **t} for k, t in plan.items()]}


class PlannerTests(unittest.TestCase):
    def test_valid_plan_normalizes(self):
        self.assertEqual(set(validate_plan(good_plan())), set(CONTRACTS))

    def test_model_output_cannot_change_the_contracted_graph(self):
        cases = {"unknown": lambda p: p["tasks"].append({"kind": "shell", "depends_on": [], "complexity": "low"}),
                 "missing": lambda p: p["tasks"].pop(),
                 "dropped required dependency": lambda p: [t.update(depends_on=[]) for t in p["tasks"] if t["kind"] == "summary"],
                 "cycle": lambda p: [t.update(depends_on=["summary"]) for t in p["tasks"] if t["kind"] == "parser"],
                 "self": lambda p: [t.update(depends_on=["parser"]) for t in p["tasks"] if t["kind"] == "parser"],
                 "complexity": lambda p: p["tasks"][0].update(complexity="free")}
        for name, mutate in cases.items():
            plan = good_plan()
            mutate(plan)
            with self.subTest(name), self.assertRaises(ValueError):
                validate_plan(plan)

    def test_invalid_or_failed_plan_falls_back_and_reports(self):
        class Provider:
            name, model, max_completion_tokens = "meta", "muse-spark-1.3", 100
            def __init__(self, result): self.result = result
            def complete(self, payload):
                if isinstance(self.result, Exception):
                    raise self.result
                return self.result, 0.01
        bad = {"choices": [{"message": {"content": "not json"}}]}
        for result in (bad, ApiError(402, "guard")):
            plan = plan_with_meta(Provider(result))
            self.assertEqual(plan["source"], "reference-fallback")
            self.assertTrue(plan["rejected_reason"])

    def test_plan_prompt_contains_only_public_contract_text(self):
        seen = {}
        class Provider:
            name, model, max_completion_tokens = "meta", "muse-spark-1.3", 100
            def complete(self, payload):
                seen.update(payload)
                return {"choices": [{"message": {"content": json.dumps(good_plan())}}]}, 0.01
        self.assertEqual(plan_with_meta(Provider())["source"], "meta")
        self.assertEqual(seen["response_format"]["type"], "json_schema")
        self.assertNotIn("acceptance_cases", json.dumps(seen["messages"]))


class RuntimeTests(unittest.TestCase):
    def test_common_model_python_now_runs(self):
        source = '''from decimal import Decimal, InvalidOperation
def f(rows):
    words = {"a", "b"}
    out = []
    for i, row in enumerate(rows, start=2):
        for _, x in zip(row, row):
            pass
        try:
            d = Decimal(row[0])
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"bad row {i}: {row!r} {d if False else 'x':>3}") from exc
        out.append(row[0] in words)
    return out
'''
        self.assertEqual(RestrictedModule(source, "f").run([["1"]]), [False])
        with self.assertRaisesRegex(ValueError, "bad row 2"):
            RestrictedModule(source, "f").run([["nope"]])

    def test_type_is_limited_to_plain_data(self):
        self.assertTrue(RestrictedModule("def f(x):\n return type(x) is int", "f").run(3))
        with self.assertRaises(RestrictedError):
            RestrictedModule("def f(x):\n return type(len)", "f").run(3)

    def test_rejection_names_the_attribute_without_case_data(self):
        report = verify_artifact("categories", artifact("categories", "def categorize_expenses(rows):\n    return [r.get('category').strip() for r in rows]\n"))
        self.assertIn("Attribute strip is not available on NoneType", report["diagnostic"])

    def test_new_syntax_keeps_bounds(self):
        for source in ['def f(x):\n return f"{x:>999999999}"', 'def f(x):\n return f"{x:{9}99999}"',
                       'def f(x):\n return f"{len}"', 'def f(x):\n s = set()\n s.add(1)\n return 1',
                       'def f(x):\n return f"{x.__class__}"', 'def f(x):\n return f"{open}"']:
            with self.subTest(source), self.assertRaises(RestrictedError):
                RestrictedModule(source, "f").run("x")


class DiagnosticTests(unittest.TestCase):
    def test_runtime_rejection_is_specific(self):
        report = verify_artifact("parser", artifact("parser", "import re\ndef parse_expenses(text):\n    return []\n"))
        self.assertIn("Import not allowed", report["diagnostic"])

    def test_public_case_is_quoted_and_hidden_case_is_withheld(self):
        public = verify_artifact("categories", artifact("categories", "def categorize_expenses(rows):\n    return []\n"))
        self.assertIn("Public case 0", public["diagnostic"])
        fixture = FixtureProvider().generate_module(CONTRACTS["parser"], {})["content"]
        # Accepts 1e2, which only a hidden case rejects.
        lax = fixture.replace('or not parts[0].isdigit()', '').replace('not parts[0] or not parts[0].isascii() ', 'not parts[0] ')
        hidden = verify_artifact("parser", artifact("parser", lax))
        self.assertFalse(hidden["passed"])
        self.assertIn("hidden", hidden["diagnostic"])
        self.assertNotIn("1e2", hidden["diagnostic"])

    def test_repair_prompt_accepts_only_bounded_feedback(self):
        spec = PUBLIC_CONTRACTS["expense-summary-v1"]
        self.assertIn("FEEDBACK", _user_prompt(spec, {"previous_source": "x", "diagnostic": "y"}))
        for bad in ({"previous_source": "x"}, {"previous_source": "x", "diagnostic": "y", "private": "z"}, {"previous_source": "x", "diagnostic": "y" * 1501}, "text"):
            with self.subTest(bad), self.assertRaises(ApiError):
                _user_prompt(spec, bad)


class SimulatedFlowTests(unittest.TestCase):
    """Real processes, provider, guard and repair path, with no AI and no real key or ledger."""

    def test_plan_rejection_repair_and_budget_flow(self):
        real = Path(__file__).resolve().parents[1] / ".budget" / "meta-usage.json"
        before = real.read_bytes() if real.exists() else None
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"MODEL_API_KEY": "REAL-KEY-MUST-NOT-BE-SENT"}):
            summary = run_demo(directory, provider="simulated-meta", quiet=True, timeout=30)
            self.assertEqual(summary["plan_source"], "simulated-meta")
            self.assertFalse(summary["real_ai_inference"])
            self.assertEqual(summary["integration"]["report"], EXPECTED_REPORT)
            self.assertEqual(summary["rejected_attempts"], 1)
            repaired = [a for a in summary["attempt_timings"] if a["repair_round"]]
            self.assertEqual([a["kind"] for a in repaired], ["categories"])
            self.assertEqual(repaired[0]["status"], "accepted")
            self.assertGreater(summary["budget"]["used_usd"], 0)
            self.assertEqual(summary["budget"]["calls"], 5)
            state = (Path(directory) / "requester/requester-state.json").read_text()
            self.assertNotIn("REAL-KEY", state)
        self.assertEqual(real.read_bytes() if real.exists() else None, before)

    def test_refused_protected_step_is_reported_and_does_not_fake_success(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = run_demo(directory, quiet=True, timeout=20, protected_runner=lambda: {"verified": False, "release_state": "not_released", "delivery_state": "not_submitted", "accepted": False, "reason": "wrong policy"})
            self.assertFalse(summary["protected"]["accepted"])
            events = json.loads((Path(directory) / "requester/requester-state.json").read_text())["events"]
            self.assertIn("attestation_refused", [e["event"] for e in events])

    def test_guard_refusal_stops_the_job_without_a_wait(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"META_BUDGET_RUN_USD": "0.0001"}):
            with self.assertRaisesRegex(RuntimeError, "402"):
                run_demo(directory, provider="simulated-meta", quiet=True, timeout=30)

    def test_lost_protected_reply_preserves_release_and_records_unknown_delivery(self):
        record = {"verified": True, "release_state": "submitted", "delivery_state": "unknown", "accepted": False, "reason": "Protected task reply unavailable"}
        with tempfile.TemporaryDirectory() as directory:
            summary = run_demo(directory, quiet=True, timeout=20, protected_runner=lambda: record)
            self.assertEqual(summary["protected"], record)
            self.assertFalse(summary["protected"]["accepted"])
            state = json.loads((Path(directory) / "requester/requester-state.json").read_text())
            events = [e["event"] for e in state["events"]]
            self.assertIn("protected_delivery_unknown", events)
            self.assertNotIn("attestation_refused", events)

    def test_legacy_protected_failure_is_never_recorded_as_a_no_release_refusal(self):
        from agent_marketplace.requester import Requester
        with tempfile.TemporaryDirectory() as directory:
            requester = Requester("http://127.0.0.1:1", "test-requester", directory)
            requester.record_protected({"verified": False, "accepted": False, "reason": "Legacy transport failure"})
            event = requester.state["events"][-1]
            self.assertEqual(event["event"], "protected_delivery_unknown")
            self.assertEqual(event["release_state"], "unknown")


class NetworkTests(unittest.TestCase):
    def test_non_loopback_requires_advertised_host(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
            run_demo(directory, quiet=True, remote_workers=1)

    def test_worker_joins_over_a_non_loopback_address_and_resumes(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            try:
                probe.connect(("192.0.2.1", 9))
                address = probe.getsockname()[0]
            except OSError:
                self.skipTest("No non-loopback interface")
        import subprocess, sys, threading
        with tempfile.TemporaryDirectory() as directory:
            ready = {}
            def joiner(info):
                ready.update(info)
                market = f"http://{address}:{info['market'].rsplit(':', 1)[1]}"
                state = Path(directory) / "remote-state.json"
                processes.append(subprocess.Popen([sys.executable, "-m", "agent_marketplace.worker", "--market", market, "--enrollment-token-file", str(Path(directory) / "enrollment.token"), "--id", "worker-remote", "--bid-credits", "1", "--poll-interval", "0.05", "--state-file", str(state), "--allow-insecure-public-network"], stdout=subprocess.DEVNULL, cwd=Path(__file__).resolve().parents[1]))
            processes = []
            try:
                summary = run_demo(directory, quiet=True, timeout=30, advertise_host=address, remote_workers=1, on_ready=joiner)
                self.assertIn("worker-remote", summary["workers_used"])
                self.assertEqual(summary["integration"]["report"], EXPECTED_REPORT)
                self.assertEqual(json.loads((Path(directory) / "remote-state.json").read_text())["worker_id"], "worker-remote")
            finally:
                for process in processes:
                    process.terminate()
                    process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
