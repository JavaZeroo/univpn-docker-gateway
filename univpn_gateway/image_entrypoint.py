"""Public image commands; vendor files are obtained only on the user's machine."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys


CLIENT_FILES = ("univpn.cmd", "univpn.ps1", "windows-profile.ps1", "windows-gateway.ps1", "windows-prerequisites.ps1")


def export_client(destination, source=Path("/opt/gateway/client")):
    """Export a fresh directory, never merge into an existing installation."""
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError("destination already exists; use the existing launcher or a new directory")
    source = Path(source)
    if any(not (source / name).is_file() for name in CLIENT_FILES):
        raise ValueError("image client files are incomplete")
    # Exported files are public launchers. Native Linux bind mounts retain the
    # container's root ownership, so the host user must be able to read them.
    # The profile generator restricts its separate private directory before keys.
    destination.mkdir(mode=0o755, parents=False)
    try:
        for name in CLIENT_FILES:
            shutil.copyfile(source / name, destination / name)
    except BaseException:
        # Only this function's fresh directory can be removed on failed export.
        shutil.rmtree(destination)
        raise
    return {"directory": str(destination), "next": ".\\univpn\\univpn.cmd"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="UniVPN Docker Gateway ready-to-run image")
    parser.add_argument("command", choices=("run", "export-client", "prepare"), nargs="?", default="run")
    parser.add_argument("--destination", default="/output/univpn")
    args = parser.parse_args(argv)
    try:
        if args.command == "export-client":
            print(json.dumps(export_client(args.destination)))
            return 0
        if args.command == "run":
            from .config import load
            load(os.environ.get("VPN_CONFIG", "/config/vpn.json"))
        from .vendor_runtime import prepare_vendor
        print("Preparing pinned official UniVPN components (cached after first download).", flush=True)
        result = prepare_vendor()
        print(json.dumps(result), flush=True)
        if args.command == "prepare":
            return 0
        from .native_service import main as run_service
        run_service()
        return 0
    except Exception as error:
        # Startup has no credentials. Still avoid arbitrary network exception text.
        if args.command == "export-client":
            print("Client export failed. Use a mounted /output directory and a fresh destination; existing files are never overwritten.", file=sys.stderr)
            return 1
        from .vendor_runtime import VendorRuntimeError
        if isinstance(error, VendorRuntimeError):
            print("Official component preparation failed: " + str(error), file=sys.stderr)
            return 1
        print("Gateway startup failed: " + type(error).__name__ + ". Check configuration, official download access and cache integrity.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
