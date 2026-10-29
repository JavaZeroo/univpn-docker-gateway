"""Bounded IPv4 TCP SOCKS5 ingress; all outbound sockets require the VPN device."""
import errno
import ipaddress
import json
import re
import selectors
import socket
import struct
import subprocess
import threading
import time


MAX_CLIENTS = 64
MAX_BUFFER = 64 * 1024
HANDSHAKE_TIMEOUT = 5.0
CONNECT_TIMEOUT = 3.0
ROUTE_TIMEOUT = 1.0
POLL_INTERVAL = 0.2


class _Rejected(Exception):
    def __init__(self, reply=1):
        self.reply = reply


def _close(channel):
    try:
        channel.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        channel.close()
    except OSError:
        pass


class SocksProxy:
    """CONNECT-only SOCKS5 server. Readiness must be a cheap cached-state reader.

    Readiness must return the boolean True; truthy status objects fail closed.
    No target DNS resolution, authentication methods, UDP, BIND or IPv6 are
    supported. A numeric IPv4 sent using SOCKS's domain field is accepted only
    in canonical dotted-decimal form. No request or exception data is logged.
    """

    def __init__(self, config, ready, host="0.0.0.0", port=1080):
        self._interface = config["vpn_interface"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,15}", self._interface):
            raise ValueError("invalid VPN interface")
        try:
            self._allowed = tuple(ipaddress.IPv4Network(value, strict=True)
                                  for value in config["allowed_cidrs"])
            self._host = str(ipaddress.IPv4Address(host))
        except (ValueError, TypeError):
            raise ValueError("explicit IPv4 proxy addresses are required") from None
        if not self._allowed or any(network.prefixlen == 0 for network in self._allowed):
            raise ValueError("explicit non-default IPv4 destinations are required")
        if not callable(ready) or type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("invalid proxy readiness or port")
        self._ready = ready
        self._port = port
        self._listener = None
        self._address = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_CLIENTS)
        self._sockets = set()
        self._flows = set()
        self._workers = set()
        self._threads = []
        self._started = False

    @property
    def address(self):
        return self._address

    def _is_ready(self):
        try:
            return not self._stop.is_set() and self._ready() is True
        except Exception:
            return False

    def start(self):
        if self._stop.is_set():
            raise RuntimeError("a closed SOCKS proxy cannot restart")
        if self._started:
            return self
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self._host, self._port))
            listener.listen(MAX_CLIENTS)
            listener.settimeout(POLL_INTERVAL)
            self._listener = listener
            self._address = listener.getsockname()
            self._started = True
            self._threads = [threading.Thread(target=self._accept, daemon=True),
                             threading.Thread(target=self._watch, daemon=True)]
            for thread in self._threads:
                thread.start()
        except BaseException:
            self.close()
            listener.close()
            raise
        return self

    def close(self):
        self._stop.set()
        if self._listener is not None:
            _close(self._listener)
        with self._lock:
            channels = tuple(self._sockets)
            workers = tuple(self._workers)
        for channel in channels:
            _close(channel)
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT + 1
        for thread in (*self._threads, *workers):
            if thread is not threading.current_thread() and thread.ident is not None:
                thread.join(max(0, deadline - time.monotonic()))

    def _watch(self):
        while not self._stop.wait(POLL_INTERVAL):
            if not self._is_ready():
                with self._lock:
                    flows = tuple(self._flows)
                for pair in flows:
                    for channel in pair:
                        _close(channel)

    def _accept(self):
        while not self._stop.is_set():
            try:
                channel, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self._slots.acquire(blocking=False):
                _close(channel)
                continue
            worker = threading.Thread(target=self._serve, args=(channel,), daemon=True)
            failed = False
            with self._lock:
                if self._stop.is_set():
                    _close(channel)
                    self._slots.release()
                    return
                self._sockets.add(channel)
                self._workers.add(worker)
                try:
                    worker.start()
                except RuntimeError:
                    self._sockets.discard(channel)
                    self._workers.discard(worker)
                    failed = True
            if failed:
                _close(channel)
                self._slots.release()

    @staticmethod
    def _receive(channel, length, deadline):
        data = bytearray()
        while len(data) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Rejected()
            channel.settimeout(remaining)
            chunk = channel.recv(length - len(data))
            if not chunk:
                raise _Rejected()
            data.extend(chunk)
        return bytes(data)

    @staticmethod
    def _send(channel, data, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _Rejected()
        channel.settimeout(remaining)
        channel.sendall(data)

    def _negotiate(self, channel, deadline):
        version, count = self._receive(channel, 2, deadline)
        if version != 5 or count == 0:
            self._send(channel, b"\x05\xff", deadline)
            raise _Rejected()
        methods = self._receive(channel, count, deadline)
        if 0 not in methods:
            self._send(channel, b"\x05\xff", deadline)
            raise _Rejected()
        self._send(channel, b"\x05\x00", deadline)

    def _request(self, channel, deadline):
        version, command, reserved, kind = self._receive(channel, 4, deadline)
        if version != 5 or reserved != 0:
            raise _Rejected(1)
        if command != 1:
            raise _Rejected(7)
        if kind == 1:
            address = ipaddress.IPv4Address(self._receive(channel, 4, deadline))
        elif kind == 3:
            length = self._receive(channel, 1, deadline)[0]
            if not 7 <= length <= 15:
                raise _Rejected(8)
            try:
                text = self._receive(channel, length, deadline).decode("ascii")
                address = ipaddress.IPv4Address(text)
                if str(address) != text:
                    raise ValueError()
            except (UnicodeError, ValueError):
                raise _Rejected(8) from None
        else:
            raise _Rejected(8)
        port = struct.unpack("!H", self._receive(channel, 2, deadline))[0]
        if (not port or address.is_unspecified or address.is_multicast or
                not any(address in network for network in self._allowed)):
            raise _Rejected(2)
        return address, port

    def _route_matches(self, address, timeout):
        try:
            result = subprocess.run(["ip", "-j", "route", "get", str(address)],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, check=True, timeout=timeout)
            routes = json.loads(result.stdout)
            return (isinstance(routes, list) and len(routes) == 1 and
                    isinstance(routes[0], dict) and routes[0].get("dev") == self._interface and
                    routes[0].get("type", "unicast") == "unicast")
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    def _dial(self, address, port, deadline):
        binding = getattr(socket, "SO_BINDTODEVICE", None)
        if binding is None:
            raise _Rejected(3)
        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with self._lock:
            self._sockets.add(upstream)
        try:
            upstream.setsockopt(socket.SOL_SOCKET, binding, self._interface.encode("ascii") + b"\0")
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._route_matches(address, min(ROUTE_TIMEOUT, remaining)):
                raise _Rejected(3)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Rejected(4)
            upstream.settimeout(min(CONNECT_TIMEOUT, remaining))
            upstream.connect((str(address), port))
            return upstream
        except BaseException:
            with self._lock:
                self._sockets.discard(upstream)
            _close(upstream)
            raise

    def _serve(self, client):
        upstream = None
        pair = None
        negotiated = False
        connected = False
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT
        try:
            self._negotiate(client, deadline)
            negotiated = True
            address, port = self._request(client, deadline)
            if not self._is_ready():
                raise _Rejected(2)
            upstream = self._dial(address, port, deadline)
            with self._lock:
                self._sockets.add(upstream)  # Also supports a controlled test dialer.
            if not self._is_ready():
                raise _Rejected(2)
            pair = (client, upstream)
            with self._lock:
                self._flows.add(pair)
            bound_host, bound_port = upstream.getsockname()[:2]
            reply = b"\x05\x00\x00\x01" + socket.inet_aton(bound_host) + struct.pack("!H", bound_port)
            self._send(client, reply, deadline)
            connected = True
            self._relay(client, upstream)
        except _Rejected as error:
            if negotiated and not connected:
                try:
                    self._send(client, bytes((5, error.reply, 0, 1)) + b"\0" * 6, deadline)
                except (OSError, _Rejected):
                    pass
        except OSError as error:
            if negotiated and not connected:
                code = {errno.EACCES: 2, errno.EPERM: 2, errno.ENETUNREACH: 3,
                        errno.EHOSTUNREACH: 4, errno.ECONNREFUSED: 5}.get(error.errno, 1)
                try:
                    self._send(client, bytes((5, code, 0, 1)) + b"\0" * 6, deadline)
                except (OSError, _Rejected):
                    pass
        except Exception:
            # Internal exceptions must never become request/target-bearing logs.
            if negotiated and not connected:
                try:
                    self._send(client, b"\x05\x01\x00\x01" + b"\0" * 6, deadline)
                except (OSError, _Rejected):
                    pass
        finally:
            with self._lock:
                if pair is not None:
                    self._flows.discard(pair)
                self._sockets.discard(client)
                if upstream is not None:
                    self._sockets.discard(upstream)
                self._workers.discard(threading.current_thread())
            _close(client)
            if upstream is not None:
                _close(upstream)
            self._slots.release()

    def _relay(self, client, upstream):
        if not self._is_ready():
            return
        peers = {client: upstream, upstream: client}
        buffers = {client: bytearray(), upstream: bytearray()}
        reading = {client: True, upstream: True}
        finished = {client: False, upstream: False}
        for channel in peers:
            channel.setblocking(False)
        with selectors.DefaultSelector() as selector:
            while not self._stop.is_set():
                for channel, other in peers.items():
                    if not reading[other] and not buffers[channel] and not finished[channel]:
                        try:
                            channel.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                        finished[channel] = True
                    events = 0
                    if reading[channel] and len(buffers[other]) < MAX_BUFFER:
                        events |= selectors.EVENT_READ
                    if buffers[channel]:
                        events |= selectors.EVENT_WRITE
                    if channel in selector.get_map():
                        if events:
                            selector.modify(channel, events)
                        else:
                            selector.unregister(channel)
                    elif events:
                        selector.register(channel, events)
                if not selector.get_map():
                    return
                try:
                    events = selector.select(POLL_INTERVAL)
                    for key, mask in events:
                        channel = key.fileobj
                        other = peers[channel]
                        if mask & selectors.EVENT_READ:
                            try:
                                data = channel.recv(min(16384, MAX_BUFFER - len(buffers[other])))
                            except BlockingIOError:
                                data = None
                            if data:
                                buffers[other].extend(data)
                            elif data == b"":
                                reading[channel] = False
                        if mask & selectors.EVENT_WRITE and buffers[channel]:
                            try:
                                count = channel.send(buffers[channel])
                            except BlockingIOError:
                                continue
                            if not count:
                                return
                            del buffers[channel][:count]
                except (OSError, ValueError):
                    return
