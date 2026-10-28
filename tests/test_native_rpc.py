import socket
import struct
import threading
import time
import unittest
from unittest.mock import patch

from univpn_gateway.native_rpc import (
    Message, MAX_PAYLOAD, ProtocolError, receive, receive_exact, request,
)


class TransportTests(unittest.TestCase):
    def test_fragmented_binary_payload(self):
        left, right = socket.socketpair()
        message = Message(4, 2, 1, 4, 17, b"\x00hello\xff\x00")
        def send():
            try:
                for byte in message.encode():
                    left.sendall(bytes([byte]))
            finally:
                left.close()
        thread = threading.Thread(target=send)
        thread.start()
        try:
            self.assertEqual(message, receive(right))
        finally:
            right.close()
            thread.join()

    def test_oversized_header_rejected_without_reading_body(self):
        left, right = socket.socketpair()
        try:
            left.sendall(struct.pack("!6I", 4, 2, 1, 4, 17, MAX_PAYLOAD + 1))
            with self.assertRaisesRegex(ProtocolError, "oversized"):
                receive(right)
        finally:
            left.close()
            right.close()

    def test_truncated_message_not_accepted(self):
        left, right = socket.socketpair()
        left.sendall(Message(4, 2, 1, 4, 17, b"abc").encode()[:-1])
        left.close()
        try:
            with self.assertRaisesRegex(ProtocolError, "closed"):
                receive(right)
        finally:
            right.close()

    def test_big_endian_header_matches_independent_fields(self):
        data = Message(2, 4, 1, 3, 17, b"otp").encode()
        self.assertEqual((2, 4, 1, 3, 17, 3), struct.unpack("!6I", data[:24]))

    def test_message_repr_hides_payload_but_encoding_preserves_it(self):
        secret = b"private-test-password-and-code"
        message = Message(2, 4, 1, 3, 17, secret)
        self.assertNotIn(secret.decode(), repr(message))
        self.assertNotIn("payload=", repr(message))
        self.assertEqual(secret, message.encode()[24:])

    def test_coalesced_frames_are_received_separately(self):
        left, right = socket.socketpair()
        first = Message(4, 2, 1, 4, 0, b"first")
        second = Message(4, 2, 2, 4, 0x10002, b"second\x00")
        try:
            left.sendall(first.encode() + second.encode())
            self.assertEqual(first, receive(right))
            self.assertEqual(second, receive(right))
        finally:
            left.close()
            right.close()

    def test_eof_before_any_header_is_not_an_empty_response(self):
        left, right = socket.socketpair()
        left.close()
        try:
            with self.assertRaisesRegex(ProtocolError, "closed"):
                receive(right)
        finally:
            right.close()

    def test_eof_in_partial_header_rejected(self):
        left, right = socket.socketpair()
        left.sendall(struct.pack("!6I", 4, 2, 1, 4, 0, 0)[:13])
        left.close()
        try:
            with self.assertRaisesRegex(ProtocolError, "closed"):
                receive(right)
        finally:
            right.close()

    def test_zero_length_payload_does_not_consume_next_header(self):
        left, right = socket.socketpair()
        try:
            left.sendall(struct.pack("!6I", 4, 2, 1, 4, 0, 0)
                         + struct.pack("!6I", 4, 2, 1, 4, 1, 1) + b"x")
            self.assertEqual(b"", receive(right).payload)
            self.assertEqual(b"x", receive(right).payload)
        finally:
            left.close()
            right.close()

    def test_timeout_during_header_or_body_is_propagated(self):
        for prefix in (b"", struct.pack("!6I", 4, 2, 1, 4, 0, 2) + b"x"):
            with self.subTest(prefix_length=len(prefix)):
                left, right = socket.socketpair()
                right.settimeout(0.05)
                try:
                    if prefix:
                        left.sendall(prefix)
                    with self.assertRaises(socket.timeout):
                        receive(right)
                finally:
                    left.close()
                    right.close()

    def test_request_sends_handshake_then_body_once_and_accepts_result_code(self):
        # The response opcode is a result code, not an echo of request opcode.
        # Feed a literal peer response and inspect independent header fields.
        left, right = socket.socketpair()
        response = struct.pack("!6I", 4, 2, 1, 4, 0, 3) + b"ok\x00"
        left.sendall(response)

        class Endpoint:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                right.close()

            def settimeout(self, timeout):
                right.settimeout(timeout)

            def connect(self, endpoint):
                self.endpoint = endpoint

            def sendall(self, data):
                right.sendall(data)

            def recv(self, size):
                return right.recv(size)

        endpoint = Endpoint()
        try:
            with patch("univpn_gateway.native_rpc.socket.AF_UNIX", 1, create=True), \
                    patch("univpn_gateway.native_rpc.socket.socket", return_value=endpoint):
                result = request("/tmp/test-endpoint", 0x20010, b"input", capacity=8,
                                 message_class=3, timeout=1)
            sent = bytearray()
            while True:
                part = left.recv(4096)
                if not part:
                    break
                sent.extend(part)
            self.assertEqual("/tmp/test-endpoint", endpoint.endpoint)
            self.assertEqual((2, 4, 1, 1, 0x10006, 0), struct.unpack("!6I", sent[:24]))
            self.assertEqual((2, 4, 1, 3, 0x20010, 8), struct.unpack("!6I", sent[24:48]))
            self.assertEqual(b"input\x00\x00\x00", bytes(sent[48:]))
            self.assertEqual(0, result.opcode)
            self.assertEqual(b"ok\x00", result.payload)
        finally:
            left.close()
            right.close()

    def test_unexpected_response_routing_or_type_rejected_without_payload_echo(self):
        sensitive = b"not-for-diagnostics"
        for source, destination, connection_type in ((1, 2, 1), (4, 3, 1), (4, 2, 2)):
            with self.subTest(source=source, destination=destination, connection_type=connection_type):
                left, right = socket.socketpair()
                left.sendall(struct.pack("!6I", source, destination, connection_type, 4, 0, len(sensitive))
                             + sensitive)

                class Endpoint:
                    def __enter__(self):
                        return self

                    def __exit__(self, *_):
                        right.close()

                    def settimeout(self, timeout):
                        right.settimeout(timeout)

                    def connect(self, _):
                        pass

                    def sendall(self, data):
                        right.sendall(data)

                    def recv(self, size):
                        return right.recv(size)

                try:
                    with patch("univpn_gateway.native_rpc.socket.AF_UNIX", 1, create=True), \
                            patch("univpn_gateway.native_rpc.socket.socket", return_value=Endpoint()):
                        with self.assertRaisesRegex(ProtocolError, "routing|connection type") as error:
                            request("/tmp/test-endpoint", 0x20010, timeout=1)
                    self.assertNotIn(sensitive.decode(), str(error.exception))
                finally:
                    left.close()
                    right.close()

    def test_request_capacity_validation_happens_before_opening_channel(self):
        for capacity in (2, MAX_PAYLOAD + 1):
            with patch("univpn_gateway.native_rpc.socket.socket") as factory:
                with self.assertRaisesRegex(ProtocolError, "capacity"):
                    request("/tmp/test-endpoint", 17, b"long", capacity=capacity)
                factory.assert_not_called()

    def test_request_timeout_closes_channel_without_retry_or_payload_echo(self):
        class Endpoint:
            closed = False
            connects = 0
            sends = 0

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.closed = True

            def settimeout(self, timeout):
                self.timeout = timeout

            def connect(self, _):
                self.connects += 1

            def sendall(self, data):
                self.sends += 1

            def recv(self, _):
                raise socket.timeout("control response timed out")

        endpoint = Endpoint()
        with patch("univpn_gateway.native_rpc.socket.AF_UNIX", 1, create=True), \
                patch("univpn_gateway.native_rpc.socket.socket", return_value=endpoint):
            with self.assertRaises(socket.timeout) as error:
                request("/tmp/test-endpoint", 17, b"secret-test-only", timeout=0.05)
        self.assertEqual(1, endpoint.connects)
        self.assertEqual(2, endpoint.sends)  # One initialization, one request.
        self.assertGreater(endpoint.timeout, 0)
        # Absolute monotonic deadline arithmetic can add sub-nanosecond roundoff.
        self.assertLessEqual(endpoint.timeout, 0.05 + 1e-9)
        self.assertTrue(endpoint.closed)
        self.assertNotIn("secret-test-only", str(error.exception))

    def test_expired_deadline_rejects_before_reading(self):
        class Channel:
            reads = 0

            def recv(self, _):
                self.reads += 1
                return b"x"

        channel = Channel()
        with patch("univpn_gateway.native_rpc.time.monotonic", return_value=100):
            with self.assertRaises(socket.timeout):
                receive_exact(channel, 1, deadline=100)
        self.assertEqual(0, channel.reads)

    def test_slow_fragments_cannot_reset_read_deadline(self):
        clock = [100.0]

        class Channel:
            reads = 0
            timeout = 7.0
            timeouts = []

            def gettimeout(self):
                return self.timeout

            def settimeout(self, timeout):
                self.timeouts.append(timeout)
                self.timeout = timeout

            def recv(self, _):
                self.reads += 1
                clock[0] += 0.4
                return b"x"

        channel = Channel()
        with patch("univpn_gateway.native_rpc.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(socket.timeout):
                receive_exact(channel, 4, deadline=101)
        self.assertEqual(3, channel.reads)
        self.assertEqual(4, len(channel.timeouts))
        for actual, expected in zip(channel.timeouts[:-1], (1.0, 0.6, 0.2)):
            self.assertAlmostEqual(expected, actual)
        self.assertEqual(7.0, channel.timeout)

    def test_header_and_body_share_one_deadline(self):
        clock = [100.0]

        class Channel:
            reads = 0

            def settimeout(self, timeout):
                self.timeout = timeout

            def recv(self, size):
                self.reads += 1
                if self.reads == 1:
                    clock[0] += 0.8
                    return struct.pack("!6I", 4, 2, 1, 3, 0x20000, 1)
                clock[0] += 0.3
                return b"x"

        channel = Channel()
        with patch("univpn_gateway.native_rpc.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(socket.timeout):
                receive(channel, deadline=101)
        self.assertEqual(2, channel.reads)
        self.assertAlmostEqual(0.2, channel.timeout)

    def test_deadline_read_restores_timeout_for_persistent_channel(self):
        left, right = socket.socketpair()
        first = Message(4, 2, 1, 3, 0x3000A, b"first")
        second = Message(4, 2, 1, 3, 0x20000, b"second")
        right.settimeout(0.75)
        try:
            left.sendall(first.encode() + second.encode())
            self.assertEqual(first, receive(right, deadline=time.monotonic() + 0.5))
            self.assertEqual(0.75, right.gettimeout())
            self.assertEqual(second, receive(right))
            self.assertEqual(0.75, right.gettimeout())
        finally:
            left.close()
            right.close()

    def test_request_deadline_includes_connect_and_both_sends(self):
        for slow_stage in range(3):
            with self.subTest(slow_stage=slow_stage):
                clock = [100.0]

                class Endpoint:
                    actions = []
                    closed = False

                    def __enter__(self):
                        return self

                    def __exit__(self, *_):
                        self.closed = True

                    def settimeout(self, timeout):
                        self.timeout = timeout

                    def advance(self, action):
                        self.actions.append(action)
                        clock[0] += 1.1 if len(self.actions) - 1 == slow_stage else 0.1

                    def connect(self, _):
                        self.advance("connect")

                    def sendall(self, _):
                        self.advance("send")

                    def recv(self, _):
                        self.actions.append("receive")
                        return b""

                endpoint = Endpoint()
                with patch("univpn_gateway.native_rpc.time.monotonic", side_effect=lambda: clock[0]), \
                        patch("univpn_gateway.native_rpc.socket.AF_UNIX", 1, create=True), \
                        patch("univpn_gateway.native_rpc.socket.socket", return_value=endpoint):
                    with self.assertRaises(socket.timeout):
                        request("/tmp/test-endpoint", 17, timeout=1)
                self.assertEqual(["connect", "send", "send"][:slow_stage + 1], endpoint.actions)
                self.assertTrue(endpoint.closed)

    def test_request_budget_is_shared_across_all_io(self):
        clock = [100.0]

        class Endpoint:
            actions = []
            timeouts = []

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.closed = True

            def settimeout(self, timeout):
                self.timeouts.append(timeout)

            def connect(self, _):
                self.actions.append("connect")
                clock[0] += 0.2

            def sendall(self, _):
                self.actions.append("send")
                clock[0] += 0.15

            def recv(self, _):
                self.actions.append("receive")
                clock[0] += 0.3
                if len(self.actions) == 4:
                    return struct.pack("!6I", 4, 2, 1, 3, 0x20000, 1)
                return b"x"

        endpoint = Endpoint()
        with patch("univpn_gateway.native_rpc.time.monotonic", side_effect=lambda: clock[0]), \
                patch("univpn_gateway.native_rpc.socket.AF_UNIX", 1, create=True), \
                patch("univpn_gateway.native_rpc.socket.socket", return_value=endpoint):
            with self.assertRaises(socket.timeout):
                request("/tmp/test-endpoint", 17, timeout=1)
        self.assertEqual(["connect", "send", "send", "receive", "receive"], endpoint.actions)
        self.assertEqual(5, len(endpoint.timeouts))
        for actual, expected in zip(endpoint.timeouts, (1.0, 0.8, 0.65, 0.5, 0.2)):
            self.assertAlmostEqual(expected, actual)
        self.assertTrue(endpoint.closed)

    def test_request_none_timeout_keeps_unbounded_legacy_api(self):
        class Endpoint:
            timeouts = []

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def settimeout(self, timeout):
                self.timeouts.append(timeout)

            def connect(self, _):
                pass

            def sendall(self, _):
                pass

            def recv(self, _):
                return struct.pack("!6I", 4, 2, 1, 4, 0x20000, 0)

        endpoint = Endpoint()
        with patch("univpn_gateway.native_rpc.time.monotonic", side_effect=AssertionError("no deadline")), \
                patch("univpn_gateway.native_rpc.socket.AF_UNIX", 1, create=True), \
                patch("univpn_gateway.native_rpc.socket.socket", return_value=endpoint):
            result = request("/tmp/test-endpoint", 17, timeout=None)
        self.assertEqual([None], endpoint.timeouts)
        self.assertEqual(0x20000, result.opcode)


if __name__ == "__main__":
    unittest.main()
