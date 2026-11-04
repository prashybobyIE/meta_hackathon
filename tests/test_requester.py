from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_marketplace.market import MarketStore
from agent_marketplace.protocol import ApiError, canonical_hash, sha256_text
from agent_marketplace.providers import FixtureProvider
from agent_marketplace.reference import CONTRACTS
from agent_marketplace.requester import Requester
from agent_marketplace.planner import reference_plan


class Harness(Requester):
    """Use the same state machine without sockets to inject lost HTTP responses."""
    def __init__(self, store, directory, **kwargs):
        kwargs.setdefault("total_budget", 40)
        super().__init__("http://127.0.0.1:9876", "requester-test", directory, **kwargs)
        self.store = store
        self.requester_url = "http://127.0.0.1:9877"
        self.lose_assignment_response = False
        self.fail_before_local_commit = False

    def market(self, method, path, body=None):
        result = self.store.handle(method, path, "requester-test", body)
        if path.endswith("/assign") and self.lose_assignment_response:
            self.lose_assignment_response = False
            raise ApiError(503, "Response lost after assignment committed")
        return result

    def _commit(self, kind, record):
        if self.fail_before_local_commit:
            self.fail_before_local_commit = False
            raise RuntimeError("Requester interrupted after remote receipt")
        super()._commit(kind, record)


class RequesterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MarketStore(Path(self.temp.name) / "market.db", "requester-test", "enrollment-test")
        self.r = Harness(self.store, Path(self.temp.name) / "requester")
        self.tokens = {}
        for worker_id in ("worker-a", "worker-b"):
            response = self.store.handle("POST", "/v1/workers/register", "enrollment-test", {"worker_id": worker_id, "capabilities": ["python-module-v1"], "provider": "fixture", "model": "reference", "slots": 1})
            self.tokens[worker_id] = response["worker_token"]
        self.r.tick(schedule=False)
        for task in self.r.state["tasks"].values():
            if task["advertised"]:
                for worker, token in self.tokens.items():
                    self.store.handle("POST", "/v1/tasks/" + task["task_id"] + "/bids", token, {"credits": 10})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def submit(self, kind="parser"):
        task = self.r.state["tasks"][kind]
        record = self.r.state["attempts"][task["current_attempt"]]
        contract = CONTRACTS[kind]
        artifact = FixtureProvider().generate_module(contract, {"allow_meta": False})
        body = {**artifact, "task_id": task["task_id"], "attempt_id": record["attempt_id"], "filename": contract["filename"], "sha256": sha256_text(artifact["content"]), "elapsed_ms": 1}
        self.r.receive(record["attempt_id"], record["token"], body)
        return record

    def test_requester_controls_readiness_and_scoped_dispatch(self):
        self.assertFalse(self.r.state["tasks"]["summary"]["advertised"])
        self.r.tick()
        record = self.r.state["attempts"][self.r.state["tasks"]["parser"]["current_attempt"]]
        with self.assertRaises(ApiError):
            self.r.dispatch(record["attempt_id"], "wrong-credential")
        dispatch = self.r.dispatch(record["attempt_id"], record["token"])
        self.assertEqual(dispatch["contract"]["id"], "expense-parser-v1")
        self.assertNotIn("dependencies", dispatch)
        self.assertEqual(self.r.reserved_credits(), 20)

    def test_lost_assignment_response_preserves_token_and_recovers(self):
        self.r.lose_assignment_response = True
        self.r.tick()
        task = self.r.state["tasks"]["parser"]
        attempt_id = task["current_attempt"]
        token = self.r.state["attempts"][attempt_id]["token"]
        self.r = Harness(self.store, Path(self.temp.name) / "requester")
        self.r.tick()
        self.assertEqual(self.r.state["attempts"][attempt_id]["token"], token)
        self.assertEqual(self.r.state["attempts"][attempt_id]["status"], "assigned")
        self.assertEqual(self.r.dispatch(attempt_id, token)["attempt_id"], attempt_id)

    def test_restart_reconciles_remote_receipt_without_double_settlement(self):
        self.r.tick()
        self.submit()
        self.r.fail_before_local_commit = True
        with self.assertRaises(RuntimeError):
            self.r.tick()
        self.assertEqual(len(self.store.snapshot()["receipts"]), 1)
        self.r = Harness(self.store, Path(self.temp.name) / "requester")
        self.r.tick()
        self.assertTrue(self.r.state["tasks"]["parser"]["accepted"])
        self.assertEqual(len(self.store.snapshot()["receipts"]), 1)
        self.assertEqual(self.r.committed_credits(), 10)

    def test_accounting_counts_only_winning_and_current_attempts(self):
        self.r.tick()
        record = self.submit()
        self.r.tick()
        stale = dict(record, attempt_id="stale", credits=999, status="expired")
        self.r.state["attempts"]["stale"] = stale
        self.assertEqual(self.r.committed_credits(), 10)
        self.r.state["attempts"]["rejected"] = {"kind": "categories", "credits": 999, "lease_until": time.time() + 100, "status": "rejected"}
        self.assertEqual(self.r.reserved_credits(), 10)


class TransientProviderRetryTests(unittest.TestCase):
    """One real durable market and requester, with a deterministic clock and no provider calls."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.clock_patch = patch("agent_marketplace.requester.time.time", side_effect=lambda: self.now)
        self.clock_patch.start()
        self.store = MarketStore(Path(self.temp.name) / "market.db", "requester-test", "enrollment-test", clock=lambda: self.now)
        self.directory = Path(self.temp.name) / "requester"
        plan = reference_plan()
        plan["tasks"]["categories"]["depends_on"] = ["parser"]
        self.r = Harness(self.store, self.directory, lease_seconds=120, max_attempts=8, planner=lambda: plan, offer_wait=0.25)
        self.tokens = {}
        self.r.tick(schedule=False)
        self.add_worker("worker-a", 1)

    def tearDown(self):
        self.store.close()
        self.clock_patch.stop()
        self.temp.cleanup()

    @property
    def task(self):
        return self.r.state["tasks"]["parser"]

    def add_worker(self, worker_id, credits):
        worker = self.store.handle("POST", "/v1/workers/register", "enrollment-test", {"worker_id": worker_id, "capabilities": ["python-module-v1"], "provider": "fixture", "model": "reference", "slots": 1})
        self.tokens[worker_id] = worker["worker_token"]
        self.store.handle("POST", "/v1/tasks/" + self.task["task_id"] + "/bids", worker["worker_token"], {"credits": credits})

    def tick(self):
        for worker_id, token in self.tokens.items():
            self.store.handle("POST", f"/v1/workers/{worker_id}/heartbeat", token, {})
        return self.r.tick()

    def current(self):
        return self.r.state["attempts"][self.task["current_attempt"]]

    def fail(self, status=503):
        record = self.current()
        self.r.report_failure(record["attempt_id"], record["token"], {"status": status})
        self.tick()
        return record

    def submit(self, record):
        contract = CONTRACTS["parser"]
        artifact = FixtureProvider().generate_module(contract, {})
        return self.r.receive(record["attempt_id"], record["token"], {**artifact, "task_id": record["task_id"], "attempt_id": record["attempt_id"], "filename": contract["filename"], "sha256": sha256_text(artifact["content"]), "elapsed_ms": 1})

    def test_cheaper_503_worker_yields_to_another_and_old_attempt_is_fenced(self):
        self.add_worker("worker-b", 10)
        self.tick()
        self.assertEqual(self.current()["worker_id"], "worker-a")
        old = self.fail()
        self.assertEqual(self.r.reserved_credits(), 0)
        self.assertEqual(self.store.snapshot()["credits"], {"reserved": 0, "settled": 0})
        with self.assertRaises(ApiError) as exc:
            self.submit(old)
        self.assertEqual(exc.exception.status, 409)
        self.tick()
        winner = self.current()
        self.assertEqual(winner["worker_id"], "worker-b")
        self.assertEqual(self.task["attempts"], 2)
        self.submit(winner)
        self.tick()
        self.assertTrue(self.task["accepted"])
        self.assertNotIn("provider_backoff", self.task)
        self.assertEqual(self.r.committed_credits(), 10)
        receipt = {"attempt_id": winner["attempt_id"], "artifact_sha256": winner["result"]["sha256"], "report_sha256": canonical_hash(winner["report"]), "receipt_id": winner["receipt_id"]}
        duplicate = self.store.handle("POST", f"/v1/tasks/{winner['task_id']}/accept", "requester-test", receipt)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(len(self.store.snapshot()["receipts"]), 1)
        self.assertEqual(self.store.snapshot()["credits"]["settled"], 10)

    def test_sole_worker_waits_without_consuming_attempts_or_offer_timeout(self):
        self.tick()
        self.fail()
        for step in (0, 0.5, 1.4):
            self.now += step
            self.tick()
            self.assertNotIn("current_attempt", self.task)
            self.assertEqual(self.task["attempts"], 1)
            self.assertEqual(self.r.reserved_credits(), 0)
            self.assertNotIn("no_offers_since", self.task)
        self.now += 0.1
        self.tick()
        self.assertEqual(self.current()["worker_id"], "worker-a")
        self.assertEqual(self.task["attempts"], 2)

    def test_exponential_cooldowns_are_bounded_and_attempt_limit_still_stops(self):
        self.r.max_attempts = 6
        self.tick()
        for delay in (2, 4, 8, 16, 30, 30):
            self.fail()
            backoff = self.task["provider_backoff"]["worker-a"]
            self.assertEqual(backoff["retry_at"] - self.now, delay)
            self.assertEqual(self.store.snapshot()["credits"], {"reserved": 0, "settled": 0})
            if self.task["attempts"] == self.r.max_attempts:
                with self.assertRaisesRegex(RuntimeError, "attempt limit"):
                    self.tick()
            else:
                self.now += delay
                self.tick()
        self.assertEqual(self.task["attempts"], 6)
        self.assertEqual(len(self.store.snapshot()["attempts"]), 6)

    def test_restart_preserves_cooldown_and_failing_worker_preference(self):
        self.tick()
        self.fail(429)
        self.r = Harness(self.store, self.directory, lease_seconds=120)
        self.now += 1
        self.tick()
        self.assertNotIn("current_attempt", self.task)
        self.assertEqual(self.task["provider_backoff"]["worker-a"]["status"], 429)
        self.now += 2
        self.add_worker("worker-b", 12)
        self.tick()
        self.assertEqual(self.current()["worker_id"], "worker-b")

    def test_failover_does_not_exceed_reward_budget(self):
        self.add_worker("worker-b", 10)
        self.r.total_budget = 5
        self.tick()
        self.fail()
        with self.assertRaisesRegex(RuntimeError, "reward budget"):
            self.tick()
        self.assertEqual(self.task["attempts"], 1)
        self.assertEqual(self.store.snapshot()["credits"], {"reserved": 0, "settled": 0})

    def test_permanent_failure_stays_excluded_without_cooldown(self):
        self.tick()
        self.fail(402)
        self.assertIn("worker-a", self.task["failed_workers"])
        self.assertNotIn("provider_backoff", self.task)
        with self.assertRaisesRegex(RuntimeError, "No usable workers"):
            self.tick()


if __name__ == "__main__":
    unittest.main()
