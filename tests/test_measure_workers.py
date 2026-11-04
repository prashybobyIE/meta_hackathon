"""Mixed-provider workers, the local adapter and the measurement script. No credentials or models."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_marketplace.budget import BudgetLedger
from agent_marketplace.demo import parse_worker_specs, run_demo
from agent_marketplace.protocol import ApiError
from agent_marketplace.providers import _LOCAL_HINTS, MetaProvider, OllamaProvider, PUBLIC_CONTRACTS
from agent_marketplace.reference import acceptance_cases, PUBLIC_CASE_COUNTS, EXPECTED_REPORT

SPEC = importlib.util.spec_from_file_location("measure_workers", Path(__file__).parents[1] / "scripts" / "measure_workers.py")
measure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(measure)
REAL_LEDGER = Path(__file__).resolve().parents[1] / ".budget" / "meta-usage.json"
CONTRACT = {"id": "expense-summary-v1", "filename": "summary.py", "function": "summarize_expenses"}


def real_ledger_bytes():
    return REAL_LEDGER.read_bytes() if REAL_LEDGER.exists() else None


class WorkerSpecTests(unittest.TestCase):
    def test_parses_one_provider_per_worker(self):
        self.assertEqual(parse_worker_specs("ollama=llama3.2:3b,meta=muse-spark-1.3,fixture"), [("ollama", "llama3.2:3b"), ("meta", "muse-spark-1.3"), ("fixture", None)])

    def test_refuses_unknown_provider_missing_model_and_too_many_workers(self):
        for bad in ("made-up=x", "ollama", "meta", "", ",".join(["fixture"] * 9)):
            with self.subTest(bad), self.assertRaises(ValueError):
                parse_worker_specs(bad)

    def test_specs_do_not_combine_with_job_wide_worker_options(self):
        specs = [("fixture", None)]
        for options in ({"failure": True}, {"single_worker": True}, {"model": "x"}):
            with self.subTest(options), tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
                run_demo(directory, quiet=True, worker_specs=specs, **options)
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
            run_demo(directory, quiet=True, worker_specs=[("meta", "muse-spark-1.3"), ("simulated-meta", None)])


class MixedRunTests(unittest.TestCase):
    def test_workers_with_different_providers_compete_in_one_run(self):
        before = real_ledger_bytes()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"MODEL_API_KEY": "REAL-KEY-MUST-NOT-BE-SENT"}):
            summary = run_demo(directory, quiet=True, timeout=30, worker_specs=[("fixture", None), ("simulated-meta", None)])
            state = json.loads((Path(directory) / "requester/requester-state.json").read_text())
        self.assertEqual(summary["provider"], "mixed")
        self.assertFalse(summary["real_ai_inference"])
        self.assertEqual(summary["plan_source"], "simulated-meta")
        self.assertEqual(summary["integration"]["report"], EXPECTED_REPORT)
        self.assertEqual({w["worker_id"]: w["provider"] for w in summary["workers"]}, {"worker-a": "fixture", "worker-b": "simulated-meta"})
        offered = {offer["model"].split(" / ")[0] for attempt in state["attempts"].values() for offer in attempt["offers"]}
        self.assertEqual(offered, {"fixture", "simulated-meta"})
        self.assertEqual(real_ledger_bytes(), before)


class OllamaAdapterTests(unittest.TestCase):
    def test_cloud_tags_are_refused_and_local_tags_allowed(self):
        for tag in ("llama3.2:cloud", "gpt-oss:120b-cloud", "library/qwen:7b-cloud"):
            with self.subTest(tag), self.assertRaises(ApiError):
                OllamaProvider(tag)
        self.assertEqual(OllamaProvider("llama3.2:3b").model, "llama3.2:3b")

    def test_reports_tokens_and_no_invented_cost(self):
        response = {"done": True, "done_reason": "stop", "response": "def summarize_expenses(rows):\n    return {}", "prompt_eval_count": 300, "eval_count": 40}
        with patch("agent_marketplace.providers.json_request", return_value=response):
            result = OllamaProvider("some-model").generate_module(CONTRACT, {})
        self.assertEqual(result["usage"], {"prompt_tokens": 300, "completion_tokens": 40, "total_tokens": 340})
        self.assertNotIn("cost_usd", result)

    def test_missing_token_counts_are_left_out(self):
        response = {"done": True, "done_reason": "stop", "response": "def summarize_expenses(rows):\n    return {}"}
        with patch("agent_marketplace.providers.json_request", return_value=response):
            self.assertNotIn("usage", OllamaProvider("some-model").generate_module(CONTRACT, {}))

    def test_hints_reach_local_models_only_and_quote_no_acceptance_input(self):
        response = {"done": True, "done_reason": "stop", "response": "def summarize_expenses(rows):\n    return {}"}
        with patch("agent_marketplace.providers.json_request", return_value=response) as request:
            OllamaProvider("some-model").generate_module(CONTRACT, {})
        self.assertIn(_LOCAL_HINTS["summarize_expenses"], request.call_args.args[2]["prompt"])
        meta_reply = {"model": "muse-spark-1.3", "choices": [{"finish_reason": "stop", "message": {"content": "def summarize_expenses(rows):\n    return {}"}}]}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"MODEL_API_KEY": "unit-test-placeholder"}), patch("agent_marketplace.providers.json_request", return_value=meta_reply) as request:
            MetaProvider(ledger=BudgetLedger(Path(directory) / "ledger.json")).generate_module(CONTRACT, {"allow_meta": True})
        self.assertNotIn(_LOCAL_HINTS["summarize_expenses"], json.dumps(request.call_args.args[2]))
        self.assertEqual(set(_LOCAL_HINTS), {spec["function"] for spec in PUBLIC_CONTRACTS.values()})
        hints = " ".join(_LOCAL_HINTS.values())
        for kind, public_count in PUBLIC_CASE_COUNTS.items():
            for argument, _, _ in acceptance_cases(kind)[public_count:]:
                if isinstance(argument, str):
                    self.assertNotIn(argument.split("\n")[-2], hints)
        for token in ("NaN", "Infinity", "1e2"):
            self.assertNotIn(token, hints)


class MeasureTests(unittest.TestCase):
    def setUp(self):
        progress = patch.object(measure, "print", create=True)
        progress.start()
        self.addCleanup(progress.stop)

    def test_measures_free_configurations_with_real_processes(self):
        configs = measure.build_configs(hosted_provider="simulated-meta", hosted_model="muse-spark-1.3", local_provider="fixture", local_model=None)
        before = real_ledger_bytes()
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "raw.jsonl"
            records = measure.measure(configs, ["meta-1", "local-2", "mixed-2"], 1, Path(directory) / "runs", raw, command="unit test", machine={"cpu": "unit"}, timeout=30)
            self.assertEqual([r["outcome"] for r in records], ["accepted"] * 3)
            self.assertEqual([r["workers"] for r in records], [1, 2, 2])
            hosted = records[0]
            self.assertEqual(hosted["meta_calls"], 5)  # Plan, three modules, one repair.
            self.assertEqual(hosted["rejected_attempts"], 1)
            self.assertGreater(hosted["meta_usd"], 0)
            self.assertGreater(hosted["attempts"][0]["prompt_tokens"], 0)
            self.assertEqual(records[1]["meta_usd"], 0)
            self.assertEqual(records[0]["peak_concurrent"], 1)  # One worker cannot overlap itself.
            self.assertEqual(records[1]["plan_width"], 2)
            self.assertEqual(records[1]["peak_concurrent"], 2)
            self.assertGreaterEqual(hosted["wall_seconds"], hosted["generation_seconds"] / 2)
            self.assertEqual(len(measure.read_records(raw)), 3)
            text = measure.render_tables(measure.read_records(raw))
            for expected in ("unit test", "meta-1", "local-2", "mixed-2", "Raw numbers per attempt"):
                self.assertIn(expected, text)
            for secret in ("dispatch_token", "def parse_expenses", "REAL-KEY"):
                self.assertNotIn(secret, raw.read_text())
        self.assertEqual(real_ledger_bytes(), before)

    def test_reference_plan_skips_the_planner_call(self):
        configs = measure.build_configs(hosted_provider="simulated-meta", local_provider="fixture", local_model=None)
        with tempfile.TemporaryDirectory() as directory:
            record = measure.measure_run("meta-ref-1", configs["meta-ref-1"], Path(directory) / "run", timeout=30)
        self.assertEqual(record["plan_source"], "reference")
        self.assertEqual(record["meta_calls"], 4)  # Three modules and one repair; no plan.

    def test_run_that_stops_early_is_recorded_as_failed(self):
        def stops(workspace, **_):
            state = {"attempts": {"a1": {"kind": "parser", "worker_id": "worker-a", "status": "rejected", "repair_round": 2, "started_at": 1000.0, "offers": [], "report": {"cases_passed": 3, "cases_total": 11}, "result": {"provider": "ollama", "model": "m", "elapsed_ms": 1500, "content": "SOURCE"}, "token": "SECRET"}}, "plan": {"source": "reference"}}
            path = Path(workspace) / "requester"
            path.mkdir(parents=True)
            (path / "requester-state.json").write_text(json.dumps(state))
            raise RuntimeError("Module parser failed acceptance after 2 repairs; stopping to limit cost")
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"META_BUDGET_LEDGER": str(Path(directory) / "ledger.json")}):
            record = measure.measure_run("local-1", {"provider": "ollama", "model": "m", "single_worker": True}, Path(directory) / "run", runner=stops)
        self.assertEqual(record["outcome"], "failed")
        self.assertIn("failed acceptance", record["error"])
        self.assertEqual((record["rejected_attempts"], record["accepted_modules"], record["generation_seconds"]), (1, 0, 1.5))
        self.assertEqual(record["attempts"][0]["cases_passed"], 3)
        self.assertNotIn("SECRET", json.dumps(record))
        self.assertNotIn("SOURCE", json.dumps(record))

    def test_billable_runs_need_the_flag_a_key_and_an_existing_ledger(self):
        configs = measure.build_configs()
        runner = unittest.mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "absent.json")
            with self.assertRaisesRegex(SystemExit, "allow-billable"):
                measure.measure(configs, ["meta-1"], 1, directory, Path(directory) / "raw", runner=runner)
            with patch.dict(os.environ, {"META_BUDGET_LEDGER": missing}):
                os.environ.pop("MODEL_API_KEY", None)
                with self.assertRaisesRegex(SystemExit, "MODEL_API_KEY"):
                    measure.measure(configs, ["meta-1"], 1, directory, Path(directory) / "raw", allow_billable=True, runner=runner)
            with patch.dict(os.environ, {"META_BUDGET_LEDGER": missing, "MODEL_API_KEY": "unit-test-placeholder"}):
                with self.assertRaisesRegex(SystemExit, "no ledger"):
                    measure.measure(configs, ["meta-1"], 1, directory, Path(directory) / "raw", allow_billable=True, runner=runner)
        runner.assert_not_called()

    def test_billable_runs_stop_before_the_spending_limit(self):
        configs = measure.build_configs()
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.json"
            calls = []

            def spends(workspace, **_):
                calls.append(workspace)
                state = json.loads(ledger.read_text())
                state["entries"].append({"id": str(len(calls)), "run": Path(workspace).name, "model": "muse-spark-1.3", "charged": 0.3, "status": "settled"})
                ledger.write_text(json.dumps(state))
                return {"complete": True}
            ledger.write_text(json.dumps({"entries": [{"id": "old", "run": "older", "model": "muse-spark-1.3", "charged": 5.0, "status": "settled"}]}))
            with patch.dict(os.environ, {"META_BUDGET_LEDGER": str(ledger), "MODEL_API_KEY": "unit-test-placeholder"}):
                records = measure.measure(configs, ["meta-1"], 10, Path(directory) / "runs", Path(directory) / "raw", allow_billable=True, stop_at_usd=1.0, runner=spends, machine={})
        # Earlier spending does not count; each run needs $0.20 of headroom: 0, 0.3, 0.6 pass, 0.9 stops.
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["meta_usd"], 0.3)


if __name__ == "__main__":
    unittest.main()
