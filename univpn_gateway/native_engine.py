"""Own one verified vendor engine and its persistent local control channels.

This is an independent Python adapter for observed local IPC, not a copy of
vendor source. It must run as root in a dedicated Docker PID namespace. The
vendor helper daemon can fork away from its launcher; exiting that container
is the cleanup boundary for such descendants. Do not restart a failed engine
inside an existing container or attach this adapter to another VPN session.
"""
from collections import deque
import hashlib
import math
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import threading
import time

from .native_rpc import Message, ProtocolError, receive
from .profile import profile


INSTALL_DIR = Path("/usr/local/UniVPN")
USER_HOME = Path("/root")
RUNTIME_DIR = Path("/run/univpn")
SOCKET_DIR = Path("/tmp")
VENDOR_SYSCONFIG = Path("/opt/gateway/vendor-sysconfig.ini")
CLIENT_SHA256 = "f26948ea1d3892f31a0692d4f771dcbc40365bfb2a39a509b9f3174d323781c4"
HELPER_SHA256 = "388d05e3e3a5aa0e7f4c80f6b642953b8b0c8fa99701f9c6e94926f1273960c0"
STARTUP_TIMEOUT = 15.0
EVENT_HISTORY = 64
_LISTEN_PORT = re.compile(rb"RPC Listen port (\d+)")


class NativeEngine:
    """Bootstrap once; never retry an authentication request automatically.

    ``config`` is the same gateway/port mapping accepted by ``profile``.
    ``call`` returns a native_rpc.Message, whose opcode is a result code.
    Event history contains header metadata only, never authentication bodies.
    ``last_alive`` is a monotonic timestamp of the most recent successful I/O;
    ``alive`` checks process/channel health, not VPN data-plane connectivity.
    """

    def __init__(self, config):
        self.config = dict(config)
        self.last_alive = None
        self.last_error = None
        self._helper = None
        self._engine = None
        self._bootstrap_channel = None
        self._sync = None
        self._async = None
        self._reader = None
        self._lock_file = None
        self._sync_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._events = deque(maxlen=EVENT_HISTORY)
        self._stopping = threading.Event()
        self._started = False
        self._closed = False
        self._broken = False

    @property
    def alive(self):
        return (self._started and not self._closed and not self._broken
                and self._engine is not None and self._engine.poll() is None
                and self._sync is not None and self._async is not None
                and self._reader is not None and self._reader.is_alive())

    def event_metadata(self):
        """Return a snapshot of the latest bounded, payload-free events."""
        with self._state_lock:
            return [dict(event) for event in self._events]

    def start(self):
        if self._started:
            if self.alive:
                return self
            raise RuntimeError("native session ended; restart the Docker container")
        if self._closed:
            raise RuntimeError("engine is closed; restart the Docker container")
        self._check_isolation()
        try:
            RUNTIME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._acquire_lock()
            self._verify_binaries()
            self._reject_existing_clients()
            self._initialize_config()
            baseline = self._helper_logs()
            self._helper = self._launch(INSTALL_DIR / "promote/UniVPNPromoteService")
            deadline = time.monotonic() + STARTUP_TIMEOUT
            endpoint = self._connect_helper(baseline, deadline)
            # Helper announcements use a different framing from CSDK requests.
            self._bootstrap_channel.sendall(
                struct.pack("!IIIIHHII", 2, 1, 2, 1, 1, 8, 2, 19070))
            client = INSTALL_DIR / "serviceclient/UniVPNCS"
            arguments = (f"UserPath={USER_HOME},LoginUser=root,"
                         f"InstallPath={INSTALL_DIR},RemotePort={endpoint}")
            self._engine = self._launch(client, arguments)
            self._sync = self._connect_control(1, deadline)
            self._async = self._connect_control(2, deadline)
            # The sync socket must remain open throughout login and MFA.
            self._reader = threading.Thread(target=self._read_events,
                                            name="univpn-native-events", daemon=True)
            self._reader.start()
            self._started = True
            # A successful read also detects missing/unwritable vendor defaults.
            result = self.call(9, 2, timeout=max(0.1, deadline - time.monotonic()))
            if result.opcode != 0xD0002 or not result.payload:
                raise ProtocolError("native system configuration initialization failed")
            return self
        except BaseException:
            self.close()
            raise

    def call(self, cls, op, payload=b"", timeout=45):
        """Send exactly once on the same sync channel and receive one frame.

        A timeout or malformed response invalidates the session: a partially
        read reply must never be mistaken for the next request's response.
        """
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        encoded = Message(2, 4, 1, cls, op, bytes(payload)).encode()
        with self._sync_lock:
            if not self.alive:
                raise RuntimeError("native engine unavailable; restart the Docker container")
            channel = self._sync
            try:
                deadline = time.monotonic() + timeout
                channel.settimeout(timeout)
                channel.sendall(encoded)
                response = receive(channel, deadline=deadline)
                self._validate_route(response, 1)
                self.last_alive = time.monotonic()
                return response
            except (OSError, ProtocolError) as error:
                self._broken = True
                self.last_error = type(error).__name__
                self._shutdown(channel)
                raise

    def close(self):
        """Close channels and terminate only launchers owned by this object.

        No process-name matching or global signal is used. The caller must exit
        the dedicated container to reap helper daemons which detached/forked.
        """
        if self._closed:
            return
        self._closed = True
        self._stopping.set()
        for channel in (self._sync, self._async, self._bootstrap_channel):
            self._shutdown(channel)
        self._sync = self._async = self._bootstrap_channel = None
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=1)
        for process in (self._engine, self._helper):
            if process is None or process.poll() is not None:
                continue
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None

    @staticmethod
    def _check_isolation():
        if os.name != "posix" or os.geteuid() != 0 or not Path("/.dockerenv").is_file():
            raise RuntimeError("native engine requires a dedicated root Docker container")

    def _acquire_lock(self):
        import fcntl
        handle = (RUNTIME_DIR / "native-engine.lock").open("a+b")
        os.chmod(handle.name, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError("a native engine already owns this container") from None
        self._lock_file = handle

    @staticmethod
    def _verify_binaries():
        for relative, expected in (("serviceclient/UniVPNCS", CLIENT_SHA256),
                                   ("promote/UniVPNPromoteService", HELPER_SHA256)):
            path = INSTALL_DIR / relative
            with path.open("rb") as stream:
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                raise RuntimeError(f"unverified vendor executable: {relative}")

    @staticmethod
    def _reject_existing_clients():
        paths = {(INSTALL_DIR / path).resolve() for path in (
            "serviceclient/UniVPNCS", "promote/UniVPNPromoteService")}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                executable = (entry / "exe").resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if executable in paths:
                raise RuntimeError("existing vendor process found; restart the Docker container")

    def _initialize_config(self):
        (INSTALL_DIR / "config").mkdir(mode=0o700, parents=True, exist_ok=True)
        source = INSTALL_DIR / "sysconfig.ini"
        cached = RUNTIME_DIR / "vendor-sysconfig.ini"
        if source.is_file():
            template = source.read_bytes()
        elif VENDOR_SYSCONFIG.is_file():
            # /run can be a tmpfs and CSDK deletes its install-time template.
            # Keep an immutable image copy available across container restarts.
            template = VENDOR_SYSCONFIG.read_bytes()
            source.write_bytes(template)
        elif cached.is_file():
            template = cached.read_bytes()
            source.write_bytes(template)
        else:
            raise RuntimeError("official sysconfig.ini template is missing")
        if not template.strip():
            raise RuntimeError("official sysconfig.ini template is empty")
        cached.write_bytes(template)
        cached.chmod(0o600)
        profile(self.config)
        # profile() creates the runtime gateway. Use complete vendor defaults
        # for the native engine, rather than its minimal CLI-compatible file.
        user_config = USER_HOME / "UniVPN/sysconfig.ini"
        user_config.write_bytes(template)
        user_config.chmod(0o600)

    @staticmethod
    def _launch(path, *arguments):
        return subprocess.Popen([str(path), *arguments], cwd=str(path.parent),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)

    @staticmethod
    def _helper_logs():
        result = {}
        for path in (INSTALL_DIR / "log").glob("UniVPN_UniVPNPromoteService_*.log"):
            try:
                info = path.stat()
                result[path] = (info.st_ino, info.st_size, info.st_mtime_ns)
            except OSError:
                continue
        return result

    def _connect_helper(self, baseline, deadline):
        while time.monotonic() < deadline:
            if self._helper.poll() not in (None, 0):
                raise RuntimeError("vendor helper launcher exited before readiness")
            current = self._helper_logs()
            candidates = []
            for path, identity in current.items():
                if baseline.get(path) == identity:
                    continue
                try:
                    with path.open("rb") as stream:
                        stream.seek(max(0, identity[1] - 65536))
                        matches = _LISTEN_PORT.findall(stream.read(65536))
                except OSError:
                    continue
                if matches:
                    endpoint = int(matches[-1])
                    if 1 <= endpoint <= 65535:
                        candidates.append((identity[2], endpoint))
            for _, endpoint in sorted(candidates, reverse=True):
                channel = socket.socket(socket.AF_UNIX)
                try:
                    channel.settimeout(min(0.25, max(0.01, deadline - time.monotonic())))
                    channel.connect(str(SOCKET_DIR / str(endpoint)))
                except OSError:
                    channel.close()
                    continue
                self._bootstrap_channel = channel
                return endpoint
            self._stopping.wait(0.05)
        raise RuntimeError("vendor helper RPC did not become ready")

    def _connect_control(self, connection_type, deadline):
        # Connect retries only happen before a channel exists. A successful
        # socket is retained: probing and closing it would terminate CSDK.
        while time.monotonic() < deadline:
            if self._engine.poll() is not None:
                raise RuntimeError("vendor engine exited before control readiness")
            channel = socket.socket(socket.AF_UNIX)
            try:
                channel.settimeout(min(0.25, max(0.01, deadline - time.monotonic())))
                channel.connect(str(SOCKET_DIR / "19060"))
            except OSError:
                channel.close()
                self._stopping.wait(0.05)
                continue
            try:
                channel.sendall(Message(2, 4, connection_type, 1, 0x10006, b"").encode())
                channel.settimeout(None)
            except BaseException:
                channel.close()
                raise
            self.last_alive = time.monotonic()
            return channel
        raise RuntimeError("vendor CSDK control channel did not become ready")

    def _read_events(self):
        channel = self._async
        try:
            while not self._stopping.is_set():
                event = receive(channel)
                self._validate_route(event, 2)
                now = time.monotonic()
                metadata = {"source": event.source, "destination": event.destination,
                            "connection_type": event.connection_type,
                            "class": event.message_class, "code": event.opcode,
                            "length": len(event.payload), "received_at": now}
                with self._state_lock:
                    self._events.append(metadata)
                    self.last_alive = now
        except (OSError, ProtocolError) as error:
            if not self._stopping.is_set():
                self.last_error = type(error).__name__
                self._broken = True

    @staticmethod
    def _validate_route(message, connection_type):
        if (message.source, message.destination, message.connection_type) != (4, 2, connection_type):
            raise ProtocolError("unexpected native response routing")

    @staticmethod
    def _shutdown(channel):
        if channel is None:
            return
        try:
            channel.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        channel.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()
