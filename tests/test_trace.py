import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from agent_marketplace.market import MarketStore
from agent_marketplace.planner import reference_plan
from agent_marketplace.protocol import ApiError, sha256_text
from agent_marketplace.providers import FixtureProvider
from agent_marketplace.reference import CONTRACTS
from agent_marketplace.requester import Requester
from agent_marketplace.trace import build_trace
from agent_marketplace.worker import Worker


class LocalRequester(Requester):
    def __init__(self, store, directory, **kwargs):
        super().__init__("http://127.0.0.1:9876", "requester-test", directory, **kwargs)
        self.store = store
        self.requester_url = "http://127.0.0.1:9877"

    def market(self, method, path, body=None):
        return self.store.handle(method, path, "requester-test", body)


class TraceProjectionTests(unittest.TestCase):
    def test_sources_are_ordered_stable_and_projected_without_payloads(self):
        status = {
            "job_id": "job1", "tasks": [{"task_id": "task1", "kind": "parser"}],
            "token": "DO-NOT-FORWARD", "events": [
                {"sequence": 1, "time": 1, "event": "plan_started", "raw_prompt": "DO-NOT-FORWARD"},
                {"sequence": 2, "time": 2, "event": "meta_call_started", "provider": "meta", "model": "muse-spark-1.3", "operation": "planning", "api_key": "DROP"},
                {"sequence": 3, "time": 6, "event": "verification_completed", "task_id": "task1", "worker_id": "worker-a", "passed": True, "cases_passed": 4, "cases_total": 4, "reason": "DO-NOT-FORWARD"}],
            "market": {"events": [{"sequence": 1, "time": 3, "kind": "bid_received", "task_id": "task1", "worker_id": "worker-a"}],
                       "bids": [{"task_id": "task1", "worker_id": "worker-a", "credits": 10}], "attempts": []},
        }
        worker_events = [
            {"time": 4, "sequence": 1, "event": "model_call_started", "worker_id": "worker-a", "task_id": "task1", "attempt_id": "attempt1", "provider": "simulated-meta", "model": "muse-spark-1.3", "content": "DO-NOT-FORWARD"},
            {"time": 5, "sequence": 2, "event": "model_call_completed", "worker_id": "worker-a", "task_id": "task1", "attempt_id": "attempt1", "provider": "simulated-meta", "usage": {"total_tokens": 3, "secret": "DO-NOT-FORWARD"}},
        ]
        trace = build_trace(status, worker_events)
        self.assertEqual(trace, build_trace(status, worker_events))
        self.assertEqual([entry["time"] for entry in trace], list(range(1, 7)))
        self.assertEqual(len({entry["id"] for entry in trace}), 6)
        self.assertNotIn("DO-NOT-FORWARD", json.dumps(trace))
        self.assertNotIn("DROP", json.dumps(trace))
        self.assertNotIn('"api_key"', json.dumps(trace))
        self.assertEqual(trace[1]["component"], "meta")
        self.assertEqual(trace[1]["caller_component"], "planner")
        self.assertEqual(trace[2]["credits"], 10)
        self.assertEqual(trace[2]["kind"], "parser")
        self.assertEqual(trace[3]["origin"], "worker-report")
        self.assertEqual(trace[3]["component"], "meta")
        self.assertEqual(trace[3]["caller_component"], "worker-a")
        self.assertEqual(trace[4]["usage"], {"total_tokens": 3})
        self.assertEqual(trace[-1]["component"], "verifier")
        self.assertIn("passed (4/4 cases)", trace[-1]["message"])

    def test_unknown_events_invalid_numbers_and_raw_messages_are_not_forwarded(self):
        events = [
            {"time": 1, "event": "SECRET-EVENT", "message": "SECRET"},
            {"time": float("nan"), "event": "plan_started"},
            {"time": 2, "event": "artifact_received", "task_id": "path/SECRET", "content": "SECRET", "token": "SECRET", "reason": "SECRET", "elapsed_ms": float("inf"), "credits": True, "artifact_sha256": "SECRET"},
        ]
        trace = build_trace({"events": events})
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0]["event"], "artifact_received")
        self.assertNotIn("SECRET", json.dumps(trace))
        self.assertNotIn("credits", trace[0])
        self.assertNotIn("elapsed_ms", trace[0])
        self.assertEqual(build_trace(None), [])

    def test_ids_survive_other_events_entering_or_leaving_a_snapshot(self):
        old = {"sequence": 1, "time": 1, "event": "plan_started"}
        retained = {"sequence": 2, "time": 2, "event": "plan_settled", "source": "reference"}
        future = {"sequence": 3, "time": 3, "event": "application_integration_started"}
        before = build_trace({"events": [old, retained]})
        after = build_trace({"events": [retained, future]})
        self.assertEqual(before[1]["id"], after[0]["id"])
        self.assertEqual(len(build_trace({"events": [retained, retained]})), 1)

    def test_numeric_projection_rejects_nonfinite_and_huge_values(self):
        trace = build_trace({"events": [
            {"time": 1, "event": "meta_call_completed", "provider": "meta", "cost_usd": float("nan"), "elapsed_ms": float("inf")},
            {"time": 10 ** 1000, "event": "plan_started"},
        ]})
        self.assertEqual(len(trace), 1)
        self.assertNotIn("cost_usd", trace[0])
        self.assertNotIn("elapsed_ms", trace[0])
        view = Requester._attempt_view({"result": {"cost_usd": float("inf"), "elapsed_ms": float("nan"), "usage": {"total_tokens": -1}}})
        self.assertIsNone(view["cost_usd"])
        self.assertIsNone(view["elapsed_ms"])
        self.assertEqual(view["usage"], {})
        json.dumps(view, allow_nan=False)

    def test_fixture_work_is_never_labeled_as_a_meta_api_call(self):
        trace = build_trace({}, [{"time": 1, "event": "model_call_started", "worker_id": "worker-a", "provider": "fixture", "model": "prewritten-reference-v1"}])
        self.assertEqual(trace[0]["component"], "worker-a")
        self.assertNotIn("Meta API", trace[0]["message"])


class RequesterEventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MarketStore(Path(self.temp.name) / "market.db", "requester-test", "enrollment-test")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_status_remains_responsive_during_single_flight_planning(self):
        entered, release = threading.Event(), threading.Event()
        calls, errors = [], []
        def planner():
            calls.append(True)
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test planner timeout")
            return reference_plan()
        requester = LocalRequester(self.store, Path(self.temp.name) / "requester", planner=planner)
        def run_tick():
            try:
                requester.tick(schedule=False)
            except Exception as exc:
                errors.append(exc)
        first = threading.Thread(target=run_tick)
        second = threading.Thread(target=requester.plan)
        first.start()
        self.assertTrue(entered.wait(1))
        second.start()
        try:
            before = time.monotonic()
            status = requester.status()
            self.assertLess(time.monotonic() - before, 0.5)
            self.assertTrue(status["planning"])
            self.assertIsNone(status["plan"])
            self.assertIn("plan_started", [e["event"] for e in status["trace"]])
        finally:
            release.set()
            first.join(3)
            second.join(3)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 1)
        status = requester.status()
        self.assertFalse(status["planning"])
        self.assertEqual(sum(e["event"] == "plan_settled" for e in status["events"]), 1)
        status["plan"]["source"] = "changed by observer"
        self.assertEqual(requester.state["plan"]["source"], "reference")

    def test_planning_failure_does_not_disclose_exception_body_in_trace(self):
        requester = LocalRequester(self.store, Path(self.temp.name) / "requester", planner=Mock(side_effect=ApiError(503, "SECRET-REMOTE-BODY")))
        with self.assertRaises(ApiError):
            requester.plan()
        status = requester.status()
        self.assertFalse(status["planning"])
        self.assertNotIn("SECRET-REMOTE-BODY", json.dumps(status))
        self.assertIn("plan_failed", [e["event"] for e in status["trace"]])

    def test_verified_acceptance_events_are_authoritative_and_have_counts(self):
        requester = LocalRequester(self.store, Path(self.temp.name) / "requester")
        tokens = {}
        for worker_id in ("worker-a", "worker-b"):
            tokens[worker_id] = self.store.handle("POST", "/v1/workers/register", "enrollment-test", {
                "worker_id": worker_id, "capabilities": ["python-module-v1"], "provider": "fixture", "model": "reference", "slots": 1})["worker_token"]
        requester.tick(schedule=False)
        for _ in range(8):
            for task in requester.state["tasks"].values():
                if task["advertised"] and not task["accepted"]:
                    for worker_id, token in tokens.items():
                        try:
                            self.store.handle("POST", "/v1/tasks/" + task["task_id"] + "/bids", token, {"credits": 10})
                        except ApiError:
                            pass
            requester.tick()
            for kind, task in requester.state["tasks"].items():
                attempt = requester.state["attempts"].get(task.get("current_attempt"))
                if attempt and not attempt.get("result"):
                    artifact = FixtureProvider().generate_module(CONTRACTS[kind], {"allow_meta": False})
                    requester.receive(attempt["attempt_id"], attempt["token"], {**artifact, "task_id": task["task_id"],
                        "attempt_id": attempt["attempt_id"], "filename": CONTRACTS[kind]["filename"], "sha256": sha256_text(artifact["content"]), "elapsed_ms": 1})
            if requester.tick():
                break
        status = requester.status()
        self.assertTrue(status["complete"])
        self.assertEqual(len(status["market"]["receipts"]), 3)
        for attempt in status["attempts"]:
            self.assertEqual(attempt["report"]["passed"], True)
            self.assertGreater(attempt["report"]["cases_total"], 0)
            self.assertEqual(attempt["report"]["cases_passed"], attempt["report"]["cases_total"])
            self.assertEqual(len(attempt["artifact_sha256"]), 64)
            events = [e["event"] for e in status["events"] if e.get("attempt_id") == attempt["attempt_id"]]
            self.assertLess(events.index("artifact_received"), events.index("verification_started"))
            self.assertLess(events.index("verification_completed"), events.index("task_accepted_and_paid"))
            self.assertNotIn("token", attempt)
            self.assertNotIn("content", attempt)
        self.assertIn("application_integration_started", [e["event"] for e in status["trace"]])
        self.assertIn("application_integration_passed", [e["event"] for e in status["trace"]])
        self.assertTrue(all(e["origin"] == "observed" for e in status["trace"]))
        self.assertNotIn("requester-test", json.dumps(status))


class WorkerCallEventTests(unittest.TestCase):
    def test_generation_and_failure_events_have_no_source_or_credentials(self):
        events = []
        worker = Worker("http://127.0.0.1:9876", "SECRET-ENROLLMENT", "worker-a", on_event=events.append)
        deadline = time.time() + 30
        dispatch = {"task_id": "task1", "attempt_id": "attempt1", "sensitivity": "public", "contract": CONTRACTS["parser"], "provider_policy": {"allow_meta": False}, "deadline": deadline}
        worker._request = Mock(side_effect=lambda method, *args, **kwargs: dispatch if method == "GET" else {"received": True})
        assignment = {"task_id": "task1", "attempt_id": "attempt1", "lease_until": deadline, "requester_url": "http://127.0.0.1:9877", "dispatch_token": "SECRET-ATTEMPT"}
        result = worker.execute_assignment(assignment)
        self.assertEqual([e["event"] for e in events], ["attempt_started", "model_call_started", "model_call_completed", "artifact_delivered"])
        self.assertTrue(all(isinstance(e["time"], (int, float)) for e in events))
        self.assertEqual(len({e["sequence"] for e in events}), 4)
        self.assertNotIn("SECRET", json.dumps(events))
        self.assertNotIn(result["content"], json.dumps(events))
        events.clear()
        worker.provider.generate_module = Mock(side_effect=ApiError(503, "SECRET-REMOTE-BODY"))
        with self.assertRaises(ApiError):
            worker.execute_assignment(assignment)
        self.assertEqual(events[-1]["event"], "model_call_failed")
        self.assertEqual(events[-1]["status"], 503)
        self.assertNotIn("SECRET", json.dumps(events))


if __name__ == "__main__":
    unittest.main()
