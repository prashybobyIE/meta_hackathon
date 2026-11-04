#!/usr/bin/env python3
"""Run native read-only networking against a temporary real marketplace.

Invoke from the repository root: python3 native/NodeProbe/smoke_market.py
--binary /path/to/node-probe. No external network, credentials or cloud resources.
"""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agent_marketplace.market import MarketStore, create_server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=Path(__file__).parent/".build/debug/node-probe")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="native-market-smoke-") as directory:
        store = MarketStore(Path(directory)/"market.sqlite", secrets.token_urlsafe(32), secrets.token_urlsafe(32))
        server = create_server(store, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = subprocess.run([str(args.binary.resolve()), "--market", f"http://127.0.0.1:{server.server_port}"],
                capture_output=True, text=True, timeout=12, check=False)
            if result.returncode:
                raise RuntimeError("Native client did not complete the loopback smoke test")
            value = json.loads(result.stdout)
            assert value["network"]["healthHTTPReachable"] is True
            assert value["network"]["snapshot"]["workerCount"] == 0
            assert value["host"]["confidentialEligibility"] is False
            print("Native client reached real marketplace /health and /v1/snapshot; confidentiality remains unestablished.")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            if hasattr(store, "close"):
                store.close()


if __name__ == "__main__":
    main()
