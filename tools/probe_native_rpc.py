"""Credential-free bootstrap probe for one explicitly verified vendor version.

Run ONLY in a fresh disposable container. This is an independently written
local IPC experiment; it neither implements SMS authentication nor modifies
the vendor executable. No decompiled source or vendor binary is distributed.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import select
import socket
import struct
import subprocess
import time

CLIENT = Path("/usr/local/UniVPN/serviceclient/UniVPNCS")
CLIENT_SHA256 = "f26948ea1d3892f31a0692d4f771dcbc40365bfb2a39a509b9f3174d323781c4"


def announcement(source, destination, application, endpoint):
    # Observed startup message only, not a general RPC protocol implementation.
    return struct.pack("!IIIIHHII", source, destination, 2, 1, 1, 8,
                       application, endpoint)


def main():
    if not Path("/.dockerenv").exists() or os.geteuid() != 0:
        raise RuntimeError("run in a fresh disposable root container")
    if hashlib.sha256(CLIENT.read_bytes()).hexdigest() != CLIENT_SHA256:
        raise RuntimeError("unverified client binary: this probe only covers 10781.21.0.0831")
    home = Path("/root/UniVPN")
    (home / "config").mkdir(parents=True, exist_ok=True)
    config = home / "sysconfig.ini"
    if config.exists():
        raise RuntimeError("existing client config found; use a fresh container without user mounts")
    os.umask(0o077)
    config.write_text("[GLOBAL]\nClientName=UniVPN\n[ADVANCED]\nClientLogLevel=1\n")
    helper = subprocess.Popen(["/usr/local/UniVPN/promote/UniVPNPromoteService"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    engine = None
    channel = None
    try:
        logfile = Path("/usr/local/UniVPN/log/UniVPN_UniVPNPromoteService_0.log")
        endpoint = None
        for _ in range(50):
            if logfile.exists():
                match = re.search(r"RPC Listen port (\d+)", logfile.read_text(errors="replace"))
                if match and Path("/tmp/" + match[1]).exists():
                    endpoint = int(match[1])
                    break
            time.sleep(0.1)
        if endpoint is None:
            raise RuntimeError("helper RPC socket did not become ready")
        # The endpoints are AF_UNIX paths despite being called ports by the vendor.
        channel = socket.socket(socket.AF_UNIX)
        channel.connect("/tmp/" + str(endpoint))
        channel.sendall(announcement(2, 1, 2, 19070))
        arguments = ("UserPath=/root,LoginUser=root,InstallPath=/usr/local/UniVPN,"
                     f"RemotePort={endpoint}")
        engine = subprocess.Popen([str(CLIENT), arguments], cwd=str(CLIENT.parent),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        data = b""
        expected = announcement(1, 2, 4, 19060)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and len(data) < 65536:
            if select.select([channel], [], [], 0.2)[0]:
                part = channel.recv(4096)
                if not part:
                    break
                data += part
                if expected in data:
                    break
        result = {
            "client_version": "10781.21.0.0831",
            "native_engine_alive": engine.poll() is None,
            "python_frontend_received_engine_announcement": expected in data,
            "startup_bytes_received": len(data),
            "gui_started": False,
            "credentials_submitted": False,
            "sms_authentication_implemented": False,
        }
        print(json.dumps(result))
        return 0 if result["native_engine_alive"] and expected in data else 1
    finally:
        if channel is not None:
            channel.close()
        for process in (engine, helper):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        # The helper daemon can fork; the disposable container's PID namespace
        # is the cleanup boundary. Never execute this probe in a running VPN.


if __name__ == "__main__":
    raise SystemExit(main())
