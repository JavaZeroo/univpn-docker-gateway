"""Fetch one pinned vendor release locally; never execute its installer script."""
from contextlib import contextmanager
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile
import tempfile
import time
from urllib.request import urlopen
import zipfile


VERSION = "10781.21.0.0831"
DOWNLOAD_URL = ("https://download.leagsoft.com/download/UniVPN/linux/"
                "univpn-linux-64-10781.21.0.0831.zip")
ZIP_SHA256 = "2315e88eb3259f78215e77a41768048d10f6c5540970b042ba76a1f12107cd9f"
ZIP_SIZE = 29951944
RUN_SHA256 = "92b07b10ed92a08cdb67ee622a83bd3e576fd14903229d23dcb728a011a8d42a"
RUN_SIZE = 30068863
RUN_MEMBER = "univpn-linux-amd64-10781.21.0.0831.run"
CACHE_NAME = "univpn-10781.21.0.0831-amd64.zip"
DOWNLOAD_TIMEOUT = 90
SOCKET_TIMEOUT = 15
MAX_ZIP_SIZE = 32 * 1024 * 1024
MAX_UNPACKED_SIZE = 256 * 1024 * 1024
TEMPLATE = "opt/gateway/vendor-sysconfig.ini"
# source, installed path, size, SHA256, normalized mode (no setuid bits).
FILES = (
    ("sysconfig.ini", "usr/local/UniVPN/sysconfig.ini", 403,
     "828ce10134190151c7fdf5882f87efa4785a77439d03dce901ef0382ca57118a", 0o644),
    ("promote/UniVPNPromoteService", "usr/local/UniVPN/promote/UniVPNPromoteService", 242352,
     "388d05e3e3a5aa0e7f4c80f6b642953b8b0c8fa99701f9c6e94926f1273960c0", 0o755),
    ("serviceclient/UniVPNCS", "usr/local/UniVPN/serviceclient/UniVPNCS", 6775624,
     "f26948ea1d3892f31a0692d4f771dcbc40365bfb2a39a509b9f3174d323781c4", 0o755),
    ("serviceclient/libgmssl.so", "usr/local/UniVPN/serviceclient/libgmssl.so", 502088,
     "178143e5123c475b9cc8490f21ed0b63ca157e584d5e6ce94e9ea2bfae93ba9c", 0o644),
    ("serviceclient/legacy.so", "usr/local/UniVPN/serviceclient/legacy.so", 4123184,
     "5c708db4059d8b8ca1446b9201438a7a92790218dbc02589decfe3d27089d501", 0o644),
    ("serviceclient/libgmcrypto.so", "lib/libgmcrypto.so", 3411264,
     "37c10deccde29c5d997414ad06541bd89ba316b79680cc35cf27a025d22c0afe", 0o644),
)


class VendorRuntimeError(RuntimeError):
    """A purpose-written, credential-free initialization failure."""


def _target(root, relative):
    candidate = root / relative
    # Ubuntu's /lib -> /usr/lib is valid when it stays inside the chosen root.
    if (not candidate.parent.resolve().is_relative_to(root) or candidate.is_symlink() or
            (candidate.exists() and not candidate.is_file())):
        raise VendorRuntimeError("vendor path is not a regular local destination")
    return candidate


def _open_regular(path):
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
             getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    if path.is_symlink():
        raise VendorRuntimeError("vendor file must not be a symbolic link")
    descriptor = os.open(path, flags)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise VendorRuntimeError("vendor file must be regular")
    return os.fdopen(descriptor, "rb")


def _matches(path, size, digest):
    try:
        with _open_regular(path) as stream:
            if os.fstat(stream.fileno()).st_size != size:
                return False
            return hashlib.sha256(stream.read(size + 1)).hexdigest() == digest
    except FileNotFoundError:
        return False


def _write_atomic(path, data, mode):
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".univpn-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def _cache_lock(cache):
    if any(path.is_symlink() for path in (cache.absolute(), *cache.absolute().parents)):
        raise VendorRuntimeError("vendor cache must be a local directory")
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not cache.is_dir():
        raise VendorRuntimeError("vendor cache must be a directory")
    if os.name != "nt" and cache.stat().st_uid != os.geteuid():
        raise VendorRuntimeError("vendor cache must belong to the runtime user")
    cache.chmod(0o700)
    lock = cache / ".bootstrap.lock"
    if lock.is_symlink():
        raise VendorRuntimeError("vendor cache lock must be regular")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(lock, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise VendorRuntimeError("vendor cache lock must be regular")
        if os.name == "nt":  # Unit tests also run on Windows; production is Linux.
            import msvcrt
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _download(path):
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT
    try:
        with urlopen(DOWNLOAD_URL, timeout=SOCKET_TIMEOUT) as response, path.open("wb") as output:
            if response.status != 200 or response.geturl() != DOWNLOAD_URL:
                raise VendorRuntimeError("official vendor download returned an unexpected response")
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) != ZIP_SIZE:
                raise VendorRuntimeError("official vendor download has an unexpected size")
            total = 0
            while True:
                if time.monotonic() >= deadline:
                    raise VendorRuntimeError("official vendor download timed out")
                chunk = response.read(128 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > ZIP_SIZE or total > MAX_ZIP_SIZE:
                    raise VendorRuntimeError("official vendor download exceeds its size limit")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    except VendorRuntimeError:
        raise
    except Exception:
        # Proxy errors may contain environment credentials; do not relay them.
        raise VendorRuntimeError("official vendor download failed") from None


def _payloads(stream):
    if os.fstat(stream.fileno()).st_size != ZIP_SIZE or ZIP_SIZE > MAX_ZIP_SIZE:
        raise VendorRuntimeError("vendor ZIP size verification failed")
    data = stream.read(ZIP_SIZE + 1)
    if hashlib.sha256(data).hexdigest() != ZIP_SHA256:
        raise VendorRuntimeError("vendor ZIP SHA256 verification failed")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if (len(members) != 3 or {m.filename for m in members} !=
                {RUN_MEMBER, RUN_MEMBER + ".cms", RUN_MEMBER + ".crl"}):
            raise VendorRuntimeError("vendor ZIP entries differ from the pinned release")
        member = archive.getinfo(RUN_MEMBER)
        if member.file_size != RUN_SIZE:
            raise VendorRuntimeError("vendor installer size verification failed")
        installer = archive.read(member)
    if hashlib.sha256(installer).hexdigest() != RUN_SHA256:
        raise VendorRuntimeError("vendor installer SHA256 verification failed")
    parts = installer.split(b"\n", 257)
    if len(parts) != 258 or not parts[-1].startswith(b"\x1f\x8b\x08"):
        raise VendorRuntimeError("pinned vendor archive boundary is missing")
    result = {}
    with tarfile.open(fileobj=io.BytesIO(parts[-1]), mode="r:gz") as archive:
        members = archive.getmembers()
        names = [m.name for m in members]
        if (len(members) > 1000 or len(names) != len(set(names)) or
                sum(m.size for m in members) > MAX_UNPACKED_SIZE):
            raise VendorRuntimeError("vendor archive contains unexpected entries")
        for member in members:
            path = PurePosixPath(member.name)
            if (not path.parts or path.is_absolute() or ".." in path.parts or
                    "\\" in member.name or not (member.isfile() or member.isdir())):
                raise VendorRuntimeError("vendor archive contains an unsafe entry")
        for source, target, size, digest, mode in FILES:
            member = archive.getmember(source)
            if not member.isfile() or member.size != size:
                raise VendorRuntimeError("vendor runtime file metadata differs")
            with archive.extractfile(member) as source_stream:
                blob = source_stream.read(size + 1)
            if len(blob) != size or hashlib.sha256(blob).hexdigest() != digest:
                raise VendorRuntimeError("vendor runtime file SHA256 verification failed")
            result[target] = (blob, mode)
    result[TEMPLATE] = result["usr/local/UniVPN/sysconfig.ini"]
    return result


def prepare_vendor(cache_dir="/var/cache/univpn", root="/"):
    """Prepare this container's vendor tree; share only an immutable pinned ZIP.

    The two path arguments are for deployment and isolated tests. They cannot
    override the official URL, release, hashes or extraction allowlist.
    Corrupt existing caches fail closed and require explicit removal/recovery.
    """
    try:
        destination = Path(root).resolve()
        destination.mkdir(mode=0o755, parents=True, exist_ok=True)
        paths = [(source, _target(destination, target), size, digest, mode)
                 for source, target, size, digest, mode in FILES]
        template = _target(destination, TEMPLATE)
        if all(_matches(path, size, digest) for _, path, size, digest, _ in paths):
            for _, path, _, _, mode in paths:
                path.chmod(mode)
            _, config, size, digest, _ = paths[0]
            if not _matches(template, size, digest):
                _write_atomic(template, config.read_bytes(), 0o644)
            template.chmod(0o644)
            return {"version": VERSION, "cache_hit": False, "already_installed": True}
        cache = Path(cache_dir)
        with _cache_lock(cache):
            cached = cache / CACHE_NAME
            hit = cached.exists() or cached.is_symlink()
            temporary = None
            try:
                if not hit:
                    descriptor, name = tempfile.mkstemp(dir=cache, prefix=".download-")
                    os.close(descriptor)
                    temporary = Path(name)
                    _download(temporary)
                    with _open_regular(temporary) as stream:
                        blobs = _payloads(stream)
                    os.replace(temporary, cached)
                else:
                    with _open_regular(cached) as stream:
                        blobs = _payloads(stream)
                for relative, (blob, mode) in blobs.items():
                    _write_atomic(_target(destination, relative), blob, mode)
                return {"version": VERSION, "cache_hit": hit, "already_installed": False}
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
    except VendorRuntimeError:
        raise
    except Exception:
        raise VendorRuntimeError("vendor runtime preparation failed") from None
