import struct
import unittest

from univpn_gateway.native_messages import (
    AUTH_OK,
    LOGIN_RESPONSE_SIZE,
    SMS_REQUIRED,
    SYSTEM_CONFIG_OK,
    TUNNEL_START_OK,
    decode_login_response,
    first_connect,
    password_login,
    sms_code,
    tunnel_start,
)


class PayloadTests(unittest.TestCase):
    def test_one_site_first_connection_without_authentication_fields(self):
        data = first_connect("vpn.example.com", 443)
        self.assertEqual(0x9D0, len(data))
        self.assertEqual(b"vpn.example.com:443", data[4:0x104].split(b"\0")[0])
        self.assertEqual(b"vpn.example.com", data[0x108:0x188].split(b"\0")[0])
        self.assertEqual(443, struct.unpack_from("<I", data, 0x188)[0])
        self.assertEqual((1, -1), struct.unpack_from("<Ii", data, 0x9C8))

    def test_login_has_no_remember_password_or_cached_secret(self):
        data = password_login("vpn.example.com", 443, "test-user", "test-password")
        self.assertEqual(0x7BC, len(data))
        self.assertEqual((1, 0, 0, 1, 1), struct.unpack_from("<5I", data))
        self.assertEqual(b"vpn.example.com:443", data[0x414:0x514].split(b"\0")[0])
        self.assertEqual(b"test-user", data[0x514:0x694].split(b"\0")[0])
        self.assertEqual(b"test-password", data[0x694:0x714].split(b"\0")[0])
        self.assertFalse(any(data[0x734:]))

    def test_certificate_verification_requires_explicit_opt_out(self):
        data = password_login(
            "vpn.example.com", 443, "test-user", "test-password",
            verify_gateway_certificate=False,
        )
        self.assertEqual(b"\0\0\0\0", data[:4])
        for value in (0, 1, None, "false"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                password_login(
                    "vpn.example.com", 443, "user", "password",
                    verify_gateway_certificate=value,
                )

    def test_sms_has_zero_prefix_and_keeps_leading_zero(self):
        data = sms_code("012345")
        self.assertEqual(0x84, len(data))
        self.assertEqual(b"\0\0\0\0" + b"012345\0", data[:11])

    def test_nul_and_overlong_secrets_are_rejected_without_echo(self):
        for secret in ("sensitive\0value", "s" * 128):
            with self.assertRaises(ValueError) as error:
                password_login("vpn.example.com", 443, "user", secret)
            self.assertNotIn(secret, str(error.exception))

    def test_non_ascii_code_and_invalid_gateway_rejected(self):
        for code in ("１２３４５６", "123\n456", "", "12345678901"):
            with self.assertRaises(ValueError):
                sms_code(code)
        with self.assertRaises(ValueError):
            first_connect("vpn.example.com\n", 443)

    def test_tunnel_start_uses_network_order_and_flag_order(self):
        self.assertEqual(
            bytes.fromhex("01020304 00000001 00000000"),
            tunnel_start(0x01020304, 1, 0),
        )
        self.assertEqual(
            bytes.fromhex("00000003 00000000 00000001"),
            tunnel_start(3, 0, 1),
        )

    def test_tunnel_mode_is_bounded_and_flags_are_binary_integers(self):
        for mode in (-1, 0x100000000, True, 1.0, "1"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                tunnel_start(mode, 0, 0)
        for flag in (-1, 2, True, 0.0, "0", None):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                tunnel_start(1, flag, 0)
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                tunnel_start(1, 0, flag)

    def test_login_response_decodes_numeric_values_in_network_order(self):
        wire = bytes.fromhex(
            "01020304 11223344 55667788 99aabbcc ddeeff00 00000001 ffffffff 1234 abcd"
        )
        expected = (0x01020304, 0x11223344, 0x55667788, 0x99AABBCC,
                    0xDDEEFF00, 1, 0xFFFFFFFF, 0x1234, 0xABCD)
        self.assertEqual(expected, decode_login_response(wire))
        self.assertEqual(expected, decode_login_response(bytearray(wire)))
        self.assertTrue(all(type(value) is int for value in decode_login_response(wire)))

    def test_login_response_rejects_truncation_and_trailing_data(self):
        for wire in (b"", b"\0" * 31, b"\0" * 33, "sensitive-response"):
            with self.subTest(length=len(wire)), self.assertRaises(ValueError) as error:
                decode_login_response(wire)
            self.assertNotIn("sensitive-response", str(error.exception))

    def test_response_constants(self):
        self.assertEqual(0x00020000, AUTH_OK)
        self.assertEqual(0x0003000A, SMS_REQUIRED)
        self.assertEqual(0x000D0002, SYSTEM_CONFIG_OK)
        self.assertEqual(0x00070000, TUNNEL_START_OK)
        self.assertEqual(32, LOGIN_RESPONSE_SIZE)


if __name__ == "__main__":
    unittest.main()
