"""Durable, metadata-only marketplace for public demo work.

This service is an ordinary authenticated application. It does not verify any
hardware attestation and never treats worker claims as host confidentiality.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from .protocol import ApiError, CAPABILITY, MAX_BODY_BYTES, is_loopback_url, new_id, validate_identifier
from .transport import caller_is_loopback, http_listener, is_loopback_host, server_tls_context

WORKER_LIVE_SECONDS = 15
MAX_CREDITS = 1_000_000


def _fields(body, allowed, required=None):
    if not isinstance(body, dict):
        raise ApiError(400, "Expected a JSON object")
    if set(body) - set(allowed):
        raise ApiError(400, "Unexpected request fields")
    if required is not None and set(required) - set(body):
        raise ApiError(400, "Missing required request fields")
    return body


def _text(value, name, minimum=1, maximum=256):
    if not isinstance(value, str) or not minimum <= len(value) <= maximum or any(ord(c) < 32 for c in value):
        raise ApiError(400, f"Invalid {name}")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ApiError(400, f"Invalid {name}") from None
    return value


def _integer(value, name, minimum=1, maximum=MAX_CREDITS):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ApiError(400, f"Invalid {name}")
    return value


def _number(value, name, minimum=0, maximum=1e13):
    if type(value) not in (int, float) or not minimum <= value <= maximum or not math.isfinite(value):
        raise ApiError(400, f"Invalid {name}")
    return float(value)


def _digest(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ApiError(400, f"Invalid {name}")
    return value


def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _json(value):
    return json.dumps(value, separators=(",", ":"), allow_nan=False)


class MarketStore:
    """Serialized SQLite state machine; receipts, tokens and fencing survive restart."""

    def __init__(self, db_path, requester_token, enrollment_token, clock=time.time, *, allow_insecure_public_network=False):
        _text(requester_token, "requester credential", maximum=1024)
        _text(enrollment_token, "enrollment credential", maximum=1024)
        if hmac.compare_digest(requester_token, enrollment_token):
            raise ValueError("Requester and enrollment credentials must differ")
        self.clock = clock
        self.allow_insecure_public_network = allow_insecure_public_network
        self._requester_hash = _token_hash(requester_token)
        self._enrollment_hash = _token_hash(enrollment_token)
        self._lock = threading.RLock()
        path = os.fspath(db_path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
            os.chmod(path, 0o600)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS workers (
                worker_id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
                capabilities TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                slots INTEGER NOT NULL, last_seen REAL NOT NULL, registered_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, capability TEXT NOT NULL, sensitivity TEXT NOT NULL,
                max_credits INTEGER NOT NULL, expires_at REAL NOT NULL, state TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bids (
                bid_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
                worker_id TEXT NOT NULL REFERENCES workers(worker_id), credits INTEGER NOT NULL,
                created_at REAL NOT NULL, UNIQUE(task_id,worker_id)
            );
            CREATE TABLE IF NOT EXISTS attempts (
                attempt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
                bid_id TEXT NOT NULL REFERENCES bids(bid_id), worker_id TEXT NOT NULL REFERENCES workers(worker_id),
                credits INTEGER NOT NULL, lease_until REAL NOT NULL, status TEXT NOT NULL,
                requester_url TEXT NOT NULL, dispatch_token TEXT NOT NULL, created_at REAL NOT NULL,
                ended_at REAL, reason TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_attempt ON attempts(task_id) WHERE status='assigned';
            CREATE TABLE IF NOT EXISTS receipts (
                receipt_id TEXT PRIMARY KEY, task_id TEXT UNIQUE NOT NULL REFERENCES tasks(task_id),
                attempt_id TEXT UNIQUE NOT NULL REFERENCES attempts(attempt_id),
                worker_id TEXT NOT NULL REFERENCES workers(worker_id), credits INTEGER NOT NULL,
                artifact_sha256 TEXT NOT NULL, report_sha256 TEXT NOT NULL, accepted_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL,
                kind TEXT NOT NULL, task_id TEXT, worker_id TEXT, attempt_id TEXT
            );
        """)

    def close(self):
        with self._lock:
            self.db.close()

    @contextmanager
    def _transaction(self):
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            else:
                self.db.execute("COMMIT")

    def _event(self, kind, task_id=None, worker_id=None, attempt_id=None):
        self.db.execute("INSERT INTO events(time,kind,task_id,worker_id,attempt_id) VALUES(?,?,?,?,?)", (self.clock(), kind, task_id, worker_id, attempt_id))

    def _expire(self):
        now = self.clock()
        rows = self.db.execute("SELECT attempt_id,task_id,worker_id FROM attempts WHERE status='assigned' AND lease_until<=?", (now,)).fetchall()
        for row in rows:
            self.db.execute("UPDATE attempts SET status='expired',ended_at=?,reason='lease expired',dispatch_token='' WHERE attempt_id=?", (now, row["attempt_id"]))
            self.db.execute("UPDATE tasks SET state='open' WHERE task_id=? AND state='assigned'", (row["task_id"],))
            self._event("lease_expired", row["task_id"], row["worker_id"], row["attempt_id"])

    def _require_role(self, token, role):
        expected = self._requester_hash if role == "requester" else self._enrollment_hash
        if not isinstance(token, str) or not hmac.compare_digest(_token_hash(token), expected):
            raise ApiError(401, "Unauthorized")

    def _worker(self, token, expected_id=None):
        if not isinstance(token, str):
            raise ApiError(401, "Unauthorized")
        row = self.db.execute("SELECT * FROM workers WHERE token_hash=?", (_token_hash(token),)).fetchone()
        if row is None:
            raise ApiError(401, "Unauthorized")
        if expected_id is not None and row["worker_id"] != expected_id:
            raise ApiError(403, "Worker identity mismatch")
        return row

    def _task(self, task_id):
        row = self.db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise ApiError(404, "Task not found")
        return row

    def _eligible(self, worker, task):
        if task["sensitivity"] != "public":
            return False
        if self.clock() - worker["last_seen"] >= WORKER_LIVE_SECONDS:
            return False
        if task["capability"] not in json.loads(worker["capabilities"]):
            return False
        active = self.db.execute("SELECT COUNT(*) FROM attempts WHERE worker_id=? AND status='assigned'", (worker["worker_id"],)).fetchone()[0]
        return active < worker["slots"]

    def _assignment(self, task_id, *, include_secret=False):
        row = self.db.execute("SELECT * FROM attempts WHERE task_id=? AND status='assigned'", (task_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        if not include_secret:
            result.pop("dispatch_token", None)
            result.pop("requester_url", None)
        return result

    def _validate_requester_url(self, value):
        value = _text(value, "requester_url", maximum=2048).rstrip("/")
        try:
            parsed = urlsplit(value)
            if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path or parsed.scheme not in ("https", "http"):
                raise ValueError()
            parsed.port
        except ValueError:
            raise ApiError(400, "Invalid requester URL") from None
        if parsed.scheme != "https" and not is_loopback_url(value) and not self.allow_insecure_public_network:
            raise ApiError(400, "Remote requester requires HTTPS; insecure public-data override is disabled")
        return value

    def handle(self, method, path, token=None, body=None):
        """Handle one v1 request; unknown fields cannot enter durable metadata."""
        path = urlsplit(path).path
        parts = path.strip("/").split("/")
        if method == "GET" and path == "/health":
            return {"ok": True, "mode": "public-demo", "confidential_execution": False}
        if method == "GET" and path == "/v1/snapshot":
            return self.snapshot()
        with self._transaction():
            self._expire()
            if method == "POST" and path == "/v1/workers/register":
                self._require_role(token, "enrollment")
                return self._register(body)
            if parts[:2] == ["v1", "workers"] and len(parts) == 4:
                worker_id = validate_identifier(parts[2], "worker_id")
                worker = self._worker(token, worker_id)
                if method == "POST" and parts[3] == "heartbeat":
                    _fields(body, [])
                    self.db.execute("UPDATE workers SET last_seen=? WHERE worker_id=?", (self.clock(), worker_id))
                    return {"ok": True}
                if method == "GET" and parts[3] == "assignments":
                    rows = self.db.execute("SELECT task_id,attempt_id,lease_until,requester_url,dispatch_token FROM attempts WHERE worker_id=? AND status='assigned' ORDER BY created_at", (worker_id,)).fetchall()
                    return {"assignments": [dict(row) for row in rows]}
            if path == "/v1/tasks":
                if method == "GET":
                    worker = self._worker(token)
                    rows = self.db.execute("SELECT * FROM tasks WHERE state='open' AND expires_at>? ORDER BY created_at", (self.clock(),)).fetchall()
                    return {"tasks": [dict(row) for row in rows if self._eligible(worker, row)]}
                if method == "POST":
                    self._require_role(token, "requester")
                    return self._create_task(body)
            if parts[:2] == ["v1", "tasks"] and len(parts) in (3, 4):
                task_id = validate_identifier(parts[2], "task_id")
                if len(parts) == 4 and parts[3] == "bids" and method == "POST":
                    worker = self._worker(token)
                    return self._bid(self._task(task_id), worker, body)
                self._require_role(token, "requester")
                task = self._task(task_id)
                if method == "GET" and len(parts) == 3:
                    return {"task": dict(task), "assignment": self._assignment(task_id)}
                if len(parts) == 4:
                    action = parts[3]
                    if method == "GET" and action == "bids":
                        if task["state"] != "open" or task["expires_at"] <= self.clock():
                            return {"bids": []}
                        bids = self.db.execute("SELECT * FROM bids WHERE task_id=? ORDER BY credits,created_at,bid_id", (task_id,)).fetchall()
                        return {"bids": [dict(bid) for bid in bids if self._eligible(self.db.execute("SELECT * FROM workers WHERE worker_id=?", (bid["worker_id"],)).fetchone(), task)]}
                    if method == "POST" and action == "assign":
                        return self._assign(task, body)
                    if method == "POST" and action == "accept":
                        return self._accept(task, body)
                    if method == "POST" and action == "reject":
                        return self._reject(task, body)
            raise ApiError(404, "Endpoint not found")

    def _register(self, body):
        _fields(body, ["worker_id", "capabilities", "provider", "model", "slots"], ["worker_id", "capabilities", "provider", "model", "slots"])
        worker_id = validate_identifier(body["worker_id"], "worker_id")
        capabilities = body["capabilities"]
        if not isinstance(capabilities, list) or not 1 <= len(capabilities) <= 16 or any(not isinstance(c, str) for c in capabilities):
            raise ApiError(400, "Invalid capabilities")
        capabilities = sorted(set(validate_identifier(c, "capability") for c in capabilities))
        provider = _text(body["provider"], "provider", maximum=64)
        model = _text(body["model"], "model", maximum=128)
        slots = _integer(body["slots"], "slots", maximum=16)
        if self.db.execute("SELECT 1 FROM workers WHERE worker_id=?", (worker_id,)).fetchone():
            raise ApiError(409, "Worker identity already enrolled")
        if self.db.execute("SELECT COUNT(*) FROM workers").fetchone()[0] >= 128:
            raise ApiError(409, "Demo worker limit reached")
        worker_token = secrets.token_urlsafe(32)
        now = self.clock()
        self.db.execute("INSERT INTO workers VALUES(?,?,?,?,?,?,?,?)", (worker_id, _token_hash(worker_token), _json(capabilities), provider, model, slots, now, now))
        self._event("worker_registered", worker_id=worker_id)
        return {"worker_id": worker_id, "worker_token": worker_token}

    def _create_task(self, body):
        fields = ["task_id", "capability", "sensitivity", "max_credits", "expires_at"]
        _fields(body, fields, fields)
        task_id = validate_identifier(body["task_id"], "task_id")
        capability = validate_identifier(body["capability"], "capability")
        if body["sensitivity"] not in ("public", "confidential"):
            raise ApiError(400, "Invalid sensitivity")
        credits = _integer(body["max_credits"], "max_credits")
        expires_at = _number(body["expires_at"], "expires_at")
        if expires_at <= self.clock():
            raise ApiError(400, "Task deadline must be in the future")
        if self.db.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
            raise ApiError(409, "Task already exists")
        if self.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] >= 512:
            raise ApiError(409, "Demo task limit reached")
        self.db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?)", (task_id, capability, body["sensitivity"], credits, expires_at, "open", self.clock()))
        self._event("task_advertised", task_id=task_id)
        return dict(self._task(task_id))

    def _bid(self, task, worker, body):
        _fields(body, ["credits"], ["credits"])
        credits = _integer(body["credits"], "credits")
        if task["sensitivity"] != "public":
            raise ApiError(403, "No verified protected backend: confidential work is disabled")
        if task["state"] != "open" or task["expires_at"] <= self.clock():
            raise ApiError(409, "Task is not open")
        if not self._eligible(worker, task):
            raise ApiError(409, "Worker is not live, capable, or available")
        if credits > task["max_credits"]:
            raise ApiError(400, "Bid exceeds task budget")
        old = self.db.execute("SELECT * FROM bids WHERE task_id=? AND worker_id=?", (task["task_id"], worker["worker_id"])).fetchone()
        if old and old["credits"] == credits:
            return dict(old)
        if old:
            self.db.execute("UPDATE bids SET credits=?,created_at=? WHERE bid_id=?", (credits, self.clock(), old["bid_id"]))
            bid_id = old["bid_id"]
        else:
            bid_id = new_id("bid")
            self.db.execute("INSERT INTO bids VALUES(?,?,?,?,?)", (bid_id, task["task_id"], worker["worker_id"], credits, self.clock()))
        self._event("bid_received", task_id=task["task_id"], worker_id=worker["worker_id"])
        return dict(self.db.execute("SELECT * FROM bids WHERE bid_id=?", (bid_id,)).fetchone())

    def _assign(self, task, body):
        fields = ["bid_id", "attempt_id", "lease_seconds", "requester_url", "dispatch_token"]
        _fields(body, fields, fields)
        bid_id = validate_identifier(body["bid_id"], "bid_id")
        attempt_id = validate_identifier(body["attempt_id"], "attempt_id")
        seconds = _number(body["lease_seconds"], "lease_seconds", minimum=0.1, maximum=3600)
        requester_url = self._validate_requester_url(body["requester_url"])
        dispatch_token = _text(body["dispatch_token"], "dispatch token", minimum=16, maximum=1024)
        if task["sensitivity"] != "public":
            raise ApiError(403, "No verified protected backend: confidential work is disabled")
        if task["state"] != "open" or task["expires_at"] <= self.clock():
            raise ApiError(409, "Task is not open")
        if self.db.execute("SELECT 1 FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone():
            raise ApiError(409, "Attempt identity has already been used")
        bid = self.db.execute("SELECT * FROM bids WHERE bid_id=? AND task_id=?", (bid_id, task["task_id"])).fetchone()
        if bid is None:
            raise ApiError(404, "Bid not found for task")
        worker = self.db.execute("SELECT * FROM workers WHERE worker_id=?", (bid["worker_id"],)).fetchone()
        if not self._eligible(worker, task) or bid["credits"] > task["max_credits"]:
            raise ApiError(409, "Bid is no longer eligible")
        lease_until = min(self.clock() + seconds, task["expires_at"])
        self.db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (attempt_id, task["task_id"], bid_id, bid["worker_id"], bid["credits"], lease_until, "assigned", requester_url, dispatch_token, self.clock(), None, None))
        self.db.execute("UPDATE tasks SET state='assigned' WHERE task_id=?", (task["task_id"],))
        self._event("attempt_assigned", task["task_id"], bid["worker_id"], attempt_id)
        return self._assignment(task["task_id"])

    def _accept(self, task, body):
        fields = ["attempt_id", "artifact_sha256", "report_sha256", "receipt_id"]
        _fields(body, fields, fields)
        attempt_id = validate_identifier(body["attempt_id"], "attempt_id")
        receipt_id = validate_identifier(body["receipt_id"], "receipt_id")
        artifact_hash = _digest(body["artifact_sha256"], "artifact_sha256")
        report_hash = _digest(body["report_sha256"], "report_sha256")
        previous = self.db.execute("SELECT * FROM receipts WHERE task_id=?", (task["task_id"],)).fetchone()
        if previous is not None:
            if previous["attempt_id"] == attempt_id and previous["receipt_id"] == receipt_id and previous["artifact_sha256"] == artifact_hash and previous["report_sha256"] == report_hash:
                return {"accepted": True, "duplicate": True, "receipt": dict(previous)}
            raise ApiError(409, "Task already has a different acceptance receipt")
        attempt = self.db.execute("SELECT * FROM attempts WHERE attempt_id=? AND task_id=?", (attempt_id, task["task_id"])).fetchone()
        if attempt is None or attempt["status"] != "assigned" or task["state"] != "assigned":
            raise ApiError(409, "Attempt is stale or not authorized")
        if self.db.execute("SELECT 1 FROM receipts WHERE receipt_id=?", (receipt_id,)).fetchone():
            raise ApiError(409, "Receipt identity already used")
        now = self.clock()
        self.db.execute("INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?)", (receipt_id, task["task_id"], attempt_id, attempt["worker_id"], attempt["credits"], artifact_hash, report_hash, now))
        self.db.execute("UPDATE attempts SET status='accepted',ended_at=?,dispatch_token='' WHERE attempt_id=?", (now, attempt_id))
        self.db.execute("UPDATE tasks SET state='accepted' WHERE task_id=?", (task["task_id"],))
        self._event("task_accepted", task["task_id"], attempt["worker_id"], attempt_id)
        receipt = dict(self.db.execute("SELECT * FROM receipts WHERE receipt_id=?", (receipt_id,)).fetchone())
        return {"accepted": True, "duplicate": False, "receipt": receipt}

    def _reject(self, task, body):
        _fields(body, ["attempt_id", "reason"], ["attempt_id", "reason"])
        attempt_id = validate_identifier(body["attempt_id"], "attempt_id")
        _text(body["reason"], "reason", maximum=256)
        attempt = self.db.execute("SELECT * FROM attempts WHERE attempt_id=? AND task_id=?", (attempt_id, task["task_id"])).fetchone()
        if attempt is None or attempt["status"] != "assigned" or task["state"] != "assigned":
            raise ApiError(409, "Attempt is stale or not authorized")
        # Diagnostics stay at the requester: metadata records only a fixed code.
        self.db.execute("UPDATE attempts SET status='rejected',ended_at=?,reason='requester rejected',dispatch_token='' WHERE attempt_id=?", (self.clock(), attempt_id))
        self.db.execute("UPDATE tasks SET state='open' WHERE task_id=?", (task["task_id"],))
        self._event("attempt_rejected", task["task_id"], attempt["worker_id"], attempt_id)
        return {"ok": True}

    def snapshot(self):
        with self._transaction():
            self._expire()
            now = self.clock()
            workers = []
            for row in self.db.execute("SELECT worker_id,capabilities,provider,model,slots,last_seen,registered_at FROM workers ORDER BY registered_at LIMIT 128"):
                worker = dict(row)
                worker["capabilities"] = json.loads(worker["capabilities"])
                worker["live"] = now - worker["last_seen"] < WORKER_LIVE_SECONDS
                worker["security_mode"] = "ordinary-public"
                workers.append(worker)
            tasks = [dict(row) for row in self.db.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 100")]
            bids = [dict(row) for row in self.db.execute("SELECT * FROM bids ORDER BY created_at DESC LIMIT 100")]
            attempts = [dict(row) for row in self.db.execute("SELECT attempt_id,task_id,bid_id,worker_id,credits,lease_until,status,created_at,ended_at,reason FROM attempts ORDER BY created_at DESC LIMIT 100")]
            receipts = [dict(row) for row in self.db.execute("SELECT * FROM receipts ORDER BY accepted_at DESC LIMIT 100")]
            events = [dict(row) for row in self.db.execute("SELECT * FROM events ORDER BY sequence DESC LIMIT 100")]
            reserved = self.db.execute("SELECT COALESCE(SUM(credits),0) FROM attempts WHERE status='assigned'").fetchone()[0]
            settled = self.db.execute("SELECT COALESCE(SUM(credits),0) FROM receipts").fetchone()[0]
            return {"mode": "public-demo", "confidential_execution": False, "workers": workers, "tasks": tasks, "bids": bids, "attempts": attempts, "receipts": receipts, "events": events, "credits": {"reserved": reserved, "settled": settled}, "snapshot_limit": 100}


class MarketHandler(BaseHTTPRequestHandler):
    server_version = "AgentMarket/0.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        # Request paths/headers may carry sensitive data; never echo them.
        pass

    def _send(self, status, body):
        raw = _json(body).encode("utf-8")
        if len(raw) > MAX_BODY_BYTES:
            status, raw = 413, b'{"error":"Snapshot too large; reduce demo workload"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        if self.headers.get("Transfer-Encoding") is not None:
            raise ApiError(400, "Transfer encoding is unsupported")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
            raise ApiError(411, "A single content length is required")
        length = int(lengths[0])
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "Request body too large")
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            raise ApiError(415, "JSON content type required")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ApiError(400, "Incomplete request body")
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result
        try:
            value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite number")))
        except (ValueError, UnicodeError, RecursionError):
            raise ApiError(400, "Invalid JSON") from None
        if not isinstance(value, dict):
            raise ApiError(400, "Expected a JSON object")
        return value

    def _handle(self, method):
        try:
            auth_values = self.headers.get_all("Authorization", [])
            if len(auth_values) > 1:
                raise ApiError(401, "Unauthorized")
            auth = auth_values[0] if auth_values else ""
            token = auth[7:] if auth.startswith("Bearer ") else None
            if self.server.restrict_snapshot and method == "GET" and urlsplit(self.path).path == "/v1/snapshot" and not caller_is_loopback(self.client_address):
                self.server.store._require_role(token, "requester")
            body = self._body() if method == "POST" else None
            result = self.server.store.handle(method, self.path, token, body)
            self._send(200, result)
        except ApiError as exc:
            self._send(exc.status, {"error": exc.message})
        except (TimeoutError, ConnectionError, BrokenPipeError):
            return
        except Exception:
            self._send(500, {"error": "Internal marketplace error"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._send(405, {"error": "Method not allowed"})

    do_DELETE = do_PUT
    do_PATCH = do_PUT


def create_server(store, host="127.0.0.1", port=0, tls_context=None):
    loopback = is_loopback_host(host)
    if not loopback and tls_context is None and not store.allow_insecure_public_network:
        raise ValueError("Non-loopback HTTP requires TLS or the explicit insecure public-data override")
    server = http_listener((host, port), MarketHandler, tls_context)
    server.daemon_threads = True
    # Beyond loopback, the snapshot needs the requester credential unless the caller is local.
    server.restrict_snapshot = not loopback
    server.store = store
    return server


def main():
    parser = argparse.ArgumentParser(description="Run ordinary public-work demo marketplace")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=".demo/market.sqlite3")
    parser.add_argument("--requester-token-file", required=True)
    parser.add_argument("--enrollment-token-file", required=True)
    parser.add_argument("--allow-insecure-public-network", action="store_true", help="Public data only: expose HTTP without transport confidentiality")
    parser.add_argument("--tls-cert", type=Path, help="Serve HTTPS with this certificate chain")
    parser.add_argument("--tls-key", type=Path, help="Private key for --tls-cert")
    args = parser.parse_args()
    try:
        tls_context = server_tls_context(args.tls_cert, args.tls_key)
    except ValueError as exc:
        parser.error(str(exc))
    store = MarketStore(args.db, Path(args.requester_token_file).read_text().strip(), Path(args.enrollment_token_file).read_text().strip(), allow_insecure_public_network=args.allow_insecure_public_network)
    try:
        server = create_server(store, args.host, args.port, tls_context=tls_context)
        print(f"Public demo marketplace listening on {'https' if tls_context else 'http'}://{args.host}:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        store.close()


if __name__ == "__main__":
    main()
