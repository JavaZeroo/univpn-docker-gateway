import json
import errno
import unittest
from unittest.mock import MagicMock, patch

from univpn_gateway.native_control import send


class ControlTests(unittest.TestCase):
    def exchange(self, response):
        channel = MagicMock()
        channel.__enter__.return_value = channel
        channel.recv.return_value = json.dumps(response).encode() + b"\n"
        with patch("univpn_gateway.native_control.socket.AF_UNIX", 1, create=True), \
                patch("univpn_gateway.native_control.socket.socket", return_value=channel):
            return send("status")

    def test_idle_status_with_no_auth_error_is_data(self):
        status = {"state": "idle", "error": None, "data_plane_ready": False}
        self.assertEqual(status, self.exchange(status))

    def test_authentication_failure_is_still_inspectable_status(self):
        status = {"state": "failed", "error": "authentication_rejected"}
        self.assertEqual(status, self.exchange(status))

    def test_control_failure_is_distinct(self):
        with self.assertRaisesRegex(RuntimeError, "operation failed"):
            self.exchange({"control_error": "operation failed: SessionExpired"})

    def test_non_object_response_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "invalid"):
            self.exchange(["unexpected"])

    def test_boot_wait_never_sends_credentials_to_a_failed_connection(self):
        unavailable = MagicMock()
        unavailable.connect.side_effect = OSError(errno.ENOENT, "not started")
        connected = MagicMock()
        connected.__enter__.return_value = connected
        connected.recv.return_value = b'{"state":"awaiting_sms"}\n'
        with patch("univpn_gateway.native_control.socket.AF_UNIX", 1, create=True), \
                patch("univpn_gateway.native_control.socket.socket", side_effect=[unavailable, connected]), \
                patch("univpn_gateway.native_control.time.sleep"):
            result = send("login", username="fixture", password="fixture-secret")
        unavailable.sendall.assert_not_called()
        unavailable.close.assert_called_once()
        connected.sendall.assert_called_once()
        self.assertEqual("awaiting_sms", result["state"])


if __name__ == "__main__":
    unittest.main()
