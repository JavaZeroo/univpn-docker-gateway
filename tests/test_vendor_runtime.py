import hashlib
import io
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from univpn_gateway import vendor_runtime as vendor


def fixture(*, extra=(), wrong_file=False):
    """Small independent release fixture; no proprietary files are needed."""
    blobs = {source: ("test-runtime:" + source).encode() for source, *_ in vendor.FILES}
    specs = tuple((source, target, len(blobs[source]),
                   hashlib.sha256(blobs[source]).hexdigest(), mode)
                  for source, target, _, _, mode in vendor.FILES)
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w:gz") as archive:
        for source, blob in blobs.items():
            if wrong_file and source == specs[-1][0]:
                blob = b"X" + blob[1:]
            member = tarfile.TarInfo(source)
            member.size = len(blob)
            member.mode = 0o4755
            archive.addfile(member, io.BytesIO(blob))
        for member, data in extra:
            archive.addfile(member, io.BytesIO(data) if data else None)
    installer = b"#!/bin/sh\n" + b"\n" * 256 + content.getvalue()
    compressed = io.BytesIO()
    with zipfile.ZipFile(compressed, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(vendor.RUN_MEMBER, installer)
        archive.writestr(vendor.RUN_MEMBER + ".cms", b"signature fixture")
        archive.writestr(vendor.RUN_MEMBER + ".crl", b"revocation fixture")
    data = compressed.getvalue()
    pins = {"FILES": specs, "ZIP_SIZE": len(data),
            "ZIP_SHA256": hashlib.sha256(data).hexdigest(),
            "RUN_SIZE": len(installer), "RUN_SHA256": hashlib.sha256(installer).hexdigest()}
    return data, pins


class VendorRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.cache, self.root = self.base / "cache", self.base / "root"
        self.data, pins = fixture()
        self.pins = patch.multiple(vendor, **pins)
        self.pins.start()
        self.addCleanup(self.pins.stop)

    def download(self, path):
        path.write_bytes(self.data)

    def prepare(self, root=None):
        return vendor.prepare_vendor(self.cache, root or self.root)

    def test_download_installs_exact_files_and_template_without_running_code(self):
        # Docker may already have created these directories for tmpfs mounts.
        (self.root / "usr/local/UniVPN/serviceclient/log").mkdir(parents=True)
        with patch.object(vendor, "_download", side_effect=self.download) as download:
            result = self.prepare()
        self.assertEqual({"version": vendor.VERSION, "cache_hit": False,
                          "already_installed": False}, result)
        download.assert_called_once()
        for _, relative, size, digest, mode in vendor.FILES:
            path = self.root / relative
            self.assertEqual(size, path.stat().st_size)
            self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
            if stat.S_IMODE(path.stat().st_mode) & 0o111:
                self.assertFalse(stat.S_IMODE(path.stat().st_mode) & 0o7000)
            if vendor.os.name != "nt":
                self.assertEqual(mode, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual((self.root / vendor.TEMPLATE).read_bytes(),
                         (self.root / "usr/local/UniVPN/sysconfig.ini").read_bytes())
        self.assertTrue((self.root / "usr/local/UniVPN/serviceclient/log").is_dir())
        self.assertFalse((self.root / "UniVPN").exists())

    def test_second_container_uses_cache_but_has_independent_mutable_copy(self):
        with patch.object(vendor, "_download", side_effect=self.download) as download:
            self.prepare()
            other = self.base / "other"
            result = self.prepare(other)
        self.assertTrue(result["cache_hit"])
        self.assertEqual(1, download.call_count)
        (self.root / "usr/local/UniVPN/sysconfig.ini").write_bytes(b"changed by engine")
        self.assertNotEqual((self.root / "usr/local/UniVPN/sysconfig.ini").read_bytes(),
                            (other / "usr/local/UniVPN/sysconfig.ini").read_bytes())

    def test_installed_files_skip_network_and_restore_missing_stable_template(self):
        with patch.object(vendor, "_download", side_effect=self.download):
            self.prepare()
        (self.root / vendor.TEMPLATE).unlink()
        with patch.object(vendor, "_download", side_effect=AssertionError("network must not run")):
            self.assertTrue(self.prepare()["already_installed"])
        self.assertTrue((self.root / vendor.TEMPLATE).is_file())

    def test_corrupt_cache_is_rejected_without_download_or_install(self):
        self.cache.mkdir()
        cached = self.cache / vendor.CACHE_NAME
        cached.write_bytes(b"damaged cache")
        with patch.object(vendor, "_download") as download:
            with self.assertRaises(vendor.VendorRuntimeError):
                self.prepare()
        download.assert_not_called()
        self.assertEqual(b"damaged cache", cached.read_bytes())
        self.assertFalse((self.root / "usr/local/UniVPN/serviceclient/UniVPNCS").exists())

    def test_failed_download_never_publishes_cache_and_removes_temporary_files(self):
        def fail(path):
            path.write_bytes(b"partial")
            raise OSError("must-not-appear-sensitive-proxy")
        with patch.object(vendor, "_download", side_effect=fail):
            with self.assertRaises(vendor.VendorRuntimeError) as error:
                self.prepare()
        self.assertNotIn("sensitive-proxy", str(error.exception))
        self.assertFalse((self.cache / vendor.CACHE_NAME).exists())
        self.assertEqual([], list(self.cache.glob(".download-*")))

    def test_downloaded_wrong_zip_hash_is_rejected(self):
        with patch.object(vendor, "_download", side_effect=self.download), \
                patch.object(vendor, "ZIP_SHA256", "0" * 64):
            with self.assertRaisesRegex(vendor.VendorRuntimeError, "ZIP SHA256"):
                self.prepare()
        self.assertFalse((self.cache / vendor.CACHE_NAME).exists())

    def test_wrong_installer_hash_is_rejected(self):
        with patch.object(vendor, "_download", side_effect=self.download), \
                patch.object(vendor, "RUN_SHA256", "0" * 64):
            with self.assertRaisesRegex(vendor.VendorRuntimeError, "installer SHA256"):
                self.prepare()

    def assert_archive_rejected(self, *, extra=(), wrong_file=False):
        data, pins = fixture(extra=extra, wrong_file=wrong_file)
        with patch.multiple(vendor, **pins), \
                patch.object(vendor, "_download", side_effect=lambda path: path.write_bytes(data)):
            with self.assertRaises(vendor.VendorRuntimeError):
                self.prepare()
        self.assertFalse((self.cache / vendor.CACHE_NAME).exists())

    def test_tar_symlink_and_hardlink_are_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
            with self.subTest(kind=kind):
                member = tarfile.TarInfo("extra-link")
                member.type, member.linkname = kind, "/outside"
                self.assert_archive_rejected(extra=((member, b""),))

    def test_tar_duplicate_name_is_rejected(self):
        member = tarfile.TarInfo(vendor.FILES[0][0])
        member.size = 1
        self.assert_archive_rejected(extra=((member, b"X"),))

    def test_tar_traversal_is_rejected(self):
        member = tarfile.TarInfo("../../outside")
        member.size = 1
        self.assert_archive_rejected(extra=((member, b"X"),))
        self.assertFalse((self.base / "outside").exists())

    def test_inner_runtime_hash_is_checked_even_when_zip_and_run_match(self):
        self.assert_archive_rejected(wrong_file=True)

    @unittest.skipIf(vendor.os.name == "nt", "symlink creation needs Windows privileges")
    def test_destination_symlink_cannot_escape_test_root(self):
        self.root.mkdir()
        outside = self.base / "outside"
        outside.mkdir()
        (self.root / "lib").symlink_to(outside, target_is_directory=True)
        with patch.object(vendor, "_download") as download:
            with self.assertRaises(vendor.VendorRuntimeError):
                self.prepare()
        download.assert_not_called()
        self.assertEqual([], list(outside.iterdir()))

    @unittest.skipIf(vendor.os.name == "nt", "symlink creation needs Windows privileges")
    def test_merged_usr_lib_symlink_inside_root_is_supported(self):
        (self.root / "usr/lib").mkdir(parents=True)
        (self.root / "lib").symlink_to("usr/lib", target_is_directory=True)
        with patch.object(vendor, "_download", side_effect=self.download):
            self.prepare()
        self.assertTrue((self.root / "usr/lib/libgmcrypto.so").is_file())

    @unittest.skipIf(vendor.os.name == "nt", "symlink creation needs Windows privileges")
    def test_symlink_cache_and_lock_are_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        self.cache.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(vendor.VendorRuntimeError):
            self.prepare()
        self.cache.unlink()
        self.cache.mkdir()
        (self.cache / ".bootstrap.lock").symlink_to(outside / "lock")
        with self.assertRaises(vendor.VendorRuntimeError):
            self.prepare()

    @unittest.skipIf(vendor.os.name == "nt", "requires POSIX FIFO support")
    def test_fifo_cache_is_rejected_without_blocking_or_network(self):
        self.cache.mkdir()
        vendor.os.mkfifo(self.cache / vendor.CACHE_NAME)
        # A child timeout makes a regression fail instead of hanging the suite.
        script = """
import sys
from unittest.mock import patch
from univpn_gateway import vendor_runtime as vendor
with patch.object(vendor, '_download', side_effect=AssertionError('network forbidden')):
    try:
        vendor.prepare_vendor(sys.argv[1], sys.argv[2])
    except vendor.VendorRuntimeError:
        print('rejected')
"""
        result = subprocess.run([sys.executable, "-B", "-c", script,
                                 str(self.cache), str(self.root)],
                                capture_output=True, text=True, timeout=3,
                                cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(0, result.returncode)
        self.assertEqual("rejected", result.stdout.strip())

    @unittest.skipIf(vendor.os.name == "nt", "symlink creation needs Windows privileges")
    def test_cache_parent_symlink_does_not_create_external_directories(self):
        outside = self.base / "outside"
        outside.mkdir()
        linked = self.base / "linked"
        linked.symlink_to(outside, target_is_directory=True)
        with patch.object(vendor, "_download") as download:
            with self.assertRaises(vendor.VendorRuntimeError):
                vendor.prepare_vendor(linked / "cache", self.root)
        download.assert_not_called()
        self.assertEqual([], list(outside.iterdir()))

    def test_concurrent_containers_publish_one_download(self):
        errors, results = [], []
        def slow_download(path):
            time.sleep(0.05)
            self.download(path)
        def worker(number):
            try:
                results.append(self.prepare(self.base / ("root-" + str(number))))
            except Exception as error:
                errors.append(type(error).__name__)
        with patch.object(vendor, "_download", side_effect=slow_download) as download:
            workers = [threading.Thread(target=worker, args=(number,)) for number in range(2)]
            for thread in workers:
                thread.start()
            for thread in workers:
                thread.join(timeout=5)
            self.assertTrue(all(not thread.is_alive() for thread in workers))
        self.assertEqual([], errors)
        self.assertEqual(2, len(results))
        self.assertEqual(1, download.call_count)
        self.assertEqual([False, True], sorted(r["cache_hit"] for r in results))


class VendorDownloadTests(unittest.TestCase):
    class Response(io.BytesIO):
        status = 200
        headers = {}
        def geturl(self):
            return vendor.DOWNLOAD_URL

    def test_fixed_url_timeout_and_maximum_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "download"
            response = self.Response(b"X" * 20)
            with patch.object(vendor, "urlopen", return_value=response) as request, \
                    patch.object(vendor, "ZIP_SIZE", 10):
                with self.assertRaisesRegex(vendor.VendorRuntimeError, "size limit"):
                    vendor._download(path)
            request.assert_called_once_with(vendor.DOWNLOAD_URL, timeout=vendor.SOCKET_TIMEOUT)

    def test_network_errors_are_sanitized(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(vendor, "urlopen", side_effect=OSError("credential-in-proxy-url")):
            with self.assertRaises(vendor.VendorRuntimeError) as error:
                vendor._download(Path(temporary) / "download")
        self.assertEqual("official vendor download failed", str(error.exception))


if __name__ == "__main__":
    unittest.main()
