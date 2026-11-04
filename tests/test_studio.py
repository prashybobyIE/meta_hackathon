"""Local dashboard controls exercise real workers and protect the control plane."""
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_marketplace.protocol import ApiError
from agent_marketplace.studio import RunManager, component_resources, create_server


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = RunManager(Path(self.temp.name), paced=False)
        self.server = create_server(self.manager, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        with urlopen(self.base) as response:
            self.page = response.read().decode()
        self.session = re.search(r'<meta name="demo-session" content="([^"]+)"', self.page).group(1)

    def tearDown(self):
        self.manager.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def post(self, path, body, *, session=True, origin=None, host=None):
        headers = {"Content-Type": "application/json"}
        if session:
            headers["X-Demo-Session"] = self.session
        if origin:
            headers["Origin"] = origin
        if host:
            headers["Host"] = host
        req = Request(self.base + path, json.dumps(body).encode(), headers, method="POST")
        try:
            with urlopen(req) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            try:
                return exc.code, json.load(exc)
            finally:
                exc.close()

    def test_no_run_or_billable_calls_on_page_load_and_session_not_in_status(self):
        self.assertIn('name="demo-session"', self.page)
        with urlopen(self.base + "/v1/status") as response:
            status = json.load(response)
        self.assertEqual(status["studio"]["phase"], "idle")
        self.assertFalse(status["studio"]["meta_available"])
        self.assertEqual(status["trace"], [])
        self.assertNotIn(self.session, json.dumps(status))
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_component_resources_distinguish_hosted_compute_from_local_agents_and_simulation(self):
        rehearsal = component_resources({"studio": {"mode": "simulated-meta", "failure_enabled": False}})
        self.assertEqual(rehearsal["meta"]["location"], "This Mac")
        self.assertEqual(rehearsal["worker-fault"]["runtime"], "Disabled for this run")
        live = component_resources({"studio": {"mode": "meta"}, "plan": {"model": "muse-spark-1.3"}})
        self.assertEqual(live["meta"]["location"], "Meta hosted service")
        self.assertEqual(live["worker-a"]["location"], "This Mac")
        self.assertEqual(live["worker-a"]["runtime"], "Separate Python worker process")
        self.assertEqual(live["verifier"]["model"], "No model grader")
        self.assertEqual(set(live), {"requester", "planner", "market", "meta", "verifier", "assembler", "ledger", "worker-a", "worker-b", "worker-fault"})

    def test_csrf_dns_rebinding_and_unexpected_run_fields_cannot_start_work(self):
        body = {"provider": "simulated-meta", "failure": False}
        for options in ({"session": False}, {"origin": "https://other.example"}, {"host": "other.example"}):
            code, _ = self.post("/v1/runs", body, **options)
            self.assertEqual(code, 403)
        for value in ({**body, "command": "ignored"}, {**body, "provider": "ollama"}, {**body, "failure": 1}):
            code, _ = self.post("/v1/runs", value)
            self.assertEqual(code, 400)
        with patch.dict(os.environ, {"MODEL_API_KEY": "not-a-real-test-key"}):
            code, _ = self.post("/v1/runs", {"provider": "meta", "failure": False})
            self.assertEqual(code, 403)
        self.assertEqual(self.manager.phase, "idle")

    def test_one_run_at_a_time_and_private_errors_are_not_reflected(self):
        waiting, release = threading.Event(), threading.Event()
        def runner(*args, **kwargs):
            waiting.set()
            release.wait(3)
            raise RuntimeError("do-not-expose-credential-or-private-path")
        self.manager.runner = runner
        try:
            code, _ = self.post("/v1/runs", {"provider": "simulated-meta", "failure": False})
            self.assertEqual(code, 202)
            self.assertTrue(waiting.wait(2))
            code, _ = self.post("/v1/runs", {"provider": "simulated-meta", "failure": False})
            self.assertEqual(code, 409)
        finally:
            release.set()
            self.manager.thread.join(3)
        status = self.manager.status()
        self.assertEqual(status["studio"]["phase"], "failed")
        self.assertNotIn("do-not-expose", json.dumps(status))

    def test_status_does_not_label_previous_results_with_the_next_run_identity(self):
        entered, resume, finish_runner = threading.Event(), threading.Event(), threading.Event()
        self.manager.phase, self.manager.run_id = "complete", "old-run"
        self.manager.cached = self.manager._empty()
        self.manager.cached["job_id"] = "old-job"
        self.manager.runner = lambda *a, **k: finish_runner.wait(3)
        original, first = self.manager._worker_events, [True]
        def collect(path):
            if first[0]:
                first[0] = False
                entered.set()
                resume.wait(3)
            return original(path)
        result = []
        with patch.object(self.manager, "_worker_events", side_effect=collect):
            reader = threading.Thread(target=lambda: result.append(self.manager.status()))
            reader.start()
            try:
                self.assertTrue(entered.wait(2))
                new = self.manager.start({"provider": "simulated-meta", "failure": False})
                resume.set()
                reader.join(3)
                self.assertEqual(result[0]["studio"]["run_id"], new["run_id"])
                self.assertNotEqual(result[0]["job_id"], "old-job")
            finally:
                resume.set()
                finish_runner.set()
                reader.join(3)
                self.manager.thread.join(3)

    def test_status_excludes_unrelated_account_totals_and_reports_only_this_run_calls(self):
        self.manager.phase, self.manager.run_id = "complete", "current-run"
        self.manager.cached = self.manager._empty()
        self.manager.cached["budget"] = {"used_usd": 987.654, "run_used_usd": 0.02, "remaining_usd": 456.7, "calls": 12345, "run_cap_usd": 1}
        self.manager.cached["events"] = [{"time": 1, "event": "model_call_started", "provider": "meta", "operation": "planning"}]
        status = self.manager.status()
        self.assertEqual(status["budget"]["calls"], 1)
        self.assertEqual(status["budget"]["used_usd"], 0.02)
        for private_total in ("987.654", "456.7", "12345", "remaining_usd"):
            self.assertNotIn(private_total, json.dumps(status))

    def test_sample_execution_binds_result_to_accepted_run_and_blocks_overlapping_start(self):
        entered, resume = threading.Event(), threading.Event()
        self.manager.phase, self.manager.run_id = "complete", "accepted-run"
        self.manager.run_path = Path(self.temp.name)
        self.manager.cached = {"complete": True}
        result = []
        def build(*a):
            entered.set()
            resume.wait(3)
            return {"total_cents": 100}
        with patch("agent_marketplace.studio.build_report", side_effect=build):
            runner = threading.Thread(target=lambda: result.append(self.manager.run_application({"input": "public CSV"})))
            runner.start()
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(ApiError) as rejected:
                    self.manager.start({"provider": "simulated-meta", "failure": False})
                self.assertEqual(rejected.exception.status, 409)
            finally:
                resume.set()
                runner.join(3)
        self.assertEqual(result[0]["run_id"], "accepted-run")

    def test_real_process_rehearsal_trace_delivery_and_accepted_app_interaction(self):
        self.assertEqual(self.post("/v1/application/run", {"input": "x"})[0], 409)
        code, _ = self.post("/v1/runs", {"provider": "simulated-meta", "failure": True})
        self.assertEqual(code, 202)
        until = time.monotonic() + 25
        observed = []
        while time.monotonic() < until:
            status = self.manager.status()
            observed.extend(e["event"] for e in status["trace"])
            if status["studio"]["phase"] in ("complete", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(status["studio"]["phase"], "complete", status.get("studio"))
        self.assertEqual(sum(t["accepted"] for t in status["tasks"]), 3)
        self.assertEqual(status["market"]["credits"]["reserved"], 0)
        receipts = status["market"]["receipts"]
        self.assertEqual(len({r["task_id"] for r in receipts}), 3)
        self.assertEqual(status["market"]["credits"]["settled"], sum(r["credits"] for r in receipts))
        self.assertNotIn("worker-fault", {r["worker_id"] for r in receipts})
        self.assertEqual(status["integration"]["report"]["total_cents"], 4025)
        events = {e["event"] for e in status["trace"]}
        self.assertIn("bid_received", events)
        self.assertIn("model_call_started", events)
        self.assertIn("verification_completed", events)
        self.assertIn("application_integration_passed", events)
        self.assertTrue(any(e["component"] == "meta" and e.get("operation") == "planning" for e in status["trace"]))
        self.assertTrue(any(e["origin"] == "worker-report" for e in status["trace"]))
        self.assertTrue(any(a["status"] == "expired" for a in status["market"]["attempts"]))
        self.assertFalse(status["confidential_policy"]["allowed"])
        for forbidden in ("worker_token", "dispatch_token", "MODEL_API_KEY", str(self.temp.name)):
            self.assertNotIn(forbidden, json.dumps(status))
        sample = "date,merchant,amount,category\n2026-11-04,Coffee,2.50,\n2026-11-04,Train,3.00,\n"
        code, result = self.post("/v1/application/run", {"input": sample})
        self.assertEqual(code, 200)
        self.assertEqual(result["report"], {"total_cents": 550, "by_category": {"food": 250, "transport": 300}, "count": 2})
        self.assertEqual(self.post("/v1/application/run", {"input": "bad csv"})[0], 400)
        self.assertEqual(self.post("/v1/application/run", {"input": "x" * 65537})[0], 413)
        # The accepted application is rerun; model calls and credits are unchanged.
        self.assertEqual(self.manager.status()["market"]["credits"], status["market"]["credits"])
        restored = RunManager(self.temp.name)
        restored.resume_completed(self.manager.run_path)
        self.assertTrue(restored.status()["studio"]["restored"])
        self.assertEqual(restored.status()["studio"]["run_id"], status["studio"]["run_id"])
        self.assertEqual(restored.run_application({"input": sample})["report"]["total_cents"], 550)
        parser = self.manager.run_path / "requester" / "artifacts" / "parser.py"
        parser.write_text(parser.read_text() + "\n# modified after acceptance\n")
        with self.assertRaisesRegex(ValueError, "does not match acceptance"):
            RunManager(self.temp.name).resume_completed(self.manager.run_path)


if __name__ == "__main__":
    unittest.main()
