import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_marketplace.budget import HARD_CEILING_USD, BudgetLedger, load_env_file
from agent_marketplace.protocol import ApiError
from agent_marketplace.providers import MetaProvider, PUBLIC_CONTRACTS


def contract():
    spec = PUBLIC_CONTRACTS["expense-summary-v1"]
    return {"id": "expense-summary-v1", "filename": spec["filename"], "function": spec["function"]}


GOOD = {"model": "muse-spark-1.3", "choices": [{"finish_reason": "stop", "message": {"content": "def summarize_expenses(rows):\n    return {}"}}], "usage": {"prompt_tokens": 1000, "completion_tokens": 1000, "total_tokens": 2000}}


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "ledger.json"

    def tearDown(self):
        self.temp.cleanup()

    def provider(self, cap=1.0, run_cap=1.0):
        return MetaProvider(ledger=BudgetLedger(self.path, cap, run_cap, "test"))

    def call(self, provider, **mock):
        with patch.dict(os.environ, {"MODEL_API_KEY": "unit-test-placeholder"}), patch("agent_marketplace.providers.json_request", **mock) as request:
            try:
                return provider.generate_module(contract(), {"allow_meta": True}), request
            except ApiError as exc:
                return exc, request

    def test_configuration_cannot_raise_the_user_ceiling(self):
        self.assertEqual(HARD_CEILING_USD, 40.0)
        self.assertEqual(BudgetLedger(self.path, 1000, 1000).cap, 40.0)
        with patch.dict(os.environ, {"META_BUDGET_USD": "1000", "META_BUDGET_RUN_USD": "1000"}):
            ledger = BudgetLedger.from_env()
        self.assertEqual((ledger.cap, ledger.run_cap), (40.0, 40.0))
        for bad in ("nan", "inf", "-5", "abc"):
            with patch.dict(os.environ, {"META_BUDGET_USD": bad}):
                self.assertLessEqual(BudgetLedger.from_env().cap, 40.0)

    def test_call_refused_before_network_when_worst_case_could_cross_cap(self):
        result, request = self.call(self.provider(cap=0.001, run_cap=0.001), return_value=GOOD)
        self.assertEqual(result.status, 402)
        request.assert_not_called()

    def test_run_cap_is_enforced_across_calls(self):
        provider = self.provider(cap=10, run_cap=0.05)
        first, _ = self.call(provider, return_value=GOOD)
        self.assertIn("content", first)
        self.assertAlmostEqual(first["cost_usd"], (1000 * 1.25 + 1000 * 4.25) / 1e6)
        for _ in range(5):
            result, request = self.call(provider, return_value=GOOD)
            if isinstance(result, ApiError):
                request.assert_not_called()
                break
        self.assertIsInstance(result, ApiError)
        self.assertLessEqual(BudgetLedger(self.path, 10, 0.05, "test").status()["run_used_usd"], 0.05)

    def test_ambiguous_failure_stays_charged_and_definite_refusal_is_released(self):
        provider = self.provider()
        self.call(provider, side_effect=ApiError(503, "timeout"))
        self.assertGreater(provider.ledger.status()["used_usd"], 0)
        used = provider.ledger.status()["used_usd"]
        self.call(provider, side_effect=ApiError(401, "bad key"))
        self.assertEqual(provider.ledger.status()["used_usd"], used)

    def test_reasoning_is_part_of_completion_unless_it_exceeds_it(self):
        ledger = BudgetLedger(self.path, 1, 1, "test")
        inside = ledger.reserve("muse-spark-1.3", 10, 100)
        self.assertAlmostEqual(ledger.settle(inside, {"prompt_tokens": 0, "completion_tokens": 1000, "completion_tokens_details": {"reasoning_tokens": 900}}), 1000 * 4.25 / 1e6)
        beyond = ledger.reserve("muse-spark-1.3", 10, 100)
        self.assertAlmostEqual(ledger.settle(beyond, {"prompt_tokens": 0, "completion_tokens": 100, "completion_tokens_details": {"reasoning_tokens": 900}}), 1000 * 4.25 / 1e6)

    def test_missing_usage_keeps_worst_case(self):
        provider = self.provider()
        self.call(provider, return_value={k: v for k, v in GOOD.items() if k != "usage"})
        self.assertGreater(provider.ledger.status()["used_usd"], 0.017)

    def test_unknown_contributor_and_corrupt_ledger_fail_closed(self):
        with self.assertRaises(ApiError):
            MetaProvider(model="muse-spark-1.3-contributor")
        result, request = self.call(MetaProvider(model="muse-unpriced", ledger=BudgetLedger(self.path)), return_value=GOOD)
        self.assertEqual(result.status, 402)
        request.assert_not_called()
        self.path.write_text("not json")
        result, request = self.call(self.provider(), return_value=GOOD)
        self.assertEqual(result.status, 402)
        request.assert_not_called()

    def test_ledger_never_records_prompts_or_key(self):
        provider = self.provider()
        self.call(provider, return_value=GOOD)
        text = self.path.read_text()
        self.assertNotIn("unit-test-placeholder", text)
        self.assertNotIn("summarize_expenses", text)

    def test_env_file_is_allowlisted_and_ignores_placeholder(self):
        env = Path(self.temp.name) / ".env"
        env.write_text("MODEL_API_KEY=REPLACE_ME\nOTHER=x\nMETA_BUDGET_USD=5\n")
        with patch.dict(os.environ, {}, clear=True):
            load_env_file(env)
            self.assertEqual(dict(os.environ), {"META_BUDGET_USD": "5"})


if __name__ == "__main__":
    unittest.main()
