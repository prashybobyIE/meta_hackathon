"""Offline regressions for private release bookkeeping, authenticated replies and UI claims."""
import base64
import copy
import json
import os
import shutil
import subprocess
import unittest
from unittest.mock import patch
from urllib.error import URLError

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from agent_marketplace import protected
    from agent_marketplace.snp import AttestationRefused, _derive
except ImportError:
    raise unittest.SkipTest("Needs cryptography: run with .venv/bin/python")

from agent_marketplace.dashboard import DASHBOARD
from agent_marketplace.protocol import ApiError


class ProtectedFlowTests(unittest.TestCase):
    def setUp(self):
        self.host = "ab" * 32
        self.policy = {"version": 1, "measurements": ["ab" * 48], "minimum_tcb": {"Genoa": {"bootloader": 3, "tee": 0, "snp": 20, "microcode": 85}}}
        self.rows = [("SYNTHETIC-PRIVATE-COFFEE", "food"), ("SYNTHETIC-PRIVATE-TRAIN", "transport")]
        self.worker_key = X25519PrivateKey.generate()
        self.verified = {
            "public_key": base64.b64encode(self.worker_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode(),
            "product": "Genoa", "host_data": self.host, "measurement": "ab" * 48, "reported_tcb": "0300000000001455",
        }
        self.roots, self.calls = {"test": []}, []
        self.behavior, self.extra_get_fails = "success", False
        self.answer = {"categories": [category for _, category in self.rows], "model": "llama-3.2-3b-q4", "elapsed_ms": 12}
        self.reply_plaintext = None
        self.addCleanup(patch.stopall)
        self.root_fetch = patch.object(protected, "amd_roots", return_value=self.roots).start()
        self.verify = patch.object(protected, "verify_evidence", side_effect=self.appraise).start()
        self.network = patch.object(protected, "json_request", side_effect=self.endpoint).start()

    def appraise(self, evidence, nonce, expected, roots, *, trust_policy=None):
        self.assertEqual(trust_policy, self.policy)
        self.assertIs(roots, self.roots)
        if expected != self.host:
            raise AttestationRefused("Container policy hash does not match the approved policy")
        return copy.deepcopy(self.verified)

    def endpoint(self, method, url, body=None, **options):
        self.calls.append((method, url, body))
        if method == "GET":
            if self.extra_get_fails and sum(method == "GET" for method, _, _ in self.calls) > 1:
                raise ApiError(503, "private-error-message-must-not-leak")
            return {"synthetic": True}
        if self.behavior == "lost_reply":
            raise ApiError(503, "private-error-message-must-not-leak")
        self.assertEqual(set(body), {"nonce", "iv", "client_public", "ciphertext"})
        nonce = body["nonce"]
        shared = self.worker_key.exchange(X25519PublicKey.from_public_bytes(base64.b64decode(body["client_public"])))
        plaintext = AESGCM(_derive(shared, nonce, "request")).decrypt(base64.b64decode(body["iv"]), base64.b64decode(body["ciphertext"]), nonce.encode())
        self.assertEqual(json.loads(plaintext), {"rows": [{"merchant": merchant} for merchant, _ in self.rows]})
        answer = self.reply_plaintext if self.reply_plaintext is not None else json.dumps(self.answer).encode()
        iv = os.urandom(12)
        ciphertext = AESGCM(_derive(shared, nonce, "response")).encrypt(iv, answer, nonce.encode())
        if self.behavior == "corrupt_reply":
            ciphertext = bytes([ciphertext[0] ^ 1]) + ciphertext[1:]
        return {"iv": base64.b64encode(iv).decode(), "ciphertext": base64.b64encode(ciphertext).decode()}

    def run_task(self):
        return protected.run_private_task("http://synthetic.invalid", self.host, self.rows, trust_policy=self.policy)

    def test_success_uses_authenticated_request_and_response_and_explicit_states(self):
        result = self.run_task()
        self.assertTrue(result["verified"])
        self.assertTrue(result["accepted"])
        self.assertEqual((result["release_state"], result["delivery_state"]), ("submitted", "response_received"))
        self.assertEqual((result["rows"], result["correct"]), (2, 2))
        self.assertEqual([method for method, _, _ in self.calls], ["GET", "POST"])
        self.assertNotIn(self.rows[0][0], json.dumps(result))

    def test_attestation_failure_never_submits_and_forwards_trust_policy(self):
        self.verify.side_effect = AttestationRefused("Guest measurement is not approved")
        result = self.run_task()
        self.assertFalse(result["verified"])
        self.assertFalse(result["accepted"])
        self.assertEqual((result["release_state"], result["delivery_state"]), ("not_released", "not_submitted"))
        self.assertEqual([method for method, _, _ in self.calls], ["GET"])
        self.assertEqual(self.verify.call_args.kwargs["trust_policy"], self.policy)

    def test_missing_or_empty_policy_prevents_even_endorsement_and_evidence_network(self):
        for policy in (None, {}, {"version": 1, "measurements": [], "minimum_tcb": {}}):
            with self.subTest(policy=policy):
                result = protected.attested_step("http://synthetic.invalid", self.host, trust_policy=policy)
                self.assertFalse(result["verified"])
                self.assertEqual(result["release_state"], "not_released")
        self.root_fetch.assert_not_called()
        self.network.assert_not_called()

    def test_endorsement_outage_is_recorded_without_release_or_private_error(self):
        self.root_fetch.side_effect = URLError("private-error-message-must-not-leak")
        for call in (self.run_task, lambda: protected.attested_step("http://synthetic.invalid", self.host, trust_policy=self.policy)):
            with self.subTest(call=call):
                result = call()
                self.assertFalse(result["verified"])
                self.assertEqual((result["release_state"], result["delivery_state"]), ("not_released", "not_submitted"))
                self.assertIn("endorsement service unavailable", result["reason"])
                self.assertNotIn("private-error", json.dumps(result))
        self.network.assert_not_called()

    def test_malformed_evidence_diagnostic_cannot_echo_private_data(self):
        self.verify.side_effect = ValueError("private-error-message-must-not-leak")
        result = self.run_task()
        self.assertEqual(result["release_state"], "not_released")
        self.assertNotIn("private-error", json.dumps(result))
        self.assertEqual(len(self.calls), 1)

    def test_lost_post_reply_preserves_verification_and_records_uncertain_delivery(self):
        self.behavior = "lost_reply"
        result = self.run_task()
        self.assertTrue(result["verified"])
        self.assertFalse(result["accepted"])
        self.assertEqual((result["release_state"], result["delivery_state"]), ("submitted", "unknown"))
        self.assertIn("ciphertext", self.calls[-1][2])
        self.assertEqual(result["transport_status"], 503)
        self.assertNotIn("private-error", json.dumps(result))

    def test_corrupt_authenticated_reply_cannot_be_accepted_or_called_unreleased(self):
        self.behavior = "corrupt_reply"
        result = self.run_task()
        self.assertTrue(result["verified"])
        self.assertFalse(result["accepted"])
        self.assertEqual((result["release_state"], result["delivery_state"]), ("submitted", "unknown"))

    def test_malformed_authenticated_reply_preserves_response_and_redacts_details(self):
        for plaintext in (b"private-error-message-must-not-leak", b'{"categories": "private-error-message-must-not-leak"}', b"null"):
            with self.subTest(plaintext=plaintext):
                self.reply_plaintext = plaintext
                result = self.run_task()
                self.assertTrue(result["verified"])
                self.assertFalse(result["accepted"])
                self.assertEqual((result["release_state"], result["delivery_state"]), ("submitted", "response_received"))
                self.assertNotIn("private-error", json.dumps(result))

    def test_valid_but_incorrect_answer_is_independently_rejected(self):
        self.answer["categories"] = ["other", "other"]
        result = self.run_task()
        self.assertTrue(result["verified"])
        self.assertFalse(result["accepted"])
        self.assertEqual(result["correct"], 0)
        self.assertEqual(result["delivery_state"], "response_received")

    def test_invalid_local_input_is_never_submitted(self):
        result = protected.run_private_task("http://synthetic.invalid", self.host, [], trust_policy=self.policy)
        self.assertTrue(result["verified"])
        self.assertEqual(result["release_state"], "not_released")
        self.assertFalse(result["accepted"])
        self.assertEqual([method for method, _, _ in self.calls], ["GET"])

    def full_step(self):
        # The public demo uses its fixed answer key; keep this test's two-row task local.
        original = protected.run_private_task
        def local_task(url, host, roots=None, *, trust_policy=None):
            return original(url, host, self.rows, roots, trust_policy=trust_policy)
        with patch.object(protected, "run_private_task", side_effect=local_task):
            return protected.attested_step("http://synthetic.invalid", self.host, trust_policy=self.policy)

    def test_optional_refusal_check_outage_preserves_accepted_task(self):
        self.extra_get_fails = True
        result = self.full_step()
        self.assertTrue(result["accepted"])
        self.assertEqual(result["delivery_state"], "response_received")
        self.assertEqual(result["refusal_check_status"], "unavailable")
        self.assertNotIn("private-error", json.dumps(result))

    def test_optional_refusal_check_checks_valid_baseline_then_wrong_policy(self):
        result = self.full_step()
        self.assertTrue(result["accepted"])
        self.assertEqual(result["refusal_check_status"], "refused")
        self.assertEqual([call.args[2] for call in self.verify.call_args_list], [self.host, self.host, "0" * 64])
        self.assertEqual(sum(method == "POST" for method, _, _ in self.calls), 1)

    def test_optional_check_rejecting_invalid_baseline_is_not_a_successful_negative_test(self):
        original = self.appraise
        def invalid_baseline(*args, **kwargs):
            if any(method == "POST" for method, _, _ in self.calls):
                raise AttestationRefused("Report signature is invalid")
            return original(*args, **kwargs)
        self.verify.side_effect = invalid_baseline
        result = self.full_step()
        self.assertTrue(result["accepted"])
        self.assertEqual(result["refusal_check_status"], "unavailable")
        self.assertEqual([call.args[2] for call in self.verify.call_args_list], [self.host, self.host])

    def test_optional_check_unexpected_acceptance_is_failed_without_erasing_task_history(self):
        self.verify.side_effect = lambda *args, **kwargs: copy.deepcopy(self.verified)
        result = self.full_step()
        self.assertTrue(result["accepted"])
        self.assertEqual(result["refusal_check_status"], "failed")
        self.assertEqual(result["release_state"], "submitted")


@unittest.skipUnless(shutil.which("node"), "Node.js is needed to evaluate the dashboard status function")
class DashboardReleaseTests(unittest.TestCase):
    def views(self, *records):
        function = "function protectedView" + DASHBOARD.split("function protectedView", 1)[1].split("\nasync function refresh", 1)[0]
        program = function + "\nconst records=JSON.parse(require('fs').readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(records.map(record=>protectedView(record,{reason:'No protected backend'}))));"
        result = subprocess.run([shutil.which("node"), "-e", program], input=json.dumps(records), capture_output=True, text=True, timeout=10, check=True)
        return json.loads(result.stdout)

    def test_post_submission_timeout_and_legacy_records_never_claim_no_release(self):
        records = [
            {"verified": True, "accepted": False, "release_state": "submitted", "delivery_state": "unknown"},
            {"verified": False, "accepted": False, "reason": "old transport failure"},
            {"verified": True, "accepted": True},
            {"release_state": "not_released", "delivery_state": "response_received"},
        ]
        views = self.views(*records)
        self.assertIn("SUBMITTED", views[0]["title"])
        for view in views:
            self.assertNotIn("no private data was released", view["title"])
            self.assertNotEqual(view["className"], "card verified")
        self.assertTrue(all("UNKNOWN" in view["title"] for view in views[1:]))

    def test_no_task_record_only_claims_refusal_for_ordinary_marketplace_workers(self):
        view = self.views(None)[0]
        self.assertIn("ordinary marketplace workers", view["title"])
        self.assertIn("No separate protected-task release or delivery state has been recorded", view["reason"])
        self.assertNotIn("not released to any worker", view["title"])

    def test_explicit_pre_submission_refusal_and_completed_rejection_are_distinct(self):
        refusal, rejected, accepted = self.views(
            {"verified": False, "accepted": False, "release_state": "not_released", "delivery_state": "not_submitted"},
            {"verified": True, "accepted": False, "release_state": "submitted", "delivery_state": "response_received"},
            {"verified": True, "accepted": True, "release_state": "submitted", "delivery_state": "response_received", "refusal_check_status": "unavailable"},
        )
        self.assertIn("no private data was released", refusal["title"])
        self.assertIn("not accepted", rejected["title"])
        self.assertEqual(accepted["className"], "card verified")
        self.assertIn("check: unavailable", accepted["reason"])
        self.assertIn("text('confidential-state',protectedStatus.title)", DASHBOARD)


if __name__ == "__main__":
    unittest.main()
