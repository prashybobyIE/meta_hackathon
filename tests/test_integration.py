import json
from pathlib import Path
import tempfile
import unittest

from agent_marketplace.demo import run_demo
from agent_marketplace.reference import EXPECTED_REPORT


class IntegrationTests(unittest.TestCase):
    def test_network_workers_complete_and_each_task_is_settled_once(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = run_demo(directory, quiet=True, timeout=20)
            self.assertEqual(summary["accepted_tasks"], 3)
            self.assertEqual(summary["attempts"], 3)
            self.assertEqual(len(summary["workers_used"]), 2)
            self.assertEqual(summary["integration"]["report"], EXPECTED_REPORT)
            self.assertEqual(summary["credits"]["reserved"], 0)
            self.assertFalse(summary["confidential_policy"]["allowed"])
            snapshot = json.loads((Path(directory) / "market-snapshot.json").read_text())
            self.assertEqual(len({r["task_id"] for r in snapshot["receipts"]}), 3)
            self.assertTrue((Path(directory) / "requester/artifacts/parser.py").exists())

    def test_worker_process_crash_expires_and_reassigns_without_reward(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = run_demo(directory, failure=True, quiet=True, timeout=20)
            self.assertTrue(summary["complete"])
            self.assertGreaterEqual(summary["expired_attempts"], 1)
            self.assertNotIn("worker-fault", summary["workers_used"])
            self.assertEqual(len(summary["workers_used"]), 2)
            snapshot = json.loads((Path(directory) / "market-snapshot.json").read_text())
            self.assertEqual(len(snapshot["receipts"]), 3)
            self.assertEqual(summary["credits"]["settled"], sum(r["credits"] for r in snapshot["receipts"]))


if __name__ == "__main__":
    unittest.main()
