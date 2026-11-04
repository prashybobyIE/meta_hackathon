"""Requester-owned acceptance checks using the restricted reference runtime."""
import copy
import json

from .protocol import canonical_hash, sha256_text
from .restricted import RestrictedError, RestrictedModule
from .workload import conforms, resolve_expression, resolve_workload


def _case_feedback(public_count, index, argument, detail):
    """Repair feedback: a public case is quoted, a hidden case is only counted."""
    if index >= public_count:
        return "A hidden acceptance case failed; its details are withheld."
    return f"Public case {index} failed. Input: {json.dumps(argument)[:500]}. {detail}"[:1500]


def _dependency_caller(task, workload, dependencies):
    """Run consumed artifacts, or return None unless each one is present and intact."""
    modules = {}
    for name in task.consumes:
        item = (dependencies or {}).get(name)
        if not isinstance(item, dict) or not isinstance(item.get("content"), str) or item.get("sha256") != sha256_text(item["content"]):
            return None
        modules[name] = RestrictedModule(item["content"], workload.tasks[name].function)
    return lambda name, argument: modules[name].run(argument)


def verify_artifact(kind, artifact, workload=None, dependencies=None):
    """`dependencies` holds the accepted artifacts of the tasks this one consumes."""
    workload = resolve_workload(workload)
    task, contract = workload.tasks[kind], workload.contracts[kind]
    cases, public_count = workload.cases(kind), workload.public_case_count(kind)
    content = artifact.get("content")
    report = {"contract_id": contract["id"], "passed": False, "cases_passed": 0, "cases_total": len(cases), "runtime": "bounded-python-subset-v1"}
    try:
        valid_digest = isinstance(content, str) and artifact.get("sha256") == sha256_text(content)
    except UnicodeError:
        valid_digest = False
    if not valid_digest or artifact.get("filename") != contract["filename"]:
        report["reason"] = "Artifact identity or digest mismatch"
        return report
    try:
        call = _dependency_caller(task, workload, dependencies)
    except (RestrictedError, UnicodeError):
        call = None
    if call is None:
        report["reason"] = "Accepted dependency artifacts are unavailable"
        return report
    index, argument = 0, None
    try:
        module = RestrictedModule(content, contract["function"])
        for index, case in enumerate(cases):
            expected, should_raise = case.expect, case.raises
            try:
                argument = resolve_expression(copy.deepcopy(case.input), call)
            except (ValueError, TypeError, ArithmeticError, KeyError, IndexError, RecursionError):
                # Not the fault of the module under test, and no case data goes back.
                report["reason"] = "An accepted dependency failed on an acceptance input"
                return report
            untouched = copy.deepcopy(argument)
            try:
                result = module.run(argument)
            except RestrictedError:
                raise
            except (ValueError, TypeError) as exc:
                if not should_raise:
                    report["reason"] = "Valid input was rejected"
                    report["diagnostic"] = _case_feedback(public_count, index, argument, f"Expected {json.dumps(expected)[:500]}, but the module raised {type(exc).__name__}: {str(exc)[:200]}")
                    return report
            else:
                shape_ok = conforms(task.output, result)
                if should_raise or not shape_ok or canonical_hash(result) != canonical_hash(expected):
                    report["reason"] = "Contract behavior did not match independent expectation"
                    wanted = "a ValueError" if should_raise else json.dumps(expected)[:500]
                    report["diagnostic"] = _case_feedback(public_count, index, argument, f"Expected {wanted}, got {json.dumps(result, default=str)[:500]}")
                    return report
                # Enforce JSON representability and bounded, plain artifacts.
                json.dumps(result, allow_nan=False)
            if argument != untouched or module.last_argument != untouched:
                report["reason"] = "Input mutation detected"
                report["diagnostic"] = "The module mutated its input; return copies instead."
                return report
            report["cases_passed"] += 1
        report["passed"] = True
    except RestrictedError as exc:
        # These messages name syntax or runtime limits, not acceptance-case data.
        report["reason"] = "Source rejected by bounded runtime or contract checks"
        report["diagnostic"] = f"Bounded runtime rejected the source: {str(exc)[:300]}"
    except (ValueError, TypeError, ArithmeticError, KeyError, IndexError, RecursionError) as exc:
        report["reason"] = "Source rejected by bounded runtime or contract checks"
        report["diagnostic"] = _case_feedback(public_count, index, argument, f"Unhandled {type(exc).__name__}.")
    return report


def report_hash(report):
    return canonical_hash(report)
