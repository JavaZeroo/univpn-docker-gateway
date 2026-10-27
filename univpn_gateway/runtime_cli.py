"""Run only the vendor native CLI; do not emulate MFA through a hidden GUI."""
import os
from pathlib import Path
import signal
import subprocess
import threading

from .config import load
from .cli import probe
from . import gateway
from .profile import profile


def main():
    os.umask(0o077)
    config = load(os.environ.get("VPN_CONFIG", "/config/vpn.json"))
    Path("/run/univpn").mkdir(mode=0o700, exist_ok=True)
    profile(config)
    done = threading.Event()
    children = []

    def stop(*_):
        done.set()
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()

    def guard():
        previous = False
        while not done.is_set():
            try:
                ready = probe(config)
                if ready != previous:
                    gateway.enable(config) if ready else gateway.disable()
                    previous = ready
            except Exception:
                # A failed guard cannot leave forwarding enabled.
                stop()
                break
            done.wait(2)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        gateway.block()
        children.append(subprocess.Popen(["/usr/local/UniVPN/promote/UniVPNPromoteService"],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        if Path("/config/wg0.conf").is_file():
            wgconfig = Path("/run/univpn/wg0.conf")
            wgconfig.write_bytes(Path("/config/wg0.conf").read_bytes())
            subprocess.run(["wg-quick", "up", str(wgconfig)], check=True)
        print("Native UniVPNCS: password-only authentication. SMS MFA is unsupported.", flush=True)
        print("Attach with docker compose attach vpn. Detach with Ctrl-P Ctrl-Q.", flush=True)
        threading.Thread(target=guard, daemon=True).start()
        if done.wait(1):
            raise RuntimeError("gateway readiness guard failed")
        client = subprocess.Popen(["/usr/local/UniVPN/serviceclient/UniVPNCS"],
                                  cwd="/usr/local/UniVPN/serviceclient")
        children.append(client)
        client.wait()
    finally:
        done.set()
        try:
            gateway.disable()
        finally:
            subprocess.run(["wg-quick", "down", "/run/univpn/wg0.conf"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            stop()
            for child in reversed(children):
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == "__main__":
    main()
