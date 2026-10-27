"""Real WireGuard -> gateway rules -> simulated VPN, in fresh Linux namespaces.

Run as root on a disposable Linux test host with iproute2, wireguard-tools,
iptables and unshare. This test creates actual kernel WireGuard interfaces;
there is no veth substitute if WireGuard is unavailable. Only the VPN segment
is simulated by veth. It does not authenticate UniVPN or test Windows/Docker
Desktop forwarding. An outer unshare keeps all test interfaces and rules out
of the invoking network namespace. No vendor executable or credential is used.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time


BODY_BYTES = 1024 * 1024
BODY_SHA256 = hashlib.sha256(b"W" * BODY_BYTES).hexdigest()
_ISOLATED = "UVPN_WG_ISOLATED_TEST"
_PARENT_NS = "UVPN_WG_PARENT_NETNS"
_DEADLINE = "UVPN_WG_TEST_DEADLINE"


SERVER = r'''
import socket, threading, time
body = b'W' * (1024 * 1024)
def tcp(host, port, http):
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port)); listener.listen()
    while True:
        connection, _ = listener.accept()
        try:
            with connection:
                connection.settimeout(2)
                if http:
                    request = b''
                    while b'\r\n\r\n' not in request and len(request) < 8192:
                        data = connection.recv(4096)
                        if not data: break
                        request += data
                    header = (b'HTTP/1.0 200 OK\r\nContent-Length: '
                              + str(len(body)).encode() + b'\r\n\r\n')
                    connection.sendall(header + body)
                else:
                    connection.sendall(b'SSH-2.0-wireguard-integration\r\n')
        except OSError:
            pass
def udp():
    endpoint = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    endpoint.bind(('10.80.0.11', 9999))
    while True:
        data, peer = endpoint.recvfrom(4096)
        endpoint.sendto(peer[0].encode() + b':' + data, peer)
for host, port, http in [('10.80.0.11', 2222, False),
                         ('10.80.0.11', 8080, True),
                         ('10.80.0.12', 8080, True)]:
    threading.Thread(target=tcp, args=(host, port, http), daemon=True).start()
threading.Thread(target=udp, daemon=True).start()
while True: time.sleep(1)
'''


PROBE = r'''
import hashlib, json, socket, sys
host, port, kind, timeout, source, nonce = sys.argv[1:]
port, timeout = int(port), float(timeout)
source_port = 40000 + int(nonce.rsplit('-', 1)[1])
try:
    if kind == 'udp':
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.settimeout(timeout)
            connection.bind(('0.0.0.0', source_port))
            connection.sendto(nonce.encode(), (host, port))
            data = connection.recv(4096)
        if data != source.encode() + b':' + nonce.encode():
            raise ValueError('UDP echo or source differs')
        result = {'source': source, 'echo': True}
    else:
        with socket.create_connection((host, port), timeout=timeout,
                                      source_address=('0.0.0.0', source_port)) as connection:
            connection.settimeout(timeout)
            if kind == 'http':
                connection.sendall(b'GET / HTTP/1.0\r\nHost: integration\r\n\r\n')
            chunks = []
            size = 0
            while True:
                data = connection.recv(65536)
                if not data: break
                chunks.append(data); size += len(data)
                if size > 2 * 1024 * 1024: raise ValueError('oversized response')
            data = b''.join(chunks)
        if kind == 'http':
            header, body = data.split(b'\r\n\r\n', 1)
            digest = hashlib.sha256(body).hexdigest()
            if (not header.startswith(b'HTTP/1.0 200 OK\r\n')
                    or b'Content-Length: 1048576' not in header
                    or len(body) != 1024 * 1024
                    or digest != hashlib.sha256(b'W' * (1024 * 1024)).hexdigest()):
                raise ValueError('incomplete or corrupt HTTP body')
            result = {'bytes': len(body), 'sha256': digest}
        else:
            if not data.startswith(b'SSH-2.0-wireguard-integration\r\n'):
                raise ValueError('unexpected SSH-like banner')
            result = {'ssh_banner': True}
    print(json.dumps(result))
except OSError:
    sys.exit(1)
except (ValueError, IndexError):
    sys.exit(2)
'''


def main():
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("requires root on a disposable Linux test host")
    if os.environ.get(_ISOLATED) != "1":
        return isolated_run()
    current = str(os.stat("/proc/self/ns/net").st_ino)
    if not os.environ.get(_PARENT_NS) or current == os.environ[_PARENT_NS]:
        raise RuntimeError("refusing to test in the invoking network namespace")
    for tool in ("ip", "wg", "iptables", "iptables-restore", "sysctl"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"required test tool is missing: {tool}")
    def terminated(*_):
        raise RuntimeError("integration test terminated")

    signal.signal(signal.SIGTERM, terminated)
    return network_test(float(os.environ[_DEADLINE]))


def isolated_run():
    if shutil.which("unshare") is None:
        raise RuntimeError("unshare is required for network isolation")
    environment = dict(os.environ)
    environment.update({
        _ISOLATED: "1",
        _PARENT_NS: str(os.stat("/proc/self/ns/net").st_ino),
        _DEADLINE: str(time.monotonic() + 26),
    })
    # A private mount namespace also confines ip-netns bind mounts. No external
    # container, existing namespace or production interface is selected here.
    child = subprocess.Popen(
        ["unshare", "--mount", "--net", "--propagation", "private",
         sys.executable, str(Path(__file__).resolve())],
        env=environment, start_new_session=True)
    try:
        return child.wait(timeout=28)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=0.5)
        raise RuntimeError("integration test exceeded its total time budget") from None


def network_test(deadline):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from univpn_gateway import gateway

    suffix = str(os.getpid())
    client, server = "uwg-client-" + suffix, "uwg-server-" + suffix
    namespaces, children = [], []
    probe_number = 0

    def budget(maximum=3):
        remaining = deadline - time.monotonic() - 2
        if remaining <= 0:
            raise RuntimeError("integration test exhausted its operation budget")
        return min(maximum, remaining)

    def run(*arguments, input=None, namespace=None):
        command = (["ip", "netns", "exec", namespace] if namespace else [])
        result = subprocess.run([*command, *arguments], input=input,
                                capture_output=True, text=True, timeout=budget())
        if result.returncode:
            # In particular, never expose wg genkey/pubkey input or output.
            raise RuntimeError(f"test command failed: {arguments[0]} ({result.returncode})")
        return result.stdout

    def probe(host, port, kind="ssh", *, namespace=client, expected=True,
              source="192.0.2.1", timeout=0.45):
        nonlocal probe_number
        probe_number += 1
        command = (["ip", "netns", "exec", namespace] if namespace else [])
        result = subprocess.run(
            [*command, sys.executable, "-c", PROBE, host, str(port), kind,
             str(timeout), source, "echo-" + str(probe_number)],
            capture_output=True, text=True, timeout=budget(timeout + 0.8))
        if expected:
            if result.returncode != 0:
                raise AssertionError(f"{kind} positive probe failed ({result.returncode})")
            return json.loads(result.stdout)
        if result.returncode != 1:
            raise AssertionError(f"{kind} refusal was not a network failure ({result.returncode})")
        return None

    def route_device(host, namespace=None):
        routes = json.loads(run("ip", "-j", "route", "get", host, namespace=namespace))
        return routes[0]["dev"]

    def keypair(directory, name):
        private = run("wg", "genkey").strip() + "\n"
        path = directory / name
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(private)
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise AssertionError("test key permissions are not 0600")
        public = run("wg", "pubkey", input=private).strip()
        return path, public

    # gateway.py normally invokes its own subprocess helper without timeouts.
    # Bound those subprocesses in this test only, including restore's stdin.
    original_run = subprocess.run

    def bounded_run(*arguments, **kwargs):
        kwargs["timeout"] = min(kwargs.get("timeout", 3), budget())
        return original_run(*arguments, **kwargs)

    try:
        for namespace in (client, server):
            run("ip", "netns", "add", namespace)
            namespaces.append(namespace)
            run("ip", "link", "set", "lo", "up", namespace=namespace)
        for namespace in (None, client, server):
            run("sysctl", "-q", "-w", "net.ipv4.conf.all.rp_filter=0", namespace=namespace)
            run("sysctl", "-q", "-w", "net.ipv4.conf.default.rp_filter=0", namespace=namespace)
        run("ip", "link", "set", "lo", "up")
        for local, remote, namespace, local_ip, remote_ip in (
            ("underlay0", "client0", client, "198.18.0.1/30", "198.18.0.2/30"),
            ("cnem_vnic", "vpn0", server, "192.0.2.1/30", "192.0.2.2/30"),
            ("eth0", "plain0", server, "198.51.100.1/30", "198.51.100.2/30"),
        ):
            run("ip", "link", "add", local, "type", "veth", "peer", "name", remote)
            run("ip", "link", "set", remote, "netns", namespace)
            run("ip", "addr", "add", local_ip, "dev", local)
            run("ip", "link", "set", local, "up")
            run("ip", "addr", "add", remote_ip, "dev", remote, namespace=namespace)
            run("ip", "link", "set", remote, "up", namespace=namespace)
        # Both interfaces are created in the namespace of their UDP underlay.
        # Failure here means unsupported real WireGuard, not a veth fallback.
        run("ip", "link", "add", "wg0", "type", "wireguard")
        run("ip", "link", "add", "wgclient", "type", "wireguard", namespace=client)
        with tempfile.TemporaryDirectory(prefix="uvpn-wg-keys-") as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            gateway_key, gateway_public = keypair(directory, "gateway.key")
            client_key, client_public = keypair(directory, "client.key")
            run("wg", "set", "wg0", "listen-port", "51820", "private-key", str(gateway_key),
                "peer", client_public, "allowed-ips", "10.203.0.2/32",
                "endpoint", "198.18.0.2:51821")
            run("wg", "set", "wgclient", "listen-port", "51821", "private-key", str(client_key),
                "peer", gateway_public, "allowed-ips", "10.80.0.0/24",
                "endpoint", "198.18.0.1:51820", "persistent-keepalive", "1", namespace=client)
        for interface, address, namespace in (
            ("wg0", "10.203.0.1/30", None), ("wgclient", "10.203.0.2/32", client)
        ):
            run("ip", "addr", "add", address, "dev", interface, namespace=namespace)
            run("ip", "link", "set", interface, "mtu", "1380", "up", namespace=namespace)
            details = json.loads(run("ip", "-j", "-d", "link", "show", interface,
                                     namespace=namespace))
            if details[0]["linkinfo"]["info_kind"] != "wireguard":
                raise AssertionError("test link is not WireGuard")
        for target in ("10.80.0.11/32", "10.80.0.12/32"):
            run("ip", "addr", "add", target, "dev", "lo", namespace=server)
        run("ip", "route", "add", "10.80.0.0/24", "dev", "wgclient", namespace=client)
        run("ip", "route", "add", "10.203.0.2/32", "dev", "wg0")
        run("ip", "route", "add", "10.80.0.0/24", "via", "192.0.2.2", "dev", "cnem_vnic")
        run("sysctl", "-q", "-w", "net.ipv4.ip_forward=1")
        run("iptables", "-N", "UNRELATED")
        run("iptables", "-A", "UNRELATED", "-j", "RETURN")
        unrelated = run("iptables", "-S", "UNRELATED")
        children.append(subprocess.Popen(
            ["ip", "netns", "exec", server, sys.executable, "-c", SERVER],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        # Readiness is a real service probe, not an arbitrary startup sleep.
        ready_deadline = min(deadline - 3, time.monotonic() + 2)
        while True:
            try:
                probe("10.80.0.11", 2222, namespace=server)
                break
            except AssertionError:
                if time.monotonic() >= ready_deadline:
                    raise
                time.sleep(0.03)
        probe("10.80.0.12", 8080, "http", namespace=server, timeout=1.2)

        subprocess.run = bounded_run
        gateway.block()
        probe("10.80.0.11", 2222, expected=False)
        config = dict(wireguard_client="10.203.0.2/32", vpn_interface="cnem_vnic",
                      allowed_cidrs=["10.80.0.11/32"])
        gateway.enable(config)
        probe("10.80.0.11", 2222, timeout=1.2)
        download = probe("10.80.0.11", 8080, "http", timeout=1.8)
        if download != {"bytes": BODY_BYTES, "sha256": BODY_SHA256}:
            raise AssertionError("HTTP download was not fully verified")
        probe("10.80.0.11", 9999, "udp")
        probe("10.80.0.12", 8080, "http", expected=False)
        gateway.enable(config)
        if run("iptables", "-S", "UNRELATED") != unrelated:
            raise AssertionError("unrelated firewall chain changed")
        if run("iptables", "-S", "FORWARD").count("-j UVPN_IN") != 1:
            raise AssertionError("repeated enable duplicated the hook")
        gateway.disable()
        probe("10.80.0.11", 8080, "http", expected=False)
        gateway.enable(config)
        probe("10.80.0.11", 2222)

        run("ip", "route", "replace", "10.80.0.0/24", "via", "198.51.100.2", "dev", "eth0")
        run("ip", "route", "add", "10.203.0.2/32", "via", "198.51.100.1",
            "dev", "plain0", namespace=server)
        if route_device("10.80.0.11") != "eth0" or route_device("10.203.0.2", server) != "plain0":
            raise AssertionError("ordinary fallback/return route was not installed")
        probe("10.80.0.11", 8080, "http", namespace=None, timeout=1.2)
        # Prove the fallback also works end-to-end through real WireGuard when
        # narrowly bypassing the interface restriction in THIS TEST namespace.
        # Remove both temporary rules before checking the genuine gateway guard.
        bypasses = (
            ("UVPN_IN", "-i", "wg0", "-s", "10.203.0.2/32", "-d", "10.80.0.11/32",
             "-o", "eth0", "-j", "ACCEPT"),
            ("UVPN_OUT", "-i", "eth0", "-s", "10.80.0.11/32", "-d", "10.203.0.2/32",
             "-o", "wg0", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"),
        )
        for chain, *rule in bypasses:
            run("iptables", "-I", chain, "1", *rule)
        try:
            probe("10.80.0.11", 2222)
            probe("10.80.0.11", 9999, "udp", source="10.203.0.2")
        finally:
            for chain, *rule in bypasses:
                run("iptables", "-D", chain, *rule)
        probe("10.80.0.11", 2222, expected=False)
        probe("10.80.0.11", 9999, "udp", expected=False)
        run("ip", "route", "replace", "10.80.0.0/24", "via", "192.0.2.2", "dev", "cnem_vnic")
        if route_device("10.80.0.11") != "cnem_vnic":
            raise AssertionError("VPN route did not recover")
        probe("10.80.0.11", 8080, "http", timeout=1.8)
        probe("10.80.0.11", 9999, "udp")
        for interface, namespace in (("wg0", None), ("wgclient", client)):
            handshake = run("wg", "show", interface, "latest-handshakes", namespace=namespace).split()
            transfer = run("wg", "show", interface, "transfer", namespace=namespace).split()
            if len(handshake) != 2 or int(handshake[1]) <= 0:
                raise AssertionError("real WireGuard handshake was not observed")
            if len(transfer) != 3 or max(int(transfer[1]), int(transfer[2])) < BODY_BYTES:
                raise AssertionError("real WireGuard transfer did not cover the download")
        print(json.dumps({
            "real_wireguard": True, "vpn_segment": "simulated_veth",
            "tcp_ssh": True, "http_bytes": download["bytes"], "http_sha256": download["sha256"],
            "udp_snat": True, "deny_unlisted_destination": True, "disable_blocks": True,
            "functional_fallback_without_guard": True, "route_fallback_blocked": True,
            "vpn_route_recovery": True, "preserve_unrelated_rules": True,
        }))
        return 0
    finally:
        subprocess.run = original_run
        for child in children:
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=0.5)
        for namespace in reversed(namespaces):
            subprocess.run(["ip", "netns", "del", namespace],
                           capture_output=True, timeout=0.5)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # Report no command arguments, config contents, subprocess output or keys.
        detail = str(error) if isinstance(error, (RuntimeError, AssertionError)) else type(error).__name__
        print("wireguard integration failed: " + detail, file=sys.stderr)
        raise SystemExit(1)
