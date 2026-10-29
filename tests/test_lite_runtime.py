import signal
import subprocess
import unittest
from unittest.mock import Mock, patch

from univpn_gateway.lite_runtime import _finish, run_interactive


class LiteForegroundTests(unittest.TestCase):
    def test_no_tty_never_starts_or_logs_in(self):
        with patch("univpn_gateway.lite_runtime.sys.stdin.isatty", return_value=False), \
                patch("univpn_gateway.lite_runtime.subprocess.Popen") as launch:
            with self.assertRaises(RuntimeError):
                run_interactive()
            launch.assert_not_called()

    def test_failed_login_is_not_retried_and_backend_is_stopped(self):
        service = Mock()
        service.poll.side_effect = (None, None, 0)
        service.wait.return_value = 0
        login = Mock()
        login.poll.return_value = 2
        login.returncode = 2
        with patch("univpn_gateway.lite_runtime.sys.stdin.isatty", return_value=True), \
                patch("univpn_gateway.lite_runtime.Path.is_socket", return_value=True), \
                patch("univpn_gateway.lite_runtime.signal.signal"), \
                patch("univpn_gateway.lite_runtime.subprocess.Popen", side_effect=(service, login)) as launch, \
                patch("univpn_gateway.lite_runtime.send") as send:
            self.assertEqual(run_interactive(), 2)
            self.assertEqual(launch.call_count, 2)
            self.assertEqual(launch.call_args_list[1].args[0], ["/usr/local/bin/vpnctl", "login"])
            # No credentials are passed; stdin/stdout are inherited for getpass.
            self.assertEqual(launch.call_args_list[1].kwargs, {})
            send.assert_not_called()
            service.terminate.assert_called_once()

    def test_child_that_does_not_stop_is_killed_after_bounded_wait(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = (subprocess.TimeoutExpired("fixture", 5), 0)
        _finish(process)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)


if __name__ == "__main__":
    unittest.main()
