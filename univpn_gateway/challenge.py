"""Backend-independent, single-attempt authentication state.

The caller owns all credentials and wire I/O. Claim an operation immediately
before sending it: an uncertain send must never make that operation retryable.
This module neither sends SMS messages nor initiates another login.
"""
from dataclasses import dataclass
from enum import Enum
import math
import threading
import time


class State(str, Enum):
    PASSWORD_PENDING = "password_pending"
    PASSWORD_SUBMITTED = "password_submitted"
    AWAITING_SMS = "awaiting_sms"
    SMS_SUBMITTED = "sms_submitted"
    AUTHENTICATED = "authenticated"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class SessionError(RuntimeError):
    pass


class StaleSession(SessionError):
    pass


class InvalidTransition(SessionError):
    pass


class SessionExpired(SessionError):
    pass


@dataclass(frozen=True)
class Snapshot:
    attempt_id: str
    state: State
    challenge_id: str | None
    deadline: float


_ACTIVE = frozenset((State.PASSWORD_PENDING, State.PASSWORD_SUBMITTED,
                     State.AWAITING_SMS, State.SMS_SUBMITTED))


def _identifier(value):
    if not isinstance(value, str) or not value:
        raise ValueError("session identifiers must be non-empty strings")
    return value


def _timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout must be a positive finite number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be a positive finite number")
    return value


class AuthAttempt:
    """Track exactly one explicit login and at most one SMS submission.

    ``attempt_id`` must be newly assigned by the caller for each user-initiated
    login. IDs from an old transport/session must not be reused. Deadlines use
    the injected monotonic clock; expiry and cancellation are terminal.
    """

    def __init__(self, attempt_id, timeout_seconds=180, *, clock=time.monotonic):
        self._attempt_id = _identifier(attempt_id)
        self._clock = clock
        self._deadline = clock() + _timeout(timeout_seconds)
        self._challenge_id = None
        self._state = State.PASSWORD_PENDING
        self._lock = threading.Lock()

    def _refresh(self):
        if self._state in _ACTIVE and self._clock() >= self._deadline:
            self._state = State.EXPIRED

    def _check(self, attempt_id, allowed):
        # A stale event must not consume or alter the current session.
        if attempt_id != self._attempt_id:
            raise StaleSession("event belongs to a different login attempt")
        self._refresh()
        if self._state == State.EXPIRED:
            raise SessionExpired("authentication session expired; no automatic retry")
        if self._state not in allowed:
            raise InvalidTransition("operation is not allowed in the current authentication state")

    def snapshot(self):
        with self._lock:
            self._refresh()
            return Snapshot(self._attempt_id, self._state,
                            self._challenge_id, self._deadline)

    def claim_password_submission(self, attempt_id):
        with self._lock:
            self._check(attempt_id, (State.PASSWORD_PENDING,))
            self._state = State.PASSWORD_SUBMITTED

    def begin_challenge(self, attempt_id, challenge_id, timeout_seconds=120):
        """Record a challenge delivered by the backend, without requesting SMS.

        Repeating the same challenge while waiting is idempotent and cannot
        extend its deadline. Replacing it requires a separate explicit login.
        """
        challenge_id = _identifier(challenge_id)
        timeout_seconds = _timeout(timeout_seconds)
        with self._lock:
            self._check(attempt_id, (State.PASSWORD_SUBMITTED, State.AWAITING_SMS))
            if self._challenge_id is not None:
                if challenge_id != self._challenge_id:
                    raise StaleSession("challenge does not belong to this session")
                return
            self._challenge_id = challenge_id
            self._deadline = min(self._deadline, self._clock() + timeout_seconds)
            self._state = State.AWAITING_SMS

    def claim_code_submission(self, attempt_id, challenge_id):
        """Consume the submission once, before wire I/O. Never stores the code."""
        with self._lock:
            if challenge_id != self._challenge_id or self._challenge_id is None:
                raise StaleSession("code belongs to a different SMS challenge")
            self._check(attempt_id, (State.AWAITING_SMS,))
            self._state = State.SMS_SUBMITTED

    def authenticated(self, attempt_id):
        """Record backend authentication, independently of VPN data-plane health."""
        with self._lock:
            self._check(attempt_id, (State.PASSWORD_SUBMITTED, State.SMS_SUBMITTED))
            self._state = State.AUTHENTICATED

    def fail(self, attempt_id):
        with self._lock:
            self._check(attempt_id, _ACTIVE)
            self._state = State.FAILED

    def cancel(self, attempt_id):
        with self._lock:
            self._check(attempt_id, (*_ACTIVE, State.CANCELLED))
            self._state = State.CANCELLED
