"""Requester-side appraisal of AMD SEV-SNP evidence, plus sealing to the attested key.

Trust is rooted in AMD's root certificate fetched from AMD, not in anything the worker or
its host supplies. A pass requires a requester-approved VM measurement and product-specific
TCB floor, the expected policy hash, and a fresh nonce bound to the channel key. It does
not establish workload correctness. Requires `cryptography`, so the public demo stays
dependency-free. The committed trust policy deliberately approves no production values.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.request import urlopen

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

REPORT_SIZE = 1184
SIGNED_SIZE = 0x2A0
REPORT_VERSION = 5  # AMD56860 rev1.58 Table23, whose layout is implemented below.
DEBUG_POLICY_BIT = 1 << 19
AMD_KDS = "https://kdsintf.amd.com/vcek/v1/{}/cert_chain"
PRODUCTS = ("Milan", "Genoa")  # Turin has a different TCB layout and is not supported here.
MAX_POLICY_BYTES = 16 * 1024
MAX_CERT_CHAIN_BYTES = 64 * 1024
TCB_COMPONENTS = {"bootloader": 0, "tee": 1, "snp": 6, "microcode": 7}
TCB_OIDS = {name: x509.ObjectIdentifier("1.3.6.1.4.1.3704.1.3." + suffix)
            for name, suffix in {"bootloader": "1", "tee": "2", "snp": "3", "microcode": "8"}.items()}


class AttestationRefused(Exception):
    """Evidence did not satisfy policy. Callers must not release data."""


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    def invalid_constant(_):
        raise ValueError("Nonfinite JSON")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)


def validate_trust_policy(policy):
    """Validate requester-owned approval, never values learned from a worker endpoint.

    version=1; measurements is a nonempty list of SHA-384 hex measurements;
    minimum_tcb maps Milan/Genoa to integer bootloader, tee, snp and microcode SVNs.
    TCB components follow AMD ABI 56860 rev1.58 Table4 (checked 2026-11-02).
    Empty deployment templates deliberately refuse private release.
    """
    if type(policy) is not dict or set(policy) != {"version", "measurements", "minimum_tcb"} or type(policy["version"]) is not int or policy["version"] != 1:
        raise AttestationRefused("Missing or invalid independently approved attestation trust policy")
    measurements = policy["measurements"]
    if type(measurements) is not list or not 1 <= len(measurements) <= 32 or any(
            not isinstance(m, str) or not re.fullmatch(r"[0-9a-f]{96}", m) or m == "0" * 96 for m in measurements):
        raise AttestationRefused("An independently approved utility VM measurement is required")
    if len(set(measurements)) != len(measurements):
        raise AttestationRefused("Duplicate approved utility VM measurement")
    minimum = policy["minimum_tcb"]
    if type(minimum) is not dict or not minimum or not set(minimum) <= set(PRODUCTS):
        raise AttestationRefused("An independently approved minimum TCB for a supported product is required")
    for components in minimum.values():
        if type(components) is not dict or set(components) != set(TCB_COMPONENTS) or any(type(v) is not int or not 0 <= v <= 255 for v in components.values()) or not any(components.values()):
            raise AttestationRefused("Minimum TCB needs bootloader, tee, snp and microcode byte values, with a nonzero floor")
    return {"version": 1, "measurements": list(measurements), "minimum_tcb": {p: dict(c) for p, c in minimum.items()}}


def load_trust_policy(path):
    """Read a bounded local policy. Missing, ambiguous or empty approval fails closed."""
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_POLICY_BYTES + 1)
        if len(raw) > MAX_POLICY_BYTES:
            raise ValueError("Oversized policy")
        return validate_trust_policy(_json(raw))
    except (OSError, ValueError, TypeError, RecursionError):
        raise AttestationRefused("Could not read a valid independently approved attestation trust policy") from None


def parse_report(raw):
    if len(raw) != REPORT_SIZE:
        raise AttestationRefused(f"Report is {len(raw)} bytes, expected {REPORT_SIZE}")
    return {
        "version": int.from_bytes(raw[0:4], "little"), "policy": int.from_bytes(raw[8:16], "little"),
        "signature_algo": int.from_bytes(raw[0x34:0x38], "little"), "report_data": raw[0x50:0x90],
        "measurement": raw[0x90:0xC0], "host_data": raw[0xC0:0xE0], "chip_id": raw[0x1A0:0x1E0],
        "reported_tcb": raw[0x180:0x188], "r": int.from_bytes(raw[0x2A0:0x2A0 + 72], "little"),
        "s": int.from_bytes(raw[0x2E8:0x2E8 + 72], "little"), "signed": raw[:SIGNED_SIZE],
    }


def amd_roots(fetch=None):
    """AMD's published ASK+ARK chains by product, fetched directly from AMD."""
    def bounded_fetch(url):
        with urlopen(url, timeout=30) as response:
            return response.read(MAX_CERT_CHAIN_BYTES + 1)
    fetch = fetch or bounded_fetch
    roots = {}
    for product in PRODUCTS:
        raw = fetch(AMD_KDS.format(product))
        if not isinstance(raw, bytes) or len(raw) > MAX_CERT_CHAIN_BYTES:
            raise AttestationRefused("AMD endorsement chain exceeds its limit")
        try:
            chain = x509.load_pem_x509_certificates(raw)
        except ValueError:
            raise AttestationRefused("Malformed AMD endorsement chain") from None
        if len(chain) != 2:
            raise AttestationRefused("Unexpected AMD endorsement chain")
        roots[product] = chain
    return roots


def _verify_pss(issuer, cert):
    if not isinstance(issuer.public_key(), rsa.RSAPublicKey):
        raise AttestationRefused("AMD issuer is not an RSA key")
    issuer.public_key().verify(cert.signature, cert.tbs_certificate_bytes, padding.PSS(padding.MGF1(hashes.SHA384()), padding.PSS.DIGEST_LENGTH), hashes.SHA384())


def verify_chain(vcek, roots):
    """VCEK must chain through an AMD-published ASK to its self-signed ARK."""
    now = datetime.now(timezone.utc)
    if type(roots) is not dict or not roots:
        raise AttestationRefused("AMD roots are unavailable")
    for product, chain in roots.items():
        if not isinstance(chain, (list, tuple)) or len(chain) != 2 or not all(isinstance(c, x509.Certificate) for c in chain):
            raise AttestationRefused("Malformed AMD endorsement chain")
        ask, ark = chain
        try:
            if any(not c.not_valid_before_utc <= now <= c.not_valid_after_utc for c in (ark, ask, vcek)):
                continue
            if ark.issuer != ark.subject or ask.issuer != ark.subject or vcek.issuer != ask.subject:
                continue
            _verify_pss(ark, ark)
            _verify_pss(ark, ask)
            _verify_pss(ask, vcek)
            return product
        except (InvalidSignature, ValueError, TypeError):
            continue
    raise AttestationRefused("VCEK does not chain to an AMD root")


def _reported_tcb(report, product):
    # AMD ABI56860 Table4: Milan/Genoa bytes0,1,6,7; bytes2..5 reserved.
    if product not in PRODUCTS or any(report["reported_tcb"][2:6]):
        raise AttestationRefused("Unsupported product or reported TCB layout")
    return {name: report["reported_tcb"][offset] for name, offset in TCB_COMPONENTS.items()}


def _certificate_tcb(vcek):
    """Bind the floor to the AMD-certified VCEK too, not only a report field.

    AMD KDS spec57230 rev1.00 Table10 (checked 2026-11-02) defines DER INTEGER
    SPL extensions. A byte >=128 has a zero sign octet; other encodings refuse.
    """
    result = {}
    try:
        for name, oid in TCB_OIDS.items():
            raw = vcek.extensions.get_extension_for_oid(oid).value.value
            if len(raw) == 3 and raw[:2] == b"\x02\x01" and raw[2] < 128:
                result[name] = raw[2]
            elif len(raw) == 4 and raw[:3] == b"\x02\x02\x00" and raw[3] >= 128:
                result[name] = raw[3]
            else:
                raise ValueError("Invalid SPL")
    except (x509.ExtensionNotFound, AttributeError, ValueError, TypeError):
        raise AttestationRefused("VCEK has missing or invalid TCB extensions") from None
    return result


def verify_evidence(evidence, nonce, expected_host_data, roots, expected_measurement=None, *, trust_policy=None):
    """Return a key bound to evidence satisfying requester-approved guest/TCB policy.

    expected_measurement is an optional additional legacy pin; it never substitutes
    for the required independently approved trust_policy.
    """
    policy = validate_trust_policy(trust_policy)
    if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{64}", nonce):
        raise AttestationRefused("Invalid requester nonce")
    if not isinstance(expected_host_data, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_host_data):
        raise AttestationRefused("Invalid approved container policy hash")
    if expected_measurement is not None and (not isinstance(expected_measurement, str) or not re.fullmatch(r"[0-9a-f]{96}", expected_measurement)):
        raise AttestationRefused("Invalid additional utility VM measurement pin")
    try:
        if type(evidence) is not dict or not isinstance(evidence.get("report"), str) or not re.fullmatch(r"[0-9a-fA-F]{2368}", evidence["report"]):
            raise ValueError("Report encoding")
        if not isinstance(evidence.get("runtime_data"), str) or len(evidence["runtime_data"]) > 8192 or not isinstance(evidence.get("certs"), str) or len(evidence["certs"]) > MAX_CERT_CHAIN_BYTES:
            raise ValueError("Evidence size")
        raw = bytes.fromhex(evidence["report"])
        runtime_data = base64.b64decode(evidence["runtime_data"], validate=True)
        claimed = _json(runtime_data)
        certificates = _json(base64.b64decode(evidence["certs"], validate=True))
        if type(claimed) is not dict or set(claimed) != {"nonce", "public_key"} or type(certificates) is not dict or not isinstance(certificates.get("vcekCert"), str) or len(certificates["vcekCert"]) > 16384:
            raise ValueError("Evidence fields")
        if not isinstance(claimed["public_key"], str) or len(claimed["public_key"]) != 44:
            raise ValueError("Channel key")
        key_bytes = base64.b64decode(claimed["public_key"], validate=True)
        # Reject malformed and small-order keys before any private input is sealed.
        X25519PrivateKey.generate().exchange(X25519PublicKey.from_public_bytes(key_bytes))
        vcek = x509.load_pem_x509_certificate(certificates["vcekCert"].encode())
    except (KeyError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise AttestationRefused(f"Malformed evidence: {type(exc).__name__}") from None
    report = parse_report(raw)
    if report["version"] != REPORT_VERSION:
        raise AttestationRefused("Unsupported report version")
    product = verify_chain(vcek, roots)
    if report["signature_algo"] != 1:
        raise AttestationRefused("Unexpected report signature algorithm")
    public = vcek.public_key()
    if not isinstance(public, ec.EllipticCurvePublicKey) or not isinstance(public.curve, ec.SECP384R1):
        raise AttestationRefused("VCEK is not an EC P-384 key")
    try:
        public.verify(utils.encode_dss_signature(report["r"], report["s"]), report["signed"], ec.ECDSA(hashes.SHA384()))
    except (InvalidSignature, ValueError):
        raise AttestationRefused("Report signature is invalid") from None
    if report["policy"] & DEBUG_POLICY_BIT:
        raise AttestationRefused("Guest policy allows debugging")
    if report["host_data"].hex() != expected_host_data:
        raise AttestationRefused("Container policy hash does not match the approved policy")
    if report["measurement"].hex() not in policy["measurements"]:
        raise AttestationRefused("Utility VM measurement is not independently approved")
    if expected_measurement is not None and report["measurement"].hex() != expected_measurement:
        raise AttestationRefused("Utility VM measurement does not match the pinned value")
    components = _reported_tcb(report, product)
    minimum = policy["minimum_tcb"].get(product)
    if minimum is None:
        raise AttestationRefused("No independently approved minimum TCB for this product")
    if _certificate_tcb(vcek) != components:
        raise AttestationRefused("Reported TCB does not match the AMD-certified VCEK")
    if any(components[name] < minimum[name] for name in TCB_COMPONENTS):
        raise AttestationRefused("Reported TCB is below the independently approved minimum")
    if report["report_data"][:32] != hashlib.sha256(runtime_data).digest() or any(report["report_data"][32:]):
        raise AttestationRefused("Report is not bound to this runtime data")
    if claimed.get("nonce") != nonce:
        raise AttestationRefused("Report is not fresh: nonce mismatch")
    return {"public_key": claimed["public_key"], "product": product, "host_data": report["host_data"].hex(), "measurement": report["measurement"].hex(), "reported_tcb": report["reported_tcb"].hex(), "reported_tcb_components": components}


def _derive(shared, nonce, direction):
    return HKDF(hashes.SHA256(), 32, None, f"aa-task-v1|{nonce}|{direction}".encode()).derive(shared)


def seal(worker_public_b64, nonce, plaintext):
    """Encrypt to the attested key. Returns (request body, shared secret for the reply)."""
    client = X25519PrivateKey.generate()
    shared = client.exchange(X25519PublicKey.from_public_bytes(base64.b64decode(worker_public_b64)))
    iv = os.urandom(12)
    return {
        "nonce": nonce, "iv": base64.b64encode(iv).decode(),
        "client_public": base64.b64encode(client.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode(),
        "ciphertext": base64.b64encode(AESGCM(_derive(shared, nonce, "request")).encrypt(iv, plaintext, nonce.encode())).decode(),
    }, shared


def unseal(shared, nonce, body):
    return AESGCM(_derive(shared, nonce, "response")).decrypt(base64.b64decode(body["iv"]), base64.b64decode(body["ciphertext"]), nonce.encode())
