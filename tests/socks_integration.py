"""Real SOCKS5/device binding over a simulated VPN in private Linux namespaces.

Run as root only in a disposable Linux test environment. The outer unshare
creates both network and mount namespaces before any interface/mount change.
The VPN is a veth, not UniVPN; no vendor process, credential, Windows service,
WireGuard interface, or existing network namespace is used.
"""
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time


ISOLATED = "UVPN_SOCKS_TEST_ISOLATED"
PARENT_NET = "UVPN_SOCKS_TEST_PARENT_NET"
PARENT_MOUNT = "UVPN_SOCKS_TEST_PARENT_MOUNT"
DEADLINE = "UVPN_SOCKS_TEST_DEADLINE"
ALLOWED = "10.80.0.11"
DENIED = "10.80.0.12"
BODY_SHA256 = hashlib.sha256(b"S" * (1024 * 1024)).hexdigest()


SERVER = r'''
import json, socket, sys, threading, time
log = sys.argv[1]
lock = threading.Lock()
body = b'S' * (1024 * 1024)

def accepted(kind, peer):
    with lock:
        with open(log, 'a', encoding='ascii') as stream:
            stream.write(json.dumps({'kind':kind, 'source':peer[0]}) + '\n')

def handle(channel, peer, kind):
    try:
        with channel:
            channel.settimeout(3)
            if kind == 'ssh':
                channel.sendall(b'SSH-2.0-socks-integration peer=' + peer[0].encode() + b'\r\n')
            elif kind == 'http':
                request = b''
                while b'\r\n\r\n' not in request and len(request) < 8192:
                    data = channel.recv(4096)
                    if not data: return
                    request += data
                header = (b'HTTP/1.0 200 OK\r\nContent-Length: 1048576\r\n'
                          + b'X-Peer-Address: ' + peer[0].encode() + b'\r\n\r\n')
                channel.sendall(header + body)
            else:
                channel.sendall(b'source=' + peer[0].encode() + b'\n')
                while True:
                    data = channel.recv(16384)
                    if not data: return
                    channel.sendall(data)
    except OSError:
        pass

def serve(listener, kind):
    while True:
        channel, peer = listener.accept()
        accepted(kind, peer)
        threading.Thread(target=handle, args=(channel,peer,kind), daemon=True).start()

listeners = []
for host, port, kind in [('10.80.0.11',2222,'ssh'), ('10.80.0.12',2222,'ssh'),
                         ('10.80.0.11',8080,'http'), ('10.80.0.11',9999,'echo')]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host,port)); listener.listen(16)
    listeners.append((listener,kind))
for listener, kind in listeners:
    threading.Thread(target=serve, args=(listener,kind), daemon=True).start()
print('READY', flush=True)
while True: time.sleep(1)
'''


CLIENT_COMMON = r'''
import socket, struct

def receive(channel, length):
    result = bytearray()
    while len(result) < length:
        data = channel.recv(length-len(result))
        if not data: raise ValueError('incomplete test response')
        result.extend(data)
    return bytes(result)

def connect(host, port):
    channel = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    channel.settimeout(1)
    channel.connect((host,port))
    return channel

def proxied(host, port, expected=0, numeric=False):
    channel = connect('10.203.0.1',1080)
    channel.sendall(b'\x05\x01\x00')
    if receive(channel,2) != b'\x05\x00': raise ValueError('method differs')
    if numeric:
        address = host.encode('ascii')
        frame = b'\x05\x01\x00\x03' + bytes((len(address),)) + address
    else:
        frame = b'\x05\x01\x00\x01' + socket.inet_aton(host)
    channel.sendall(frame + struct.pack('!H',port))
    reply = receive(channel,10)
    if reply[0] != 5 or reply[1] != expected or reply[2:4] != b'\x00\x01':
        raise ValueError('unexpected SOCKS reply')
    if not expected and (socket.inet_ntoa(reply[4:8]) != '192.0.2.1'
                         or struct.unpack('!H',reply[8:])[0] == 0):
        raise ValueError('outbound bound address differs')
    return channel
'''


PROBE = CLIENT_COMMON + r'''
import hashlib, http.client, json, sys
host, port, kind, source, expected, numeric = sys.argv[1:]
port, expected = int(port), int(expected)
try:
    channel = connect(host,port) if kind == 'direct' else proxied(host,port,expected,numeric=='1')
    with channel:
        if expected:
            result = {'refused':expected}
        elif kind == 'http':
            channel.sendall(b'GET / HTTP/1.0\r\nHost: integration\r\n\r\n')
            response = http.client.HTTPResponse(channel)
            response.begin()
            body = response.read(1024*1024 + 1)
            digest = hashlib.sha256(body).hexdigest()
            if (response.status != 200 or response.getheader('X-Peer-Address') != source
                    or response.getheader('Content-Length') != '1048576'
                    or len(body) != 1024*1024
                    or digest != hashlib.sha256(b'S'*(1024*1024)).hexdigest()):
                raise ValueError('HTTP body or source differs')
            result = {'bytes':len(body),'sha256':digest,'source':source}
        else:
            data = b''
            while not data.endswith(b'\r\n') and len(data) < 256:
                part = channel.recv(256-len(data))
                if not part: break
                data += part
            if data != b'SSH-2.0-socks-integration peer=' + source.encode() + b'\r\n':
                raise ValueError('SSH-like banner or source differs')
            result = {'ssh_banner':True,'source':source}
    print(json.dumps(result))
except (OSError,ValueError):
    sys.exit(2)
'''


LIVE = CLIENT_COMMON + r'''
import json, sys
try:
    with proxied('10.80.0.11',9999) as channel:
        line = b''
        while not line.endswith(b'\n') and len(line) < 128:
            line += receive(channel,1)
        if line != b'source=192.0.2.1\n': raise ValueError('echo source differs')
        channel.sendall(b'keep-alive')
        if receive(channel,10) != b'keep-alive': raise ValueError('echo differs')
        print(json.dumps({'active':True}), flush=True)
        channel.settimeout(2)
        try:
            data = channel.recv(1)
        except ConnectionResetError:
            data = b''
        if data: raise ValueError('stream was not closed cleanly')
        print(json.dumps({'closed':True}), flush=True)
except (OSError,ValueError):
    sys.exit(2)
'''


def inode(kind, process="self"):
    return str(os.stat(f"/proc/{process}/ns/{kind}").st_ino)


def isolated_run():
    if shutil.which("unshare") is None:
        raise RuntimeError("unshare is required")
    environment = dict(os.environ)
    environment.update({ISOLATED: "1", PARENT_NET: inode("net"),
                        PARENT_MOUNT: inode("mnt"), DEADLINE: str(time.monotonic() + 26),
                        "PYTHONDONTWRITEBYTECODE": "1"})
    child = subprocess.Popen(["unshare", "--mount", "--net", "--propagation", "private",
                              sys.executable, "-B", str(Path(__file__).resolve())],
                             env=environment, start_new_session=True)

    def stop_group():
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            child.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=0.5)

    def interrupted(*_):
        raise RuntimeError("SOCKS integration interrupted")

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        return child.wait(timeout=28)
    except subprocess.TimeoutExpired:
        stop_group()
        raise RuntimeError("SOCKS integration exceeded its total time budget") from None
    except BaseException:
        stop_group()
        raise
    finally:
        signal.signal(signal.SIGTERM, previous)


def network_test(deadline):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from univpn_gateway.socks_proxy import SocksProxy

    suffix = str(os.getpid())
    client, server = "usocks-client-" + suffix, "usocks-server-" + suffix
    namespaces, children = [], []
    proxy = None
    ready = threading.Event()
    ready.set()

    def budget(maximum=3):
        remaining = deadline - time.monotonic() - 2
        if remaining <= 0:
            raise RuntimeError("SOCKS integration exhausted its operation budget")
        return min(maximum, remaining)

    def run(*arguments):
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=budget())
        if result.returncode:
            raise RuntimeError(f"test command failed: {arguments[0]} ({result.returncode})")
        return result.stdout

    def line(child):
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            if not selector.select(budget(2)):
                raise RuntimeError("test child did not become ready")
        text = child.stdout.readline()
        if not text:
            raise RuntimeError("test child exited before readiness")
        return text.strip()

    def probe(host=ALLOWED, port=2222, kind="ssh", *, namespace=client,
              source="192.0.2.1", reply=0, numeric=False):
        command = ["ip", "netns", "exec", namespace] if namespace else []
        result = subprocess.run([*command, sys.executable, "-B", "-c", PROBE, host,
                                 str(port), kind, source, str(reply), "1" if numeric else "0"],
                                capture_output=True, text=True, timeout=budget(2))
        if result.returncode:
            raise AssertionError(f"{kind} SOCKS/control probe failed ({result.returncode})")
        return json.loads(result.stdout)

    def route_device(host):
        return json.loads(run("ip", "-j", "route", "get", host))[0]["dev"]

    def assert_no_accepts(records, before):
        # Accept logging is synchronous in each accept loop, not delayed until a
        # handler gets scheduled. Observe a bounded window after refusal too.
        until = time.monotonic() + 0.3
        while True:
            budget(0.1)
            if len(records.read_text().splitlines()) != before:
                raise AssertionError("a refused SOCKS request still reached a server")
            remaining = until - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.02, remaining))

    def stop_children():
        for child in children:
            if child.poll() is None:
                try:
                    child.terminate()
                except ProcessLookupError:
                    pass
        for child in children:
            try:
                child.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                try:
                    child.kill()
                except ProcessLookupError:
                    pass
                child.wait(timeout=0.5)
            for stream in (child.stdout, child.stderr):
                if stream:
                    stream.close()

    try:
        # /run itself is private, so even ip-netns's name/bind-mount files cannot
        # appear in the caller's /run/netns. The inode guard ran before this mount.
        run("mount", "-t", "tmpfs", "-o", "mode=0755,nosuid,nodev", "tmpfs", "/run")
        Path("/run/netns").mkdir(mode=0o755)
        for namespace in (client, server):
            run("ip", "netns", "add", namespace)
            namespaces.append(namespace)
            run("ip", "-n", namespace, "link", "set", "lo", "up")
        run("ip", "link", "set", "lo", "up")
        for local, peer, namespace, local_ip, remote_ip in (
            ("entry", "client0", client, "10.203.0.1/30", "10.203.0.2/30"),
            ("cnem_vnic", "vpn0", server, "192.0.2.1/30", "192.0.2.2/30"),
            ("eth0", "plain0", server, "198.51.100.1/30", "198.51.100.2/30"),
        ):
            run("ip", "link", "add", local, "type", "veth", "peer", "name", peer)
            run("ip", "link", "set", peer, "netns", namespace)
            run("ip", "addr", "add", local_ip, "dev", local)
            run("ip", "link", "set", local, "up")
            run("ip", "-n", namespace, "addr", "add", remote_ip, "dev", peer)
            run("ip", "-n", namespace, "link", "set", peer, "up")
        for target in (ALLOWED, DENIED):
            run("ip", "-n", server, "addr", "add", target + "/32", "dev", "lo")
        run("ip", "route", "add", ALLOWED + "/32", "via", "192.0.2.2", "dev", "cnem_vnic")
        run("ip", "route", "add", "default", "via", "198.51.100.2", "dev", "eth0")
        if route_device(ALLOWED) != "cnem_vnic":
            raise AssertionError("VPN route does not select the simulated VPN")

        with tempfile.TemporaryDirectory(prefix="usocks-" + suffix + "-", dir="/run") as temporary:
            records = Path(temporary) / "accepts.jsonl"
            records.touch(mode=0o600)
            server_child = subprocess.Popen(["ip", "netns", "exec", server,
                                             sys.executable, "-B", "-c", SERVER, str(records)],
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            text=True, bufsize=1)
            children.append(server_child)
            if line(server_child) != "READY":
                raise AssertionError("test services are not ready")
            proxy = SocksProxy({"allowed_cidrs": [ALLOWED + "/32"], "vpn_interface": "cnem_vnic"},
                               ready.is_set, host="0.0.0.0", port=1080).start()
            ssh = probe()
            probe(numeric=True)
            http = probe(port=8080, kind="http")
            if http["sha256"] != BODY_SHA256 or http["bytes"] != 1024 * 1024:
                raise AssertionError("HTTP payload differs")
            # The unlisted target is genuinely reachable via the ordinary path.
            probe(DENIED, kind="direct", namespace=None, source="198.51.100.1")
            before = len(records.read_text().splitlines())
            probe(DENIED, reply=2)
            assert_no_accepts(records, before)

            live = subprocess.Popen(["ip", "netns", "exec", client, sys.executable, "-B", "-c", LIVE],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
            children.append(live)
            if json.loads(line(live)) != {"active": True}:
                raise AssertionError("persistent test stream is not ready")
            # Check the kernel option on this real live outbound socket; source
            # selection alone could also occur on an accidentally unbound socket.
            bound = False
            with proxy._lock:
                for _, upstream in tuple(proxy._flows):
                    try:
                        if upstream.getpeername() == (ALLOWED, 9999):
                            device = upstream.getsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, 16)
                            bound = device.rstrip(b"\0") == b"cnem_vnic"
                    except OSError:
                        pass
            if not bound:
                raise AssertionError("SO_BINDTODEVICE is not active on the real stream")
            revoked_at = time.monotonic()
            ready.clear()
            probe(reply=2)
            output, _ = live.communicate(timeout=budget(1.2))
            closed = json.loads(output.strip()) if output.strip() else {}
            if (live.returncode or not closed.get("closed") or
                    time.monotonic() - revoked_at >= 1):
                raise AssertionError("readiness revocation did not promptly close the stream")
            ready.set()
            time.sleep(0.4)  # Allow the readiness watch to complete a recovery poll.
            probe()

            # Leave readiness true: only the device/route guard must reject the
            # functional ordinary fallback, rather than a failed readiness flag.
            run("ip", "route", "del", ALLOWED + "/32", "via", "192.0.2.2", "dev", "cnem_vnic")
            if route_device(ALLOWED) != "eth0":
                raise AssertionError("ordinary fallback route is not active")
            plain = probe(kind="direct", namespace=None, source="198.51.100.1")
            before = len(records.read_text().splitlines())
            probe(reply=3)
            assert_no_accepts(records, before)
            run("ip", "route", "add", ALLOWED + "/32", "via", "192.0.2.2", "dev", "cnem_vnic")
            probe()
            print(json.dumps({"socks_tcp_ssh": ssh["ssh_banner"], "http_bytes": http["bytes"],
                              "http_sha256": http["sha256"], "vpn_source": ssh["source"],
                              "numeric_domain_without_dns": True, "kernel_device_binding": True,
                              "unlisted_destination_blocked": True, "ready_revokes_existing": True,
                              "ready_recovery": True, "working_plain_source": plain["source"],
                              "route_fallback_blocked": True, "fallback_server_accepts_unchanged": True,
                              "refusal_observation_seconds": 0.3,
                              "vpn_simulated_by_veth": True}))
            # Stop processes before removing their temporary record directory.
            proxy.close()
            proxy = None
            stop_children()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        if proxy is not None:
            proxy.close()
        stop_children()
        for namespace in reversed(namespaces):
            try:
                subprocess.run(["ip", "netns", "del", namespace], capture_output=True, timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass  # The private mount/net namespaces disappear on process exit.


def main():
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("requires root in a disposable Linux test environment")
    if os.environ.get(ISOLATED) != "1":
        return isolated_run()
    parent = os.getppid()
    if (os.environ.get(PARENT_NET) != inode("net", parent) or
            os.environ.get(PARENT_MOUNT) != inode("mnt", parent) or
            inode("net") == inode("net", parent) or inode("mnt") == inode("mnt", parent)):
        raise RuntimeError("refusing to test without private network and mount namespaces")
    for tool in ("ip", "mount"):
        if shutil.which(tool) is None:
            raise RuntimeError("required isolated-network test tool is missing")
    deadline = float(os.environ[DEADLINE])
    def terminated(*_):
        raise RuntimeError("SOCKS integration terminated or exceeded its operation budget")
    signal.signal(signal.SIGTERM, terminated)
    signal.signal(signal.SIGALRM, terminated)
    signal.setitimer(signal.ITIMER_REAL, max(0.1, deadline - time.monotonic() - 2))
    network_test(deadline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
