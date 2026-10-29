import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from univpn_gateway import lite_setup as setup


class LiteSetupTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.path = self.root / "config" / "vpn.json"
        # CI can run unprivileged. Owner checks have separate explicit tests.
        self.root_patch = patch.object(setup, "_require_root")
        self.owner_patch = patch.object(setup, "_require_owner")
        self.real_require_root = setup._require_root
        self.real_require_owner = setup._require_owner
        self.root_patch.start()
        self.owner_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.owner_patch.stop)

    def valid(self):
        return {"edition": "lite", "gateway": "vpn.example.com", "port": 443,
                "vpn_interface": "cnem_vnic", "allowed_cidrs": ["10.80.0.11/32"],
                "probe": {"host": "10.80.0.11", "port": 22, "banner_prefix": "SSH-2.0-"}}

    def existing(self, data=None, raw=None):
        self.path.parent.mkdir(mode=0o700)
        self.path.write_text(raw if raw is not None else json.dumps(data or self.valid()), encoding="utf-8")
        self.path.chmod(0o600)

    def wizard(self, answers=None):
        reader = Mock(side_effect=answers or ["vpn.example.com:41024", "10.80.0.11/32", "", ""])
        result = setup.configure(self.path, input_fn=reader)
        return result, reader

    def test_wizard_only_collects_network_settings_and_derives_default_probe(self):
        result, reader = self.wizard()
        self.assertEqual(41024, result["port"])
        self.assertEqual(self.valid()["probe"], result["probe"])
        self.assertEqual("lite", result["edition"])
        self.assertNotIn("wireguard_client", result)
        self.assertNotIn("approved_certificate_sha256", result)
        self.assertEqual(result, json.loads(self.path.read_text()))
        self.assertEqual(4, reader.call_count)
        prompts = " ".join(call.args[0] for call in reader.call_args_list).lower()
        for forbidden in ("username", "password", "otp", "sms"):
            self.assertNotIn(forbidden, prompts)
        if os.name == "posix":
            self.assertEqual(0o700, stat.S_IMODE(self.path.parent.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(self.path.stat().st_mode))

    def test_multiple_targets_custom_probe_and_explicit_approved_pin(self):
        result, _ = self.wizard(["vpn.example.com", "10.80.0.0/24, 10.90.0.11/32",
                                 "10.90.0.11:2222", "AB" * 32])
        self.assertEqual(443, result["port"])
        self.assertEqual(2222, result["probe"]["port"])
        self.assertEqual("ab" * 32, result["approved_certificate_sha256"])

    def test_existing_valid_file_is_reused_without_prompt_write_or_chmod(self):
        self.existing(raw=json.dumps(self.valid(), indent=4) + "\n")
        before = self.path.read_bytes()
        reader = Mock(side_effect=AssertionError("must not ask"))
        with patch.object(setup._Parent, "make_private", side_effect=AssertionError("must not chmod")), \
                patch.object(setup, "_save_new", side_effect=AssertionError("must not save")):
            self.assertEqual(self.valid(), setup.configure(self.path, interactive=False, input_fn=reader))
        reader.assert_not_called()
        self.assertEqual(before, self.path.read_bytes())

    def test_background_missing_config_does_not_prompt_or_create_directory(self):
        reader = Mock(side_effect=AssertionError("must not ask"))
        with self.assertRaisesRegex(setup.SetupError, "docker run -it"):
            setup.configure(self.path, interactive=False, input_fn=reader)
        reader.assert_not_called()
        self.assertFalse(self.path.parent.exists())

    def test_existing_unknown_secret_or_nested_fields_are_rejected_read_only(self):
        for location in ("password", "username", "otp", "token", "unknown"):
            with self.subTest(field=location):
                data = self.valid()
                data[location] = "private-value-do-not-echo"
                if self.path.parent.exists():
                    self.path.unlink()
                else:
                    self.path.parent.mkdir(mode=0o700)
                self.path.write_text(json.dumps(data))
                self.path.chmod(0o600)
                before = self.path.read_bytes()
                with self.assertRaises(setup.SetupError) as error:
                    setup.configure(self.path, interactive=False)
                self.assertNotIn("private-value", str(error.exception))
                self.assertEqual(before, self.path.read_bytes())
        data = self.valid()
        data["probe"]["password"] = "private-nested"
        self.path.write_text(json.dumps(data))
        with self.assertRaises(setup.SetupError):
            setup.configure(self.path, interactive=False)

    def test_invalid_endpoints_cidrs_probe_and_pin_never_publish_file(self):
        cases = [
            ["vpn.example.com:0", "10.80.0.11/32", "", ""],
            ["https://vpn.example.com", "10.80.0.11/32", "", ""],
            ["[::1]:443", "10.80.0.11/32", "", ""],
            ["999.1.1.1:443", "10.80.0.11/32", "", ""],
            ["vpn.example.com", "0.0.0.0/0", "10.80.0.11", ""],
            ["vpn.example.com", "0.0.0.0/1,128.0.0.0/1", "10.80.0.11", ""],
            ["vpn.example.com", "10.80.0.1/24", "10.80.0.11", ""],
            ["vpn.example.com", "10.80.0.11/32", "10.90.0.11", ""],
            ["vpn.example.com", "10.80.0.11/32", "", "not-a-pin"],
        ]
        for answers in cases:
            with self.subTest(case=cases.index(answers)):
                with self.assertRaises(setup.SetupError):
                    self.wizard(answers)
                self.assertFalse(self.path.exists())
                self.assertFalse(self.path.parent.exists())

    def test_noninteractive_existing_file_requires_lite_edition(self):
        data = self.valid()
        data["edition"] = "full"
        self.existing(data)
        with self.assertRaises(setup.SetupError):
            setup.configure(self.path, interactive=False)

    def test_duplicate_json_keys_and_invalid_json_do_not_echo_contents(self):
        self.existing(raw='{"edition":"lite","edition":"private-value"}')
        with self.assertRaises(setup.SetupError) as error:
            setup.configure(self.path, interactive=False)
        self.assertNotIn("private-value", str(error.exception))
        self.path.write_text("private-not-json")
        with self.assertRaises(setup.SetupError) as error:
            setup.configure(self.path, interactive=False)
        self.assertNotIn("private-not-json", str(error.exception))

    def test_non_text_cidrs_and_non_utf8_json_are_rejected(self):
        data = self.valid()
        data["allowed_cidrs"] = [123]
        self.existing(data)
        with self.assertRaises(setup.SetupError):
            setup.configure(self.path, interactive=False)
        self.path.write_bytes(json.dumps(self.valid()).encode("utf-16"))
        with self.assertRaisesRegex(setup.SetupError, "UTF-8 JSON"):
            setup.configure(self.path, interactive=False)

    def test_root_directory_is_not_used_as_configuration_directory(self):
        reader = Mock(side_effect=AssertionError("must not ask"))
        root_file = Path(self.path.anchor) / "vpn.json"
        with self.assertRaisesRegex(setup.SetupError, "dedicated configuration directory"):
            setup.configure(root_file, input_fn=reader)
        reader.assert_not_called()

    def test_cancelled_wizard_does_not_create_config_or_authenticate(self):
        for error in (EOFError(), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                with self.assertRaisesRegex(setup.SetupError, "no authentication"):
                    setup.configure(self.path, input_fn=Mock(side_effect=error))
                self.assertFalse(self.path.parent.exists())

    def test_publication_race_never_overwrites_existing_file(self):
        original = setup._Parent.publish
        def race(parent, temporary, name):
            descriptor = parent.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(b"keep-existing")
            original(parent, temporary, name)
        with patch.object(setup._Parent, "publish", race):
            with self.assertRaisesRegex(setup.SetupError, "nothing was overwritten"):
                self.wizard()
        self.assertEqual(b"keep-existing", self.path.read_bytes())
        self.assertEqual(["vpn.json"], [path.name for path in self.path.parent.iterdir()])

    @unittest.skipIf(os.name == "nt", "symlink creation needs Windows privileges")
    def test_symlink_parent_and_file_are_rejected_without_external_writes(self):
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        self.path.parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(setup.SetupError):
            self.wizard()
        self.assertEqual([], list(outside.iterdir()))
        self.path.parent.unlink()
        self.path.parent.mkdir(mode=0o700)
        target = outside / "keep"
        target.write_text("private-do-not-read")
        self.path.symlink_to(target)
        with self.assertRaises(setup.SetupError):
            setup.configure(self.path, interactive=False)
        self.assertEqual("private-do-not-read", target.read_text())

    @unittest.skipIf(os.name == "nt", "Unix mode checks")
    def test_existing_loose_permissions_are_rejected_not_repaired(self):
        self.existing()
        self.path.chmod(0o644)
        with self.assertRaises(setup.SetupError):
            setup.configure(self.path, interactive=False)
        self.assertEqual(0o644, stat.S_IMODE(self.path.stat().st_mode))

    @unittest.skipIf(os.name == "nt", "Unix ownership checks")
    def test_nonroot_and_nonroot_owned_config_are_rejected(self):
        with patch.object(setup.os, "geteuid", return_value=1000):
            with self.assertRaises(setup.SetupError):
                self.real_require_root()
        with self.assertRaises(setup.SetupError):
            self.real_require_owner(Mock(st_uid=1000))


if __name__ == "__main__":
    unittest.main()
