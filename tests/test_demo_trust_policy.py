"""The CLI must require a local trust decision before starting a protected run."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_marketplace.demo import main

try:
    from agent_marketplace.snp import load_trust_policy
except ImportError:
    raise unittest.SkipTest("Needs cryptography: run with .venv/bin/python")


class ProtectedConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.host_file = self.root / "host.txt"
        self.host_file.write_text("ab" * 32)
        self.trust_file = self.root / "trust.json"
        # Synthetic local test policy. These are not production trust values.
        self.policy = {"version": 1, "measurements": ["cd" * 48], "minimum_tcb": {
            "Genoa": {"bootloader": 3, "tee": 0, "snp": 20, "microcode": 85}}}
        self.trust_file.write_text(json.dumps(self.policy))

    def invoke(self, extra=()):
        argv = ["demo", "--protected-url", "http://127.0.0.1:1",
                "--protected-policy", str(self.host_file),
                "--protected-trust-policy", str(self.trust_file), *extra]
        with patch("sys.argv", argv), patch("agent_marketplace.demo.load_env_file"), contextlib.redirect_stderr(io.StringIO()) as errors:
            try:
                main()
            except SystemExit as exc:
                return exc.code, errors.getvalue()
        return 0, errors.getvalue()

    def test_local_approved_policy_reaches_protected_flow(self):
        with patch("agent_marketplace.demo.run_demo") as run, patch("agent_marketplace.protected.attested_step", return_value={"accepted": False}) as protected:
            self.assertEqual(self.invoke()[0], 0)
            run.assert_called_once()
            run.call_args.kwargs["protected_runner"]()
            protected.assert_called_once_with("http://127.0.0.1:1", "ab" * 32, trust_policy=load_trust_policy(self.trust_file))

    def test_empty_or_malformed_trust_policy_refuses_before_starting_services(self):
        for policy in ({"version": 1, "measurements": [], "minimum_tcb": {}}, {"version": 1}, "not-json"):
            with self.subTest(policy=policy), patch("agent_marketplace.demo.run_demo") as run:
                self.trust_file.write_text(json.dumps(policy) if not isinstance(policy, str) else policy)
                code, error = self.invoke()
                self.assertEqual(code, 2)
                self.assertIn("configuration refused", error)
                run.assert_not_called()

    def test_missing_trust_file_refuses_before_starting_services(self):
        self.trust_file.unlink()
        with patch("agent_marketplace.demo.run_demo") as run:
            self.assertEqual(self.invoke()[0], 2)
            run.assert_not_called()

    def test_invalid_container_policy_hash_refuses_before_starting_services(self):
        for value in ("", "0" * 64, "g" * 64, "ab " * 32):
            with self.subTest(value=value), patch("agent_marketplace.demo.run_demo") as run:
                self.host_file.write_text(value)
                self.assertEqual(self.invoke()[0], 2)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
