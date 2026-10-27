"""Per-endpoint TLS preflight policy; never changes the system trust store.

An approved leaf pin is checked on a separate, credential-free TLS connection.
The decision is NOT engine-side certificate pinning: a subsequent CSDK
connection is a different connection and may observe a different peer. The
caller must bind the decision to the exact gateway/port and current login;
this module does not close that out-of-band/engine binding gap.
"""
from dataclasses import dataclass
import hashlib
import hmac
import math
import re
import socket
import ssl


class CertificatePolicyError(RuntimeError):
    """Certificate verification failed without exposing peer certificate data."""


@dataclass(frozen=True, repr=False)
class CertificateDecision:
    gateway: str
    port: int
    sha256: str
    verify_in_engine: bool

    def __repr__(self):
        # Do not accidentally print an endpoint or any peer certificate data.
        return f"CertificateDecision(verify_in_engine={self.verify_in_engine!r})"

    def allows_override_for(self, gateway, port):
        """Only an explicitly pinned decision for this exact endpoint qualifies.

        This check does not establish that a later engine connection has the
        same certificate; see the module's connection-binding limitation.
        """
        return (not self.verify_in_engine and gateway == self.gateway
                and type(port) is int and port == self.port)


def verify_gateway(gateway, port, approved_sha256=None, timeout=8):
    """Verify a gateway TLS leaf without sending VPN credentials.

    Without a pin, require the default CA chain and hostname validation and
    leave validation enabled in the engine. With an explicit 64-hex SHA256 pin,
    inspect that endpoint's leaf and require an exact constant-time match;
    only that decision may request an exception for the current engine login.
    A failure never falls back to another verification policy.
    """
    if (not isinstance(gateway, str) or not gateway or "\0" in gateway
            or any(character.isspace() for character in gateway)):
        raise ValueError("gateway must be a non-empty hostname or IP address")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("timeout must be a positive finite number")
    if approved_sha256 is not None and (
            not isinstance(approved_sha256, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", approved_sha256) is None):
        raise ValueError("approved_sha256 must contain exactly 64 hexadecimal characters")

    pinned = approved_sha256 is not None
    try:
        if pinned:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        else:
            context = ssl.create_default_context()
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
        with socket.create_connection((gateway, port), timeout=timeout) as connection:
            with context.wrap_socket(connection, server_hostname=gateway) as peer:
                leaf = peer.getpeercert(binary_form=True)
                if not isinstance(leaf, bytes) or not leaf:
                    raise CertificatePolicyError("gateway certificate verification failed")
                digest = hashlib.sha256(leaf).digest()
                if pinned and not hmac.compare_digest(digest, bytes.fromhex(approved_sha256)):
                    raise CertificatePolicyError("gateway certificate verification failed")
        return CertificateDecision(gateway, port, digest.hex(), not pinned)
    except (OSError, ssl.SSLError, ValueError, CertificatePolicyError):
        # Neither the original SSL exception nor raw peer metadata are included.
        raise CertificatePolicyError("gateway certificate verification failed") from None
