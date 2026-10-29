import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from univpn_gateway.image_entrypoint import CLIENT_FILES, export_client, main


class PublicImageTests(unittest.TestCase):
    def test_lite_cannot_export_or_prepare_a_windows_client(self):
        with patch.dict(os.environ, {"VPN_EDITION": "lite"}), \
                patch("univpn_gateway.image_entrypoint.export_client") as export, \
                patch("univpn_gateway.vendor_runtime.prepare_vendor") as prepare, \
                patch("builtins.print"):
            self.assertEqual(main(["export-client"]), 2)
            export.assert_not_called()
            prepare.assert_not_called()

    def test_lite_configure_does_not_download_or_authenticate(self):
        with patch.dict(os.environ, {"VPN_EDITION": "lite", "VPN_CONFIG": "/config/lite/vpn.json"}), \
                patch("univpn_gateway.lite_setup.configure", return_value={}) as configure, \
                patch("univpn_gateway.vendor_runtime.prepare_vendor") as prepare, \
                patch("univpn_gateway.image_entrypoint.sys.stdin.isatty", return_value=True), \
                patch("builtins.print"):
            self.assertEqual(main(["configure"]), 0)
            configure.assert_called_once_with("/config/lite/vpn.json", interactive=True)
            prepare.assert_not_called()

    def test_export_only_inert_client_files_and_reject_existing_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            for name in CLIENT_FILES:
                (source / name).write_text("# test client", encoding="utf-8")
            (source / "private-key").write_text("do not export", encoding="utf-8")
            destination = root / "univpn"
            result = export_client(destination, source)
            self.assertEqual(set(p.name for p in destination.iterdir()), set(CLIENT_FILES))
            self.assertEqual(result["directory"], str(destination))
            with self.assertRaises(ValueError):
                export_client(destination, source)
            self.assertEqual((destination / CLIENT_FILES[0]).read_text(), "# test client")

    def test_missing_client_does_not_create_half_export(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "univpn"
            with self.assertRaises(ValueError):
                export_client(destination, folder)
            self.assertFalse(destination.exists())

    def test_export_never_downloads_or_starts_vendor_runtime(self):
        with patch("univpn_gateway.image_entrypoint.export_client", return_value={"directory": "test"}) as export, \
                patch("univpn_gateway.vendor_runtime.prepare_vendor") as prepare, \
                patch("builtins.print"):
            self.assertEqual(main(["export-client"]), 0)
            export.assert_called_once()
            prepare.assert_not_called()

    def test_invalid_vpn_config_prevents_download(self):
        with patch("univpn_gateway.config.load", side_effect=ValueError("invalid config")), \
                patch("univpn_gateway.vendor_runtime.prepare_vendor") as prepare, \
                patch("builtins.print"):
            self.assertEqual(main(["run"]), 1)
            prepare.assert_not_called()

    def test_prepare_does_not_start_or_authenticate_engine(self):
        with patch("univpn_gateway.vendor_runtime.prepare_vendor", return_value={"version": "pinned"}) as prepare, \
                patch("univpn_gateway.native_service.main") as runtime, patch("builtins.print"):
            self.assertEqual(main(["prepare"]), 0)
            prepare.assert_called_once()
            runtime.assert_not_called()


if __name__ == "__main__":
    unittest.main()
