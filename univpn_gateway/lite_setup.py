"""Container-only network configuration wizard, with no authentication inputs.

Production runs as Linux root. Directory traversal and publication use relative
file descriptors there; the path-based fallback supports offline Windows tests.
Existing configuration is read-only. A certificate pin is accepted only when
the user says it was verified beforehand; this module never fetches or trusts one.
"""
from contextlib import contextmanager
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import uuid

MAX_CONFIG_BYTES = 65536
_FIELDS = frozenset(("edition", "gateway", "port", "vpn_interface", "allowed_cidrs", "probe",
                     "approved_certificate_sha256", "tunnel_mode", "gm_algorithm", "route_cover"))
_PROBE_FIELDS = frozenset(("host", "port", "banner_prefix"))


class SetupError(RuntimeError):
    """A purpose-written setup error that never contains input or file contents."""


def _require_root():
    if os.name == "posix" and os.geteuid() != 0:
        raise SetupError("run lite configuration as the container root user")


def _require_owner(info):
    if os.name == "posix" and info.st_uid != 0:
        raise SetupError("configuration must belong to the container root user")


def _private_mode(info, expected):
    if os.name == "posix" and stat.S_IMODE(info.st_mode) != expected:
        raise SetupError("existing configuration needs directory mode 0700 and file mode 0600")


def _not_link(info):
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise SetupError("configuration paths must not contain links or reparse points")


class _Parent:
    def __init__(self, path, descriptor=None):
        self.path, self.descriptor = path, descriptor

    def open(self, name, flags, mode=0o600):
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        if self.descriptor is not None:
            return os.open(name, flags, mode, dir_fd=self.descriptor)
        candidate = self.path / name
        try:
            _not_link(candidate.lstat())
        except FileNotFoundError:
            pass
        return os.open(candidate, flags, mode)

    def info(self):
        return os.fstat(self.descriptor) if self.descriptor is not None else self.path.lstat()

    def make_private(self):
        if self.descriptor is not None:
            os.fchmod(self.descriptor, 0o700)
        else:
            self.path.chmod(0o700)

    def unlink(self, name):
        try:
            if self.descriptor is not None:
                os.unlink(name, dir_fd=self.descriptor)
            else:
                (self.path / name).unlink()
        except FileNotFoundError:
            pass

    def publish(self, temporary, name):
        # A hard link publishes the complete file only if the target is absent.
        # Unlike rename/replace, it cannot overwrite an existing config or link.
        if self.descriptor is not None:
            os.link(temporary, name, src_dir_fd=self.descriptor,
                    dst_dir_fd=self.descriptor, follow_symlinks=False)
        else:
            os.link(self.path / temporary, self.path / name, follow_symlinks=False)


@contextmanager
def _parent(path, *, create=False):
    directory = path.parent
    if os.name != "posix":
        current = Path(directory.anchor)
        for part in directory.parts[1:]:
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                if not create:
                    raise
                current.mkdir(mode=0o700)
                info = current.lstat()
            _not_link(info)
            if not stat.S_ISDIR(info.st_mode):
                raise SetupError("configuration parent must be a directory")
        yield _Parent(directory)
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(directory.anchor, flags)
    try:
        for part in directory.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=descriptor)
                child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield _Parent(directory, descriptor)
    finally:
        os.close(descriptor)


def _port(value):
    if type(value) is not int or not 1 <= value <= 65535:
        raise SetupError("ports must be integers between 1 and 65535")
    return value


def _host(value, *, ip_only=False):
    if not isinstance(value, str) or not value or len(value) > 127:
        raise SetupError("enter an IPv4 address or gateway hostname")
    if ip_only or re.fullmatch(r"[0-9.]+", value):
        try:
            return str(ipaddress.IPv4Address(value))
        except ipaddress.AddressValueError:
            raise SetupError("enter a valid IPv4 address") from None
    if any(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) is None
           for label in value.split(".")):
        raise SetupError("enter a gateway hostname without a URL, path or IPv6 address")
    return value


def _endpoint(value, default_port, *, ip_only=False, default_host=None):
    if not isinstance(value, str) or value.count(":") > 1:
        raise SetupError("use hostname-or-IPv4:port; IPv6 is not supported")
    value = value.strip()
    host, separator, port = value.partition(":")
    host = host or default_host
    if separator and (not port.isascii() or not port.isdigit()):
        raise SetupError("enter a valid endpoint port")
    return _host(host, ip_only=ip_only), _port(int(port) if separator else default_port)


def _validate(data):
    if not isinstance(data, dict) or set(data) - _FIELDS:
        raise SetupError("unknown configuration fields are forbidden; never store credentials")
    if data.get("edition") != "lite" or data.get("vpn_interface") != "cnem_vnic":
        raise SetupError("this configuration must use edition lite and interface cnem_vnic")
    _host(data.get("gateway"))
    _port(data.get("port"))
    targets = data.get("allowed_cidrs")
    if (not isinstance(targets, list) or not 1 <= len(targets) <= 256
            or any(not isinstance(value, str) for value in targets)):
        raise SetupError("provide a nonempty IPv4 CIDR list with at most 256 entries")
    try:
        networks = [ipaddress.IPv4Network(value, strict=True) for value in targets]
    except (ValueError, TypeError):
        raise SetupError("target CIDRs must be canonical IPv4 networks") from None
    if (any(network.prefixlen == 0 for network in networks)
            or len(set(networks)) != len(networks)
            or any(network.prefixlen == 0 for network in ipaddress.collapse_addresses(networks))):
        raise SetupError("default routes and duplicate targets are forbidden")
    probe = data.get("probe")
    if not isinstance(probe, dict) or set(probe) != _PROBE_FIELDS:
        raise SetupError("probe requires only host, port and banner_prefix")
    address = ipaddress.IPv4Address(_host(probe["host"], ip_only=True))
    _port(probe["port"])
    if not any(address in network for network in networks):
        raise SetupError("the SSH probe must be inside an allowed CIDR")
    banner = probe["banner_prefix"]
    if not isinstance(banner, str) or not 1 <= len(banner.encode("utf-8")) <= 128 or re.search(r"[\x00-\x1f]", banner):
        raise SetupError("probe needs a nonempty safe banner prefix")
    pin = data.get("approved_certificate_sha256")
    if pin is not None and (not isinstance(pin, str) or re.fullmatch(r"[0-9a-fA-F]{64}", pin) is None):
        raise SetupError("an approved certificate SHA256 must contain exactly 64 hexadecimal characters")
    for key, choices in (("tunnel_mode", (1, 2, 3)), ("gm_algorithm", (0, 1)), ("route_cover", (0, 1))):
        if key in data and (type(data[key]) is not int or data[key] not in choices):
            raise SetupError("invalid native tunnel option")
    return data


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SetupError("duplicate configuration fields are forbidden")
        result[key] = value
    return result


def _existing(path):
    try:
        with _parent(path) as parent:
            descriptor = parent.open(path.name, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise SetupError("configuration must be a regular file")
                _require_owner(parent.info())
                _require_owner(info)
                _private_mode(parent.info(), 0o700)
                _private_mode(info, 0o600)
                raw = source.read(MAX_CONFIG_BYTES + 1)
            if len(raw) > MAX_CONFIG_BYTES:
                raise SetupError("configuration is too large")
            try:
                return _validate(json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object))
            except (UnicodeError, ValueError):
                raise SetupError("configuration must be valid UTF-8 JSON") from None
    except FileNotFoundError:
        return None


def _save_new(path, data):
    payload = (json.dumps(data, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
    with _parent(path, create=True) as parent:
        _require_owner(parent.info())
        parent.make_private()
        temporary = ".lite-setup-" + uuid.uuid4().hex
        try:
            descriptor = parent.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                parent.publish(temporary, path.name)
            except FileExistsError:
                raise SetupError("configuration appeared during setup; nothing was overwritten") from None
        finally:
            parent.unlink(temporary)


def configure(path="/config/vpn.json", *, interactive=True, input_fn=input):
    """Reuse valid config read-only, or collect network-only inputs and create it.

    ``interactive`` must reflect the caller's TTY policy. Noninteractive mode
    never asks a question or creates directories when configuration is absent.
    ``input_fn`` is injectable for offline tests; no authentication is performed.
    """
    _require_root()
    path = Path(os.path.abspath(os.fspath(path)))
    if not path.name or path.parent == Path(path.anchor):
        raise SetupError("use a configuration filename inside a dedicated configuration directory")
    try:
        existing = _existing(path)
        if existing is not None:
            return existing
        if not interactive:
            raise SetupError("configuration is missing; run the container configure command with docker run -it first")
        gateway, port = _endpoint(input_fn("VPN gateway hostname or IPv4[:port]: "), 443)
        targets = [value.strip() for value in input_fn("Allowed IPv4 CIDRs, comma separated: ").split(",")]
        default = targets[0].split("/")[0] if len(targets) == 1 and targets[0].endswith("/32") else None
        prompt = "SSH probe IPv4[:port]" + (" [" + default + ":22]" if default else "") + ": "
        host, probe_port = _endpoint(input_fn(prompt), 22, ip_only=True, default_host=default)
        pin = input_fn("Previously verified and approved certificate SHA256 [blank = normal CA verification]: ").strip()
        data = {"edition": "lite", "gateway": gateway, "port": port, "vpn_interface": "cnem_vnic",
                "allowed_cidrs": targets, "probe": {"host": host, "port": probe_port, "banner_prefix": "SSH-2.0-"}}
        if pin:
            data["approved_certificate_sha256"] = pin.lower()
        _validate(data)
        _save_new(path, data)
        return data
    except SetupError:
        raise
    except (EOFError, KeyboardInterrupt):
        raise SetupError("configuration cancelled; no authentication was attempted") from None
    except OSError:
        raise SetupError("configuration filesystem operation failed; no existing file was overwritten") from None
