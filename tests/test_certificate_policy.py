from dataclasses import FrozenInstanceError
import hashlib
import socket
import ssl
import unittest
from unittest.mock import MagicMock, patch

from univpn_gateway.certificate_policy import CertificatePolicyError, verify_gateway


class CertificatePolicyTests(unittest.TestCase):
    def setUp(self):
        self.leaf = b"private-peer-certificate-DER-fixture"
        self.pin = hashlib.sha256(self.leaf).hexdigest()
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.peer = MagicMock()
        self.peer.__enter__.return_value = self.peer
        self.peer.getpeercert.return_value = self.leaf
        self.context = MagicMock()
        self.context.wrap_socket.return_value = self.peer
        self.connect_patch = patch("univpn_gateway.certificate_policy.socket.create_connection",
                                   return_value=self.connection)
        self.default_patch = patch("univpn_gateway.certificate_policy.ssl.create_default_context",
                                   return_value=self.context)
        self.pin_patch = patch("univpn_gateway.certificate_policy.ssl.SSLContext",
                               return_value=self.context)
        self.connect = self.connect_patch.start()
        self.default_context = self.default_patch.start()
        self.pin_context = self.pin_patch.start()
        self.addCleanup(self.connect_patch.stop)
        self.addCleanup(self.default_patch.stop)
        self.addCleanup(self.pin_patch.stop)

    def test_default_requires_chain_and_hostname_validation(self):
        decision = verify_gateway("vpn.example.com", 443)
        self.default_context.assert_called_once_with()
        self.pin_context.assert_not_called()
        self.assertIs(self.context.check_hostname, True)
        self.assertEqual(ssl.CERT_REQUIRED, self.context.verify_mode)
        self.context.wrap_socket.assert_called_once_with(self.connection,
                                                        server_hostname="vpn.example.com")
        self.peer.getpeercert.assert_called_once_with(binary_form=True)
        self.connect.assert_called_once_with(("vpn.example.com", 443), timeout=8)
        self.assertEqual(("vpn.example.com", 443, self.pin, True),
                         (decision.gateway, decision.port, decision.sha256,
                          decision.verify_in_engine))
        self.assertFalse(decision.allows_override_for("vpn.example.com", 443))

    def test_explicit_pin_requires_exact_digest_and_scopes_override(self):
        decision = verify_gateway("vpn.example.com", 49999,
                                  approved_sha256=self.pin.upper(), timeout=3)
        self.default_context.assert_not_called()
        self.pin_context.assert_called_once_with(ssl.PROTOCOL_TLS_CLIENT)
        self.assertIs(self.context.check_hostname, False)
        self.assertEqual(ssl.CERT_NONE, self.context.verify_mode)
        self.assertFalse(decision.verify_in_engine)
        self.assertTrue(decision.allows_override_for("vpn.example.com", 49999))
        self.assertFalse(decision.allows_override_for("other.example.com", 49999))
        self.assertFalse(decision.allows_override_for("vpn.example.com", 443))
        self.assertEqual(self.pin, decision.sha256)
        self.connect.assert_called_once_with(("vpn.example.com", 49999), timeout=3)

    def test_pin_match_uses_constant_time_comparison(self):
        with patch("univpn_gateway.certificate_policy.hmac.compare_digest",
                   return_value=True) as compare:
            verify_gateway("vpn.example.com", 443, self.pin)
        compare.assert_called_once_with(hashlib.sha256(self.leaf).digest(), bytes.fromhex(self.pin))

    def test_pin_mismatch_is_generic_and_never_falls_back(self):
        with self.assertRaises(CertificatePolicyError) as error:
            verify_gateway("vpn.example.com", 443, "00" * 32)
        self.assertEqual("gateway certificate verification failed", str(error.exception))
        self.assertNotIn(self.leaf.decode(), str(error.exception))
        self.default_context.assert_not_called()
        self.pin_context.assert_called_once()
        self.connect.assert_called_once()
        self.peer.__exit__.assert_called_once()
        self.connection.__exit__.assert_called_once()

    def test_chain_failure_never_downgrades_or_exposes_peer_details(self):
        self.context.wrap_socket.side_effect = ssl.SSLCertVerificationError(
            1, "private peer subject and identity")
        with self.assertRaises(CertificatePolicyError) as error:
            verify_gateway("vpn.example.com", 443)
        self.assertEqual("gateway certificate verification failed", str(error.exception))
        self.assertNotIn("private peer", str(error.exception))
        self.pin_context.assert_not_called()
        self.connect.assert_called_once()

    def test_network_error_does_not_retry_or_fall_back(self):
        self.connect.side_effect = socket.timeout("private endpoint timeout")
        with self.assertRaises(CertificatePolicyError) as error:
            verify_gateway("vpn.example.com", 443, self.pin)
        self.assertEqual("gateway certificate verification failed", str(error.exception))
        self.default_context.assert_not_called()
        self.connect.assert_called_once()

    def test_invalid_pin_rejected_before_network_or_context_creation(self):
        for value in ("", "a" * 63, "a" * 65, "g" * 64, "a:" * 32, 123, b"a" * 64):
            with self.subTest(kind=type(value).__name__, length=len(value) if hasattr(value, "__len__") else 0):
                with self.assertRaises(ValueError):
                    verify_gateway("vpn.example.com", 443, value)
        self.connect.assert_not_called()
        self.default_context.assert_not_called()
        self.pin_context.assert_not_called()

    def test_empty_or_nonbinary_certificate_rejected(self):
        for leaf in (None, b"", "certificate text", {}):
            with self.subTest(kind=type(leaf).__name__):
                self.peer.getpeercert.return_value = leaf
                with self.assertRaises(CertificatePolicyError):
                    verify_gateway("vpn.example.com", 443, self.pin)

    def test_decision_is_immutable_and_repr_is_opaque(self):
        decision = verify_gateway("private-endpoint.example.com", 443, self.pin)
        with self.assertRaises(FrozenInstanceError):
            decision.verify_in_engine = True
        self.assertEqual("CertificateDecision(verify_in_engine=False)", repr(decision))
        self.assertNotIn(self.leaf.decode(), repr(decision))
        self.assertNotIn("private-endpoint", repr(decision))
        self.assertFalse(hasattr(decision, "certificate"))

    def test_no_application_data_or_credentials_are_sent(self):
        verify_gateway("vpn.example.com", 443, self.pin)
        for channel in (self.connection, self.peer):
            channel.send.assert_not_called()
            channel.sendall.assert_not_called()
            channel.write.assert_not_called()

    def test_invalid_endpoint_or_timeout_rejected_before_connection(self):
        for gateway, port, timeout in (("", 443, 8), ("bad\nname", 443, 8),
                                       ("vpn.example.com", True, 8),
                                       ("vpn.example.com", 0, 8),
                                       ("vpn.example.com", 65536, 8),
                                       ("vpn.example.com", 443, 0),
                                       ("vpn.example.com", 443, float("nan"))):
            with self.assertRaises(ValueError):
                verify_gateway(gateway, port, timeout=timeout)
        self.connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
