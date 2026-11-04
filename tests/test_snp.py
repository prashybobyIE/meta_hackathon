import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from agent_marketplace.snp import AttestationRefused, TCB_COMPONENTS, TCB_OIDS, amd_roots, load_trust_policy, seal, unseal, validate_trust_policy, verify_evidence
except ImportError:
    raise unittest.SkipTest("Needs cryptography: run with .venv/bin/python")

PSS = padding.PSS(padding.MGF1(hashes.SHA384()), padding.PSS.DIGEST_LENGTH)
HOST = "ab" * 32
NONCE = "ad" * 32
MEASUREMENT = "12" * 48
TCB = {"bootloader": 4, "tee": 0, "snp": 21, "microcode": 86}
TRUST = {"version": 1, "measurements": [MEASUREMENT], "minimum_tcb": {"Genoa": {"bootloader": 3, "tee": 0, "snp": 20, "microcode": 85}}}
PUBLIC_KEY = base64.b64encode(X25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def cert(subject, issuer, public, signer, tcb=None, expired=False):
    name = lambda n: x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, n)])
    now = datetime.now(timezone.utc)
    builder = x509.CertificateBuilder().subject_name(name(subject)).issuer_name(name(issuer)).public_key(public).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=2)).not_valid_after(now + timedelta(days=-1 if expired else 1))
    if tcb is not None:
        for component, value in tcb.items():
            raw = b"\x02\x01" + bytes([value]) if value < 128 else b"\x02\x02\x00" + bytes([value])
            builder = builder.add_extension(x509.UnrecognizedExtension(TCB_OIDS[component], raw), critical=False)
    return builder.sign(signer, hashes.SHA384(), rsa_padding=PSS)


def chain(product="Genoa", tcb=TCB, expired=False):
    ark_key, ask_key = rsa.generate_private_key(65537, 2048), rsa.generate_private_key(65537, 2048)
    vcek_key = ec.generate_private_key(ec.SECP384R1())
    ark = cert("ARK", "ARK", ark_key.public_key(), ark_key)
    ask = cert("ASK", "ARK", ask_key.public_key(), ark_key)
    return {product: (ask, ark)}, cert("VCEK", "ASK", vcek_key.public_key(), ask_key, tcb=tcb, expired=expired), vcek_key


class VerifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.roots, cls.vcek, cls.key = chain()

    def evidence(self, nonce=NONCE, host=HOST, policy=0, key=None, runtime=None, measurement=MEASUREMENT, tcb=TCB, vcek=None, version=5):
        runtime = runtime if runtime is not None else json.dumps({"nonce": nonce, "public_key": PUBLIC_KEY}).encode()
        raw = bytearray(1184)
        raw[0:4] = version.to_bytes(4, "little")
        raw[8:16] = policy.to_bytes(8, "little")
        raw[0x34:0x38] = (1).to_bytes(4, "little")
        raw[0x50:0x70] = hashlib.sha256(runtime).digest()
        raw[0xC0:0xE0] = bytes.fromhex(host)
        raw[0x90:0xC0] = bytes.fromhex(measurement)
        for name, offset in TCB_COMPONENTS.items():
            raw[0x180 + offset] = tcb[name]
        r, s = utils.decode_dss_signature((key or self.key).sign(bytes(raw[:0x2A0]), ec.ECDSA(hashes.SHA384())))
        raw[0x2A0:0x2A0 + 72] = r.to_bytes(72, "little")
        raw[0x2E8:0x2E8 + 72] = s.to_bytes(72, "little")
        certs = base64.b64encode(json.dumps({"vcekCert": (vcek or self.vcek).public_bytes(serialization.Encoding.PEM).decode()}).encode()).decode()
        return {"report": raw.hex(), "runtime_data": base64.b64encode(runtime).decode(), "certs": certs}

    def refused(self, evidence, pattern, nonce=NONCE, roots=None, trust=TRUST):
        with self.assertRaisesRegex(AttestationRefused, pattern):
            verify_evidence(evidence, nonce, HOST, roots or self.roots, trust_policy=trust)

    def test_valid_evidence_returns_the_bound_key(self):
        result = verify_evidence(self.evidence(), NONCE, HOST, self.roots, trust_policy=TRUST)
        self.assertEqual(result["public_key"], PUBLIC_KEY)
        self.assertEqual(result["reported_tcb_components"], TCB)

    def test_each_policy_failure_is_refused(self):
        self.refused(self.evidence(host="cd" * 32), "policy hash")
        self.refused(self.evidence(policy=1 << 19), "debugging")
        self.refused(self.evidence(), "not fresh", nonce="bd" * 32)
        self.refused(self.evidence(key=ec.generate_private_key(ec.SECP384R1())), "signature")
        other, _, _ = chain()
        self.refused(self.evidence(), "AMD root", roots=other)

    def test_host_cannot_edit_report_or_swap_key(self):
        evidence = self.evidence()
        raw = bytearray.fromhex(evidence["report"])
        raw[0xC0] ^= 1
        self.refused({**evidence, "report": raw.hex()}, "signature")
        swapped = base64.b64encode(json.dumps({"nonce": NONCE, "public_key": base64.b64encode(os.urandom(32)).decode()}).encode()).decode()
        self.refused({**evidence, "runtime_data": swapped}, "not bound")

    def test_malformed_evidence_is_refused_without_crash(self):
        for bad in ({}, {"report": "zz", "runtime_data": "!", "certs": "x"}, {**self.evidence(), "report": "00"}):
            with self.subTest(bad=list(bad)), self.assertRaises(AttestationRefused):
                verify_evidence(bad, NONCE, HOST, self.roots, trust_policy=TRUST)

    def test_guest_measurement_must_be_independently_approved(self):
        self.refused(self.evidence(measurement="ff" * 48), "not independently approved")
        self.refused(self.evidence(measurement="00" * 48, tcb={name: 0 for name in TCB}), "not independently approved")
        with self.assertRaisesRegex(AttestationRefused, "pinned value"):
            verify_evidence(self.evidence(), NONCE, HOST, self.roots, "ff" * 48, trust_policy=TRUST)
        # A legacy single measurement argument is not a substitute for mandatory TCB policy.
        with self.assertRaisesRegex(AttestationRefused, "trust policy"):
            verify_evidence(self.evidence(), NONCE, HOST, self.roots, MEASUREMENT)

    def test_missing_or_empty_approval_is_refused(self):
        for policy in (None, {}, {"version": 1, "measurements": [], "minimum_tcb": {}}):
            with self.subTest(policy=policy), self.assertRaises(AttestationRefused):
                verify_evidence(self.evidence(), NONCE, HOST, self.roots, trust_policy=policy)

    def test_tcb_floor_is_componentwise_and_product_specific(self):
        for component in TCB:
            trust = copy.deepcopy(TRUST)
            trust["minimum_tcb"]["Genoa"][component] = TCB[component] + 1
            with self.subTest(component=component):
                self.refused(self.evidence(), "below", trust=trust)
        # Larger low-order and other components must not compensate for an old microcode.
        trust = copy.deepcopy(TRUST)
        trust["minimum_tcb"]["Genoa"] = {"bootloader": 0, "tee": 0, "snp": 0, "microcode": 87}
        self.refused(self.evidence(), "below", trust=trust)
        trust["minimum_tcb"] = {"Milan": dict(TCB)}
        self.refused(self.evidence(), "minimum TCB for this product", trust=trust)

    def test_report_tcb_must_match_amd_certified_key_versions(self):
        self.refused(self.evidence(tcb={**TCB, "microcode": 255}), "AMD-certified VCEK")
        for bad_tcb in (None, {k: v for k, v in TCB.items() if k != "snp"}):
            roots, vcek, key = chain(tcb=bad_tcb)
            self.refused(self.evidence(vcek=vcek, key=key), "TCB extensions", roots=roots)

    def test_high_svn_bytes_are_decoded_as_unsigned_der_integers(self):
        high = {name: 255 for name in TCB}
        roots, vcek, key = chain(tcb=high)
        result = verify_evidence(self.evidence(vcek=vcek, key=key, tcb=high), NONCE, HOST, roots, trust_policy=TRUST)
        self.assertEqual(result["reported_tcb_components"], high)

    def test_unsupported_product_has_no_implicit_approval(self):
        roots = {"Turin": self.roots["Genoa"]}
        self.refused(self.evidence(), "Unsupported product", roots=roots)

    def test_only_the_inspected_report_version_is_supported(self):
        for version in (0, 1, 2, 3, 4, 6, 0xffffffff):
            # Still signed with the synthetic VCEK: do not mistake signature validity
            # for approval of a report layout we have not implemented.
            with self.subTest(version=version):
                self.refused(self.evidence(version=version), "Unsupported report version")

    def test_expired_certificates_are_refused(self):
        roots, vcek, key = chain(expired=True)
        self.refused(self.evidence(vcek=vcek, key=key), "AMD root", roots=roots)

    def test_channel_key_and_evidence_types_are_bounded_and_validated(self):
        for runtime in (b"[]", b'"text"', b'{"nonce":"x","nonce":"y","public_key":"x"}',
                        json.dumps({"nonce": NONCE, "public_key": "AAAA"}).encode(),
                        json.dumps({"nonce": NONCE, "public_key": base64.b64encode(bytes(32)).decode()}).encode()):
            with self.subTest(runtime=runtime), self.assertRaises(AttestationRefused):
                verify_evidence(self.evidence(runtime=runtime), NONCE, HOST, self.roots, trust_policy=TRUST)
        valid = self.evidence()
        for bad in ([], None, {**valid, "certs": "x" * 65537}, {**valid, "runtime_data": "x" * 8193},
                    {**valid, "certs": base64.b64encode(b'[]').decode()}):
            with self.subTest(kind=type(bad)), self.assertRaises(AttestationRefused):
                verify_evidence(bad, NONCE, HOST, self.roots, trust_policy=TRUST)


class TrustPolicyTests(unittest.TestCase):
    def test_policy_file_is_local_strict_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps(TRUST))
            self.assertEqual(load_trust_policy(path), TRUST)
            for raw in ('{"version":1,"version":1,"measurements":[],"minimum_tcb":{}}', 'x' * 16385, 'null', 'not json'):
                path.write_text(raw)
                with self.subTest(raw=raw[:20]), self.assertRaises(AttestationRefused):
                    load_trust_policy(path)
            with self.assertRaises(AttestationRefused):
                load_trust_policy(path.with_name("missing"))

    def test_policy_fields_cannot_disable_guest_or_tcb_approval(self):
        bad = [None, {}, {**TRUST, "version": True}, {**TRUST, "extra": 1},
               {**TRUST, "measurements": ["0" * 96]}, {**TRUST, "measurements": [MEASUREMENT, MEASUREMENT]},
               {**TRUST, "minimum_tcb": {}}, {**TRUST, "minimum_tcb": {"Turin": TCB}},
               {**TRUST, "minimum_tcb": {"Genoa": {name: 0 for name in TCB}}}]
        for invalid in (True, -1, 256, 1.0, "5"):
            bad.append({**TRUST, "minimum_tcb": {"Genoa": {**TCB, "snp": invalid}}})
        for value in bad:
            with self.subTest(value=value), self.assertRaises(AttestationRefused):
                validate_trust_policy(value)

    def test_checked_in_template_cannot_enable_release(self):
        path = Path(__file__).resolve().parents[1] / "protected_worker/deploy/trust_policy.json"
        with self.assertRaisesRegex(AttestationRefused, "independently approved"):
            load_trust_policy(path)

    def test_amd_chain_download_is_bounded_without_network(self):
        with self.assertRaisesRegex(AttestationRefused, "limit"):
            amd_roots(fetch=lambda _: b"x" * 65537)
        with self.assertRaisesRegex(AttestationRefused, "Malformed"):
            amd_roots(fetch=lambda _: b"invalid")


class SealingTests(unittest.TestCase):
    def test_worker_round_trip_and_replay_refused(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "protected_worker"))
        import server
        server.process = lambda plaintext: b"out:" + plaintext
        nonce = os.urandom(32).hex()
        body, shared = seal(server._public, nonce, b"private")
        self.assertNotIn(b"private", base64.b64decode(body["ciphertext"]))
        self.assertEqual(unseal(shared, nonce, server.run_task(body)), b"out:private")
        with self.assertRaisesRegex(ValueError, "already used"):
            server.run_task(body)


if __name__ == "__main__":
    unittest.main()
