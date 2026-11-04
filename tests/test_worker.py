import ast
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent_marketplace.protocol import ApiError, CAPABILITY, json_request, sha256_text
from agent_marketplace.providers import (
    DEFAULT_META_MODEL, META_ENDPOINT, MAX_SOURCE_BYTES, PUBLIC_CONTRACTS,
    FixtureProvider, MetaProvider, OllamaProvider, get_provider,
)
from agent_marketplace.worker import Worker, WorkerCrash, validate_endpoint


def setUpModule():
    # Mocked calls must never be charged to the real spending ledger.
    global _ledger_dir, _ledger_patch
    _ledger_dir = tempfile.TemporaryDirectory()
    _ledger_patch = patch.dict(os.environ, {"META_BUDGET_LEDGER": os.path.join(_ledger_dir.name, "ledger.json")})
    _ledger_patch.start()


def tearDownModule():
    _ledger_patch.stop()
    _ledger_dir.cleanup()


def contract(contract_id="expense-summary-v1"):
    spec = PUBLIC_CONTRACTS[contract_id]
    return {"id": contract_id, "filename": spec["filename"], "function": spec["function"], "description": "public fixture"}


class ProviderPolicyTests(unittest.TestCase):
    def test_fixture_is_source_and_honestly_labeled(self):
        provider = FixtureProvider()
        for contract_id in PUBLIC_CONTRACTS:
            result = provider.generate_module(contract(contract_id), {}, "public")
            ast.parse(result["content"])
            self.assertEqual(result["provider"], "fixture")
            self.assertEqual(result["model"], "prewritten-reference-v1")

    def test_private_and_unknown_contracts_fail_before_network(self):
        with patch("agent_marketplace.providers.json_request") as request:
            for provider in [FixtureProvider(), MetaProvider(), OllamaProvider("test-model")]:
                with self.assertRaises(ApiError) as error:
                    provider.generate_module(contract(), {"allow_meta": True}, "confidential")
                self.assertEqual(error.exception.status, 403)
                bad = contract()
                bad["id"] = "unknown"
                with self.assertRaises(ApiError):
                    provider.generate_module(bad, {"allow_meta": True})
            request.assert_not_called()

    def test_unknown_provider_and_contract_identity_have_no_fallback(self):
        with self.assertRaises(ApiError):
            get_provider("made-up")
        bad = contract()
        bad["filename"] = "../../private.py"
        with self.assertRaises(ApiError):
            FixtureProvider().generate_module(bad, {})

    def test_meta_requires_explicit_permission_and_key(self):
        with patch("agent_marketplace.providers.json_request") as request:
            for policy in [{}, {"allow_meta": 1}, {"allow_meta": "true"}, None]:
                with self.assertRaises(ApiError) as error:
                    MetaProvider().generate_module(contract(), policy)
                self.assertEqual(error.exception.status, 403)
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ApiError) as error:
                    MetaProvider().generate_module(contract(), {"allow_meta": True})
                self.assertEqual(error.exception.status, 503)
            request.assert_not_called()

    def test_meta_sends_only_frozen_public_prompt_not_caller_details(self):
        public = contract()
        public["description"] = "PRIVATE-SPEC-MUST-NOT-LEAVE"
        public["tool_result"] = "PRIVATE-TOOL-OUTPUT"
        response = {"model": "muse-spark-1.3", "choices": [{"finish_reason": "stop", "message": {"content": "```python\ndef summarize_expenses(rows):\n    return {}\n```"}}], "usage": {"total_tokens": 42, "secret": "not copied"}}
        with patch.dict(os.environ, {"MODEL_API_KEY": "unit-test-placeholder"}), patch("agent_marketplace.providers.json_request", return_value=response) as request:
            result = MetaProvider().generate_module(public, {"allow_meta": True})
        args, kwargs = request.call_args
        self.assertEqual(args[:2], ("POST", META_ENDPOINT))
        payload = args[2]
        self.assertEqual(payload["model"], DEFAULT_META_MODEL)
        self.assertEqual(payload["max_completion_tokens"], 8192)
        self.assertNotIn("PRIVATE-", json.dumps(payload))
        self.assertEqual(kwargs["token"], "unit-test-placeholder")
        self.assertEqual(result["usage"], {"total_tokens": 42})
        self.assertTrue(result["content"].startswith("def "))

    def test_incomplete_malformed_and_oversized_meta_outputs_rejected(self):
        responses = [
            {},
            {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]},
            {"choices": [{"finish_reason": "stop", "message": {"content": None}}]},
            {"choices": [{"finish_reason": "stop", "message": {"content": "\ud800"}}]},
            {"choices": [{"finish_reason": "stop", "message": {"content": "x" * (MAX_SOURCE_BYTES + 1)}}]},
        ]
        with patch.dict(os.environ, {"MODEL_API_KEY": "unit-test-placeholder"}):
            for response in responses:
                with self.subTest(response_kind=list(response)):
                    with patch("agent_marketplace.providers.json_request", return_value=response), self.assertRaises(ApiError):
                        MetaProvider().generate_module(contract(), {"allow_meta": True})

    def test_provider_errors_hide_remote_body_and_never_retry_generation(self):
        with patch.dict(os.environ, {"MODEL_API_KEY": "unit-test-placeholder"}), patch("agent_marketplace.providers.json_request", side_effect=ApiError(429, "sensitive remote error")) as request:
            with self.assertRaises(ApiError) as error:
                MetaProvider().generate_module(contract(), {"allow_meta": True})
            self.assertNotIn("sensitive", str(error.exception))
            self.assertEqual(request.call_count, 1)

    def test_ollama_is_loopback_and_explicit_model_only(self):
        for endpoint in ["http://example.com", "http://127.0.0.1:11434/path", "http://user:pass@localhost:11434"]:
            with self.assertRaises(ApiError):
                OllamaProvider("some-model", endpoint=endpoint)
        with self.assertRaises(ApiError):
            OllamaProvider()
        with patch("agent_marketplace.providers.json_request", return_value={"done": True, "done_reason": "stop", "response": "def summarize_expenses(rows):\n    return {}"}) as request:
            result = OllamaProvider("some-model").generate_module(contract(), {})
        self.assertEqual(result["provider"], "ollama")
        self.assertEqual(request.call_args.args[1], "http://127.0.0.1:11434/api/generate")


class WorkerEndpointTests(unittest.TestCase):
    def test_plaintext_remote_endpoint_requires_explicit_override(self):
        with self.assertRaises(ApiError):
            Worker("http://192.0.2.5:9000", "enroll", "worker1")
        self.assertEqual(validate_endpoint("https://example.com/"), "https://example.com")
        self.assertEqual(validate_endpoint("http://192.0.2.5", True), "http://192.0.2.5")
        for url in ["file:///tmp/a", "http://user:pass@localhost", "http://localhost/path", "http://localhost?token=x", "http://localhost:bad"]:
            with self.assertRaises(ApiError):
                validate_endpoint(url)


class WorkerHTTPTests(unittest.TestCase):
    def setUp(self):
        self.state = {"requests": [], "results": [], "bid": False, "sensitivity": "public", "contracts": 0, "delivery_failures": 0}
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _auth(self, expected):
                if self.headers.get("Authorization") != "Bearer " + expected:
                    self._send(401, {"error": "unauthorized"})
                    return False
                return True

            def do_GET(self):
                state["requests"].append(("GET", self.path, self.headers.get("Authorization")))
                if self.path == "/transport":
                    raw = state.get("transport_raw", b'{"ok":true}')
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                elif self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/transport")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif self.path == "/v1/tasks" and self._auth("worker-token"):
                    self._send(200, {"tasks": [
                        {"task_id": "task1", "capability": CAPABILITY, "sensitivity": "public", "max_credits": 10},
                        {"task_id": "private1", "capability": CAPABILITY, "sensitivity": "confidential", "max_credits": 10},
                    ]})
                elif self.path == "/v1/workers/worker1/assignments" and self._auth("worker-token"):
                    assignments = [{"task_id": "task1", "attempt_id": "attempt1", "lease_until": time.time() + 30, "requester_url": "http://127.0.0.1:" + str(self.server.server_port), "dispatch_token": "attempt-token"}] if state["bid"] else []
                    self._send(200, {"assignments": assignments})
                elif self.path == "/v1/attempts/attempt1" and self._auth("attempt-token"):
                    state["contracts"] += 1
                    self._send(200, {"task_id": "task1", "attempt_id": "attempt1", "sensitivity": state["sensitivity"], "contract": contract(), "provider_policy": {"allow_meta": False}, "deadline": time.time() + 30})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                state["requests"].append(("POST", self.path, self.headers.get("Authorization")))
                if self.path == "/v1/workers/register" and self._auth("enroll-token"):
                    self._send(200, {"worker_id": "worker1", "worker_token": "worker-token"})
                elif self.path == "/v1/workers/worker1/heartbeat" and self._auth("worker-token"):
                    self._send(200, {"ok": True})
                elif self.path == "/v1/tasks/task1/bids" and self._auth("worker-token"):
                    state["bid"] = True
                    self._send(200, {"bid_id": "bid1", "credits": body["credits"]})
                elif self.path == "/v1/attempts/attempt1/result" and self._auth("attempt-token"):
                    if state["delivery_failures"]:
                        state["delivery_failures"] -= 1
                        self._send(503, {"error": "temporarily unavailable"})
                    else:
                        state["results"].append(body)
                        self._send(200, {"received": True})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def worker(self, **kwargs):
        return Worker(self.url, "enroll-token", "worker1", **kwargs)

    def test_public_network_flow_bids_delivers_and_never_accepts_itself(self):
        events = []
        worker = self.worker(on_event=events.append)
        self.assertEqual(worker.run_once(), 1)
        self.assertEqual(worker.run_once(), 0)
        self.assertEqual(len(self.state["results"]), 1)
        result = self.state["results"][0]
        self.assertEqual(result["sha256"], sha256_text(result["content"]))
        self.assertEqual(result["provider"], "fixture")
        self.assertEqual(result["task_id"], "task1")
        paths = [entry[1] for entry in self.state["requests"]]
        self.assertNotIn("/v1/tasks/private1/bids", paths)
        self.assertFalse(any(path.endswith("/accept") for path in paths))
        serialized = json.dumps(events)
        self.assertNotIn("enroll-token", serialized)
        self.assertNotIn("worker-token", serialized)
        self.assertNotIn("attempt-token", serialized)

    def test_confidential_dispatch_rejected_without_provider_call(self):
        self.state["sensitivity"] = "confidential"
        worker = self.worker()
        with patch.object(worker.provider, "generate_module") as generate:
            self.assertEqual(worker.run_once(), 0)
            generate.assert_not_called()
        self.assertEqual(self.state["results"], [])
        self.assertEqual(worker.last_error["status"], 403)

    def test_intentional_failure_occurs_before_contract_fetch(self):
        worker = self.worker(crash_on_first_assignment=True)
        with self.assertRaises(WorkerCrash):
            worker.run_once()
        self.assertEqual(self.state["contracts"], 0)
        self.assertEqual(self.state["results"], [])

    def test_only_delivery_is_retried_generation_is_once(self):
        self.state["delivery_failures"] = 2
        worker = self.worker()
        with patch.object(worker.provider, "generate_module", wraps=worker.provider.generate_module) as generate:
            self.assertEqual(worker.run_once(), 1)
            self.assertEqual(generate.call_count, 1)
        self.assertEqual(len(self.state["results"]), 1)

    def test_expired_assignment_cannot_fetch_contract(self):
        worker = self.worker()
        with self.assertRaises(ApiError) as error:
            worker.execute_assignment({"task_id": "task1", "attempt_id": "attempt1", "lease_until": time.time() - 1, "requester_url": self.url, "dispatch_token": "attempt-token"})
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.state["requests"], [])

    def test_transport_rejects_malformed_json_without_interpreting_it(self):
        for raw in [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'[]', b'{"x":"\xff"}', b'{"x":1e999}']:
            self.state["transport_raw"] = raw
            with self.subTest(raw=raw), self.assertRaises(ApiError) as error:
                json_request("GET", self.url + "/transport")
            self.assertEqual(error.exception.status, 502)

    def test_authenticated_redirect_is_not_followed(self):
        with self.assertRaises(ApiError) as error:
            json_request("GET", self.url + "/redirect", token="attempt-token")
        self.assertEqual(error.exception.status, 400)
        self.assertEqual([item[1] for item in self.state["requests"]], ["/redirect"])

    def test_loopback_requests_bypass_proxy_environment(self):
        with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1", "all_proxy": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1", "no_proxy": "", "NO_PROXY": ""}):
            self.assertEqual(json_request("GET", self.url + "/transport"), {"ok": True})


if __name__ == "__main__":
    unittest.main()
