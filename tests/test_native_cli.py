import contextlib
import io
import os
import sys
import unittest
from unittest.mock import patch

from univpn_gateway.cli import main


class TerminalFlowTests(unittest.TestCase):
    def run_cli(self, command, responses, secrets=(), tty=True):
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"VPN_BACKEND": "native"}), \
                patch.object(sys, "argv", ["vpnctl", "--config", "examples/vpn.json", *command]), \
                patch.object(sys.stdin, "isatty", return_value=tty), \
                patch("builtins.input", return_value="fixture-user"), \
                patch("univpn_gateway.cli.getpass.getpass", side_effect=secrets) as prompt, \
                patch("univpn_gateway.native_control.send", side_effect=responses) as send, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main()
        for secret in secrets:
            self.assertNotIn(secret, output.getvalue() + errors.getvalue())
        return code, send.call_args_list, prompt.call_count

    def test_login_continues_same_challenge_without_second_login(self):
        challenge = {"state": "awaiting_sms", "attempt_id": "fixture-attempt",
                     "challenge_id": "fixture-challenge"}
        code, calls, prompts = self.run_cli(
            ["login"], [challenge, {"state": "authenticated", "authenticated": True}],
            ["fixture-password", "012345"])
        self.assertEqual(0, code)
        self.assertEqual(["login", "sms"], [c.args[0] for c in calls])
        self.assertEqual("fixture-attempt", calls[1].kwargs["attempt_id"])
        self.assertEqual("fixture-challenge", calls[1].kwargs["challenge_id"])
        self.assertEqual(2, prompts)

    def test_no_otp_keeps_session_available_for_later(self):
        code, calls, prompts = self.run_cli(
            ["login", "--no-otp"], [{"state": "awaiting_sms"}], ["fixture-password"])
        self.assertEqual(0, code)
        self.assertEqual(["login"], [c.args[0] for c in calls])
        self.assertEqual(1, prompts)

    def test_later_sms_uses_current_attempt_without_password(self):
        code, calls, prompts = self.run_cli(["sms"], [
            {"state": "awaiting_sms", "attempt_id": "a", "challenge_id": "c"},
            {"state": "authenticated", "authenticated": True}], ["012345"])
        self.assertEqual(0, code)
        self.assertEqual(["status", "sms"], [c.args[0] for c in calls])
        self.assertEqual(1, prompts)

    def test_expired_session_does_not_prompt_or_resend(self):
        code, calls, prompts = self.run_cli(["sms"], [{"state": "expired"}])
        self.assertEqual(2, code)
        self.assertEqual(["status"], [c.args[0] for c in calls])
        self.assertEqual(0, prompts)

    def test_authentication_alone_does_not_pass_status_healthcheck(self):
        code, calls, prompts = self.run_cli(["status"], [{
            "state": "authenticated", "authenticated": True, "data_plane_ready": False}])
        self.assertEqual(1, code)
        self.assertEqual(0, prompts)

    def test_non_tty_does_not_read_or_send_credentials(self):
        code, calls, prompts = self.run_cli(["login"], [], tty=False)
        self.assertEqual(2, code)
        self.assertEqual([], calls)
        self.assertEqual(0, prompts)


if __name__ == "__main__":
    unittest.main()
