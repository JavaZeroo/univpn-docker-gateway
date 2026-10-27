import threading
import unittest

from univpn_gateway.challenge import (AuthAttempt, InvalidTransition,
                                      SessionExpired, StaleSession, State)


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


class ChallengeTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.session = AuthAttempt("attempt-1", clock=self.clock)

    def challenge(self, timeout=30):
        self.session.claim_password_submission("attempt-1")
        self.session.begin_challenge("attempt-1", "challenge-1", timeout)

    def test_one_submission_then_backend_authentication(self):
        self.challenge()
        self.session.claim_code_submission("attempt-1", "challenge-1")
        with self.assertRaises(InvalidTransition):
            self.session.claim_code_submission("attempt-1", "challenge-1")
        self.session.authenticated("attempt-1")
        self.assertEqual(State.AUTHENTICATED, self.session.snapshot().state)

    def test_sms_is_not_authentication_until_submitted(self):
        self.challenge()
        with self.assertRaises(InvalidTransition):
            self.session.authenticated("attempt-1")
        self.assertEqual(State.AWAITING_SMS, self.session.snapshot().state)

    def test_old_attempt_and_challenge_cannot_consume_current(self):
        self.challenge()
        for attempt, challenge in (("old-attempt", "challenge-1"),
                                   ("attempt-1", "old-challenge")):
            with self.assertRaises(StaleSession):
                self.session.claim_code_submission(attempt, challenge)
            self.assertEqual(State.AWAITING_SMS, self.session.snapshot().state)
        self.session.claim_code_submission("attempt-1", "challenge-1")

    def test_duplicate_challenge_does_not_extend_deadline(self):
        self.challenge(timeout=30)
        self.clock.now = 20
        self.session.begin_challenge("attempt-1", "challenge-1", 100)
        self.assertEqual(30, self.session.snapshot().deadline)
        self.clock.now = 30
        with self.assertRaises(SessionExpired):
            self.session.claim_code_submission("attempt-1", "challenge-1")

    def test_new_challenge_cannot_replace_existing(self):
        self.challenge()
        with self.assertRaises(StaleSession):
            self.session.begin_challenge("attempt-1", "challenge-2")
        self.assertEqual("challenge-1", self.session.snapshot().challenge_id)

    def test_challenge_cannot_extend_attempt_timeout(self):
        self.clock.now = 175
        self.session.claim_password_submission("attempt-1")
        self.session.begin_challenge("attempt-1", "challenge-1", 120)
        self.assertEqual(180, self.session.snapshot().deadline)
        self.clock.now = 180
        self.assertEqual(State.EXPIRED, self.session.snapshot().state)

    def test_expired_session_never_restarts(self):
        self.clock.now = 181
        with self.assertRaises(SessionExpired):
            self.session.claim_password_submission("attempt-1")
        with self.assertRaises(SessionExpired):
            self.session.begin_challenge("attempt-1", "new-challenge")

    def test_cancellation_blocks_late_code_and_success(self):
        self.challenge()
        self.session.cancel("attempt-1")
        self.session.cancel("attempt-1")
        for action in (
            lambda: self.session.claim_code_submission("attempt-1", "challenge-1"),
            lambda: self.session.authenticated("attempt-1"),
            lambda: self.session.claim_password_submission("attempt-1"),
        ):
            with self.assertRaises(InvalidTransition):
                action()
        self.clock.now = 500
        self.assertEqual(State.CANCELLED, self.session.snapshot().state)

    def test_password_failure_is_terminal_without_retry(self):
        self.session.claim_password_submission("attempt-1")
        self.session.fail("attempt-1")
        for action in (
            lambda: self.session.claim_password_submission("attempt-1"),
            lambda: self.session.begin_challenge("attempt-1", "challenge-1"),
            lambda: self.session.authenticated("attempt-1"),
        ):
            with self.assertRaises(InvalidTransition):
                action()
        self.assertEqual(State.FAILED, self.session.snapshot().state)

    def test_password_only_backend_can_authenticate(self):
        self.session.claim_password_submission("attempt-1")
        self.session.authenticated("attempt-1")
        self.clock.now = 500
        self.assertEqual(State.AUTHENTICATED, self.session.snapshot().state)

    def test_concurrent_code_submissions_only_one_wins(self):
        self.challenge()
        barrier = threading.Barrier(3)
        results = []

        def submit():
            barrier.wait()
            try:
                self.session.claim_code_submission("attempt-1", "challenge-1")
                results.append("claimed")
            except InvalidTransition:
                results.append("rejected")

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)
        self.assertCountEqual(["claimed", "rejected"], results)

    def test_invalid_deadline_values_rejected(self):
        for value in (0, -1, float("nan"), float("inf"), True, "30"):
            with self.assertRaises(ValueError):
                AuthAttempt("attempt", value, clock=self.clock)


if __name__ == "__main__":
    unittest.main()
