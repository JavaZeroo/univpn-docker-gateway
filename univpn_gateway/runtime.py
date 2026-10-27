"""Supervise the official client without initiating authentication."""
import os
from pathlib import Path
import signal
import subprocess
import time

from .config import load
from . import gateway
from .cli import probe


def profile(config):
    directory = Path("/root/UniVPN")
    (directory / "config").mkdir(parents=True, exist_ok=True)
    (directory / "config/GATEWAY.ini").write_text(
        "[GLOBAL]\niConnectionType = 1\nDescription =\n"
        "sign_certificate =\nencryp_certificate =\n"
        f"GatewayAddress = {config['gateway']}\nGatewayPort = {config['port']}\n"
        "TunnelMode = 2\nPreflinkEnable = 0\nDefaultGateway = -1\n"
        "iroutecoverEnable = 1\nigmalgorithmEnable = 0\nPreflinkTotal = 0\n")
    (directory / "sysconfig.ini").write_text(
        "[GLOBAL]\nClientName = UniVPN\nClientCustomized = false\n"
        "[ADVANCED]\nClientDetectLatestVersion = 0\nClientAutoBoot = 0\n"
        "ClientLanguageID = 1033\nClientServerCheck = 1\nClientShowLogFlag = 0\n"
        "[PROXY]\nProxyType = 0\n[Session0]\nConnectType = 1\nRemPwd = 0\n"
        "AutoLogin = 0\nProfileName = GATEWAY.ini\nProfileUser =\nProfileInfo =\n")


def main():
    os.umask(0o077)
    config = load(os.environ.get("VPN_CONFIG", "/config/vpn.json"))
    Path("/run/univpn").mkdir(mode=0o700, exist_ok=True)
    Path("/run/user/0").mkdir(mode=0o700, parents=True, exist_ok=True)
    profile(config)
    children = []
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        gateway.block()
        # No desktop, window manager, VNC, web server or browser.
        commands = [
            ["dbus-daemon", "--session", "--nofork", "--address=unix:path=/run/univpn/bus"],
            ["Xvfb", ":1", "-screen", "0", "1024x768x24", "-nolisten", "tcp"],
            ["/usr/local/UniVPN/promote/UniVPNPromoteService"],
        ]
        for command in commands:
            children.append(subprocess.Popen(command, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL))
        time.sleep(2)
        children.append(subprocess.Popen(["/usr/local/UniVPN/UniVPN"],
                                         cwd="/usr/local/UniVPN", stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL))
        if Path("/config/wg0.conf").is_file():
            # wg-quick requires the .conf basename to match the interface.
            wgconfig = Path("/run/univpn/wg0.conf")
            wgconfig.write_bytes(Path("/config/wg0.conf").read_bytes())
            subprocess.run(["wg-quick", "up", str(wgconfig)], check=True)
        previous = False
        print("Runtime started. Authentication is manual: docker compose exec vpn vpnctl login",
              flush=True)
        while running:
            if any(p.poll() is not None for p in children):
                raise RuntimeError("a required client/display process exited")
            ready = probe(config)
            if ready != previous:
                gateway.enable(config) if ready else gateway.disable()
                print("Target probe ready" if ready else "Target probe failed; forwarding blocked",
                      flush=True)
                previous = ready
            time.sleep(2)
    finally:
        try:
            gateway.disable()
        finally:
            subprocess.run(["wg-quick", "down", "/run/univpn/wg0.conf"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for child in reversed(children):
                child.terminate()
            for child in reversed(children):
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == "__main__":
    main()
