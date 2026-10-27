import json
import struct
import threading
import unittest
from unittest.mock import Mock

from univpn_gateway.certificate_policy import CertificateDecision, CertificatePolicyError
from univpn_gateway.native_auth import NativeAuth, NativeAuthError, RestartRequired
from univpn_gateway.native_messages import AUTH_OK, SMS_REQUIRED, TUNNEL_START_OK
from univpn_gateway.native_rpc import Message


def reply(code, message_class=3):
    return Message(4, 2, 1, message_class, code, b"private-response-payload")


class Clock:
    now = 100

    def __call__(self):
        return self.now


class Engine:
    alive = True

    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []
        self.events = []

    def call(self, message_class, opcode, payload=b"", timeout=45):
        self.calls.append((message_class, opcode, payload, timeout))
        result = next(self.replies)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return result()
        return result

    def event_metadata(self):
        return self.events


class NativeAuthTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.config = {"gateway": "vpn.example.com", "port": 443}
        self.verifier = Mock(return_value=CertificateDecision(
            "vpn.example.com", 443, "ab" * 32, True))
        self.probe = Mock(return_value=False)

    def auth(self, replies, config=None):
        self.engine = Engine(replies)
        return NativeAuth(self.engine, config or self.config,
                          verifier=self.verifier, probe=self.probe, clock=self.clock)

    def stages(self):
        return [(call[0], call[1]) for call in self.engine.calls]

    def pending(self):
        auth = self.auth([reply(0x10000), reply(0x10004), reply(SMS_REQUIRED),
                          reply(AUTH_OK), reply(TUNNEL_START_OK)])
        status = auth.login("private-user", "private-password")
        self.assertEqual("awaiting_sms", status["state"])
        return auth, status

    def test_mfa_continues_same_engine_and_health_is_independent(self):
        auth, status = self.pending()
        self.assertEqual(180, status["expires_in"])
        self.assertFalse(auth.health())
        self.probe.assert_not_called()
        result = auth.sms(status["attempt_id"], status["challenge_id"], "123456")
        self.assertEqual("authenticated", result["state"])
        self.assertTrue(result["authenticated"])
        self.assertTrue(result["tunnel_started"])
        self.assertFalse(result["data_plane_ready"])
        self.assertIs(auth._engine, self.engine)
        self.assertEqual([(1, 4), (1, 18), (3, 5), (3, 17), (5, 10)], self.stages())
        self.assertFalse(auth.health())
        self.probe.return_value = True
        self.assertTrue(auth.health())
        self.assertTrue(auth.status()["data_plane_ready"])
        self.engine.alive = False
        self.assertFalse(auth.health())
        self.assertFalse(auth.status()["data_plane_ready"])

    def test_password_only_defaults_preserve_certificate_check_and_tunnel_flags(self):
        auth = self.auth([reply(0x10000), reply(0x10004), reply(AUTH_OK),
                          reply(TUNNEL_START_OK)])
        result = auth.login("private-user", "private-password")
        self.verifier.assert_called_once_with("vpn.example.com", 443,
                                              approved_sha256=None, timeout=8)
        self.assertEqual(1, struct.unpack_from("<I", self.engine.calls[2][2])[0])
        self.assertEqual((2, 0, 1), struct.unpack("!III", self.engine.calls[3][2]))
        self.assertEqual(136, len(self.engine.calls[1][2]))
        self.assertEqual(443, struct.unpack_from("<I", self.engine.calls[1][2], 132)[0])
        self.assertTrue(result["tunnel_started"])
        self.probe.assert_not_called()

    def test_explicit_matching_pin_applies_only_to_this_login(self):
        config = dict(self.config, approved_certificate_sha256="ab" * 32,
                      tunnel_mode=1, gm_algorithm=1, route_cover=0)
        self.verifier.return_value = CertificateDecision("vpn.example.com", 443, "ab" * 32, False)
        auth = self.auth([reply(0x10000), reply(0x10004), reply(AUTH_OK),
                          reply(TUNNEL_START_OK)], config)
        auth.login("private-user", "private-password")
        self.assertEqual(0, struct.unpack_from("<I", self.engine.calls[2][2])[0])
        self.assertEqual((1, 1, 0), struct.unpack("!III", self.engine.calls[3][2]))
        self.assertEqual("ab" * 32, self.verifier.call_args.kwargs["approved_sha256"])
        self.assertNotIn("verify_gateway_certificate", config)

    def test_pin_failure_occurs_before_any_engine_request_and_is_opaque(self):
        self.verifier.side_effect = CertificatePolicyError("private-subject-certificate-details")
        auth = self.auth([])
        with self.assertRaises(NativeAuthError) as error:
            auth.login("private-user", "private-password")
        self.assertNotIn("private-subject", str(error.exception))
        self.assertEqual([], self.engine.calls)
        self.assertEqual("failed", auth.status()["state"])
        with self.assertRaises(RestartRequired):
            auth.login("private-user", "private-password")
        self.verifier.assert_called_once()

    def test_certificate_decision_cannot_be_reused_for_another_endpoint(self):
        self.config["approved_certificate_sha256"] = "ab" * 32
        for gateway, port in (("other.example.com", 443), ("vpn.example.com", 8443)):
            with self.subTest(gateway=gateway, port=port):
                self.verifier.return_value = CertificateDecision(gateway, port, "ab" * 32, False)
                auth = self.auth([])
                with self.assertRaises(NativeAuthError):
                    auth.login("private-user", "private-password")
                self.assertEqual([], self.engine.calls)

    def test_false_certificate_flag_requires_the_explicit_current_pin(self):
        self.verifier.return_value = CertificateDecision("vpn.example.com", 443, "ab" * 32, False)
        for pin in (None, "cd" * 32, "bad-pin"):
            with self.subTest(pin=pin):
                config = dict(self.config, approved_certificate_sha256=pin)
                auth = self.auth([], config)
                with self.assertRaises(NativeAuthError):
                    auth.login("private-user", "private-password")
                self.assertEqual([], self.engine.calls)

    def test_first_connect_and_link_selection_fail_before_password(self):
        for replies, expected in (([reply(0x10001)], [(1, 4)]),
                                  ([reply(0x10000), reply(0x10005)], [(1, 4), (1, 18)])):
            with self.subTest(stages=expected):
                auth = self.auth(replies)
                result = auth.login("private-user", "private-password")
                self.assertEqual("failed", result["state"])
                self.assertEqual(expected, self.stages())
                with self.assertRaises(RestartRequired):
                    auth.login("private-user", "private-password")

    def test_password_rejection_never_retries(self):
        auth = self.auth([reply(0x10000), reply(0x10004), reply(0x30001)])
        result = auth.login("private-user", "private-password")
        self.assertEqual("failed", result["state"])
        self.assertTrue(result["requires_restart"])
        with self.assertRaises(RestartRequired):
            auth.login("private-user", "private-password")
        self.assertEqual([(1, 4), (1, 18), (3, 5)], self.stages())

    def test_sms_is_submitted_at_most_once(self):
        auth, status = self.pending()
        auth.sms(status["attempt_id"], status["challenge_id"], "123456")
        with self.assertRaises(NativeAuthError):
            auth.sms(status["attempt_id"], status["challenge_id"], "123456")
        self.assertEqual(1, self.stages().count((3, 17)))
        self.assertTrue(auth.status()["authenticated"])

    def test_stale_identifiers_do_not_consume_the_current_challenge(self):
        auth, status = self.pending()
        for attempt, challenge in (("stale-attempt", status["challenge_id"]),
                                   (status["attempt_id"], "stale-challenge")):
            with self.assertRaises(NativeAuthError):
                auth.sms(attempt, challenge, "123456")
            self.assertEqual("awaiting_sms", auth.status()["state"])
        auth.sms(status["attempt_id"], status["challenge_id"], "123456")
        self.assertEqual(1, self.stages().count((3, 17)))

    def test_expired_challenge_never_sends_code_or_reuses_engine(self):
        auth, status = self.pending()
        self.clock.now += 180
        with self.assertRaises(NativeAuthError):
            auth.sms(status["attempt_id"], status["challenge_id"], "123456")
        self.assertEqual("expired", auth.status()["state"])
        self.assertTrue(auth.status()["requires_restart"])
        with self.assertRaises(RestartRequired):
            auth.login("private-user", "private-password")
        self.assertNotIn((3, 17), self.stages())

    def test_cancellation_is_terminal_and_does_not_request_another_sms(self):
        auth, status = self.pending()
        self.assertEqual("cancelled", auth.cancel(status["attempt_id"])["state"])
        with self.assertRaises(NativeAuthError):
            auth.sms(status["attempt_id"], status["challenge_id"], "123456")
        with self.assertRaises(RestartRequired):
            auth.login("private-user", "private-password")
        self.assertEqual([(1, 4), (1, 18), (3, 5)], self.stages())

    def test_sms_timeout_does_not_replay_credentials_or_code(self):
        auth = self.auth([reply(0x10000), reply(0x10004), reply(SMS_REQUIRED),
                          TimeoutError("private-code-and-endpoint-details")])
        status = auth.login("private-user", "private-password")
        with self.assertRaises(NativeAuthError) as error:
            auth.sms(status["attempt_id"], status["challenge_id"], "123456")
        self.assertNotIn("private-code", str(error.exception))
        self.assertEqual("failed", auth.status()["state"])
        with self.assertRaises(NativeAuthError):
            auth.sms(status["attempt_id"], status["challenge_id"], "123456")
        self.assertEqual(1, self.stages().count((3, 17)))
        self.assertEqual(1, self.stages().count((3, 5)))

    def test_invalid_local_sms_input_does_not_consume_wire_submission(self):
        auth, status = self.pending()
        with self.assertRaises(ValueError):
            auth.sms(status["attempt_id"], status["challenge_id"], "bad-code")
        self.assertEqual("awaiting_sms", auth.status()["state"])
        auth.sms(status["attempt_id"], status["challenge_id"], "123456")

    def test_second_sms_challenge_is_terminal_and_never_resends_password(self):
        auth = self.auth([reply(0x10000), reply(0x10004), reply(SMS_REQUIRED), reply(SMS_REQUIRED)])
        status = auth.login("private-user", "private-password")
        result = auth.sms(status["attempt_id"], status["challenge_id"], "123456")
        self.assertEqual("failed", result["state"])
        self.assertEqual([(1, 4), (1, 18), (3, 5), (3, 17)], self.stages())

    def test_authenticated_is_not_tunnel_or_data_plane_success(self):
        auth = self.auth([reply(0x10000), reply(0x10004), reply(AUTH_OK), reply(0x70001)])
        result = auth.login("private-user", "private-password")
        self.assertTrue(result["authenticated"])
        self.assertFalse(result["tunnel_started"])
        self.assertTrue(result["requires_restart"])
        self.assertFalse(auth.health())
        self.probe.assert_not_called()

    def test_probe_failure_or_exception_blocks_readiness(self):
        auth = self.auth([reply(0x10000), reply(0x10004), reply(AUTH_OK), reply(TUNNEL_START_OK)])
        auth.login("private-user", "private-password")
        self.probe.return_value = True
        self.assertTrue(auth.health())
        self.probe.side_effect = OSError("private target details")
        self.assertFalse(auth.health())
        self.assertFalse(auth.status()["data_plane_ready"])

    def test_status_never_returns_payloads_credentials_or_unsanitized_events(self):
        auth, _ = self.pending()
        self.engine.events = [{"class": 3, "code": SMS_REQUIRED, "length": 32,
                               "received_at": 10.5, "payload": b"private-event",
                               "detail": "private-password", "username": "private-user"},
                              {"class": "private-user", "code": "private-password"}]
        status = auth.status()
        encoded = json.dumps(status)
        for secret in ("private-user", "private-password", "private-response", "private-event"):
            self.assertNotIn(secret, encoded)
        self.assertEqual([{"class": 3, "code": SMS_REQUIRED, "length": 32,
                           "received_at": 10.5}], status["events"])
        self.probe.assert_not_called()

    def test_login_total_deadline_stops_before_password(self):
        def slow_first_connect():
            self.clock.now += 600
            return reply(0x10000)

        auth = self.auth([slow_first_connect])
        with self.assertRaises(NativeAuthError):
            auth.login("private-user", "private-password")
        self.assertEqual("expired", auth.status()["state"])
        self.assertEqual([(1, 4)], self.stages())

    def test_status_and_cancel_do_not_block_behind_long_sms_io(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        errors = []

        def delayed_reply():
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test did not release engine")
            return reply(AUTH_OK)

        auth = self.auth([reply(0x10000), reply(0x10004), reply(SMS_REQUIRED), delayed_reply])
        status = auth.login("private-user", "private-password")

        def submit():
            try:
                auth.sms(status["attempt_id"], status["challenge_id"], "123456")
            except NativeAuthError as error:
                errors.append(error)
            finally:
                finished.set()

        thread = threading.Thread(target=submit)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(auth.status()["operation_in_progress"])
            self.assertFalse(auth.health())
            with self.assertRaises(NativeAuthError):
                auth.sms(status["attempt_id"], status["challenge_id"], "123456")
            self.assertEqual("cancelled", auth.cancel(status["attempt_id"])["state"])
        finally:
            release.set()
            thread.join(2)
        self.assertTrue(finished.is_set())
        self.assertEqual(1, len(errors))
        self.assertNotIn((5, 10), self.stages())
        self.assertFalse(auth.status()["authenticated"])


if __name__ == "__main__":
    unittest.main()
