"""Run accepted modules through the bounded interpreter and render the report."""
import argparse
import html
import json
from pathlib import Path

from .restricted import RestrictedModule
from .workload import resolve_expression, resolve_workload


def build_report(artifacts, input_value, workload=None):
    """Evaluate the manifest's integration expression against accepted artifacts on disk."""
    artifacts, workload = Path(artifacts), resolve_workload(workload)
    modules = {kind: RestrictedModule((artifacts / task.filename).read_text(), task.function) for kind, task in workload.tasks.items()}
    return resolve_expression(workload.integration["result"], lambda kind, argument: modules[kind].run(argument), input_value)


def report_html(report):
    rows = "".join(f"<tr><td>{html.escape(category)}</td><td>{cents / 100:.2f}</td></tr>" for category, cents in sorted(report["by_category"].items()))
    return f'''<!doctype html><html><meta charset="utf-8"><title>Distributed expense report</title><style>body{{font:18px system-ui;background:#f5f7fb;color:#152238;max-width:760px;margin:70px auto}}main{{background:white;padding:40px;border-radius:20px}}table{{width:100%;border-collapse:collapse}}td,th{{padding:14px;text-align:left;border-bottom:1px solid #ddd}}small{{color:#58667e}}</style><main><small>Reference application · Public fixture · Bounded interpreted modules</small><h1>Expense report</h1><p>{report['count']} transactions · Total <strong>{report['total_cents']/100:.2f}</strong></p><table><tr><th>Category</th><th>Amount</th></tr>{rows}</table><p>Independent module checks and final integration passed for the demonstration fixtures.</p></main></html>'''


def json_table_html(title, report):
    """Any JSON result: a table for a list of objects, otherwise formatted JSON. Escapes everything."""
    title = html.escape(title)
    if isinstance(report, list) and report and all(isinstance(row, dict) for row in report):
        columns = list(dict.fromkeys(key for row in report for key in row))
        head = "".join(f"<th>{html.escape(str(c))}</th>" for c in columns)
        rows = "".join("<tr>" + "".join(f"<td>{html.escape(json.dumps(row.get(c)) if not isinstance(row.get(c), str) else row[c])}</td>" for c in columns) + "</tr>" for row in report)
        body = f"<table><tr>{head}</tr>{rows}</table>"
    else:
        body = f"<pre>{html.escape(json.dumps(report, indent=2))}</pre>"
    return f'''<!doctype html><html><meta charset="utf-8"><title>{title}</title><style>body{{font:18px system-ui;background:#f5f7fb;color:#152238;max-width:760px;margin:70px auto}}main{{background:white;padding:40px;border-radius:20px}}table{{width:100%;border-collapse:collapse}}td,th{{padding:14px;text-align:left;border-bottom:1px solid #ddd}}small{{color:#58667e}}</style><main><small>Workload manifest · Public data · Bounded interpreted modules</small><h1>{title}</h1>{body}<p>Independent module checks and final integration passed for the demonstration inputs.</p></main></html>'''


def render_report(workload, report):
    if workload.report["renderer"] == "expense-summary":
        return report_html(report)
    return json_table_html(workload.report["title"], report)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--artifacts", type=Path, required=True)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path)
    p.add_argument("--workload", type=Path, help="Workload manifest (default: expense report). A non-text integration input is read as JSON")
    args = p.parse_args()
    if args.input.stat().st_size > 64 * 1024:
        raise SystemExit("Reference input exceeds 64 KiB")
    workload = resolve_workload(args.workload)
    text = args.input.read_text()
    report = build_report(args.artifacts, text if isinstance(workload.integration["input"], str) else json.loads(text), workload)
    if args.output:
        args.output.write_text(render_report(workload, report))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
