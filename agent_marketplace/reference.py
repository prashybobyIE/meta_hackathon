"""The default expense-report workload under its long-standing names.

The data lives in workloads/expense_report/manifest.json and stays on the requester.
"""
from .workload import default_workload

_workload = default_workload()

CONTRACTS = _workload.contracts
DEPENDENCIES = {kind: list(task.depends_on) for kind, task in _workload.tasks.items()}
PUBLIC_CSV = _workload.integration["input"]
EXPECTED_REPORT = _workload.integration["expect"]
PUBLIC_CASE_COUNTS = {kind: _workload.public_case_count(kind) for kind in _workload.tasks}


def acceptance_cases(kind):
    if kind not in _workload.tasks:
        raise ValueError("Unknown reference module")
    return [(case.input, case.expect, case.raises) for case in _workload.cases(kind)]
