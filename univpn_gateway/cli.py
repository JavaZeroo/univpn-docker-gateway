import argparse
import getpass
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

from .config import load


def probe(config):
    target = config.get("probe")
    if not target:
        return False
    dev = config["vpn_interface"]
    if not Path(f"/sys/class/net/{dev}").exists():
        return False
    route = subprocess.run(["ip", "-j", "route", "get", target["host"]],
                           capture_output=True, text=True, check=True)
    if not any(r.get("dev") == dev for r in json.loads(route.stdout)):
        return False
    try:
        with socket.create_connection((target["host"], target["port"]), timeout=2) as conn:
            return conn.recv(256).startswith(target["banner_prefix"].encode())
    except OSError:
        return False


def main():
    parser = argparse.ArgumentParser(description="Experimental UniVPN container control")
    parser.add_argument("--config", default=os.environ.get("VPN_CONFIG", "/config/vpn.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("inspect-ui", help="local widget names/roles only; never read text values")
    login_command = sub.add_parser("login", help="one interactive login; no automatic retries/SMS resend")
    login_command.add_argument("--no-otp", action="store_true", help="return after the SMS challenge; use vpnctl sms later")
    sub.add_parser("sms", help="submit a code to the existing native authentication session")
    sub.add_parser("cancel", help="cancel the current pending native authentication attempt")
    route = sub.add_parser("gateway")
    route.add_argument("action", choices=["enable", "disable"])
    args = parser.parse_args()
    try:
        config = load(args.config)
        if os.environ.get("VPN_BACKEND") == "native":
            from .native_control import send
            if args.command == "status":
                result = send("status")
                print(json.dumps(result))
                return 0 if result.get("data_plane_ready") else 1
            if args.command in ("login", "sms", "cancel"):
                if args.command != "cancel" and not sys.stdin.isatty():
                    raise ValueError("interactive credentials require a TTY")
                if args.command == "login":
                    username = input("VPN username: ")
                    password = getpass.getpass("VPN password: ")
                    result = send("login", username=username, password=password)
                    del password
                    if result.get("state") != "awaiting_sms" or args.no_otp:
                        print(json.dumps(result))
                        return 0 if result.get("state") in ("awaiting_sms", "authenticated") else 1
                    print("SMS requested. Keep this session; enter the latest code below.")
                else:
                    result = send("status")
                if args.command == "cancel":
                    print(json.dumps(send("cancel", attempt_id=result["attempt_id"])))
                    return 0
                if result.get("state") != "awaiting_sms":
                    raise ValueError("no active SMS challenge; explicit restart/login may be required")
                code = getpass.getpass("SMS code (hidden): ")
                result = send("sms", attempt_id=result["attempt_id"],
                              challenge_id=result["challenge_id"], code=code)
                del code
                print(json.dumps(result))
                return 0 if result.get("authenticated") else 1
        if args.command == "status":
            ok = probe(config)
            print(json.dumps({"ready": ok, "check": "VPN route + target banner"}))
            return 0 if ok else 1
        if args.command == "gateway":
            from . import gateway
            if args.action == "enable":
                if not probe(config):
                    raise ValueError("target probe failed; forwarding remains blocked")
                gateway.enable(config)
            else:
                gateway.disable()
            return 0
        if os.environ.get("VPN_BACKEND") == "cli":
            raise ValueError("native CLI backend: use docker compose attach vpn; SMS is unsupported by the vendor CLI")
        from .accessibility import AccessibleUI
        ui = AccessibleUI()
        if args.command == "inspect-ui":
            print(json.dumps(ui.inspect(), ensure_ascii=False, indent=2))
            return 0
        from .auth import login
        if not sys.stdin.isatty():
            raise ValueError("login requires an interactive TTY; use docker compose exec without -T")
        if not config.get("probe"):
            raise ValueError("configure a real banner probe before login")
        mapping = json.loads(Path(config["ui_mapping"]).read_text())
        import fcntl
        with open("/run/univpn/login.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            username = input("VPN username: ")
            password = getpass.getpass("VPN password: ")
            print("Submitting one login. Any SMS code will be entered in this session.")
            result = login(ui, mapping, username, password,
                           lambda: getpass.getpass("Latest SMS code (hidden): "),
                           lambda: probe(config))
            print(result)
        return 0
    except (ValueError, OSError, RuntimeError, KeyError, subprocess.CalledProcessError) as error:
        # Don't print subprocess output or UI text; they may contain credentials.
        print(f"vpnctl: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
