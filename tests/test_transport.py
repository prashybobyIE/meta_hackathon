"""Listener scope and TLS behaviour over real sockets. Nothing here uses a second machine."""
import http.client
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from agent_marketplace.demo import run_demo
from agent_marketplace.market import MarketStore, create_server as market_server
from agent_marketplace.protocol import ApiError, TLS_VERIFY_FAILED, json_request
from agent_marketplace.reference import EXPECTED_REPORT
from agent_marketplace.requester import Requester, create_server as requester_server
from agent_marketplace.transport import client_tls_context, server_tls_context

ROOT = Path(__file__).resolve().parents[1]
REQUESTER_TOKEN, ENROLLMENT_TOKEN = "requester-secret", "enrollment-secret"
_spec = importlib.util.spec_from_file_location("create_dev_tls_cert", ROOT / "scripts" / "create_dev_tls_cert.py")
cert_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cert_tool)
needs_openssl = unittest.skipUnless(shutil.which("openssl"), "openssl is unavailable")


def this_machine_address():
    """A non-loopback address of this machine, whose callers are not loopback peers."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))
            return probe.getsockname()[0]
        except OSError:
            return None


def refused(test, status, call):
    with test.assertRaises(ApiError) as caught:
        call()
    test.assertEqual(caught.exception.status, status)


class Services(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def serve(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.addCleanup(stop)
        return server

    def store(self, allow_insecure=False):
        store = MarketStore(self.directory / "market.sqlite3", REQUESTER_TOKEN, ENROLLMENT_TOKEN, allow_insecure_public_network=allow_insecure)
        self.addCleanup(store.close)
        return store

    def market(self, host="127.0.0.1", tls_context=None, allow_insecure=False):
        store = self.store(allow_insecure)
        return store, self.serve(market_server(store, host, tls_context=tls_context))

    def requester(self, market_url, host="127.0.0.1", tls_context=None, allow_insecure=False, ca_file=None):
        requester = Requester(market_url, REQUESTER_TOKEN, self.directory / "requester", allow_insecure_public_network=allow_insecure, ca_file=ca_file)
        advertise = None if host == "127.0.0.1" else f"{'https' if tls_context else 'http'}://127.0.0.1:1"
        return self.serve(requester_server(requester, host=host, advertise_url=advertise, tls_context=tls_context))


class PlainLoopbackDefaults(Services):
    def test_market_and_requester_stay_open_to_local_callers_without_credentials(self):
        _, market = self.market()
        url = f"http://127.0.0.1:{market.server_port}"
        self.assertTrue(json_request("GET", url + "/health")["ok"])
        self.assertEqual(json_request("GET", url + "/v1/snapshot")["mode"], "public-demo")
        requester = self.requester(url)
        origin = f"http://127.0.0.1:{requester.server_port}"
        self.assertEqual(json_request("GET", origin + "/health")["role"], "requester")
        status = json_request("GET", origin + "/v1/status")
        self.assertEqual(status["market"]["mode"], "public-demo")

    def test_loopback_bound_market_does_not_gate_on_peer_address(self):
        _, market = self.market()
        with patch("agent_marketplace.market.caller_is_loopback", return_value=False):
            self.assertEqual(json_request("GET", f"http://127.0.0.1:{market.server_port}/v1/snapshot")["mode"], "public-demo")

    def test_beyond_loopback_without_tls_or_override_is_refused(self):
        store = self.store()
        with self.assertRaisesRegex(ValueError, "requires TLS"):
            market_server(store, "0.0.0.0")
        requester = Requester("http://127.0.0.1:1", REQUESTER_TOKEN, self.directory / "requester")
        with self.assertRaisesRegex(ValueError, "requires TLS"):
            requester_server(requester, host="0.0.0.0", advertise_url="http://127.0.0.1:1")


class RemoteBoundAccess(Services):
    """Bound to all interfaces. A mock marks loopback peers as remote so these always run."""

    def setUp(self):
        super().setUp()
        _, self.server = self.market("0.0.0.0", allow_insecure=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.requester_http = self.requester(self.url, host="0.0.0.0", allow_insecure=True)
        self.origin = f"http://127.0.0.1:{self.requester_http.server_port}"

    def as_remote(self):
        patches = [patch(name + ".caller_is_loopback", return_value=False) for name in ("agent_marketplace.market", "agent_marketplace.requester")]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_unauthenticated_remote_callers_are_refused_but_health_stays_public(self):
        self.as_remote()
        refused(self, 401, lambda: json_request("GET", self.url + "/v1/snapshot"))
        refused(self, 401, lambda: json_request("GET", self.origin + "/v1/status"))
        self.assertTrue(json_request("GET", self.url + "/health")["ok"])
        self.assertTrue(json_request("GET", self.origin + "/health")["ok"])

    def test_wrong_credentials_are_refused(self):
        self.as_remote()
        for token in ("wrong", ENROLLMENT_TOKEN, "café"):
            refused(self, 401, lambda: json_request("GET", self.url + "/v1/snapshot", token=token))
            refused(self, 401, lambda: json_request("GET", self.origin + "/v1/status", token=token))

    def test_non_ascii_header_is_refused_not_an_error(self):
        self.as_remote()
        for port, path in ((self.server.server_port, "/v1/snapshot"), (self.requester_http.server_port, "/v1/status")):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.putrequest("GET", path)
            connection.putheader("Authorization", b"Bearer caf\xc3\xa9")
            connection.endheaders()
            self.assertEqual(connection.getresponse().status, 401)
            connection.close()

    def test_requester_credential_is_accepted_remotely(self):
        self.as_remote()
        self.assertEqual(json_request("GET", self.url + "/v1/snapshot", token=REQUESTER_TOKEN)["mode"], "public-demo")
        self.assertEqual(json_request("GET", self.origin + "/v1/status", token=REQUESTER_TOKEN)["market"]["mode"], "public-demo")

    def test_request_target_spellings_do_not_bypass_the_check(self):
        self.as_remote()
        spellings = [(self.server.server_port, t) for t in ("/v1/snapshot?x=1", "/v1/snapshot#x", "http://example.test/v1/snapshot")]
        spellings += [(self.requester_http.server_port, t) for t in ("/v1/status?x=1", "/v1/status;a=b", "http://example.test/v1/status")]
        for port, target in spellings:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", target)
            response = connection.getresponse()
            body = response.read()
            connection.close()
            self.assertIn(response.status, (401, 404), target)
            self.assertNotIn(b"workers", body, target)

    def test_real_loopback_peer_is_served_for_the_local_dashboard_and_probe(self):
        self.assertEqual(json_request("GET", self.url + "/v1/snapshot")["mode"], "public-demo")
        self.assertEqual(json_request("GET", self.origin + "/v1/status")["market"]["mode"], "public-demo")

    def test_real_non_loopback_peer_through_this_machines_address_is_refused(self):
        address = this_machine_address()
        if not address:
            self.skipTest("No non-loopback interface")
        market = f"http://{address}:{self.server.server_port}"
        requester = f"http://{address}:{self.requester_http.server_port}"
        refused(self, 401, lambda: json_request("GET", market + "/v1/snapshot"))
        refused(self, 401, lambda: json_request("GET", requester + "/v1/status"))
        self.assertTrue(json_request("GET", market + "/health")["ok"])
        self.assertEqual(json_request("GET", market + "/v1/snapshot", token=REQUESTER_TOKEN)["mode"], "public-demo")
        self.assertEqual(json_request("GET", requester + "/v1/status", token=REQUESTER_TOKEN)["market"]["mode"], "public-demo")


@needs_openssl
class TlsTransport(Services):
    def setUp(self):
        super().setUp()
        self.cert, self.key = cert_tool.create_certificate(self.directory / "tls")
        self.other_cert, _ = cert_tool.create_certificate(self.directory / "other")
        self.server_context = server_tls_context(self.cert, self.key)

    def enroll(self, url, **options):
        return json_request("POST", url + "/v1/workers/register", {"worker_id": "tls-worker", "capabilities": ["python-module-v1"], "provider": "fixture", "model": "fixture", "slots": 1}, token=ENROLLMENT_TOKEN, **options)

    def test_round_trip_with_the_trusted_certificate(self):
        store, market = self.market(tls_context=self.server_context)
        url = f"https://127.0.0.1:{market.server_port}"
        trusted = client_tls_context(self.cert)
        self.assertTrue(json_request("GET", url + "/health", ssl_context=trusted)["ok"])
        self.assertEqual(self.enroll(url, ssl_context=trusted)["worker_id"], "tls-worker")
        self.assertEqual(len(store.snapshot()["workers"]), 1)

    def test_requester_and_market_talk_over_tls(self):
        _, market = self.market(tls_context=self.server_context)
        requester = self.requester(f"https://127.0.0.1:{market.server_port}", tls_context=self.server_context, ca_file=self.cert)
        status = json_request("GET", f"https://127.0.0.1:{requester.server_port}/v1/status", ssl_context=client_tls_context(self.cert))
        self.assertEqual(status["market"]["mode"], "public-demo")

    def test_wrong_or_missing_ca_is_refused_before_any_credential_is_sent(self):
        store, market = self.market(tls_context=self.server_context)
        url = f"https://127.0.0.1:{market.server_port}"
        refused(self, TLS_VERIFY_FAILED, lambda: self.enroll(url, ssl_context=client_tls_context(self.other_cert)))
        refused(self, TLS_VERIFY_FAILED, lambda: self.enroll(url))
        self.assertEqual(store.snapshot()["workers"], [])

    def test_certificate_for_another_name_is_refused(self):
        # Keep the SAN list free of the address the client dials.
        with patch.object(cert_tool, "subject_alt_names", return_value="DNS:market.example.test"):
            only_cert, only_key = cert_tool.create_certificate(self.directory / "only")
        store, market = self.market(tls_context=server_tls_context(only_cert, only_key))
        refused(self, TLS_VERIFY_FAILED, lambda: self.enroll(f"https://127.0.0.1:{market.server_port}", ssl_context=client_tls_context(only_cert)))
        self.assertEqual(store.snapshot()["workers"], [])

    def test_plain_http_to_tls_listener_gets_no_service(self):
        store, market = self.market(tls_context=self.server_context)
        refused(self, 503, lambda: json_request("GET", f"http://127.0.0.1:{market.server_port}/health"))
        refused(self, 503, lambda: self.enroll(f"http://127.0.0.1:{market.server_port}"))
        self.assertEqual(store.snapshot()["workers"], [])

    def test_tls_client_to_plain_listener_fails_without_service(self):
        store, market = self.market()
        refused(self, 503, lambda: self.enroll(f"https://127.0.0.1:{market.server_port}", ssl_context=client_tls_context(self.cert)))
        self.assertEqual(store.snapshot()["workers"], [])

    def test_stalled_connection_does_not_block_other_clients(self):
        _, market = self.market(tls_context=self.server_context)
        with socket.create_connection(("127.0.0.1", market.server_port), timeout=5):
            result = json_request("GET", f"https://127.0.0.1:{market.server_port}/health", ssl_context=client_tls_context(self.cert), timeout=3)
        self.assertTrue(result["ok"])

    def test_tls_beyond_loopback_needs_no_override_and_still_gates_status(self):
        _, market = self.market("0.0.0.0", tls_context=self.server_context)
        url = f"https://127.0.0.1:{market.server_port}"
        trusted = client_tls_context(self.cert)
        with patch("agent_marketplace.market.caller_is_loopback", return_value=False):
            refused(self, 401, lambda: json_request("GET", url + "/v1/snapshot", ssl_context=trusted))
            self.assertEqual(json_request("GET", url + "/v1/snapshot", token=REQUESTER_TOKEN, ssl_context=trusted)["mode"], "public-demo")

    def test_a_ca_for_one_call_does_not_change_default_trust(self):
        _, market = self.market(tls_context=self.server_context)
        url = f"https://127.0.0.1:{market.server_port}/health"
        self.assertTrue(json_request("GET", url, ssl_context=client_tls_context(self.cert))["ok"])
        refused(self, TLS_VERIFY_FAILED, lambda: json_request("GET", url))

    def test_mismatched_or_missing_files_are_configuration_errors(self):
        with self.assertRaises(ValueError):
            server_tls_context(self.cert, None)
        with self.assertRaises(ValueError):
            server_tls_context(self.cert, self.directory / "missing.pem")
        with self.assertRaises(ValueError):
            server_tls_context(self.cert, self.directory / "other" / "key.pem")
        with self.assertRaises(ValueError):
            client_tls_context(self.directory / "missing.pem")

    def test_tls_requester_cannot_advertise_plain_http(self):
        requester = Requester("http://127.0.0.1:1", REQUESTER_TOKEN, self.directory / "requester")
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            requester_server(requester, host="0.0.0.0", advertise_url="http://127.0.0.1:1", tls_context=self.server_context)


@needs_openssl
class CertificateTool(unittest.TestCase):
    def test_key_is_owner_only_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = cert_tool.create_certificate(directory)
            self.assertEqual(stat.S_IMODE(os.stat(key).st_mode), 0o600)
            before = key.read_bytes()
            with self.assertRaises(ValueError):
                cert_tool.create_certificate(directory)
            self.assertEqual(key.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["cert.pem", "key.pem"])

    def test_host_values_cannot_inject_configuration(self):
        for host in ("a\nb", "a,DNS:evil", "-bad", ""):
            with self.assertRaises(ValueError):
                cert_tool.subject_alt_names([host])

    def test_generated_keys_are_gitignored(self):
        if not shutil.which("git"):
            self.skipTest("git is unavailable")
        for name in ("key.pem", "cert.pem", "server.key", ".demo/tls/key.pem"):
            result = subprocess.run(["git", "check-ignore", "-q", "--no-index", name], cwd=ROOT)
            self.assertEqual(result.returncode, 0, name)


@needs_openssl
class DemoOverTls(unittest.TestCase):
    def join_worker(self, directory, address, cert, state):
        processes = []
        def joiner(info):
            market = f"https://{address}:{urlsplit(info['market']).port}"
            processes.append(subprocess.Popen([sys.executable, "-m", "agent_marketplace.worker", "--market", market, "--ca-file", str(cert), "--enrollment-token-file", str(Path(directory) / "enrollment.token"), "--id", "worker-remote", "--bid-credits", "1", "--poll-interval", "0.05", "--state-file", str(state)], stdout=subprocess.DEVNULL, cwd=ROOT))
        return processes, joiner

    def test_remote_workers_demo_completes_over_tls_without_the_insecure_override(self):
        address = this_machine_address() or "127.0.0.1"
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as certs:
            cert, key = cert_tool.create_certificate(certs, [address])
            state = Path(directory) / "remote-state.json"
            processes, joiner = self.join_worker(directory, address, cert, state)
            try:
                summary = run_demo(directory, quiet=True, timeout=30, advertise_host=address, remote_workers=1, on_ready=joiner, tls_cert=cert, tls_key=key)
            finally:
                for process in processes:
                    process.terminate()
                    process.wait(timeout=5)
            self.assertIn("worker-remote", summary["workers_used"])
            self.assertEqual(summary["integration"]["report"], EXPECTED_REPORT)
            self.assertEqual(urlsplit(summary["market"]).scheme, "https")
            self.assertEqual(urlsplit(summary["dashboard"]).scheme, "https")
            self.assertEqual(json.loads(state.read_text())["worker_id"], "worker-remote")

    def test_loopback_demo_over_tls(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as certs:
            cert, key = cert_tool.create_certificate(certs)
            summary = run_demo(directory, quiet=True, timeout=30, tls_cert=cert, tls_key=key)
            self.assertEqual(summary["integration"]["report"], EXPECTED_REPORT)
            self.assertEqual(urlsplit(summary["market"]).scheme, "https")

    def test_certificate_that_does_not_cover_the_advertised_host_stops_the_demo(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as certs:
            with patch.object(cert_tool, "subject_alt_names", return_value="DNS:market.example.test"):
                cert, key = cert_tool.create_certificate(certs)
            with self.assertRaisesRegex(RuntimeError, "certificate must cover"):
                run_demo(directory, quiet=True, timeout=10, advertise_host="127.0.0.1", remote_workers=1, tls_cert=cert, tls_key=key)

    def test_worker_with_wrong_ca_stops_with_a_clear_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = cert_tool.create_certificate(Path(directory) / "a")
            other, _ = cert_tool.create_certificate(Path(directory) / "b")
            token = Path(directory) / "enrollment.token"
            token.write_text(ENROLLMENT_TOKEN)
            store = MarketStore(Path(directory) / "market.sqlite3", REQUESTER_TOKEN, ENROLLMENT_TOKEN)
            server = market_server(store, tls_context=server_tls_context(cert, key))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = subprocess.run([sys.executable, "-m", "agent_marketplace.worker", "--market", f"https://127.0.0.1:{server.server_port}", "--ca-file", str(other), "--enrollment-token-file", str(token), "--id", "worker-untrusting"], capture_output=True, text=True, cwd=ROOT, timeout=30)
                self.assertEqual(result.returncode, 1)
                self.assertIn("TLS certificate verification failed", result.stdout)
                self.assertEqual(store.snapshot()["workers"], [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                store.close()


if __name__ == "__main__":
    unittest.main()
