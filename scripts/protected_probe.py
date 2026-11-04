"""Go/no-go: verify a live confidential worker, release sealed data, then try to fool the verifier."""
import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_marketplace.protocol import json_request
from agent_marketplace.snp import AttestationRefused, amd_roots, load_trust_policy, seal, unseal, verify_evidence

PRIVATE = b'{"rows":[{"merchant":"SYNTHETIC-PRIVATE-MERCHANT"}]}'
p = argparse.ArgumentParser()
p.add_argument("--url", required=True)
p.add_argument("--host-data", default=(Path(__file__).resolve().parents[1] / "protected_worker/deploy/expected_host_data.txt"))
p.add_argument("--trust-policy", type=Path, default=Path(__file__).resolve().parents[1] / "protected_worker/deploy/trust_policy.json", help="Requester-approved VM measurements and product-specific TCB floors; the empty template refuses")
args = p.parse_args()
try:
    trust_policy = load_trust_policy(args.trust_policy)
except AttestationRefused as exc:
    p.error(str(exc))
expected = Path(args.host_data).read_text().strip()
roots = amd_roots()
nonce = secrets.token_hex(32)
evidence = json_request("GET", f"{args.url}/attest?nonce={nonce}", timeout=60)
print("evidence fields:", {k: (len(v) if v else v) for k, v in evidence.items()})
result = verify_evidence(evidence, nonce, expected, roots, trust_policy=trust_policy)
print("VERIFIED:", json.dumps(result, indent=1))
body, shared = seal(result["public_key"], nonce, PRIVATE)
reply = unseal(shared, nonce, json_request("POST", args.url + "/task", body, timeout=120))
print("worker decrypted inside boundary:", reply.decode())
assert b"SYNTHETIC" not in base64.b64decode(body["ciphertext"]), "plaintext on the wire"


def refused(name, *call):
    try:
        verify_evidence(*call, trust_policy=trust_policy)
        print("FAIL, accepted:", name)
    except AttestationRefused as exc:
        print(f"refused [{name}]: {exc}")


refused("wrong approved policy", evidence, nonce, "0" * 64, roots)
refused("stale nonce", evidence, secrets.token_hex(32), expected, roots)
raw = bytearray(bytes.fromhex(evidence["report"]))
raw[0xC0] ^= 1
refused("host-edited report", {**evidence, "report": raw.hex()}, nonce, expected, roots)
swapped = base64.b64encode(json.dumps({"nonce": nonce, "public_key": base64.b64encode(os.urandom(32)).decode()}).encode()).decode()
refused("host-swapped key", {**evidence, "runtime_data": swapped}, nonce, expected, roots)
