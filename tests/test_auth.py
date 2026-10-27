import unittest

from univpn_gateway.auth import AuthError, Challenge, login


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, value):
        self.now += value


class FakeUI:
    def __init__(self):
        self.actions = []

    def fill(self, key, value):
        self.actions.append(("fill", key, value))

    def press(self, key):
        self.actions.append(("press", key))

    def matches(self, key):
        return key == "otp"


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.ui = FakeUI()
        self.mapping = dict(username="user", password="password", submit="login",
                            otp=dict(input="otp", submit="confirm", timeout_seconds=5))

    def login(self, read_otp, ready):
        return login(self.ui, self.mapping, "test-user", "test-secret", read_otp, ready,
                     clock=self.clock, sleep=self.clock.sleep, timeout=10)

    def test_otp_is_sent_once_to_existing_session(self):
        # Keep OTP dialog visible for >1 s: it must not cause another submit.
        self.assertEqual("ready", self.login(lambda: "123456", lambda: self.clock() > 4))
        self.assertEqual(1, self.ui.actions.count(("press", "login")))
        self.assertEqual(1, self.ui.actions.count(("press", "confirm")))

    def test_expired_code_never_reaches_ui(self):
        def delayed():
            self.clock.sleep(6)
            return "123456"
        with self.assertRaisesRegex(AuthError, "expired"):
            self.login(delayed, lambda: False)
        self.assertNotIn(("press", "confirm"), self.ui.actions)

    def test_failed_probe_never_means_success(self):
        with self.assertRaisesRegex(AuthError, "timed out"):
            self.login(lambda: "123456", lambda: False)
        self.assertEqual(1, self.ui.actions.count(("press", "login")))
        self.assertEqual(1, self.ui.actions.count(("press", "confirm")))

    def test_no_otp_without_a_visible_challenge(self):
        self.ui.matches = lambda _: False
        self.assertEqual("ready", self.login(lambda: self.fail("unexpected OTP request"),
                                             lambda: self.clock() > 2))

    def test_no_certificate_acceptance(self):
        self.mapping["certificate"] = "certificate"
        self.ui.matches = lambda key: key == "certificate"
        with self.assertRaisesRegex(AuthError, "certificate"):
            self.login(lambda: "123456", lambda: False)

    def test_invalid_otp_not_submitted(self):
        with self.assertRaises(AuthError):
            self.login(lambda: "123\n456", lambda: False)
        self.assertNotIn(("press", "confirm"), self.ui.actions)

    def test_challenge_cannot_be_reused(self):
        challenge = Challenge(10)
        challenge.consume(1)
        with self.assertRaises(AuthError):
            challenge.consume(2)


if __name__ == "__main__":
    unittest.main()
