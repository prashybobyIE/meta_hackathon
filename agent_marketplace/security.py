"""Fail-closed policy seam. No hardware attestation verifier is installed yet."""
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

from .protocol import ApiError, is_loopback_url


@dataclass(frozen=True)
class ExecutionDecision:
    allowed: bool
    mode: str
    reason: str
    host_confidentiality_verified: bool = False

    def to_dict(self):
        return asdict(self)


def assess_execution(sensitivity: str, evidence=None) -> ExecutionDecision:
    if sensitivity == "public":
        return ExecutionDecision(True, "public-development", "Public work; ordinary host process, no host-confidentiality claim")
    if sensitivity == "confidential":
        return ExecutionDecision(False, "unavailable", "No genuine protected backend and attestation verifier are configured")
    return ExecutionDecision(False, "invalid", "Unknown sensitivity; classification cannot be inferred")


def require_public_work(sensitivity: str) -> None:
    decision = assess_execution(sensitivity)
    if not decision.allowed:
        raise ApiError(403, decision.reason)


def validate_transport_url(url: str, allow_insecure_public_network=False) -> str:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or p.fragment or p.query:
        raise ApiError(400, "Invalid service URL")
    if p.scheme == "http" and not is_loopback_url(url) and not allow_insecure_public_network:
        raise ApiError(400, "Remote protocol traffic requires HTTPS; HTTP is loopback development only")
    return url.rstrip("/")
