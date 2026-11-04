"""Loopback stand-in for the Meta chat API, for free rehearsal and tests. NOT AI.

Returns prewritten output through the real provider, budget and repair code paths:
a task's declared first attempt, which fails acceptance, then its reference source once a
repair prompt arrives. Results are always labeled simulated-meta.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json

from .workload import resolve_workload


def _reply(body, workload):
    usage = {"prompt_tokens": 400, "completion_tokens": 200, "total_tokens": 600}
    if "response_format" in body:
        tasks = [{"kind": k, "depends_on": list(t.depends_on), "complexity": t.complexity, "rationale": "Simulated rationale, no AI"} for k, t in workload.tasks.items()]
        content = json.dumps({"tasks": tasks})
    else:
        text = body["messages"][-1]["content"]
        task = next(t for t in workload.tasks.values() if t.spec in text)
        repairing = "PREVIOUS ATTEMPT" in text
        content = task.simulated_first_attempt if task.simulated_first_attempt and not repairing else task.fixture_source
    return {"model": body["model"], "choices": [{"finish_reason": "stop", "message": {"content": content}}], "usage": usage}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        # Rehearsal evidence: tests inspect exactly what a model would have been sent.
        self.server.requests.append(body)
        raw = json.dumps(_reply(body, self.server.workload)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def create_server(workload=None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    server.workload = resolve_workload(workload)
    server.requests = []
    return server
