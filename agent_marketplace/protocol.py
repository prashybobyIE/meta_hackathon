"""Small shared JSON transport and protocol helpers; no secret-bearing logging."""
from __future__ import annotations

import hashlib
from http.client import HTTPException
import ipaddress
import json
import math
import re
import ssl
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request

MAX_BODY_BYTES = 128 * 1024
CAPABILITY = "python-module-v1"
TLS_VERIFY_FAILED = 495  # Local client status; retrying cannot fix an untrusted certificate.


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def decode_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError("Nonfinite JSON constant")
    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("Nonfinite JSON number")
        return result
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant, parse_float=finite_float)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_hash(value) -> str:
    return sha256_text(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False))


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def validate_identifier(value, field: str = "id") -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", value):
        raise ApiError(400, f"Invalid {field}")
    return value


def is_loopback_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
            return False
        host = parsed.hostname
        if host == "localhost":
            return True
        return bool(host and ipaddress.ip_address(host).is_loopback)
    except ValueError:
        return False


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ApiError(400, "Redirects are disabled for authenticated protocol requests")


def json_request(method: str, url: str, body=None, token=None, timeout=5, ssl_context=None):
    """JSON request with bounded responses and no credential-bearing redirects.

    ssl_context applies to this call only, so a development CA never becomes
    trusted for other destinations such as a hosted model API.
    """
    from urllib.request import build_opener
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError()
        parsed.port
        data = None if body is None else json.dumps(body, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ApiError(400, "Invalid protocol URL or JSON body") from None
    if data is not None and len(data) > MAX_BODY_BYTES:
        raise ApiError(413, "Request body too large")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        if not isinstance(token, str) or len(token) > 4096 or any(c in token for c in "\r\n"):
            raise ApiError(400, "Invalid credential format")
        headers["Authorization"] = "Bearer " + token
    try:
        req = Request(url, data=data, headers=headers, method=method)
        handlers = [_NoRedirect()]
        if is_loopback_url(url):
            handlers.append(ProxyHandler({}))
        if ssl_context is not None:
            handlers.append(HTTPSHandler(context=ssl_context))
        with build_opener(*handlers).open(req, timeout=timeout) as response:
            raw = response.read(MAX_BODY_BYTES + 1)
            if len(raw) > MAX_BODY_BYTES:
                raise ApiError(413, "Response body too large")
            result = decode_json(raw)
            if not isinstance(result, dict):
                raise ApiError(502, "Expected a JSON object response")
            return result
    except HTTPError as exc:
        try:
            detail = decode_json(exc.read(4096)).get("error", "HTTP request failed")
        except (ValueError, AttributeError, OSError, TimeoutError, RecursionError):
            detail = "HTTP request failed"
        finally:
            exc.close()
        raise ApiError(exc.code, str(detail)[:300]) from None
    except (URLError, TimeoutError, OSError, HTTPException) as exc:
        if isinstance(getattr(exc, "reason", exc), ssl.SSLCertVerificationError):
            raise ApiError(TLS_VERIFY_FAILED, "TLS certificate verification failed") from exc
        raise ApiError(503, "Protocol endpoint unavailable") from exc
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ApiError(502, "Invalid JSON response") from exc
