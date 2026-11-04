"""Repeated dashboard runs must not inherit a failed rehearsal's spending context."""
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent_marketplace import demo


class DemoCleanupTests(unittest.TestCase):
    def test_setup_failure_closes_unstarted_listeners_and_restores_model_context(self):
        previous = {"META_BUDGET_RUN": "previous-run", "SIMULATED_META_URL": "http://127.0.0.1:9001", "META_BUDGET_LEDGER": "/tmp/previous-ledger"}
        market = SimpleNamespace(server_address=("127.0.0.1", 9002), shutdown=Mock(), server_close=Mock())
        simulator = SimpleNamespace(server_address=("127.0.0.1", 9003), shutdown=Mock(), server_close=Mock())
        store = Mock()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, previous), \
                patch.object(demo, "MarketStore", return_value=store), \
                patch.object(demo, "market_server", return_value=market), \
                patch.object(demo, "simulated_server", return_value=simulator), \
                patch.object(demo.BudgetLedger, "from_env", side_effect=RuntimeError("setup failure")):
            with self.assertRaisesRegex(RuntimeError, "setup failure"):
                demo.run_demo(directory, provider="simulated-meta", quiet=True)
            self.assertEqual({k: os.environ[k] for k in previous}, previous)
        store.close.assert_called_once()
        for server in (market, simulator):
            server.server_close.assert_called_once()
            server.shutdown.assert_not_called()


if __name__ == "__main__":
    unittest.main()
