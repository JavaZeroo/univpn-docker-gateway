import hashlib
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from univpn_gateway.native_engine import NativeEngine
from univpn_gateway.native_rpc import Message, ProtocolError, receive


class Process:
    def __init__(self, exit_code=None, terminate_timeout=False):
        self.exit_code = exit_code
        self.terminate_timeout = terminate_timeout
        self.terminated = 0
        self.killed = 0

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated += 1
        if not self.terminate_timeout:
            self.exit_code = -15

    def kill(self):
        self.killed += 1
        self.exit_code = -9

    def wait(self, timeout):
        if self.exit_code is None:
            raise subprocess.TimeoutExpired("test-owned-process", timeout)
        return self.exit_code


class NativeEngineTests(unittest.TestCase):
    def connected_engine(self):
        engine = NativeEngine({"gateway": "vpn.example.com", "port": 443})
        peer, engine._sync = socket.socketpair()
        events, engine._async = socket.socketpair()
        engine._engine = Process()
        engine._reader = Mock()
        engine._reader.is_alive.return_value = True
        engine._started = True
        self.addCleanup(peer.close)
        self.addCleanup(events.close)
        self.addCleanup(engine.close)
        return engine, peer, events

    def test_calls_keep_one_sync_socket_and_never_repeat_registration(self):
        engine, peer, _ = self.connected_engine()
        requests = []

        def server():
            for code in (0x3000A, 0x20000):
                requests.append(receive(peer))
                peer.sendall(Message(4, 2, 1, 2, code, b"result").encode())

        worker = threading.Thread(target=server)
        worker.start()
        first = engine.call(3, 5, b"password-test-only", timeout=1)
        second = engine.call(3, 17, b"code-test-only", timeout=1)
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual([5, 17], [request.opcode for request in requests])
        self.assertEqual([b"password-test-only", b"code-test-only"],
                         [request.payload for request in requests])
        self.assertEqual((0x3000A, 0x20000), (first.opcode, second.opcode))
        self.assertTrue(engine.alive)
        self.assertIsInstance(engine.last_alive, float)

    def test_partial_response_timeout_invalidates_session_without_replay(self):
        engine, peer, _ = self.connected_engine()
        peer.sendall(struct.pack("!6I", 4, 2, 1, 2, 0x20000, 10) + b"x")
        with self.assertRaises(socket.timeout):
            engine.call(3, 17, b"do-not-replay", timeout=0.03)
        self.assertFalse(engine.alive)
        self.assertEqual("TimeoutError", engine.last_error)
        with self.assertRaisesRegex(RuntimeError, "restart"):
            engine.call(3, 17, b"do-not-replay")
        self.assertEqual(17, receive(peer).opcode)
        self.assertEqual(b"", peer.recv(1))

    def test_bad_route_invalidates_session_without_exposing_body(self):
        engine, peer, _ = self.connected_engine()
        secret = b"private-auth-response"
        peer.sendall(Message(1, 2, 1, 2, 0, secret).encode())
        with self.assertRaisesRegex(ProtocolError, "routing") as error:
            engine.call(9, 2, timeout=1)
        self.assertNotIn(secret.decode(), str(error.exception))
        self.assertFalse(engine.alive)

    def test_timeout_validation_does_not_write(self):
        engine, peer, _ = self.connected_engine()
        peer.settimeout(0.03)
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                engine.call(3, 17, b"must-not-send", timeout=timeout)
        with self.assertRaises(socket.timeout):
            peer.recv(1)
        self.assertTrue(engine.alive)

    def test_async_history_is_bounded_and_never_keeps_payloads(self):
        engine, _, events = self.connected_engine()
        engine._reader = threading.Thread(target=engine._read_events, daemon=True)
        engine._reader.start()
        for value in range(70):
            events.sendall(Message(4, 2, 2, 2, value, b"secret-event-body").encode())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            metadata = engine.event_metadata()
            if metadata and metadata[-1]["code"] == 69:
                break
            time.sleep(0.005)
        self.assertEqual(64, len(metadata))
        self.assertEqual(list(range(6, 70)), [event["code"] for event in metadata])
        self.assertEqual({"source", "destination", "connection_type", "class", "code",
                          "length", "received_at"}, set(metadata[0]))
        self.assertNotIn("secret", repr(engine.__dict__))
        metadata[0]["code"] = -1
        self.assertEqual(6, engine.event_metadata()[0]["code"])
        engine.close()
        self.assertFalse(engine._reader.is_alive())

    def test_async_eof_makes_session_unavailable(self):
        engine, _, events = self.connected_engine()
        engine._reader = threading.Thread(target=engine._read_events, daemon=True)
        engine._reader.start()
        events.close()
        engine._reader.join(timeout=1)
        self.assertFalse(engine.alive)
        self.assertEqual("ProtocolError", engine.last_error)

    def test_close_only_terminates_owned_launchers_and_is_idempotent(self):
        engine, _, _ = self.connected_engine()
        owned_engine, owned_helper = Process(terminate_timeout=True), Process(exit_code=0)
        engine._engine, engine._helper = owned_engine, owned_helper
        foreign = Process()
        engine.close()
        engine.close()
        self.assertEqual((1, 1), (owned_engine.terminated, owned_engine.killed))
        self.assertEqual(0, owned_helper.terminated)  # Daemon launcher already exited.
        self.assertEqual((0, 0), (foreign.terminated, foreign.killed))
        self.assertFalse(engine.alive)
        with self.assertRaisesRegex(RuntimeError, "restart"):
            engine.start()

    def test_start_registers_both_channels_and_checks_vendor_defaults(self):
        engine = NativeEngine({"gateway": "vpn.example.com", "port": 443})
        helper_channel = Mock()
        helper, native = Process(exit_code=0), Process()
        peer, sync = socket.socketpair()
        events, asynchronous = socket.socketpair()
        requests = []

        def server():
            requests.append(receive(peer))
            peer.sendall(Message(4, 2, 1, 9, 0xD0002, b"vendor-defaults").encode())

        def connect_helper(*_):
            engine._bootstrap_channel = helper_channel
            return 29191

        worker = threading.Thread(target=server)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as directory, \
                    patch("univpn_gateway.native_engine.RUNTIME_DIR", Path(directory)), \
                    patch.object(engine, "_check_isolation"), \
                    patch.object(engine, "_acquire_lock"), \
                    patch.object(engine, "_verify_binaries"), \
                    patch.object(engine, "_reject_existing_clients"), \
                    patch.object(engine, "_initialize_config"), \
                    patch.object(engine, "_helper_logs", return_value={}), \
                    patch.object(engine, "_launch", side_effect=[helper, native]) as launch, \
                    patch.object(engine, "_connect_helper", side_effect=connect_helper), \
                    patch.object(engine, "_connect_control", side_effect=[sync, asynchronous]) as connect:
                self.assertIs(engine, engine.start())
                self.assertIs(engine, engine.start())
                self.assertEqual([((1, unittest.mock.ANY),), ((2, unittest.mock.ANY),)],
                                 [(call.args,) for call in connect.call_args_list])
                self.assertIn("RemotePort=29191", launch.call_args_list[1].args[1])
                helper_channel.sendall.assert_called_once_with(
                    struct.pack("!IIIIHHII", 2, 1, 2, 1, 1, 8, 2, 19070))
            worker.join(timeout=2)
            self.assertEqual([(9, 2)], [(r.message_class, r.opcode) for r in requests])
        finally:
            engine.close()
            peer.close()
            events.close()
            worker.join(timeout=2)

    def test_defaults_survive_vendor_template_deletion_without_using_minimal_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install, home, runtime = root / "install", root / "home", root / "runtime"
            install.mkdir()
            runtime.mkdir()
            template = b"[GLOBAL]\nClientName=UniVPN\n[ADVANCED]\nVendorOption=1\n"
            source = install / "sysconfig.ini"
            source.write_bytes(template)

            def write_profile(_):
                user = home / "UniVPN"
                (user / "config").mkdir(parents=True, exist_ok=True)
                (user / "config/GATEWAY.ini").write_text("gateway fixture")
                (user / "sysconfig.ini").write_text("minimal profile defaults")

            with patch("univpn_gateway.native_engine.INSTALL_DIR", install), \
                    patch("univpn_gateway.native_engine.USER_HOME", home), \
                    patch("univpn_gateway.native_engine.RUNTIME_DIR", runtime), \
                    patch("univpn_gateway.native_engine.profile", side_effect=write_profile):
                engine = NativeEngine({"gateway": "vpn.example.com", "port": 443})
                engine._initialize_config()
                self.assertTrue((install / "config").is_dir())
                self.assertEqual(template, (home / "UniVPN/sysconfig.ini").read_bytes())
                source.unlink()
                engine._initialize_config()
                self.assertEqual(template, source.read_bytes())
                self.assertTrue((home / "UniVPN/config/GATEWAY.ini").is_file())

    def test_both_vendor_binary_hashes_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            install = Path(directory)
            client, helper = install / "serviceclient/UniVPNCS", install / "promote/UniVPNPromoteService"
            client.parent.mkdir()
            helper.parent.mkdir()
            client.write_bytes(b"known client fixture")
            helper.write_bytes(b"unexpected helper fixture")
            with patch("univpn_gateway.native_engine.INSTALL_DIR", install), \
                    patch("univpn_gateway.native_engine.CLIENT_SHA256",
                          hashlib.sha256(client.read_bytes()).hexdigest()), \
                    patch("univpn_gateway.native_engine.HELPER_SHA256", "0" * 64):
                with self.assertRaisesRegex(RuntimeError, "UniVPNPromoteService"):
                    NativeEngine._verify_binaries()

    def test_stable_image_template_restores_defaults_after_runtime_tmpfs_is_lost(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install, home, runtime = root / "install", root / "home", root / "runtime"
            stable = root / "vendor-sysconfig.ini"
            install.mkdir()
            runtime.mkdir()
            template = b"[GLOBAL]\nClientName=UniVPN\n[ADVANCED]\nFullDefaults=1\n"
            stable.write_bytes(template)

            def write_profile(_):
                (home / "UniVPN/config").mkdir(parents=True, exist_ok=True)
                (home / "UniVPN/sysconfig.ini").write_text("minimal profile")

            with patch("univpn_gateway.native_engine.INSTALL_DIR", install), \
                    patch("univpn_gateway.native_engine.USER_HOME", home), \
                    patch("univpn_gateway.native_engine.RUNTIME_DIR", runtime), \
                    patch("univpn_gateway.native_engine.VENDOR_SYSCONFIG", stable), \
                    patch("univpn_gateway.native_engine.profile", side_effect=write_profile):
                engine = NativeEngine({"gateway": "vpn.example.com", "port": 443})
                engine._initialize_config()
                self.assertEqual(template, (install / "sysconfig.ini").read_bytes())
                self.assertEqual(template, (home / "UniVPN/sysconfig.ini").read_bytes())
                (install / "sysconfig.ini").unlink()
                (runtime / "vendor-sysconfig.ini").unlink()
                engine._initialize_config()
                self.assertEqual(template, (install / "sysconfig.ini").read_bytes())
                self.assertEqual(template, (home / "UniVPN/sysconfig.ini").read_bytes())

    def test_control_readiness_retries_refused_connect_but_keeps_successful_socket(self):
        engine = NativeEngine({"gateway": "vpn.example.com", "port": 443})
        engine._engine = Process()
        refused, ready = Mock(), Mock()
        refused.connect.side_effect = ConnectionRefusedError()
        with patch("univpn_gateway.native_engine.socket.AF_UNIX", 1, create=True), \
                patch("univpn_gateway.native_engine.socket.socket", side_effect=[refused, ready]):
            channel = engine._connect_control(2, time.monotonic() + 1)
        self.assertIs(ready, channel)
        refused.close.assert_called_once()
        ready.close.assert_not_called()
        ready.sendall.assert_called_once_with(struct.pack("!6I", 2, 4, 2, 1, 0x10006, 0))

    def test_stale_helper_log_is_not_treated_as_new_readiness(self):
        engine = NativeEngine({"gateway": "vpn.example.com", "port": 443})
        engine._helper = Process(exit_code=0)
        baseline = {Path("old-log"): (1, 100, 123)}
        with patch.object(engine, "_helper_logs", return_value=baseline), \
                patch("univpn_gateway.native_engine.socket.socket") as factory:
            with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                engine._connect_helper(baseline, time.monotonic() + 0.02)
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
