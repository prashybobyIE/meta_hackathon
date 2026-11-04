import unittest

from agent_marketplace.protocol import sha256_text
from agent_marketplace.providers import FixtureProvider
from agent_marketplace.reference import CONTRACTS
from agent_marketplace.restricted import RestrictedError, RestrictedModule
from agent_marketplace.security import assess_execution
from agent_marketplace.verification import verify_artifact


class VerificationTests(unittest.TestCase):
    def artifact(self, kind, source=None):
        contract = CONTRACTS[kind]
        result = FixtureProvider().generate_module(contract, {"allow_meta": False})
        if source is not None:
            result["content"] = source
        return {**result, "filename": contract["filename"], "sha256": sha256_text(result["content"])}

    def test_independent_contracts_accept_reference_sources(self):
        for kind in CONTRACTS:
            with self.subTest(kind=kind):
                report = verify_artifact(kind, self.artifact(kind))
                self.assertTrue(report["passed"], report)

    def test_host_interfaces_and_reflection_are_not_exposed(self):
        for source in ["import os\ndef f(x):\n return os.listdir('/')", "def f(x):\n return open('/etc/passwd').read()", "def f(x):\n return x.__class__", "def f(x):\n return eval(x)"]:
            with self.subTest(source=source), self.assertRaises(RestrictedError):
                RestrictedModule(source, "f").run("public")

    def test_execution_limits_cover_loops_expansion_and_cycles(self):
        for source in ["def f(x):\n while True:\n  pass", "def f(x):\n return 'x' * 1000000000", "def f(x):\n a = []\n a.append(a)\n return a", "def f(x):\n return ('x' * 60000).join(['x'] * 1000)", "def f(x):\n return ['x' * 10000] * 1000", "def f(x):\n return sorted([x], key=str)"]:
            with self.subTest(source=source), self.assertRaises(RestrictedError):
                RestrictedModule(source, "f", max_steps=1000).run(None)

    def test_invalid_loop_control_and_decimal_growth_reject_cleanly(self):
        for source in ["def f(x):\n break", "def f(x):\n continue", "from decimal import Decimal\ndef f(x):\n d=Decimal('1000000000000')\n for i in range(10):\n  d=d*d\n return int(d)"]:
            with self.subTest(source=source), self.assertRaises(RestrictedError):
                RestrictedModule(source, "f").run(None)

    def test_invalid_unicode_artifact_rejects_without_verifier_crash(self):
        artifact = {"filename": "summary.py", "sha256": "0"*64, "content": "\ud800"}
        self.assertFalse(verify_artifact("summary", artifact)["passed"])

    def test_csv_column_expansion_is_bounded_inside_primitive(self):
        source = "import csv\nimport io\ndef f(x):\n return list(csv.DictReader(io.StringIO(x)))"
        with self.assertRaises(RestrictedError):
            RestrictedModule(source, "f").run(",".join(str(i) for i in range(200)) + "\n")

    def test_digest_mismatch_and_incorrect_module_rejected(self):
        artifact = self.artifact("summary")
        artifact["sha256"] = "0" * 64
        self.assertFalse(verify_artifact("summary", artifact)["passed"])
        self.assertFalse(verify_artifact("summary", self.artifact("summary", "def summarize_expenses(rows):\n return {'total_cents':0,'by_category':{},'count':0}\n"))["passed"])

    def test_input_mutation_is_detected(self):
        original = self.artifact("categories")["content"]
        source = original.replace("item = dict(row)", "item = row")
        report = verify_artifact("categories", self.artifact("categories", source))
        self.assertFalse(report["passed"])
        self.assertEqual(report["reason"], "Input mutation detected")

    def test_tuple_cannot_satisfy_list_contract(self):
        original = self.artifact("categories")["content"]
        report = verify_artifact("categories", self.artifact("categories", original.replace("return result", "return tuple(result)")))
        self.assertFalse(report["passed"])

    def test_confidential_mode_never_trusts_self_reported_evidence(self):
        self.assertTrue(assess_execution("public").allowed)
        for evidence in [None, {"secure": True}, {"platform": "pkvm", "debug": False, "attested": True}]:
            decision = assess_execution("confidential", evidence)
            self.assertFalse(decision.allowed)
            self.assertFalse(decision.host_confidentiality_verified)


if __name__ == "__main__":
    unittest.main()
