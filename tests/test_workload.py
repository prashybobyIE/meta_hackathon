import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_marketplace import demo as demo_module
from agent_marketplace import providers
from agent_marketplace.demo import run_demo
from agent_marketplace.market import MarketStore
from agent_marketplace.planner import plan_with_meta, reference_plan, validate_plan
from agent_marketplace.protocol import ApiError, canonical_hash, sha256_text
from agent_marketplace.providers import FixtureProvider, MetaProvider, _user_prompt
from agent_marketplace.reference import CONTRACTS, EXPECTED_REPORT, PUBLIC_CSV, acceptance_cases
from agent_marketplace.report import build_report, render_report
from agent_marketplace.requester import Requester
from agent_marketplace.simulated_meta import create_server as simulated_server
from agent_marketplace.verification import verify_artifact
from agent_marketplace.workload import WorkloadError, default_workload, load_public_contracts, load_workload

WORKLOADS = Path(demo_module.__file__).resolve().parent / "workloads"
INVENTORY = WORKLOADS / "inventory_reorder" / "manifest.json"
EXPENSE = WORKLOADS / "expense_report" / "manifest.json"
# Text that occurs only in hidden cases. Checked against JSON-encoded output, hence the \\n.
HIDDEN_TOKENS = {
    "inventory": ["WASHER", "SCREW-X", "DUP-A", "NEG-A", "FRAC-A", "EXTRA-A", "WRONG-HEAD"],
    "expense": ["NaN,x", "Infinity,x", "1e2,x", "1,x,extra", "merchant,amount\\nm,1", '"amount": 1.2', '"amount": "10"'],
}
PUBLIC_CATALOG = "sku;reorder_point;pack_size\nBOLT-M4;100;25\nNUT-M4;40;10\n"
HIDDEN_CATALOG = "sku;reorder_point;pack_size\n WASHER ; 0 ; 1 \nSCREW-X;7;3"


def artifact_for(workload, kind, source=None):
    task = workload.tasks[kind]
    source = task.fixture_source if source is None else source
    return {"content": source, "filename": task.filename, "sha256": sha256_text(source)}


def dependency_for(workload, kind, source=None):
    task, item = workload.tasks[kind], artifact_for(workload, kind, source)
    return {"contract_id": task.contract_id, "filename": task.filename, "function": task.function, "sha256": item["sha256"], "content": item["content"]}


class ManifestValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "workload"
        shutil.copytree(INVENTORY.parent, self.directory)
        self.path = self.directory / "manifest.json"
        self.manifest = json.loads(self.path.read_text())

    def load(self, mutate):
        manifest = copy.deepcopy(self.manifest)
        mutate(manifest)
        self.path.write_text(json.dumps(manifest))
        return load_workload(self.path)

    def assert_refused(self, mutate, message):
        with self.assertRaises(WorkloadError) as caught:
            self.load(mutate)
        self.assertIn(message, str(caught.exception))
        return str(caught.exception)

    def test_shipped_manifests_validate(self):
        self.assertEqual(load_workload(EXPENSE).id, "expense-report")
        self.assertEqual(load_workload(INVENTORY).id, "inventory-reorder")

    def test_structure_is_validated_with_a_location_in_the_error(self):
        reorder = lambda m: m["tasks"]["reorder"]
        catalog = lambda m: m["tasks"]["catalog"]
        cases = {
            "unknown top-level field": (lambda m: m.update(extra=1), "manifest: unknown field 'extra'"),
            "missing field": (lambda m: m.pop("integration"), "manifest: missing field 'integration'"),
            "unknown task field": (lambda m: catalog(m).update(prompt="x"), "tasks.catalog: unknown field 'prompt'"),
            "unknown case field": (lambda m: catalog(m)["public_cases"][0].update(note="x"), "tasks.catalog.public_cases[0]: unknown field 'note'"),
            "wrong schema": (lambda m: m.update(schema="v0"), "schema: must be"),
            "bad kind": (lambda m: m["tasks"].update({"../x": m["tasks"].pop("catalog")}), "tasks key"),
            "path filename": (lambda m: catalog(m).update(filename="../x.py"), "tasks.catalog.filename"),
            "private function": (lambda m: catalog(m).update(function="_hidden"), "tasks.catalog.function"),
            "keyword function": (lambda m: catalog(m).update(function="import"), "tasks.catalog.function"),
            "complexity": (lambda m: catalog(m).update(complexity="free"), "tasks.catalog.complexity"),
            "unhashable complexity": (lambda m: catalog(m).update(complexity=["low"]), "tasks.catalog.complexity"),
            "budget below prices": (lambda m: m.update(budget_credits=10), "budget_credits"),
            "budget type": (lambda m: m.update(budget_credits=True), "budget_credits"),
            "unknown dependency": (lambda m: reorder(m).update(depends_on=["catalog", "ghost"]), "tasks.reorder.depends_on"),
            "self dependency": (lambda m: catalog(m).update(depends_on=["catalog"]), "tasks.catalog.depends_on"),
            "cycle": (lambda m: catalog(m).update(depends_on=["reorder"]), "cycle"),
            "consumes outside depends_on": (lambda m: reorder(m).update(depends_on=[]), "tasks.reorder.consumes"),
            "duplicate contract": (lambda m: reorder(m).update(contract_id=catalog(m)["contract_id"]), "contract_id values must be unique"),
            "no public cases": (lambda m: catalog(m).update(public_cases=[]), "tasks.catalog.public_cases"),
            "no hidden cases": (lambda m: catalog(m).update(hidden_cases=[]), "tasks.catalog.hidden_cases"),
            "raises false": (lambda m: catalog(m)["public_cases"][2].update(raises=False), ".raises: must be true"),
            "expect and raises": (lambda m: catalog(m)["public_cases"][2].update(expect={}), "unknown field 'expect'"),
            "expect wrong shape": (lambda m: catalog(m)["public_cases"][1].update(expect=[]), "output schema"),
            "bad output type": (lambda m: catalog(m).update(output={"type": "object"}), "tasks.catalog.output"),
            "reserved key": (lambda m: catalog(m)["public_cases"][1].update(input={"$eval": "x"}), "reserved"),
            "input form in case": (lambda m: catalog(m)["public_cases"][1].update(input={"$input": True}), "only available in the integration"),
            "call without consumes": (lambda m: catalog(m)["public_cases"][1].update(input={"$call": "reorder", "arg": 1}), "$call is not available"),
            "consumer never calls": (lambda m: [c["input"].update(catalog={}) for c in reorder(m)["public_cases"]], "must exercise every consumed task"),
            "call to unconsumed": (lambda m: reorder(m).update(consumes=[], depends_on=[]) or reorder(m)["public_cases"][0]["input"].update(catalog={"$call": "catalog", "arg": PUBLIC_CATALOG}), "$call is not available"),
            "integration misses a task": (lambda m: m["integration"].update(result={"$call": "catalog", "arg": {"$input": True}}), "must exercise every task"),
            "integration unknown task": (lambda m: m["integration"]["result"].update({"$call": "ghost"}), "unknown task"),
            "renderer": (lambda m: m["report"].update(renderer="exec"), "report.renderer"),
            "absolute fixture": (lambda m: catalog(m).update(fixture="/etc/passwd"), "tasks.catalog.fixture"),
            "escaping fixture": (lambda m: catalog(m).update(fixture="../../x.py"), "tasks.catalog.fixture"),
            "missing fixture": (lambda m: catalog(m).update(fixture="fixtures/none.py"), "tasks.catalog.fixture"),
        }
        for name, (mutate, message) in cases.items():
            with self.subTest(name):
                self.assert_refused(mutate, message)

    def test_fixture_must_pass_restricted_source_checks(self):
        (self.directory / "fixtures" / "evil.py").write_text("X = 1\ndef parse_catalog(text):\n    return {}\n")
        self.assert_refused(lambda m: m["tasks"]["catalog"].update(fixture="fixtures/evil.py"), "rejected by the restricted runtime")

    def test_strict_json_is_required(self):
        for text in ('{"schema": 1, "schema": 2}', '{"budget_credits": NaN}', "not json", "[]"):
            with self.subTest(text):
                self.path.write_text(text)
                with self.assertRaises(WorkloadError):
                    load_workload(self.path)
        with self.assertRaises(WorkloadError):
            load_workload(self.directory / "absent.json")

    def test_private_sensitivity_is_refused(self):
        for value in ("confidential", "private", None):
            with self.subTest(value):
                self.assert_refused(lambda m: m.update(sensitivity=value), "sensitivity: must be public")

    def test_hidden_case_text_in_a_public_field_is_refused_without_quoting_it(self):
        hidden = self.manifest["tasks"]["catalog"]["hidden_cases"][1]["input"]
        places = {
            "spec": lambda m: m["tasks"]["reorder"].update(spec=m["tasks"]["reorder"]["spec"] + " Example: " + hidden),
            "description": lambda m: m["tasks"]["catalog"].update(description="Example: " + hidden),
            "planning brief": lambda m: m.update(planning_brief="Example: " + hidden),
            "title": lambda m: m.update(title=hidden.replace("\n", " ")[:100]),
            "other task's public case": lambda m: m["tasks"]["reorder"]["public_cases"][0]["input"].update(note=hidden),
            "integration": lambda m: m["integration"]["result"]["arg"].update(note=hidden),
        }
        for name, mutate in places.items():
            with self.subTest(name):
                error = self.assert_refused(mutate, "tasks.catalog.hidden_cases[1]: its text appears in public field")
                self.assertNotIn("DUP-A", error)

    def test_hidden_expected_output_in_a_spec_is_refused(self):
        expected = json.dumps(self.manifest["tasks"]["reorder"]["hidden_cases"][0]["expect"], indent=1)
        self.assert_refused(lambda m: m["tasks"]["catalog"].update(spec="Output like " + expected), "its text appears in public field")

    def test_hidden_case_cannot_duplicate_a_public_input(self):
        def mutate(m):
            task = m["tasks"]["catalog"]
            task["hidden_cases"].append(copy.deepcopy(task["public_cases"][1]))
        self.assert_refused(mutate, "duplicates a public case input")

    def test_public_case_cannot_reach_a_hidden_dependency_input(self):
        def mutate(m):
            m["tasks"]["reorder"]["public_cases"][0]["input"]["catalog"]["arg"] = HIDDEN_CATALOG
        self.assert_refused(mutate, "must equal a returning public acceptance input of catalog")

    def test_dependency_call_must_use_an_input_the_artifact_was_accepted_on(self):
        def mutate(m):
            m["tasks"]["reorder"]["hidden_cases"][0]["input"]["catalog"]["arg"] = "sku;reorder_point;pack_size\nNEVER-CHECKED;1;1\n"
        self.assert_refused(mutate, "must equal a returning acceptance input of catalog")

    def test_private_looking_data_is_refused(self):
        refused = {
            "credential in spec": (lambda m: m["tasks"]["catalog"].update(spec="Use Bearer " + "abcdefghijklmnopqrstuvwxyz"), "looks like a credential"),
            "key name": (lambda m: m["tasks"]["catalog"].update(description="Read MODEL_API_KEY first"), "looks like a credential"),
            "private field": (lambda m: m["tasks"]["reorder"]["public_cases"][0]["input"].update(Secret="x"), "named like a credential"),
        }
        for name, (mutate, message) in refused.items():
            with self.subTest(name):
                self.assert_refused(mutate, message)


class ProjectionTests(unittest.TestCase):
    def test_projection_is_an_allowlist_with_no_cases_or_integration(self):
        for name, path in (("inventory", INVENTORY), ("expense", EXPENSE)):
            workload = load_workload(path)
            projected = workload.public_contracts()
            self.assertEqual({k for c in projected.values() for k in c}, {"filename", "function", "spec", "fixture_source", "consumes"})
            for token in HIDDEN_TOKENS[name]:
                self.assertNotIn(token, json.dumps(projected))

    def test_task_repr_does_not_carry_cases(self):
        self.assertNotIn("WASHER", repr(load_workload(INVENTORY).tasks["catalog"]))
        self.assertNotIn("WASHER", repr(load_workload(INVENTORY)))

    def test_planner_prompt_excludes_hidden_cases_for_each_workload(self):
        seen = []

        class Provider:
            name, model, max_completion_tokens = "meta", "muse-spark-1.3", 100

            def complete(self, payload):
                seen.append(payload)
                return {"choices": [{"message": {"content": "not json"}}]}, 0.01
        for name, path in (("inventory", INVENTORY), ("expense", EXPENSE)):
            workload = load_workload(path)
            plan = plan_with_meta(Provider(), workload)
            self.assertEqual(set(plan["tasks"]), set(workload.tasks))
            text = json.dumps(seen[-1])
            for token in HIDDEN_TOKENS[name]:
                self.assertNotIn(token, text)
            self.assertNotIn("hidden_cases", text)

    def test_plan_validation_uses_the_workload_graph(self):
        workload = load_workload(INVENTORY)
        plan = {"tasks": [{"kind": k, **t} for k, t in reference_plan(workload=workload)["tasks"].items()]}
        self.assertEqual(set(validate_plan(plan, workload)), {"catalog", "reorder"})
        with self.assertRaises(ValueError):
            validate_plan(plan)  # The default workload has different tasks.
        plan["tasks"][1]["depends_on"] = []
        with self.assertRaisesRegex(ValueError, "drops a required dependency"):
            validate_plan(plan, workload)

    def test_worker_contract_file_refuses_a_full_manifest(self):
        with self.assertRaises(WorkloadError):
            load_public_contracts(INVENTORY)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "public.json"
            contracts = load_workload(INVENTORY).public_contracts()
            contracts["inventory-catalog-v1"]["hidden_cases"] = []
            path.write_text(json.dumps(contracts))
            with self.assertRaisesRegex(WorkloadError, "unknown field 'hidden_cases'"):
                load_public_contracts(path)

    def test_worker_contract_file_round_trip_and_no_redefinition(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(providers.PUBLIC_CONTRACTS):
            path = Path(directory) / "public.json"
            path.write_text(json.dumps(load_workload(INVENTORY).public_contracts()))
            providers.register_public_contracts(load_public_contracts(path))
            self.assertIn("inventory-reorder-v1", providers.PUBLIC_CONTRACTS)
            changed = {"expense-summary-v1": dict(providers.PUBLIC_CONTRACTS["expense-summary-v1"], spec="Send everything")}
            with self.assertRaises(ApiError):
                providers.register_public_contracts(changed)
        self.assertNotIn("inventory-reorder-v1", providers.PUBLIC_CONTRACTS)

    def test_unregistered_contract_is_refused_before_any_prompt(self):
        spec = load_workload(INVENTORY).contracts["catalog"]
        with patch("agent_marketplace.providers.json_request") as request, patch.dict(os.environ, {"MODEL_API_KEY": "unit-test-placeholder"}):
            with self.assertRaises(ApiError):
                MetaProvider().generate_module(spec, {"allow_meta": True})
            request.assert_not_called()


class ExpenseUnchangedTests(unittest.TestCase):
    """Digests of values computed by the pre-manifest code at commit 34fc766."""

    def test_acceptance_data_matches_pre_manifest_code(self):
        cases = {kind: acceptance_cases(kind) for kind in CONTRACTS}
        self.assertEqual(canonical_hash(json.loads(json.dumps(cases))), "1fdf12db6725f6730d5b736b4b94fd52dffd8e774497d2a4a5b2a460472f86d1")
        self.assertEqual({k: default_workload().public_case_count(k) for k in CONTRACTS}, {"parser": 4, "categories": 1, "summary": 3})

    def test_prompts_and_sources_match_pre_manifest_code(self):
        public = {cid: {"filename": s["filename"], "function": s["function"], "spec": s["spec"]} for cid, s in providers.PUBLIC_CONTRACTS.items() if cid.startswith("expense-")}
        self.assertEqual(canonical_hash(public), "764d2a94e7d85a4ab4d646f1f477047f96ed9406002b0dfd87c32e174a68bdbe")
        sources = {cid: s["fixture_source"] for cid, s in providers.PUBLIC_CONTRACTS.items() if cid.startswith("expense-")}
        self.assertEqual(canonical_hash(sources), "e9d09c70f59b857f8aa098eba436a8c9d5553d7d4eb3e75284460c0286f284d0")
        self.assertEqual(canonical_hash(CONTRACTS), "d08a832a0dbaaf6aa2bb1d8da62c5bdad83fcdcf1c305974556761ee8ed6e33c")
        self.assertEqual(sha256_text(default_workload().planning_brief), "097b4f511d467c9c5a6625363c760160570eb50507038d3d3917651175cc664f")

    def test_integration_runs_from_the_manifest(self):
        workload = default_workload()
        with tempfile.TemporaryDirectory() as directory:
            for task in workload.tasks.values():
                (Path(directory) / task.filename).write_text(task.fixture_source)
            self.assertEqual(build_report(directory, PUBLIC_CSV), EXPECTED_REPORT)
        self.assertIn("Expense report", render_report(workload, EXPECTED_REPORT))


class InventoryAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.workload = load_workload(INVENTORY)
        self.dependencies = {"catalog": dependency_for(self.workload, "catalog")}

    def verify(self, kind="reorder", source=None, dependencies="default"):
        dependencies = self.dependencies if dependencies == "default" else dependencies
        return verify_artifact(kind, artifact_for(self.workload, kind, source), self.workload, dependencies)

    def test_fixtures_pass_and_declared_broken_attempt_fails(self):
        self.assertTrue(self.verify("catalog", dependencies=None)["passed"])
        report = self.verify()
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["cases_total"], 8)
        broken = self.verify(source=self.workload.tasks["reorder"].simulated_first_attempt)
        self.assertFalse(broken["passed"])
        self.assertIn("Public case 0", broken["diagnostic"])
        self.assertIn("BOLT-M4", broken["diagnostic"])  # Resolved public dependency output.

    def test_hidden_failure_is_withheld_for_a_consuming_module(self):
        lax = self.workload.tasks["reorder"].fixture_source.replace('isinstance(on_hand, bool) or ', '')
        report = self.verify(source=lax)
        self.assertFalse(report["passed"])
        self.assertEqual(report["diagnostic"], "A hidden acceptance case failed; its details are withheld.")

    def test_consumer_is_refused_without_an_intact_accepted_artifact(self):
        for name, dependencies in {"none": None, "empty": {}, "wrong digest": {"catalog": {**self.dependencies["catalog"], "sha256": "0" * 64}}}.items():
            with self.subTest(name):
                report = self.verify(dependencies=dependencies)
                self.assertFalse(report["passed"])
                self.assertEqual(report["reason"], "Accepted dependency artifacts are unavailable")
                self.assertNotIn("diagnostic", report)

    def test_acceptance_runs_the_delivered_artifact(self):
        # A different artifact changes what the consumer receives, so the consumer's result changes.
        wrong = "def parse_catalog(text):\n    return {}\n"
        report = self.verify(dependencies={"catalog": dependency_for(self.workload, "catalog", wrong)})
        self.assertFalse(report["passed"])
        self.assertIn('"catalog": {}', report["diagnostic"])

    def test_failing_dependency_is_not_blamed_on_the_module(self):
        failing = "def parse_catalog(text):\n    raise ValueError(text)\n"
        report = self.verify(dependencies={"catalog": dependency_for(self.workload, "catalog", failing)})
        self.assertEqual(report["reason"], "An accepted dependency failed on an acceptance input")
        self.assertNotIn("diagnostic", report)

    def test_module_mutating_consumed_output_is_detected(self):
        source = self.workload.tasks["reorder"].fixture_source.replace('entry = catalog[sku]\n', 'entry = catalog[sku]\n        entry["seen"] = 1\n')
        self.assertEqual(self.verify(source=source)["reason"], "Input mutation detected")


class ProviderDependencyTests(unittest.TestCase):
    def setUp(self):
        self.workload = load_workload(INVENTORY)
        patcher = patch.dict(providers.PUBLIC_CONTRACTS, self.workload.public_contracts())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.contract = self.workload.contracts["reorder"]
        self.good = {"catalog": dependency_for(self.workload, "catalog")}

    def test_worker_side_checks_refuse_mismatched_delivery(self):
        provider = FixtureProvider()
        self.assertEqual(provider.generate_module(self.contract, {}, dependencies=self.good)["provider"], "fixture")
        bad = {
            "missing": None,
            "extra": {**self.good, "other": self.good["catalog"]},
            "digest": {"catalog": {**self.good["catalog"], "sha256": "0" * 64}},
            "identity": {"catalog": {**self.good["catalog"], "contract_id": "expense-summary-v1"}},
            "field": {"catalog": {**self.good["catalog"], "note": "extra"}},
            "oversized": {"catalog": {**self.good["catalog"], "content": "x" * 40000, "sha256": sha256_text("x" * 40000)}},
        }
        for name, dependencies in bad.items():
            with self.subTest(name), self.assertRaises(ApiError):
                provider.generate_module(self.contract, {}, dependencies=dependencies)
        with self.assertRaises(ApiError):
            provider.generate_module(self.workload.contracts["catalog"], {}, dependencies=self.good)

    def test_first_prompt_has_dependency_source_and_repair_prompt_does_not(self):
        spec = providers.PUBLIC_CONTRACTS["inventory-reorder-v1"]
        source = self.good["catalog"]["content"]
        self.assertIn(source, _user_prompt(spec, None, self.good))
        self.assertNotIn(source, _user_prompt(spec, {"previous_source": "prior", "diagnostic": "public"}, self.good))
        self.assertEqual(_user_prompt(providers.PUBLIC_CONTRACTS["expense-summary-v1"], None), providers.PUBLIC_CONTRACTS["expense-summary-v1"]["spec"])


class Harness(Requester):
    """The requester state machine without sockets, as in test_requester."""

    def __init__(self, store, directory, workload):
        super().__init__("http://127.0.0.1:9876", "requester-test", directory, workload=workload)
        self.store = store
        self.requester_url = "http://127.0.0.1:9877"

    def market(self, method, path, body=None):
        return self.store.handle(method, path, "requester-test", body)


class DependencyDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MarketStore(Path(self.temp.name) / "market.db", "requester-test", "enrollment-test")
        self.addCleanup(self.store.close)
        self.workload = load_workload(INVENTORY)
        self.r = Harness(self.store, Path(self.temp.name) / "requester", self.workload)
        self.token = self.store.handle("POST", "/v1/workers/register", "enrollment-test", {"worker_id": "worker-a", "capabilities": ["python-module-v1"], "provider": "fixture", "model": "reference", "slots": 1})["worker_token"]

    def bid(self, kind):
        self.store.handle("POST", f"/v1/tasks/{self.r.state['tasks'][kind]['task_id']}/bids", self.token, {"credits": 10})

    def current(self, kind):
        return self.r.state["attempts"][self.r.state["tasks"][kind]["current_attempt"]]

    def deliver(self, kind, source=None):
        record, contract = self.current(kind), self.workload.contracts[kind]
        item = artifact_for(self.workload, kind, source)
        self.r.receive(record["attempt_id"], record["token"], {**item, "task_id": record["task_id"], "attempt_id": record["attempt_id"], "provider": "fixture", "model": "reference", "elapsed_ms": 1})
        self.assertEqual(item["filename"], contract["filename"])
        return record

    def test_artifact_reaches_the_consumer_only_after_acceptance(self):
        self.r.tick(schedule=False)
        self.assertFalse(self.r.state["tasks"]["reorder"]["advertised"])
        self.bid("catalog")
        self.r.tick()
        first = self.current("catalog")
        self.assertNotIn("dependencies", self.r.dispatch(first["attempt_id"], first["token"]))
        with self.assertRaises(ApiError) as refused:
            self.r.accepted_dependencies("reorder")
        self.assertEqual(refused.exception.status, 409)

        # A rejected artifact is never delivered and does not open the consumer.
        self.deliver("catalog", "def parse_catalog(text):\n    return {}\n")
        self.r.tick()
        self.assertEqual(self.r.state["tasks"]["catalog"]["rejections"], 1)
        self.assertFalse(self.r.state["tasks"]["reorder"]["advertised"])
        with self.assertRaises(ApiError):
            self.r.accepted_dependencies("reorder")

        # Delivered: the bytes accepted and receipted, not a prewritten copy.
        self.r.tick()
        accepted_source = self.workload.tasks["catalog"].fixture_source + "\n# accepted variant\n"
        self.deliver("catalog", accepted_source)
        self.r.tick()
        self.assertTrue(self.r.state["tasks"]["catalog"]["accepted"])
        self.assertTrue(self.r.state["tasks"]["reorder"]["advertised"])
        self.bid("reorder")
        self.r.tick()
        record = self.current("reorder")
        dispatch = self.r.dispatch(record["attempt_id"], record["token"])
        delivered = dispatch["dependencies"]["catalog"]
        self.assertEqual(delivered["content"], accepted_source)
        receipt = next(r for r in self.store.snapshot()["receipts"] if r["task_id"] == self.r.state["tasks"]["catalog"]["task_id"])
        self.assertEqual(delivered["sha256"], receipt["artifact_sha256"])
        self.assertEqual(set(dispatch["contract"]), {"id", "filename", "function", "description"})
        for token in HIDDEN_TOKENS["inventory"]:
            self.assertNotIn(token, json.dumps(dispatch))

    def test_dispatch_refuses_an_assignment_made_before_the_dependency_was_accepted(self):
        task_id = self.r.state["tasks"]["reorder"]["task_id"]
        self.store.handle("POST", "/v1/tasks", "requester-test", {"task_id": task_id, "capability": "python-module-v1", "sensitivity": "public", "max_credits": 16, "expires_at": 4102444800})
        bid = self.store.handle("POST", f"/v1/tasks/{task_id}/bids", self.token, {"credits": 10})
        token = "d" * 32
        lease = self.store.handle("POST", f"/v1/tasks/{task_id}/assign", "requester-test", {"bid_id": bid["bid_id"], "attempt_id": "attempt-early", "lease_seconds": 30, "requester_url": self.r.requester_url, "dispatch_token": token})
        self.r.state["attempts"]["attempt-early"] = {"kind": "reorder", "task_id": task_id, "attempt_id": "attempt-early", "worker_id": "worker-a", "credits": 10, "token": token, "lease_until": lease["lease_until"], "sensitivity": "public", "status": "assigned"}
        with self.assertRaises(ApiError) as refused:
            self.r.dispatch("attempt-early", token)
        self.assertEqual(refused.exception.status, 409)

    def test_state_for_another_manifest_is_refused_on_resume(self):
        with self.assertRaisesRegex(ValueError, "different workload manifest"):
            Harness(self.store, self.r.workspace, default_workload())

    def test_status_and_state_carry_no_hidden_cases(self):
        self.r.tick(schedule=False)
        text = json.dumps(self.r.status()) + self.r.path.read_text()
        for token in HIDDEN_TOKENS["inventory"]:
            self.assertNotIn(token, text)
        self.assertEqual(self.r.status()["workload"], {"id": "inventory-reorder", "title": "Inventory reorder plan"})


class CapturingSimulator:
    """Keeps the simulator so a test can read every prompt a model would have been sent."""

    def __init__(self):
        self.server = None

    def __call__(self, workload):
        self.server = simulated_server(workload)
        return self.server


class EndToEndTests(unittest.TestCase):
    """Real market, worker processes and leases. Fixture or loopback simulator only."""

    def assert_settled(self, summary, tasks, directory):
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["accepted_tasks"], tasks)
        self.assertEqual(summary["credits"]["reserved"], 0)
        snapshot = json.loads((Path(directory) / "market-snapshot.json").read_text())
        self.assertEqual(len({r["task_id"] for r in snapshot["receipts"]}), tasks)
        self.assertEqual(summary["credits"]["settled"], sum(r["credits"] for r in snapshot["receipts"]))

    def test_expense_fixture_is_the_default(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = run_demo(directory, quiet=True, timeout=30)
            self.assertEqual(summary["workload"], "expense-report")
            self.assert_settled(summary, 3, directory)
            self.assertEqual(summary["integration"]["report"], EXPECTED_REPORT)
            self.assertEqual((Path(directory) / "public-expenses.csv").read_text(), PUBLIC_CSV)
            self.assertFalse((Path(directory) / "public-contracts.json").exists())

    def test_inventory_fixture_runs_through_the_same_market(self):
        workload = load_workload(INVENTORY)
        with tempfile.TemporaryDirectory() as directory:
            summary = run_demo(directory, quiet=True, timeout=30, workload=INVENTORY)
            self.assertEqual(summary["workload"], "inventory-reorder")
            self.assert_settled(summary, 2, directory)
            self.assertEqual(summary["integration"]["report"], workload.integration["expect"])
            self.assertEqual(len(summary["workers_used"]), 2)
            root = Path(directory)
            self.assertTrue((root / "requester/artifacts/reorder.py").exists())
            self.assertTrue((root / "public-catalog.txt").exists())
            self.assertIn("FILTER-A", (root / "requester/report.html").read_text())
            # The worker on the consumer really received the accepted artifact's digest.
            logs = "".join(p.read_text() for p in root.glob("worker-*.log"))
            events = [json.loads(line) for line in logs.splitlines() if line.startswith("{")]
            received = [e for e in events if e["event"] == "dependency_artifact_received"]
            self.assertEqual({e["artifact_sha256"] for e in received}, {sha256_text(workload.tasks["catalog"].fixture_source)})
            # Workers were given only the projection; hidden text stays on the requester.
            written = (root / "public-contracts.json").read_text() + (root / "summary.json").read_text() + (root / "requester/requester-state.json").read_text()
            for token in HIDDEN_TOKENS["inventory"]:
                self.assertNotIn(token, written)

    def test_inventory_recovers_from_a_crashed_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = run_demo(directory, failure=True, quiet=True, timeout=30, workload=INVENTORY)
            self.assert_settled(summary, 2, directory)
            self.assertGreaterEqual(summary["expired_attempts"], 1)
            self.assertNotIn("worker-fault", summary["workers_used"])

    def test_inventory_simulated_meta_plans_rejects_repairs_and_keeps_hidden_cases_out(self):
        real = Path(demo_module.__file__).resolve().parents[1] / ".budget" / "meta-usage.json"
        before = real.read_bytes() if real.exists() else None
        workload = load_workload(INVENTORY)
        capture = CapturingSimulator()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"MODEL_API_KEY": "REAL-KEY-MUST-NOT-BE-SENT"}), patch.object(demo_module, "simulated_server", capture):
            summary = run_demo(directory, provider="simulated-meta", quiet=True, timeout=30, workload=INVENTORY)
            self.assert_settled(summary, 2, directory)
            self.assertEqual(summary["plan_source"], "simulated-meta")
            self.assertFalse(summary["real_ai_inference"])
            self.assertEqual(summary["integration"]["report"], workload.integration["expect"])
            self.assertEqual(summary["rejected_attempts"], 1)
            repaired = [a for a in summary["attempt_timings"] if a["repair_round"]]
            self.assertEqual([(a["kind"], a["status"]) for a in repaired], [("reorder", "accepted")])
            self.assertEqual(summary["budget"]["calls"], 4)
            self.assertNotIn("REAL-KEY", (Path(directory) / "requester/requester-state.json").read_text())
        self.assertEqual(real.read_bytes() if real.exists() else None, before)

        requests = capture.server.requests
        self.assertEqual(len(requests), 4)
        for token in HIDDEN_TOKENS["inventory"]:
            self.assertNotIn(token, json.dumps(requests))
        self.assertNotIn("REAL-KEY", json.dumps(requests))
        prompts = [r["messages"][-1]["content"] for r in requests if "response_format" not in r]
        catalog_source = workload.tasks["catalog"].fixture_source
        first_reorder = next(p for p in prompts if workload.tasks["reorder"].spec in p and "PREVIOUS ATTEMPT" not in p)
        repair = next(p for p in prompts if "PREVIOUS ATTEMPT" in p)
        catalog_prompt = next(p for p in prompts if workload.tasks["catalog"].spec in p)
        self.assertIn(catalog_source, first_reorder)
        self.assertNotIn(catalog_source, repair)
        self.assertNotIn("ACCEPTED DEPENDENCY", catalog_prompt)
        self.assertIn("Public case 0", repair)
        self.assertIn(workload.tasks["reorder"].simulated_first_attempt, repair)

    def test_expense_simulated_meta_prompts_exclude_hidden_cases(self):
        capture = CapturingSimulator()
        with tempfile.TemporaryDirectory() as directory, patch.object(demo_module, "simulated_server", capture):
            summary = run_demo(directory, provider="simulated-meta", quiet=True, timeout=30)
            self.assertEqual(summary["integration"]["report"], EXPECTED_REPORT)
            self.assertEqual(summary["budget"]["calls"], 5)
        text = json.dumps(capture.server.requests)
        self.assertEqual(len(capture.server.requests), 5)
        for token in HIDDEN_TOKENS["expense"]:
            self.assertNotIn(token, text)
        self.assertNotIn("ACCEPTED DEPENDENCY", text)


class CommandLineTests(unittest.TestCase):
    def run_main(self, *arguments):
        with patch.object(sys, "argv", ["demo", *arguments]), patch.object(demo_module, "load_env_file"), patch("builtins.print"):
            demo_module.main()

    def test_workload_flag_runs_the_named_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "run"
            self.run_main("--workspace", str(workspace), "--workload", str(INVENTORY), "--timeout", "30")
            self.assertEqual(json.loads((workspace / "summary.json").read_text())["workload"], "inventory-reorder")

    def test_invalid_manifest_stops_with_a_clear_error_and_starts_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "bad.json"
            bad.write_text('{"schema": "wrong"}')
            workspace = Path(directory) / "run"
            with self.assertRaises(SystemExit) as stopped:
                self.run_main("--workspace", str(workspace), "--workload", str(bad))
            self.assertIn("manifest: missing field", str(stopped.exception))
            self.assertFalse(workspace.exists())

    def test_protected_step_is_limited_to_the_expense_workload(self):
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            self.run_main("--workload", str(INVENTORY), "--protected-url", "http://127.0.0.1:1")


if __name__ == "__main__":
    unittest.main()
