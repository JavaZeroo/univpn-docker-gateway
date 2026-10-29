import contextlib
import io
import threading
import unittest
from unittest.mock import Mock, patch

from univpn_gateway.certificate_policy import CertificateDecision
from univpn_gateway.native_auth import NativeAuth
from univpn_gateway.native_messages import AUTH_OK, TUNNEL_START_OK
from univpn_gateway.native_rpc import Message
from univpn_gateway.native_service import dispatch, update_forwarding


class Engine:
    """Credential-free transport fixture; stores no request payloads."""
    alive = True

    def __init__(self):
        self.replies = iter((0x10000, 0x10004, AUTH_OK, TUNNEL_START_OK))

    def call(self, message_class, opcode, payload=b"", timeout=45):
        return Message(4, 2, 1, message_class, next(self.replies), b"")

    def event_metadata(self):
        return []


class NativeServiceTests(unittest.TestCase):
    def test_lite_health_never_manages_wireguard_forwarding(self):
        auth = Mock()
        auth.health.side_effect = (True, False)
        with patch("univpn_gateway.native_service.gateway.enable") as enable, \
                patch("univpn_gateway.native_service.gateway.disable") as disable, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(update_forwarding(auth, {"edition": "lite"}, False))
            self.assertFalse(update_forwarding(auth, {"edition": "lite"}, True))
            enable.assert_not_called()
            disable.assert_not_called()

    def authenticated(self):
        engine = Engine()
        config = {"gateway": "vpn.example.com", "port": 443}
        probe = Mock(return_value=True)
        verifier = Mock(return_value=CertificateDecision(
            "vpn.example.com", 443, "ab" * 32, True))
        auth = NativeAuth(engine, config, verifier=verifier, probe=probe)
        auth.login("fixture-user", "fixture-password")
        self.assertTrue(auth.health())
        probe.reset_mock()
        return auth, engine, config, probe

    def test_status_reads_cached_state_without_starting_a_probe(self):
        auth, _, _, probe = self.authenticated()
        probe.side_effect = AssertionError("status must not initiate a probe")
        result = dispatch(auth, {"command": "status"})
        self.assertTrue(result["authenticated"])
        self.assertTrue(result["data_plane_ready"])
        self.assertFalse(auth._probe_lock.locked())
        probe.assert_not_called()

    def overlap_status_and_background_probe(self, probe_ready, engine_dies=False):
        auth, engine, config, probe = self.authenticated()
        progress, entered, release = threading.Event(), threading.Event(), threading.Event()
        status_results, background_results, errors, probe_owners = [], [], [], []

        def controlled_probe(_):
            probe_owners.append(threading.current_thread().name)
            entered.set()
            progress.set()
            if not release.wait(2):
                raise AssertionError("fixture probe was not released")
            return probe_ready

        probe.side_effect = controlled_probe

        def status_reader():
            try:
                status_results.append(dispatch(auth, {"command": "status"}))
            except Exception as error:
                errors.append(type(error).__name__)
            finally:
                # In the regression, an active probe signals progress instead.
                # With the fix, this request completes before any probe starts.
                progress.set()

        def background_guard():
            try:
                background_results.append(update_forwarding(auth, config, True))
            except Exception as error:
                errors.append(type(error).__name__)

        reader = threading.Thread(target=status_reader, name="status-reader")
        background = threading.Thread(target=background_guard, name="background-guard")
        output = io.StringIO()
        with patch("univpn_gateway.native_service.gateway.disable") as disable, \
                patch("univpn_gateway.native_service.gateway.enable") as enable, \
                contextlib.redirect_stdout(output):
            try:
                reader.start()
                self.assertTrue(progress.wait(1))
                background.start()
                self.assertTrue(entered.wait(1))
                self.assertTrue(auth._probe_lock.locked())
                for _ in range(20):
                    status_results.append(dispatch(auth, {"command": "status"}))
                if engine_dies:
                    engine.alive = False
            finally:
                release.set()
                for worker in (reader, background):
                    if worker.ident is not None:
                        worker.join(timeout=2)
            self.assertFalse(reader.is_alive())
            self.assertFalse(background.is_alive())
            self.assertEqual([], errors)
            self.assertEqual(["background-guard"], probe_owners)
            self.assertEqual(1, probe.call_count)
            self.assertEqual(21, len(status_results))
            self.assertTrue(all(result["data_plane_ready"] for result in status_results))
            expected_ready = probe_ready and not engine_dies
            self.assertEqual([expected_ready], background_results)
            self.assertEqual(int(not expected_ready), disable.call_count)
            enable.assert_not_called()
            self.assertEqual(expected_ready, dispatch(auth, {"command": "status"})["data_plane_ready"])
        self.assertNotIn("fixture-user", output.getvalue())
        self.assertNotIn("fixture-password", output.getvalue())

    def test_concurrent_status_cannot_disable_healthy_forwarding(self):
        self.overlap_status_and_background_probe(True)

    def test_true_probe_failure_still_disables_forwarding_during_status_requests(self):
        self.overlap_status_and_background_probe(False)

    def test_engine_death_during_probe_still_disables_forwarding(self):
        self.overlap_status_and_background_probe(True, engine_dies=True)

    def test_background_enables_forwarding_after_a_successful_probe(self):
        auth, _, config, _ = self.authenticated()
        with patch("univpn_gateway.native_service.gateway.disable") as disable, \
                patch("univpn_gateway.native_service.gateway.enable") as enable, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(update_forwarding(auth, config, False))
            enable.assert_called_once_with(config)
            disable.assert_not_called()

    def test_existing_control_commands_keep_their_arguments_and_results(self):
        auth = Mock()
        cases = (
            ({"command": "login", "username": "fixture-user", "password": "fixture-password"},
             "login", ("fixture-user", "fixture-password")),
            ({"command": "sms", "attempt_id": "attempt-1", "challenge_id": "challenge-1",
              "code": "123456"}, "sms", ("attempt-1", "challenge-1", "123456")),
            ({"command": "cancel", "attempt_id": "attempt-1"}, "cancel", ("attempt-1",)),
        )
        with contextlib.redirect_stdout(io.StringIO()) as stdout, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            for request, method, arguments in cases:
                expected = {"state": "fixture-state"}
                handler = getattr(auth, method)
                handler.return_value = expected
                self.assertIs(expected, dispatch(auth, request))
                handler.assert_called_once_with(*arguments)
            with self.assertRaisesRegex(ValueError, "^unknown control command$"):
                dispatch(auth, {"command": "unknown", "password": "must-not-be-logged"})
        auth.health.assert_not_called()
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
