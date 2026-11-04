"""Attested worker that runs inside an Azure confidential container group.

It creates an X25519 key in memory, binds that key and the requester's nonce into a
hardware SNP report through the SKR sidecar, and processes only data sealed to that key.
It never logs plaintext. It proves nothing by itself: the requester's verifier decides.
"""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import threading
from urllib.request import ProxyHandler, Request, build_opener

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from task import process

SKR = os.environ.get("SKR_URL", "http://127.0.0.1:9000")
MAX_BODY = 256 * 1024
_key = X25519PrivateKey.generate()
_public = base64.b64encode(_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
_used = set()
_lock = threading.Lock()


def derive(shared, nonce, direction):
    return HKDF(hashes.SHA256(), 32, None, f"aa-task-v1|{nonce}|{direction}".encode()).derive(shared)


def platform_certs():
    directory = os.environ.get("UVM_SECURITY_CONTEXT_DIR", "")
    path = os.path.join(directory, "host-amd-cert-base64")
    if not directory or not os.path.exists(path):
        return None
    return open(path).read().strip()


def attest(nonce):
    runtime_data = json.dumps({"nonce": nonce, "public_key": _public}, sort_keys=True, separators=(",", ":")).encode()
    request = Request(SKR + "/attest/raw", json.dumps({"runtime_data": base64.b64encode(runtime_data).decode()}).encode(), {"Content-Type": "application/json"})
    with build_opener(ProxyHandler({})).open(request, timeout=30) as response:
        report = json.load(response)["report"]
    return {"report": report, "runtime_data": base64.b64encode(runtime_data).decode(), "certs": platform_certs()}


def run_task(body):
    nonce = body["nonce"]
    if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{64}", nonce):
        raise ValueError("Invalid nonce")
    with _lock:
        if nonce in _used:
            raise ValueError("Nonce already used")
        _used.add(nonce)
    shared = _key.exchange(X25519PublicKey.from_public_bytes(base64.b64decode(body["client_public"])))
    plaintext = AESGCM(derive(shared, nonce, "request")).decrypt(base64.b64decode(body["iv"]), base64.b64decode(body["ciphertext"]), nonce.encode())
    output = process(plaintext)
    iv = os.urandom(12)
    return {"iv": base64.b64encode(iv).decode(), "ciphertext": base64.b64encode(AESGCM(derive(shared, nonce, "response")).encrypt(iv, output, nonce.encode())).decode()}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, code, value):
        raw = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        match = re.fullmatch(r"/attest\?nonce=([0-9a-f]{64})", self.path)
        if self.path == "/health":
            return self.send(200, {"ok": True})
        if not match:
            return self.send(404, {"error": "Not found"})
        try:
            self.send(200, attest(match.group(1)))
        except Exception as exc:
            self.send(502, {"error": "Attestation unavailable: " + type(exc).__name__})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if self.path != "/task" or not 0 < length <= MAX_BODY:
            return self.send(404, {"error": "Not found"})
        try:
            self.send(200, run_task(json.loads(self.rfile.read(length))))
        except Exception as exc:
            # Exception type only: messages could echo decrypted data.
            self.send(400, {"error": "Task refused: " + type(exc).__name__})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8443), Handler).serve_forever()
