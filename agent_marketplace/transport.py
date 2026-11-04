"""Optional stdlib TLS for demo services and the listener-scope checks they share.

TLS here only covers bearer credentials and metadata on the wire between a client
and a service whose certificate it verified. It does not protect anything from the
service operator or a worker host, and it does not make any work confidential.
"""
from __future__ import annotations

from http.server import ThreadingHTTPServer
import ipaddress
import ssl

TLS_HANDSHAKE_SECONDS = 10


def is_loopback_host(host) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def caller_is_loopback(client_address) -> bool:
    """True when the connection's peer address is this machine's loopback.

    A reverse proxy on this machine makes every caller look local; see the runbook.
    """
    return is_loopback_host(client_address[0])


def server_tls_context(cert_file=None, key_file=None):
    """Return a server context, or None when TLS was not requested."""
    if not cert_file and not key_file:
        return None
    if not cert_file or not key_file:
        raise ValueError("TLS needs both a certificate and a key file")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(str(cert_file), str(key_file))
    except OSError:  # Includes ssl.SSLError; the message never contains key material.
        raise ValueError("Could not load the TLS certificate and key files") from None
    return context


def client_tls_context(ca_file=None):
    """Trust only ca_file, with hostname checks; None means normal system trust."""
    if not ca_file:
        return None
    try:
        context = ssl.create_default_context(cafile=str(ca_file))
    except OSError:
        raise ValueError("Could not load the TLS CA file") from None
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class TLSThreadingHTTPServer(ThreadingHTTPServer):
    """Handshake on the per-connection thread so a stalled client cannot block accept()."""

    def __init__(self, address, handler, context):
        self.tls_context = context
        super().__init__(address, handler)

    def finish_request(self, request, client_address):
        request.settimeout(TLS_HANDSHAKE_SECONDS)
        try:
            connection = self.tls_context.wrap_socket(request, server_side=True)
        except OSError:  # Plain HTTP, an untrusting client, or a stalled handshake.
            return
        try:
            self.RequestHandlerClass(connection, client_address, self)
        finally:
            connection.close()


def http_listener(address, handler, tls_context=None):
    if tls_context is None:
        return ThreadingHTTPServer(address, handler)
    return TLSThreadingHTTPServer(address, handler, tls_context)
