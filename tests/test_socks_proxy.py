from contextlib import redirect_stderr, redirect_stdout
import errno
import io
import ipaddress
import json
import socket
import struct
import subprocess
import threading
import time
import unittest
from unittest.mock import Mock, patch

from univpn_gateway import socks_proxy as socks


def receive(channel, count):
    result = bytearray()
    while len(result) < count:
        data = channel.recv(count - len(result))
        if not data:
            raise EOFError("test connection closed")
        result.extend(data)
    return bytes(result)


def connect(address):
    # A literal-address socket avoids test-side DNS too.
    channel = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    channel.settimeout(2)
    channel.connect(address)
    return channel


def ipv4_request(host, port, command=1):
    return bytes((5, command, 0, 1)) + socket.inet_aton(host) + struct.pack("!H", port)


def numeric_domain_request(host, port):
    value = host.encode("ascii")
    return b"\x05\x01\x00\x03" + bytes((len(value),)) + value + struct.pack("!H", port)


class Echo:
    def __init__(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.listener.settimeout(0.1)
        self.address = self.listener.getsockname()
        self.stop = threading.Event()
        self.clients = []
        self.threads = []
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            try:
                channel, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.clients.append(channel)
            thread = threading.Thread(target=self.echo, args=(channel,), daemon=True)
            self.threads.append(thread)
            thread.start()

    @staticmethod
    def echo(channel):
        try:
            channel.settimeout(3)
            while True:
                data = channel.recv(16384)
                if not data:
                    return
                channel.sendall(data)
        except OSError:
            pass
        finally:
            socks._close(channel)

    def close(self):
        self.stop.set()
        socks._close(self.listener)
        for channel in self.clients:
            socks._close(channel)
        self.thread.join(1)
        for thread in self.threads:
            thread.join(1)


class LimitedSocket:
    def __init__(self, channel):
        self.channel = channel
        self.writes = 0

    def __getattr__(self, name):
        return getattr(self.channel, name)

    def send(self, data):
        self.writes += 1
        return self.channel.send(data[:7])


class SocksProxyTests(unittest.TestCase):
    def setUp(self):
        self.ready = threading.Event()
        self.ready.set()
        self.proxy = socks.SocksProxy(
            {"allowed_cidrs": ["127.0.0.1/32"], "vpn_interface": "vpn0"},
            self.ready.is_set, host="127.0.0.1", port=0)
        self.addCleanup(self.proxy.close)
        self.dial = Mock(side_effect=AssertionError("unexpected outbound dial"))
        self.proxy._dial = self.dial
        self.proxy.start()

    def client(self):
        channel = connect(self.proxy.address)
        self.addCleanup(channel.close)
        channel.sendall(b"\x05\x01\x00")
        self.assertEqual(b"\x05\x00", receive(channel, 2))
        return channel

    def request(self, frame):
        channel = self.client()
        channel.sendall(frame)
        return channel, receive(channel, 10)

    def use_echo(self, limited=False):
        server = Echo()
        self.addCleanup(server.close)
        def dial(address, port, deadline):
            self.assertEqual("127.0.0.1", str(address))
            self.assertEqual(server.address[1], port)
            self.assertGreater(deadline, time.monotonic())
            channel = connect((str(address), port))
            return LimitedSocket(channel) if limited else channel
        self.dial.side_effect = dial
        return server

    def test_real_loopback_stream_and_bound_address_reply(self):
        server = self.use_echo()
        channel, reply = self.request(ipv4_request(*server.address))
        self.assertEqual(b"\x05\x00\x00\x01", reply[:4])
        self.assertEqual("127.0.0.1", socket.inet_ntoa(reply[4:8]))
        self.assertNotEqual(0, struct.unpack("!H", reply[8:])[0])
        channel.sendall(b"TCP payload")
        self.assertEqual(b"TCP payload", receive(channel, 11))

    def test_canonical_ipv4_in_domain_field_does_not_resolve_dns(self):
        server = self.use_echo()
        with patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")):
            channel, reply = self.request(numeric_domain_request(*server.address))
            self.assertEqual(0, reply[1])
            channel.sendall(b"numeric")
            self.assertEqual(b"numeric", receive(channel, 7))

    def test_domain_ipv6_and_noncanonical_numeric_addresses_are_rejected(self):
        frames = [numeric_domain_request("secret.example", 22),
                  numeric_domain_request("127.000.0.1", 22),
                  numeric_domain_request("2130706433", 22),
                  b"\x05\x01\x00\x03\x00", b"\x05\x01\x00\x03\xff",
                  b"\x05\x01\x00\x04" + b"\0" * 18]
        with patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")):
            for frame in frames:
                with self.subTest(frame_type=frame[3]):
                    _, reply = self.request(frame)
                    self.assertEqual(8, reply[1])
        self.dial.assert_not_called()

    def test_bind_and_udp_are_explicitly_unsupported(self):
        for command in (2, 3, 255):
            _, reply = self.request(ipv4_request("127.0.0.1", 22, command))
            self.assertEqual(7, reply[1])
        self.dial.assert_not_called()

    def test_unlisted_address_and_zero_port_never_dial(self):
        for host, port in (("127.0.0.2", 22), ("127.0.0.1", 0)):
            _, reply = self.request(ipv4_request(host, port))
            self.assertEqual(2, reply[1])
        self.dial.assert_not_called()

    def test_invalid_request_version_or_reserved_byte_is_rejected(self):
        for header in (b"\x04\x01\x00\x01", b"\x05\x01\x01\x01"):
            _, reply = self.request(header)
            self.assertEqual(1, reply[1])
        self.dial.assert_not_called()

    def test_bad_greeting_or_no_supported_method_has_no_outbound_effect(self):
        for greeting in (b"\x04\x01", b"\x05\x00", b"\x05\x01\x02"):
            with connect(self.proxy.address) as channel:
                channel.sendall(greeting)
                self.assertEqual(b"\x05\xff", receive(channel, 2))
        self.dial.assert_not_called()

    def test_not_ready_or_failed_ready_reader_denies_new_connection(self):
        self.ready.clear()
        _, reply = self.request(ipv4_request("127.0.0.1", 22))
        self.assertEqual(2, reply[1])
        self.proxy._ready = Mock(side_effect=RuntimeError("private details"))
        _, reply = self.request(ipv4_request("127.0.0.1", 22))
        self.assertEqual(2, reply[1])
        self.dial.assert_not_called()

    def test_truthy_status_object_cannot_accidentally_bypass_ready_gate(self):
        self.proxy._ready = lambda: {"data_plane_ready": False}
        _, reply = self.request(ipv4_request("127.0.0.1", 22))
        self.assertEqual(2, reply[1])
        self.dial.assert_not_called()

    def test_readiness_revoked_during_connect_prevents_success_reply(self):
        server = self.use_echo()
        original = self.dial.side_effect
        def dial(*arguments):
            channel = original(*arguments)
            self.ready.clear()
            return channel
        self.dial.side_effect = dial
        _, reply = self.request(ipv4_request(*server.address))
        self.assertEqual(2, reply[1])

    def test_readiness_revocation_closes_existing_stream_within_one_second(self):
        server = self.use_echo()
        channel, reply = self.request(ipv4_request(*server.address))
        self.assertEqual(0, reply[1])
        channel.sendall(b"before")
        self.assertEqual(b"before", receive(channel, 6))
        start = time.monotonic()
        self.ready.clear()
        channel.settimeout(1)
        try:
            self.assertEqual(b"", channel.recv(1))
        except ConnectionResetError:
            pass
        self.assertLess(time.monotonic() - start, 1)

    def test_ready_recovery_allows_a_new_stream_without_restarting_proxy(self):
        server = self.use_echo()
        self.ready.clear()
        _, reply = self.request(ipv4_request(*server.address))
        self.assertEqual(2, reply[1])
        self.ready.set()
        channel, reply = self.request(ipv4_request(*server.address))
        self.assertEqual(0, reply[1])
        channel.sendall(b"recovered")
        self.assertEqual(b"recovered", receive(channel, 9))

    def test_one_mib_bidirectional_stream_and_half_close(self):
        server = self.use_echo()
        channel, reply = self.request(ipv4_request(*server.address))
        self.assertEqual(0, reply[1])
        payload = bytes(range(256)) * 4096
        errors = []
        def upload():
            try:
                channel.sendall(payload)
                channel.shutdown(socket.SHUT_WR)
            except OSError:
                errors.append("upload failed")
        worker = threading.Thread(target=upload, daemon=True)
        worker.start()
        response = bytearray()
        while True:
            chunk = channel.recv(65536)
            if not chunk:
                break
            response.extend(chunk)
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(payload, response)

    def test_partial_upstream_writes_preserve_all_bytes(self):
        server = self.use_echo(limited=True)
        channel, reply = self.request(ipv4_request(*server.address))
        self.assertEqual(0, reply[1])
        body = b"partial write" * 200
        channel.sendall(body)
        self.assertEqual(body, receive(channel, len(body)))

    def test_pipelined_application_bytes_are_not_lost(self):
        server = self.use_echo()
        channel = self.client()
        channel.sendall(ipv4_request(*server.address) + b"pipelined")
        self.assertEqual(0, receive(channel, 10)[1])
        self.assertEqual(b"pipelined", receive(channel, 9))

    def test_total_handshake_deadline_does_not_reset_after_each_byte(self):
        with patch.object(socks, "HANDSHAKE_TIMEOUT", 0.3):
            with connect(self.proxy.address) as channel:
                channel.sendall(b"\x05")
                time.sleep(0.16)
                channel.sendall(b"\x01")
                time.sleep(0.2)
                channel.settimeout(0.1)
                try:
                    self.assertEqual(b"", channel.recv(1))
                except ConnectionResetError:
                    pass
        self.dial.assert_not_called()

    def test_capacity_is_bounded_and_closed_handshakes_return_slots(self):
        self.proxy.close()
        with patch.object(socks, "MAX_CLIENTS", 2):
            self.proxy = socks.SocksProxy(
                {"allowed_cidrs": ["127.0.0.1/32"], "vpn_interface": "vpn0"},
                self.ready.is_set, host="127.0.0.1", port=0).start()
        self.addCleanup(self.proxy.close)
        clients = [connect(self.proxy.address) for _ in range(3)]
        for channel in clients:
            self.addCleanup(channel.close)
        clients[2].settimeout(1)
        self.assertEqual(b"", clients[2].recv(1))
        clients[0].close()
        clients[1].close()
        deadline = time.monotonic() + 1
        while True:
            with self.proxy._lock:
                if not self.proxy._workers:
                    break
            if time.monotonic() >= deadline:
                self.fail("handshake capacity was not released")
            time.sleep(0.01)
        channel = self.client()
        channel.sendall(ipv4_request("127.0.0.2", 22))
        self.assertEqual(2, receive(channel, 10)[1])

    def test_errors_and_requests_are_not_logged(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            self.dial.side_effect = RuntimeError("secret target and request")
            _, reply = self.request(ipv4_request("127.0.0.1", 22))
            self.assertEqual(1, reply[1])
            self.proxy.close()
        self.assertEqual("", output.getvalue())


class SocksDialTests(unittest.TestCase):
    def setUp(self):
        self.proxy = socks.SocksProxy(
            {"allowed_cidrs": ["10.80.0.11/32"], "vpn_interface": "cnem_vnic"}, lambda: True)
        self.addCleanup(self.proxy.close)
        self.address = ipaddress.IPv4Address("10.80.0.11")

    def test_device_binding_precedes_connect_and_has_no_unbound_fallback(self):
        channel = Mock(spec=socket.socket)
        with patch.object(socket, "SO_BINDTODEVICE", 25, create=True), \
                patch.object(socket, "socket", return_value=channel), \
                patch.object(self.proxy, "_route_matches", return_value=True):
            self.assertIs(channel, self.proxy._dial(self.address, 22, time.monotonic() + 5))
        channel.setsockopt.assert_called_once_with(socket.SOL_SOCKET, 25, b"cnem_vnic\0")
        channel.connect.assert_called_once_with(("10.80.0.11", 22))
        names = [call[0] for call in channel.mock_calls]
        self.assertLess(names.index("setsockopt"), names.index("connect"))

    def test_failed_device_binding_never_connects_or_uses_route_fallback(self):
        channel = Mock(spec=socket.socket)
        channel.setsockopt.side_effect = OSError(errno.EPERM, "binding denied")
        with patch.object(socket, "SO_BINDTODEVICE", 25, create=True), \
                patch.object(socket, "socket", return_value=channel), \
                patch.object(self.proxy, "_route_matches") as route:
            with self.assertRaises(OSError):
                self.proxy._dial(self.address, 22, time.monotonic() + 5)
        channel.connect.assert_not_called()
        route.assert_not_called()
        channel.close.assert_called_once()

    def test_wrong_route_or_exhausted_deadline_never_connects(self):
        for expired in (False, True):
            channel = Mock(spec=socket.socket)
            def route(*arguments):
                if expired:
                    time.sleep(0.03)
                    return True
                return False
            with patch.object(socket, "SO_BINDTODEVICE", 25, create=True), \
                    patch.object(socket, "socket", return_value=channel), \
                    patch.object(self.proxy, "_route_matches", side_effect=route):
                with self.assertRaises(socks._Rejected):
                    self.proxy._dial(self.address, 22, time.monotonic() + (0.01 if expired else 5))
            channel.connect.assert_not_called()

    def test_route_query_requires_one_matching_unicast_route(self):
        for data, expected in (([{"dev": "cnem_vnic"}], True),
                               ([{"dev": "eth0"}], False), ([], False),
                               ([{"dev": "cnem_vnic", "type": "blackhole"}], False),
                               ({"dev": "cnem_vnic"}, False)):
            with patch.object(socks.subprocess, "run", return_value=Mock(stdout=json.dumps(data))) as run:
                self.assertEqual(expected, self.proxy._route_matches(self.address, 0.5))
            self.assertEqual(["ip", "-j", "route", "get", "10.80.0.11"], run.call_args.args[0])
            self.assertEqual(0.5, run.call_args.kwargs["timeout"])
            self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_route_query_timeout_and_malformed_json_fail_closed(self):
        with patch.object(socks.subprocess, "run", side_effect=subprocess.TimeoutExpired("ip", 1)):
            self.assertFalse(self.proxy._route_matches(self.address, 1))
        with patch.object(socks.subprocess, "run", return_value=Mock(stdout="not JSON")):
            self.assertFalse(self.proxy._route_matches(self.address, 1))

    def test_missing_bind_to_device_support_fails_closed(self):
        with patch.object(socket, "SO_BINDTODEVICE", None, create=True):
            with self.assertRaises(socks._Rejected):
                self.proxy._dial(self.address, 22, time.monotonic() + 5)


if __name__ == "__main__":
    unittest.main()
