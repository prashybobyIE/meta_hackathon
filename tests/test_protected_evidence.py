"""Exercise the release gate with real signed synthetic evidence, without cloud calls."""
import base64
import copy
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from agent_marketplace.protected import run_private_task
    from agent_marketplace.snp import _derive
except ImportError:
    raise unittest.SkipTest("Needs cryptography: run with .venv/bin/python")

import test_snp as samples
from agent_marketplace.protocol import ApiError


class SignedEvidenceReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        roots, vcek, key = samples.chain()
        cls.signer = SimpleNamespace(roots=roots, vcek=vcek, key=key)

    def setUp(self):
        self.worker_key = X25519PrivateKey.generate()
        self.public_key = base64.b64encode(self.worker_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        self.rows = [("SYNTHETIC-PRIVATE-TRAIN", "transport")]
        self.measurement = samples.MEASUREMENT
        self.policy = copy.deepcopy(samples.TRUST)
        self.calls = []
        self.lose_reply = False

    def endpoint(self, method, url, body=None, **options):
        self.calls.append(method)
        if method == "GET":
            nonce = parse_qs(urlsplit(url).query)["nonce"][0]
            runtime = json.dumps({"nonce": nonce, "public_key": self.public_key}).encode()
            return samples.VerifierTests.evidence(self.signer, nonce=nonce, runtime=runtime, measurement=self.measurement)
        nonce = body["nonce"]
        shared = self.worker_key.exchange(X25519PublicKey.from_public_bytes(base64.b64decode(body["client_public"])))
        plaintext = AESGCM(_derive(shared, nonce, "request")).decrypt(base64.b64decode(body["iv"]), base64.b64decode(body["ciphertext"]), nonce.encode())
        self.assertEqual(json.loads(plaintext)["rows"], [{"merchant": self.rows[0][0]}])
        if self.lose_reply:
            raise ApiError(503, "Synthetic lost response after decryption")
        iv = os.urandom(12)
        answer = json.dumps({"categories": ["transport"], "model": "synthetic-test-model", "elapsed_ms": 1}).encode()
        return {"iv": base64.b64encode(iv).decode(), "ciphertext": base64.b64encode(AESGCM(_derive(shared, nonce, "response")).encrypt(iv, answer, nonce.encode())).decode()}

    def run_task(self):
        with patch("agent_marketplace.protected.json_request", side_effect=self.endpoint):
            return run_private_task("http://synthetic.invalid", samples.HOST, self.rows, self.signer.roots, trust_policy=self.policy)

    def test_signed_unapproved_vm_never_receives_encrypted_task(self):
        self.measurement = "ef" * 48
        record = self.run_task()
        self.assertFalse(record["verified"])
        self.assertEqual(record["release_state"], "not_released")
        self.assertEqual(self.calls, ["GET"])

    def test_signed_tcb_below_local_floor_never_receives_encrypted_task(self):
        self.policy["minimum_tcb"]["Genoa"]["microcode"] = samples.TCB["microcode"] + 1
        record = self.run_task()
        self.assertFalse(record["verified"])
        self.assertEqual(record["release_state"], "not_released")
        self.assertEqual(self.calls, ["GET"])

    def test_approved_signed_guest_receives_task_and_response_is_accepted(self):
        record = self.run_task()
        self.assertTrue(record["verified"])
        self.assertTrue(record["accepted"])
        self.assertEqual((record["release_state"], record["delivery_state"]), ("submitted", "response_received"))
        self.assertEqual(self.calls, ["GET", "POST"])
        self.assertNotIn(self.rows[0][0], json.dumps(record))

    def test_lost_reply_after_signed_appraisal_retains_submission_uncertainty(self):
        self.lose_reply = True
        record = self.run_task()
        self.assertTrue(record["verified"])
        self.assertFalse(record["accepted"])
        self.assertEqual((record["release_state"], record["delivery_state"]), ("submitted", "unknown"))
        self.assertEqual(self.calls, ["GET", "POST"])


if __name__ == "__main__":
    unittest.main()
