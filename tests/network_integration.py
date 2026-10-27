"""Real TCP/HTTP/UDP and route-fallback tests in isolated Linux namespaces.

Run as root on a disposable Linux test host, not in a production VPN namespace.
Uses veth links in place of WireGuard/UniVPN: proves forwarding rules, not either
tunnel protocol or vendor authentication.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

if os.environ.get("UVPN_ISOLATED_TEST") != "1":
    env = dict(os.environ, UVPN_ISOLATED_TEST="1")
    raise SystemExit(subprocess.call(["unshare", "--net", sys.executable, __file__], env=env))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from univpn_gateway import gateway


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


suffix = str(os.getpid())
client = "uvpn-client-" + suffix
server = "uvpn-server-" + suffix
children = []

SERVER = r'''
import socket, threading, time
def tcp(host, port, reply):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port)); s.listen()
    while True:
        c, peer = s.accept()
        with c: c.sendall(reply)
def udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(('10.80.0.11', 9999))
    while True:
        data, peer = s.recvfrom(4096)
        s.sendto(peer[0].encode() + b':' + data, peer)
for host, port, reply in [('10.80.0.11',2222,b'SSH-2.0-integration\r\n'),
                          ('10.80.0.11',8080,b'HTTP/1.0 200 OK\r\n\r\nworks'),
                          ('10.80.0.12',8080,b'forbidden')]:
    threading.Thread(target=tcp,args=(host,port,reply),daemon=True).start()
threading.Thread(target=udp,daemon=True).start()
while True: time.sleep(1)
'''
PROBE = r'''
import socket, sys
host, port, kind = sys.argv[1], int(sys.argv[2]), sys.argv[3]
try:
    if kind == 'udp':
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(1)
        s.sendto(b'hello', (host,port)); data = s.recv(1024)
    else:
        s = socket.create_connection((host,port), timeout=1); data = s.recv(1024)
    print(data.decode().strip())
except OSError: sys.exit(1)
'''


def probe(host, port, kind="tcp", namespace=client, expected=True):
    result = subprocess.run(["ip", "netns", "exec", namespace, sys.executable,
                             "-c", PROBE, host, str(port), kind], capture_output=True, text=True)
    assert (result.returncode == 0) == expected, (host, port, result.stdout, result.stderr)
    return result.stdout


try:
    for namespace in (client, server):
        run("ip", "netns", "add", namespace)
        run("ip", "-n", namespace, "link", "set", "lo", "up")
    run("ip", "link", "set", "lo", "up")
    for name, peer, namespace, local_ip, remote_ip in (
        ("wg0", "client0", client, "10.203.0.1/30", "10.203.0.2/30"),
        ("cnem_vnic", "vpn0", server, "192.0.2.1/30", "192.0.2.2/30"),
        ("eth0", "plain0", server, "198.51.100.1/30", "198.51.100.2/30"),
    ):
        run("ip", "link", "add", name, "type", "veth", "peer", "name", peer)
        run("ip", "link", "set", peer, "netns", namespace)
        run("ip", "addr", "add", local_ip, "dev", name)
        run("ip", "link", "set", name, "up")
        run("ip", "-n", namespace, "addr", "add", remote_ip, "dev", peer)
        run("ip", "-n", namespace, "link", "set", peer, "up")
    for target in ("10.80.0.11/32", "10.80.0.12/32"):
        run("ip", "-n", server, "addr", "add", target, "dev", "lo")
    run("ip", "-n", client, "route", "add", "10.80.0.0/24", "via", "10.203.0.1")
    run("ip", "route", "add", "10.80.0.0/24", "via", "192.0.2.2", "dev", "cnem_vnic")
    # No return route to client: successful requests initially require real SNAT.
    run("sysctl", "-w", "net.ipv4.ip_forward=1")
    run("iptables", "-N", "UNRELATED")
    run("iptables", "-A", "UNRELATED", "-j", "RETURN")
    unrelated_before = run("iptables", "-S", "UNRELATED")
    children.append(subprocess.Popen(["ip", "netns", "exec", server,
                                      sys.executable, "-c", SERVER]))
    time.sleep(1)
    # Prove target services exist before testing the filter's refusal.
    probe("10.80.0.11", 2222, namespace=server)
    probe("10.80.0.12", 8080, namespace=server)
    gateway.block()
    probe("10.80.0.11", 2222, expected=False)
    config = dict(wireguard_client="10.203.0.2/32", vpn_interface="cnem_vnic",
                  allowed_cidrs=["10.80.0.11/32"])
    gateway.enable(config)
    assert "SSH-2.0-" in probe("10.80.0.11", 2222)
    assert "200 OK" in probe("10.80.0.11", 8080)
    assert "192.0.2.1:hello" in probe("10.80.0.11", 9999, "udp")
    probe("10.80.0.12", 8080, expected=False)
    # Duplicate calls must not duplicate hooks or erase unrelated chains.
    gateway.enable(config)
    assert run("iptables", "-S", "UNRELATED") == unrelated_before
    assert run("iptables", "-S", "FORWARD").count("-j UVPN_IN") == 1
    gateway.disable()
    probe("10.80.0.11", 8080, expected=False)
    gateway.enable(config)
    probe("10.80.0.11", 8080)
    # Create a functioning fallback path; verify it works from the gateway.
    run("ip", "route", "replace", "10.80.0.0/24", "via", "198.51.100.2", "dev", "eth0")
    run("ip", "-n", server, "route", "add", "10.203.0.0/30", "via", "198.51.100.1")
    subprocess.run([sys.executable, "-c", PROBE, "10.80.0.11", "8080", "tcp"], check=True)
    # Leave filter's allow rules installed: interface restriction alone must block fallback.
    probe("10.80.0.11", 8080, expected=False)
    probe("10.80.0.11", 9999, "udp", expected=False)
    print(json.dumps({"tcp_ssh": True, "http": True, "udp_snat": True,
                      "deny_unlisted_destination": True, "disable_blocks": True,
                      "route_fallback_blocked": True, "preserve_unrelated_rules": True}))
finally:
    for child in children:
        child.terminate()
        child.wait(timeout=3)
    for namespace in (client, server):
        subprocess.run(["ip", "netns", "del", namespace], capture_output=True)
