"""Requester flow for one private task on an attested confidential worker.

Data is released only after verify_evidence passes. Rows here are synthetic. The answer key
stays on the requester, so acceptance is independent of the worker. Accuracy on these rows
says nothing about other inputs, and attestation says nothing about output quality.
"""
from __future__ import annotations

import base64
import json
import math
import re
import secrets
import time

from .protocol import ApiError, json_request
from .snp import AttestationRefused, amd_roots, seal, unseal, validate_trust_policy, verify_evidence

PRIVATE_ROWS = [
    ("Blue Tokai Coffee Roasters", "food"), ("Saravana Bhavan Restaurant", "food"), ("Domino's Pizza", "food"),
    ("Namma Metro Card Recharge", "transport"), ("Ola Cabs Ride", "transport"), ("IndiGo Airlines Ticket", "transport"),
    ("Coursera Course Fee", "education"), ("Allen Coaching Tuition", "education"),
    ("Apollo Pharmacy", "health"), ("Dr Lal PathLabs Blood Test", "health"),
    ("Third Wave Coffee", "food"), ("Rapido Bike Taxi", "transport"), ("Udemy Python Course", "education"), ("Practo Doctor Consultation", "health"),
    ("Haldiram Sweets", "food"), ("IRCTC Train Booking", "transport"), ("BYJU'S Learning App", "education"), ("MedPlus Medicines", "health"),
]
MIN_ACCURACY = 0.7


def _record():
    return {"verified": False, "release_state": "not_released", "delivery_state": "not_submitted", "accepted": False}


def _failure(record, stage, exc):
    # Neither transport errors nor malformed decrypted replies may echo private data.
    if isinstance(exc, AttestationRefused) and record["release_state"] == "not_released":
        record["reason"] = str(exc)[:300]
    else:
        record["reason"] = {
            "policy": "Protected trust policy is unavailable or invalid",
            "roots": "AMD attestation endorsement service unavailable",
            "attestation": "Protected worker attestation unavailable or malformed",
            "sealing": "Private task could not be prepared for protected submission",
            "submission": "Protected task was submitted; delivery or execution is unknown",
            "reply": "Protected worker returned an invalid task response",
        }[stage]
        if isinstance(exc, ApiError):
            record["transport_status"] = exc.status
    return record


def _valid_rows(rows):
    return isinstance(rows, (list, tuple)) and 0 < len(rows) <= 25 and all(
        isinstance(row, (list, tuple)) and len(row) == 2
        and isinstance(row[0], str) and 0 < len(row[0]) <= 200
        and row[1] in ("food", "transport", "education", "health", "other") for row in rows
    )


def _valid_answer(answer, count):
    return isinstance(answer, dict) and isinstance(answer.get("categories"), list) and len(answer["categories"]) == count and all(
        isinstance(value, str) and value in ("food", "transport", "education", "health", "other") for value in answer["categories"]
    ) and isinstance(answer.get("model"), str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", answer["model"]) and type(answer.get("elapsed_ms")) in (int, float) and math.isfinite(answer["elapsed_ms"]) and 0 <= answer["elapsed_ms"] <= 3600000


def run_private_task(url, approved_host_data, rows=PRIVATE_ROWS, roots=None, *, trust_policy=None):
    """Return bounded metadata, retaining release uncertainty after submission starts.

    `submitted` is conservative: the encrypted POST may have reached the worker even if
    its response is lost. `response_received` requires authenticated decryption; neither
    successful attestation nor an HTTP response is evidence of task acceptance.
    """
    record, stage = _record(), "policy"
    try:
        trust_policy = validate_trust_policy(trust_policy)
        stage = "roots"
        roots = amd_roots() if roots is None else roots
        stage = "attestation"
        nonce = secrets.token_hex(32)
        started = time.monotonic()
        evidence = json_request("GET", f"{url}/attest?nonce={nonce}", timeout=60)
        verified = verify_evidence(evidence, nonce, approved_host_data, roots, trust_policy=trust_policy)
        record.update({key: verified[key] for key in ("product", "host_data", "measurement", "reported_tcb")})
        record.update(verified=True, attestation_ms=round((time.monotonic() - started) * 1000))
        stage = "sealing"
        if not _valid_rows(rows):
            raise ValueError("Invalid private task input")
        body, shared = seal(verified["public_key"], nonce, json.dumps({"rows": [{"merchant": m} for m, _ in rows]}).encode())
        wire = base64.b64decode(body["ciphertext"], validate=True)
        if any(m.encode() in wire for m, _ in rows):
            raise ValueError("Plaintext found in sealed payload")
        record.update(rows=len(rows), ciphertext_bytes=len(wire))
        stage = "submission"
        record.update(release_state="submitted", delivery_state="unknown")
        reply = json_request("POST", url + "/task", body, timeout=600)
        plaintext = unseal(shared, nonce, reply)
        record["delivery_state"] = "response_received"
        stage = "reply"
        answer = json.loads(plaintext)
        if not _valid_answer(answer, len(rows)):
            raise ValueError("Invalid protected response")
        correct = sum(got == want for got, (_, want) in zip(answer["categories"], rows))
        record.update(model=answer["model"], inference_ms=answer["elapsed_ms"], correct=correct, accepted=correct / len(rows) >= MIN_ACCURACY)
        if not record["accepted"]:
            record["reason"] = "Protected task did not pass independent acceptance"
        return record
    except Exception as exc:
        return _failure(record, stage, exc)


def attested_step(url, approved_host_data, *, trust_policy=None):
    """Appraise, submit and check independently; preserve the stage reached on failure."""
    record, stage = _record(), "policy"
    try:
        trust_policy = validate_trust_policy(trust_policy)
        stage = "roots"
        roots = amd_roots()
    except Exception as exc:
        return _failure(record, stage, exc)
    record = run_private_task(url, approved_host_data, roots=roots, trust_policy=trust_policy)
    if record["delivery_state"] != "response_received":
        return record
    # An optional diagnostic must not erase an already submitted or accepted task.
    try:
        record["refusal_check"] = demonstrate_refusal(url, roots, trust_policy=trust_policy, approved_host_data=approved_host_data)
        record["refusal_check_status"] = "refused"
    except RuntimeError:
        record.update(refusal_check="Unapproved policy check did not refuse the evidence", refusal_check_status="failed")
    except Exception:
        record.update(refusal_check="Unapproved policy check unavailable", refusal_check_status="unavailable")
    return record


def demonstrate_refusal(url, roots, *, trust_policy=None, approved_host_data=None):
    """Verify fresh valid evidence before checking the same report with a wrong hash."""
    trust_policy = validate_trust_policy(trust_policy)
    if not isinstance(approved_host_data, str) or not re.fullmatch(r"[0-9a-f]{64}", approved_host_data):
        raise AttestationRefused("Approved policy hash is required for the refusal check")
    nonce = secrets.token_hex(32)
    evidence = json_request("GET", f"{url}/attest?nonce={nonce}", timeout=60)
    verify_evidence(evidence, nonce, approved_host_data, roots, trust_policy=trust_policy)
    wrong_hash = ("0" if approved_host_data != "0" * 64 else "1") * 64
    try:
        verify_evidence(evidence, nonce, wrong_hash, roots, trust_policy=trust_policy)
    except AttestationRefused as exc:
        if str(exc) != "Container policy hash does not match the approved policy":
            raise
        return str(exc)
    raise RuntimeError("Verifier accepted a wrong policy")
