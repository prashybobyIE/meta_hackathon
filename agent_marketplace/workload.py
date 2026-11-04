"""Declarative workload manifests: strict validation and the public projection.

A manifest is requester-owned JSON. It holds public specs and hidden acceptance cases
side by side, so the only route from a manifest to a worker or a model prompt is
`Workload.public_contracts()`, an allowlist of named public fields. Validation also refuses
manifests whose public text repeats hidden case text or looks like private data. That
check catches literal copies; it does not prove a spec reveals nothing about a hidden case.

A manifest is data. It cannot name code to run beyond the bounded module fixtures, which
must pass the restricted runtime's source checks. See docs/implementation/WORKLOADS.md.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import keyword
from pathlib import Path
import re

from .protocol import canonical_hash, decode_json
from .restricted import RestrictedError, RestrictedModule

SCHEMA = "agent-marketplace-workload-v1"
COMPLEXITY_CREDITS = {"low": 12, "medium": 16, "high": 20}
DEFAULT_WORKLOAD_PATH = Path(__file__).resolve().parent / "workloads" / "expense_report" / "manifest.json"
REPORT_RENDERERS = ("expense-summary", "json-table")

MAX_MANIFEST_BYTES = 256 * 1024
MAX_SOURCE_BYTES = 32 * 1024
MAX_TASKS = 16
MAX_CASES = 64
MAX_CONSUMES = 2
MAX_DEPTH = 20
MIN_FRAGMENT = 8  # Shorter strings ("[]", "x") occur in ordinary public text by chance.

_KIND = re.compile(r"[a-z][a-z0-9_]{0,31}")
_CONTRACT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}-v[0-9]+")
_FILENAME = re.compile(r"[a-z][a-z0-9_]{0,63}\.py")
_FUNCTION = re.compile(r"[a-z][a-z0-9_]{0,63}")
_DATA_FILENAME = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_SECRET_TEXT = re.compile(r"MODEL_API_KEY|-----BEGIN [A-Z ]*PRIVATE KEY|\bsk-[A-Za-z0-9_-]{16,}|\bBearer\s+[A-Za-z0-9._-]{16,}")
_PRIVATE_KEYS = {"private", "secret", "secrets", "password", "passwd", "token", "credential", "credentials", "api_key", "apikey"}


class WorkloadError(ValueError):
    """Names a manifest location. Never quotes case data, which may be hidden."""


def _fail(where, message):
    raise WorkloadError(f"{where}: {message}")


def _exact(value, where, required, optional=()):
    if type(value) is not dict:
        _fail(where, "must be an object")
    unknown = sorted(set(value) - set(required) - set(optional))
    if unknown:
        _fail(where, "unknown field " + ", ".join(repr(k[:64]) for k in unknown))
    missing = sorted(set(required) - set(value))
    if missing:
        _fail(where, "missing field " + ", ".join(repr(k) for k in missing))
    return value


def _text(value, where, maximum, pattern=None):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _fail(where, f"must be nonblank text of at most {maximum} characters")
    if any(ord(c) < 32 and c != "\n" for c in value):
        _fail(where, "must not contain control characters")
    if pattern and not pattern.fullmatch(value):
        _fail(where, f"must match {pattern.pattern}")
    return value


def _is_form(value):
    return type(value) is dict and any(key.startswith("$") for key in value)


def _check_value(value, where, *, calls=None, allow_input=False, depth=0):
    """Check JSON data. `$call` and `$input` are the only forms; other `$` keys are refused."""
    if depth > MAX_DEPTH:
        _fail(where, f"nests deeper than {MAX_DEPTH} levels")
    if _is_form(value):
        if "$input" in value:
            if not allow_input:
                _fail(where, "$input is only available in the integration result")
            if set(value) != {"$input"} or value["$input"] is not True:
                _fail(where, "$input must be exactly {\"$input\": true}")
            return
        if "$call" not in value:
            _fail(where, "keys beginning with $ are reserved")
        if calls is None:
            _fail(where, "$call is not available here")
        _exact(value, where, ("$call", "arg"))
        if not isinstance(value["$call"], str):
            _fail(where, "$call must name a task")
        calls.append((value["$call"], value["arg"], where))
        _check_value(value["arg"], where + ".arg", calls=calls, allow_input=allow_input, depth=depth + 1)
    elif type(value) is dict:
        for key, item in value.items():
            _check_value(item, f"{where}.{key[:32]}", calls=calls, allow_input=allow_input, depth=depth + 1)
    elif type(value) is list:
        for index, item in enumerate(value):
            _check_value(item, f"{where}[{index}]", calls=calls, allow_input=allow_input, depth=depth + 1)


def _check_output(schema, where, depth=0):
    if depth > 6:
        _fail(where, "output schema is too deep")
    if type(schema) is not dict or "type" not in schema:
        _fail(where, "output schema needs a type")
    kind = schema["type"]
    if kind in ("int", "str", "bool", "float"):
        _exact(schema, where, ("type",))
    elif kind == "list":
        _exact(schema, where, ("type", "items"))
        _check_output(schema["items"], where + ".items", depth + 1)
    elif kind == "dict":
        _exact(schema, where, ("type",), ("fields", "values"))
        if "fields" not in schema and "values" not in schema:
            _fail(where, "dict output needs fields or values")
        fields = schema.get("fields", {})
        if type(fields) is not dict:
            _fail(where + ".fields", "must be an object")
        for name, item in fields.items():
            _check_output(item, f"{where}.fields.{name[:32]}", depth + 1)
        if "values" in schema:
            _check_output(schema["values"], where + ".values", depth + 1)
    else:
        _fail(where, "output type must be int, str, bool, float, list or dict")


def conforms(schema, value):
    """Exact types, so a tuple cannot pass as a list or a bool as an int. Extra fields are allowed."""
    kind = schema["type"]
    if kind in ("int", "str", "bool", "float"):
        return type(value) is {"int": int, "str": str, "bool": bool, "float": float}[kind]
    if kind == "list":
        return type(value) is list and all(conforms(schema["items"], item) for item in value)
    if type(value) is not dict:
        return False
    return (all(conforms(item, value.get(name)) for name, item in schema.get("fields", {}).items())
            and ("values" not in schema or all(conforms(schema["values"], item) for item in value.values())))


def resolve_expression(value, call=None, input_value=None):
    """Evaluate `$call` and `$input` forms; everything else is a literal copied by value."""
    if _is_form(value):
        if "$input" in value:
            return input_value
        return call(value["$call"], resolve_expression(value["arg"], call, input_value))
    if type(value) is dict:
        return {key: resolve_expression(item, call, input_value) for key, item in value.items()}
    if type(value) is list:
        return [resolve_expression(item, call, input_value) for item in value]
    return value


@dataclass(frozen=True)
class Case:
    input: object
    expect: object
    raises: bool


@dataclass(frozen=True)
class Task:
    kind: str
    contract_id: str
    filename: str
    function: str
    description: str
    spec: str
    depends_on: tuple
    consumes: tuple
    complexity: str
    output: dict
    public_cases: tuple = field(repr=False)
    hidden_cases: tuple = field(repr=False)
    fixture_source: str = field(repr=False)
    simulated_first_attempt: str | None = field(repr=False, default=None)


@dataclass(frozen=True)
class Workload:
    id: str
    title: str
    path: Path
    digest: str
    planning_brief: str
    budget_credits: int
    tasks: dict = field(repr=False)
    integration: dict = field(repr=False)
    report: dict = field(repr=False)

    @property
    def is_default(self):
        return self.path == DEFAULT_WORKLOAD_PATH

    @property
    def identity(self):
        return {"id": self.id, "digest": self.digest}

    @property
    def contracts(self):
        """What the requester dispatches: identity and description, no cases."""
        return {k: {"id": t.contract_id, "filename": t.filename, "function": t.function, "description": t.description} for k, t in self.tasks.items()}

    def cases(self, kind):
        """Public cases first; a case index at or past public_case_count is hidden."""
        task = self.tasks[kind]
        return list(task.public_cases) + list(task.hidden_cases)

    def public_case_count(self, kind):
        return len(self.tasks[kind].public_cases)

    def public_contracts(self):
        """The only workload data a worker receives. Keep this an explicit allowlist."""
        return {t.contract_id: {
            "filename": t.filename, "function": t.function, "spec": t.spec, "fixture_source": t.fixture_source,
            "consumes": {dep: self.tasks[dep].contract_id for dep in t.consumes},
        } for t in self.tasks.values()}


def _fixture(base, value, where, function):
    _text(value, where, 256)
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".py":
        _fail(where, "must be a relative .py path inside the workload directory")
    resolved = (base / relative).resolve()
    if not resolved.is_relative_to(base) or not resolved.is_file():
        _fail(where, "must name a file inside the workload directory")
    if resolved.stat().st_size > MAX_SOURCE_BYTES:
        _fail(where, f"exceeds {MAX_SOURCE_BYTES} bytes")
    try:
        source = resolved.read_text(encoding="utf-8")
    except UnicodeError:
        _fail(where, "must be UTF-8 text")
    try:
        RestrictedModule(source, function)
    except RestrictedError as exc:
        _fail(where, f"rejected by the restricted runtime: {exc}")
    return source


def _parse_case(raw, where, schema, calls, *, may_call):
    if type(raw) is not dict:
        _fail(where, "must be an object")
    if "raises" in raw:
        _exact(raw, where, ("input", "raises"))
        if raw["raises"] is not True:
            _fail(where + ".raises", "must be true; omit it for a case that returns")
    else:
        _exact(raw, where, ("input", "expect"))
        _check_value(raw["expect"], where + ".expect")
        if not conforms(schema, raw["expect"]):
            _fail(where + ".expect", "does not match the task's output schema")
    _check_value(raw["input"], where + ".input", calls=calls if may_call else None)
    return Case(raw["input"], raw.get("expect"), "raises" in raw)


def _check_graph(tasks):
    done = set()
    while len(done) < len(tasks):
        ready = {k for k, t in tasks.items() if k not in done and set(t.depends_on) <= done}
        if not ready:
            _fail("tasks", "dependency graph contains a cycle")
        done |= ready


def _check_case_calls(tasks, kind, visibility, calls):
    """A case may run a consumed artifact only on an input that artifact was accepted on.

    The dependency's output is then fixed by its own acceptance, so a failure lands on the
    module under test. A public case may reach only the dependency's public inputs, because
    repair feedback can quote its resolved input.
    """
    task = tasks[kind]
    for called, arg, where in calls:
        if called not in task.consumes:
            _fail(where, f"$call may only name a task listed in {kind}.consumes")
        if _contains_form(arg):
            _fail(where, "$call in an acceptance case takes a literal argument")
        dep = tasks[called]
        allowed = dep.public_cases if visibility == "public_cases" else dep.public_cases + dep.hidden_cases
        if not any(not c.raises and c.input == arg for c in allowed):
            _fail(where, f"$call argument must equal a returning {'public ' if visibility == 'public_cases' else ''}acceptance input of {called}")


def _contains_form(value):
    if _is_form(value):
        return True
    if type(value) is dict:
        return any(_contains_form(v) for v in value.values())
    if type(value) is list:
        return any(_contains_form(v) for v in value)
    return False


def _strings(value):
    if type(value) is str:
        yield value
    elif type(value) is dict:
        for item in value.values():
            yield from _strings(item)
    elif type(value) is list:
        for item in value:
            yield from _strings(item)


def _without_call_args(value):
    """Called arguments belong to the dependency's own cases and are checked there."""
    if _is_form(value):
        return None
    if type(value) is dict:
        return {k: _without_call_args(v) for k, v in value.items()}
    if type(value) is list:
        return [_without_call_args(v) for v in value]
    return value


def _fragment_pattern(text):
    """Match a literal copy despite spacing changes, but not as part of a longer value.

    The boundaries matter: a hidden "d,m,1" must not match inside a public "d,m,1.001".
    """
    squashed = "".join(text.split())
    if len(squashed) < MIN_FRAGMENT:
        return None
    return re.compile(r"(?<![\w.-])" + r"\s*".join(re.escape(c) for c in squashed) + r"(?![\w.-])")


def _check_private_keys(value, where):
    if type(value) is dict:
        for key, item in value.items():
            if key.lower() in _PRIVATE_KEYS:
                _fail(where, "public data must not carry a field named like a credential or private value")
            _check_private_keys(item, f"{where}.{key[:32]}")
    elif type(value) is list:
        for index, item in enumerate(value):
            _check_private_keys(item, f"{where}[{index}]")


def _check_disclosure(manifest, tasks, integration):
    """Refuse manifests that would put hidden cases or private-looking data in public fields."""
    public = {"title": manifest["title"], "planning_brief": manifest["planning_brief"]}
    for kind, task in tasks.items():
        public.update({f"tasks.{kind}.description": task.description, f"tasks.{kind}.spec": task.spec, f"tasks.{kind}.fixture": task.fixture_source})
        if task.simulated_first_attempt:
            public[f"tasks.{kind}.simulated_first_attempt"] = task.simulated_first_attempt
        for index, case in enumerate(task.public_cases):
            where = f"tasks.{kind}.public_cases[{index}]"
            _check_private_keys(case.input, where)
            _check_private_keys(case.expect, where)
            for text in _strings([case.input, case.expect]):
                public[where + ":text"] = public.get(where + ":text", "") + "\n" + text
            public[where + ":json"] = json.dumps([case.input, case.expect])
    _check_private_keys(integration, "integration")
    public["integration"] = json.dumps(integration) + "\n" + "\n".join(_strings(integration))
    for where, text in public.items():
        if _SECRET_TEXT.search(text):
            _fail(where, "contains text that looks like a credential")
    for kind, task in tasks.items():
        public_inputs = [c.input for c in task.public_cases]
        for index, case in enumerate(task.hidden_cases):
            where = f"tasks.{kind}.hidden_cases[{index}]"
            if case.input in public_inputs:
                _fail(where, "duplicates a public case input, so it is not hidden")
            literal = _without_call_args(case.input)
            fragments = list(_strings(literal)) + [json.dumps(literal)]
            if not case.raises:
                fragments.append(json.dumps(case.expect))
            for fragment in fragments:
                pattern = _fragment_pattern(fragment)
                if pattern is None:
                    continue
                for place, text in public.items():
                    if pattern.search(text):
                        _fail(where, f"its text appears in public field {place.split(':')[0]}")


def _parse_task(kind, raw, base):
    where = f"tasks.{kind}"
    _exact(raw, where, ("contract_id", "filename", "function", "description", "spec", "depends_on", "complexity", "output", "public_cases", "hidden_cases", "fixture"), ("consumes", "simulated_first_attempt"))
    _text(raw["contract_id"], where + ".contract_id", 64, _CONTRACT_ID)
    _text(raw["filename"], where + ".filename", 64, _FILENAME)
    _text(raw["function"], where + ".function", 64, _FUNCTION)
    if keyword.iskeyword(raw["function"]):
        _fail(where + ".function", "must not be a Python keyword")
    _text(raw["description"], where + ".description", 1000)
    _text(raw["spec"], where + ".spec", 4000)
    if not isinstance(raw["complexity"], str) or raw["complexity"] not in COMPLEXITY_CREDITS:
        _fail(where + ".complexity", "must be " + ", ".join(sorted(COMPLEXITY_CREDITS)))
    _check_output(raw["output"], where + ".output")
    for name in ("depends_on", "consumes"):
        items = raw.get(name, [])
        if type(items) is not list or any(not isinstance(i, str) for i in items) or len(set(items)) != len(items):
            _fail(f"{where}.{name}", "must be a list of unique task names")
    if len(raw.get("consumes", [])) > MAX_CONSUMES:
        _fail(where + ".consumes", f"may list at most {MAX_CONSUMES} tasks")
    if not set(raw.get("consumes", [])) <= set(raw["depends_on"]):
        _fail(where + ".consumes", "every consumed task must also be in depends_on")
    calls = {}
    cases = {}
    for name in ("public_cases", "hidden_cases"):
        items = raw[name]
        if type(items) is not list or not 1 <= len(items) <= MAX_CASES:
            _fail(f"{where}.{name}", f"must be a list of 1 to {MAX_CASES} cases")
        calls[name] = []
        cases[name] = tuple(_parse_case(item, f"{where}.{name}[{i}]", raw["output"], calls[name], may_call=bool(raw.get("consumes"))) for i, item in enumerate(items))
    if not any(not c.raises for c in cases["public_cases"] + cases["hidden_cases"]):
        _fail(where, "needs at least one case that returns")
    if raw.get("consumes") and not all(any(called == dep for called, _, _ in calls["public_cases"]) for dep in raw["consumes"]):
        _fail(where + ".public_cases", "must exercise every consumed task through $call")
    fixture = _fixture(base, raw["fixture"], where + ".fixture", raw["function"])
    broken = None
    if "simulated_first_attempt" in raw:
        broken = _fixture(base, raw["simulated_first_attempt"], where + ".simulated_first_attempt", raw["function"])
    task = Task(kind, raw["contract_id"], raw["filename"], raw["function"], raw["description"], raw["spec"], tuple(raw["depends_on"]), tuple(raw.get("consumes", [])), raw["complexity"], raw["output"], cases["public_cases"], cases["hidden_cases"], fixture, broken)
    return task, calls


def _parse_integration(raw, tasks):
    _exact(raw, "integration", ("input", "input_filename", "result", "expect"))
    _check_value(raw["input"], "integration.input")
    _check_value(raw["expect"], "integration.expect")
    _text(raw["input_filename"], "integration.input_filename", 64, _DATA_FILENAME)
    calls = []
    _check_value(raw["result"], "integration.result", calls=calls, allow_input=True)
    unknown = sorted({called for called, _, _ in calls} - set(tasks))
    if unknown:
        _fail("integration.result", "calls an unknown task")
    missing = sorted(set(tasks) - {called for called, _, _ in calls})
    if missing:
        _fail("integration.result", "must exercise every task; missing " + ", ".join(missing))
    return dict(raw)


def load_workload(path=None):
    """Load and validate a manifest. Raises WorkloadError, a ValueError, on any problem."""
    path = Path(path).resolve() if path else DEFAULT_WORKLOAD_PATH
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            _fail("manifest", f"exceeds {MAX_MANIFEST_BYTES} bytes")
        raw_bytes = path.read_bytes()
    except OSError:
        _fail("manifest", f"cannot read {path}")
    try:
        manifest = decode_json(raw_bytes)
    except (ValueError, UnicodeError, RecursionError) as exc:
        _fail("manifest", f"is not strict JSON ({type(exc).__name__})")
    _exact(manifest, "manifest", ("schema", "id", "title", "sensitivity", "planning_brief", "budget_credits", "tasks", "integration", "report"))
    if manifest["schema"] != SCHEMA:
        _fail("schema", f"must be {SCHEMA}")
    if manifest["sensitivity"] != "public":
        _fail("sensitivity", "must be public; private data is not accepted in a workload manifest")
    _text(manifest["id"], "id", 64, re.compile(r"[a-z0-9][a-z0-9-]{0,63}"))
    _text(manifest["title"], "title", 120)
    _text(manifest["planning_brief"], "planning_brief", 2000)
    raw_tasks = manifest["tasks"]
    if type(raw_tasks) is not dict or not 1 <= len(raw_tasks) <= MAX_TASKS:
        _fail("tasks", f"must be an object with 1 to {MAX_TASKS} tasks")
    budget = manifest["budget_credits"]
    ceiling = COMPLEXITY_CREDITS["high"] * len(raw_tasks)
    if type(budget) is not int or not ceiling <= budget <= 1000:
        _fail("budget_credits", f"must be an integer from {ceiling} (every task at the highest price) to 1000")
    tasks, calls = {}, {}
    for kind, raw in raw_tasks.items():
        _text(kind, "tasks key", 32, _KIND)
        tasks[kind], calls[kind] = _parse_task(kind, raw, path.parent)
    for attribute in ("contract_id", "filename", "function"):
        values = [getattr(t, attribute) for t in tasks.values()]
        if len(set(values)) != len(values):
            _fail("tasks", f"{attribute} values must be unique")
    for kind, task in tasks.items():
        for dep in task.depends_on:
            if dep not in tasks or dep == kind:
                _fail(f"tasks.{kind}.depends_on", "must name other tasks in this manifest")
    _check_graph(tasks)
    for kind in tasks:
        for visibility, found in calls[kind].items():
            _check_case_calls(tasks, kind, visibility, found)
    integration = _parse_integration(manifest["integration"], tasks)
    _exact(manifest["report"], "report", ("renderer", "title"))
    if manifest["report"]["renderer"] not in REPORT_RENDERERS:
        _fail("report.renderer", "must be " + ", ".join(REPORT_RENDERERS))
    _text(manifest["report"]["title"], "report.title", 120)
    _check_disclosure(manifest, tasks, integration)
    digest = canonical_hash({"manifest": manifest, "sources": {k: [t.fixture_source, t.simulated_first_attempt] for k, t in tasks.items()}})
    return Workload(manifest["id"], manifest["title"], path, digest, manifest["planning_brief"], budget, tasks, integration, manifest["report"])


_default = None


def default_workload():
    global _default
    if _default is None:
        _default = load_workload()
    return _default


def resolve_workload(workload=None):
    """Accept a loaded workload, a manifest path, or None for the default."""
    if workload is None:
        return default_workload()
    return workload if isinstance(workload, Workload) else load_workload(workload)


def load_public_contracts(path):
    """Read a worker's contract file. A full manifest fails here, by its unknown fields."""
    try:
        contracts = decode_json(Path(path).read_bytes())
    except OSError:
        _fail("contracts file", f"cannot read {path}")
    except (ValueError, UnicodeError, RecursionError):
        _fail("contracts file", "is not strict JSON")
    if type(contracts) is not dict or not 1 <= len(contracts) <= MAX_TASKS:
        _fail("contracts file", f"must map 1 to {MAX_TASKS} contract ids to public contracts")
    for contract_id, spec in contracts.items():
        where = f"contracts[{contract_id[:64]!r}]"
        _text(contract_id, where, 64, _CONTRACT_ID)
        _exact(spec, where, ("filename", "function", "spec", "fixture_source", "consumes"))
        _text(spec["filename"], where + ".filename", 64, _FILENAME)
        _text(spec["function"], where + ".function", 64, _FUNCTION)
        _text(spec["spec"], where + ".spec", 4000)
        if not isinstance(spec["fixture_source"], str) or len(spec["fixture_source"].encode("utf-8")) > MAX_SOURCE_BYTES:
            _fail(where + ".fixture_source", "must be bounded module source")
        consumes = spec["consumes"]
        if type(consumes) is not dict or len(consumes) > MAX_CONSUMES:
            _fail(where + ".consumes", "must map dependency names to contract ids")
        for name, dep_id in consumes.items():
            _text(name, where + ".consumes", 32, _KIND)
            _text(dep_id, where + ".consumes", 64, _CONTRACT_ID)
    return contracts


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate a workload manifest and optionally export its public worker contracts")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--public-out", type=Path, help="Write the public projection, safe to copy to a worker's --contracts-file")
    args = parser.parse_args(argv)
    try:
        workload = load_workload(args.manifest)
    except WorkloadError as exc:
        raise SystemExit(f"Invalid workload: {exc}") from None
    if args.public_out:
        args.public_out.write_text(json.dumps(workload.public_contracts(), indent=2) + "\n")
    print(json.dumps({"valid": True, "id": workload.id, "tasks": list(workload.tasks), "digest": workload.digest}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
