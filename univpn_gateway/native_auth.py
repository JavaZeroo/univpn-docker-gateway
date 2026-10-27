"""Single-attempt native authentication, independent of GUI and forwarding.

The runtime owns the engine lifetime. A failed, cancelled, or expired attempt
requires a fresh engine; this controller never replays a password or requests
another SMS. Authentication and tunnel-start replies are not data-plane proof.
"""
import copy
import hmac
import math
import re
import struct
import threading
import time
import uuid

from .certificate_policy import CertificatePolicyError, verify_gateway
from .challenge import AuthAttempt, SessionError, State
from .native_messages import (AUTH_OK, SMS_REQUIRED, TUNNEL_START_OK,
                              first_connect, fixed_text, password_login,
                              sms_code, tunnel_start)


class NativeAuthError(RuntimeError):
    """A sanitized authentication/control failure."""


class RestartRequired(NativeAuthError):
    """The native authentication context must not be reused."""


def _target_probe(config):
    # The existing probe also verifies the target route and VPN interface.
    # Import lazily so the state controller remains usable off Linux in tests.
    from .cli import probe
    return probe(config)


def _link_select(gateway, port):
    # The pinned amd64 ABI uses little-endian native fields for this payload.
    return (struct.pack("<I", 0) + fixed_text(gateway, 128, "gateway")
            + struct.pack("<I", port))


def _metadata(event):
    keys = ("source", "destination", "connection_type", "class", "code", "length")
    clean = {key: event[key] for key in keys
             if type(event.get(key)) is int and 0 <= event[key] <= 0xFFFFFFFF}
    received = event.get("received_at")
    if (type(received) in (int, float) and math.isfinite(received)
            and received >= 0):
        clean["received_at"] = received
    return clean


class NativeAuth:
    """Own one explicit login and one possible continuation on the same engine.

    Only ephemeral local variables hold credentials and encoded payloads.
    Status contains state/response metadata, never credentials or raw events.
    An approved certificate decision is scoped to this endpoint and login; its
    independent TLS preflight is not engine-side certificate pinning.
    """

    def __init__(self, engine, config, verifier=verify_gateway, probe=_target_probe,
                 *, clock=time.monotonic):
        self._engine = engine
        self._config = copy.deepcopy(config)
        self._verifier = verifier
        self._probe = probe
        self._clock = clock
        self._lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._probe_lock = threading.Lock()
        self._attempt = None
        self._busy = False
        self._tunnel_started = False
        self._data_plane_ready = False
        self._error = None
        self._last_response = None

    def _alive(self):
        try:
            return self._engine.alive is True
        except Exception:
            return False

    def _snapshot(self):
        snapshot = self._attempt.snapshot() if self._attempt else None
        if snapshot and snapshot.state == State.EXPIRED:
            self._data_plane_ready = False
            self._tunnel_started = False
            self._error = "authentication_session_expired"
        return snapshot

    def _events(self):
        try:
            events = self._engine.event_metadata()
        except Exception:
            return []
        if not isinstance(events, (list, tuple)):
            return []
        clean = []
        for event in events[-64:]:
            if not isinstance(event, dict):
                continue
            metadata = _metadata(event)
            if metadata:
                clean.append(metadata)
        return clean

    def status(self):
        """Return sanitized metadata without running a target probe."""
        alive = self._alive()
        with self._lock:
            snapshot = self._snapshot()
            if not alive:
                self._data_plane_ready = False
            state = snapshot.state.value if snapshot else "idle"
            result = {
                "state": state,
                "attempt_id": snapshot.attempt_id if snapshot else None,
                "challenge_id": snapshot.challenge_id if snapshot else None,
                "expires_in": max(0, math.ceil(snapshot.deadline - self._clock())) if snapshot else 0,
                "authenticated": bool(snapshot and snapshot.state == State.AUTHENTICATED),
                "tunnel_started": self._tunnel_started,
                "engine_alive": alive,
                "data_plane_ready": self._data_plane_ready,
                "operation_in_progress": self._busy,
                "requires_restart": bool(snapshot and snapshot.state in (
                    State.FAILED, State.EXPIRED, State.CANCELLED)) or bool(
                        snapshot and snapshot.state == State.AUTHENTICATED
                        and not self._tunnel_started and self._error),
                "error": self._error,
                "last_response": dict(self._last_response) if self._last_response else None,
            }
        result["events"] = self._events()
        return result

    def _active(self, attempt_id, states):
        # Called under the state lock. Never let a late reply revive a session.
        snapshot = self._snapshot()
        if not snapshot or snapshot.attempt_id != attempt_id or snapshot.state not in states:
            raise NativeAuthError("authentication session is no longer active; restart the native engine")
        return snapshot

    def _fail(self, reason):
        with self._lock:
            snapshot = self._snapshot()
            if snapshot and snapshot.state in (
                    State.PASSWORD_PENDING, State.PASSWORD_SUBMITTED,
                    State.AWAITING_SMS, State.SMS_SUBMITTED):
                self._attempt.fail(snapshot.attempt_id)
                self._error = reason
            elif snapshot and snapshot.state == State.AUTHENTICATED:
                self._error = reason
            self._data_plane_ready = False

    def _remaining(self, attempt_id, maximum):
        with self._lock:
            snapshot = self._snapshot()
            if not snapshot or snapshot.attempt_id != attempt_id:
                raise NativeAuthError("authentication session is no longer active")
            remaining = snapshot.deadline - self._clock()
        if remaining <= 0:
            raise NativeAuthError("authentication deadline elapsed; restart the native engine")
        return min(maximum, remaining)

    def _call(self, attempt_id, message_class, opcode, payload, maximum=45):
        if not self._alive():
            raise NativeAuthError("native engine is unavailable; restart the native engine")
        response = self._engine.call(message_class, opcode, payload,
                                     timeout=self._remaining(attempt_id, maximum))
        # Retain only response header/length. Never retain the Message payload.
        with self._lock:
            self._last_response = _metadata({"class": response.message_class,
                                              "code": response.opcode,
                                              "length": len(response.payload)})
        return response

    def _complete(self, attempt_id, response):
        with self._lock:
            snapshot = self._active(attempt_id, (State.PASSWORD_SUBMITTED, State.SMS_SUBMITTED))
            if response.opcode == SMS_REQUIRED and snapshot.state == State.PASSWORD_SUBMITTED:
                self._attempt.begin_challenge(attempt_id, uuid.uuid4().hex, timeout_seconds=180)
                return
            if response.opcode != AUTH_OK:
                self._fail("authentication_rejected")
                return
            self._attempt.authenticated(attempt_id)
        tunnel = _tunnel_payload(self._config)
        response = self._call(attempt_id, 5, 10, tunnel)
        with self._lock:
            self._active(attempt_id, (State.AUTHENTICATED,))
            self._tunnel_started = response.opcode == TUNNEL_START_OK
            self._data_plane_ready = False
            self._error = None if self._tunnel_started else "tunnel_start_rejected"

    def login(self, username, password):
        """Start one explicit password attempt; never automatically retries."""
        if not self._operation_lock.acquire(blocking=False):
            raise NativeAuthError("an authentication operation is already in progress")
        started = False
        try:
            with self._lock:
                if self._attempt is not None:
                    raise RestartRequired("restart the native engine before another login")
                attempt_id = uuid.uuid4().hex
                self._attempt = AuthAttempt(attempt_id, timeout_seconds=600, clock=self._clock)
                self._busy = True
                started = True
            gateway, port = self._config["gateway"], self._config["port"]
            if not self._alive():
                raise NativeAuthError("native engine is unavailable; restart the native engine")
            decision = self._verifier(
                gateway, port,
                approved_sha256=self._config.get("approved_certificate_sha256"),
                timeout=self._remaining(attempt_id, 8))
            approved_pin = self._config.get("approved_certificate_sha256")
            override_approved = (
                isinstance(approved_pin, str)
                and re.fullmatch(r"[0-9a-fA-F]{64}", approved_pin) is not None
                and isinstance(decision.sha256, str)
                and re.fullmatch(r"[0-9a-fA-F]{64}", decision.sha256) is not None
                and hmac.compare_digest(decision.sha256.lower(), approved_pin.lower()))
            if (decision.gateway != gateway or decision.port != port
                    or type(decision.verify_in_engine) is not bool
                    or (not decision.verify_in_engine and (
                        not override_approved or not decision.allows_override_for(gateway, port)))):
                raise CertificatePolicyError("gateway certificate verification failed")
            with self._lock:
                self._active(attempt_id, (State.PASSWORD_PENDING,))
            response = self._call(attempt_id, 1, 4, first_connect(gateway, port))
            if response.opcode != 0x10000:
                self._fail("first_connect_rejected")
            else:
                with self._lock:
                    self._active(attempt_id, (State.PASSWORD_PENDING,))
                response = self._call(attempt_id, 1, 18, _link_select(gateway, port))
                if response.opcode != 0x10004:
                    self._fail("link_selection_rejected")
                else:
                    body = password_login(gateway, port, username, password,
                                          verify_gateway_certificate=decision.verify_in_engine)
                    with self._lock:
                        self._attempt.claim_password_submission(attempt_id)
                    response = self._call(attempt_id, 3, 5, body)
                    self._complete(attempt_id, response)
        except RestartRequired:
            raise
        except CertificatePolicyError:
            self._fail("certificate_verification_failed")
            raise NativeAuthError("gateway certificate verification failed; restart the native engine") from None
        except Exception:
            if started:
                self._fail("native_authentication_failed")
            raise NativeAuthError("native authentication failed; restart the native engine") from None
        finally:
            if started:
                with self._lock:
                    self._busy = False
            self._operation_lock.release()
        return self.status()

    def sms(self, attempt_id, challenge_id, code):
        """Continue this engine's existing challenge once, without logging in again."""
        # Local input errors do not consume the one permitted wire submission.
        body = sms_code(code)
        if not self._operation_lock.acquire(blocking=False):
            raise NativeAuthError("an authentication operation is already in progress")
        claimed = False
        try:
            with self._lock:
                if self._attempt is None:
                    raise NativeAuthError("no SMS challenge is pending")
                self._attempt.claim_code_submission(attempt_id, challenge_id)
                self._busy = True
                claimed = True
            response = self._call(attempt_id, 3, 17, body, maximum=65)
            self._complete(attempt_id, response)
        except SessionError:
            raise NativeAuthError("SMS challenge is stale, expired, or already submitted") from None
        except Exception:
            if claimed:
                self._fail("native_authentication_failed")
            raise NativeAuthError("native authentication failed; restart the native engine") from None
        finally:
            if claimed:
                with self._lock:
                    self._busy = False
            self._operation_lock.release()
        return self.status()

    def cancel(self, attempt_id):
        """Cancel locally; late responses cannot authenticate or start a tunnel."""
        with self._lock:
            if self._attempt is None:
                raise NativeAuthError("no authentication session exists")
            try:
                self._attempt.cancel(attempt_id)
            except SessionError:
                raise NativeAuthError("authentication session cannot be cancelled") from None
            self._tunnel_started = False
            self._data_plane_ready = False
            self._error = "authentication_cancelled"
        return self.status()

    def health(self):
        """Require authentication, tunnel acknowledgement, and a real target probe."""
        if not self._probe_lock.acquire(blocking=False):
            return False
        try:
            with self._lock:
                snapshot = self._snapshot()
                if (self._busy or not self._alive() or not snapshot
                        or snapshot.state != State.AUTHENTICATED or not self._tunnel_started):
                    self._data_plane_ready = False
                    return False
                attempt_id = snapshot.attempt_id
            try:
                ready = self._probe(self._config) is True
            except Exception:
                ready = False
            with self._lock:
                snapshot = self._snapshot()
                self._data_plane_ready = bool(
                    ready and not self._busy and self._alive() and self._tunnel_started
                    and snapshot and snapshot.attempt_id == attempt_id
                    and snapshot.state == State.AUTHENTICATED)
                return self._data_plane_ready
        finally:
            self._probe_lock.release()


def _tunnel_payload(config):
    return tunnel_start(config.get("tunnel_mode", 2),
                        config.get("gm_algorithm", 0),
                        config.get("route_cover", 1))
