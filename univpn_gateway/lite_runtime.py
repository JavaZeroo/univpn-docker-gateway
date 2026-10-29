"""Keep a foreground Docker TTY for one login; never replay credentials."""
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .native_control import CONTROL_PATH, send


def _finish(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def run_interactive():
    """Service owns RPC/networking; vpnctl alone reads the real inherited TTY."""
    if not sys.stdin.isatty():
        raise RuntimeError("lite foreground login needs docker run -it")
    stopped = threading.Event()
    service = None
    login = None
    previous = {}

    def stop(signum, frame):
        stopped.set()
        for process in (login, service):
            if process is not None and process.poll() is None:
                process.terminate()

    numbers = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        numbers.append(signal.SIGHUP)
    for number in numbers:
        previous[number] = signal.signal(number, stop)
    try:
        service = subprocess.Popen([sys.executable, "-m", "univpn_gateway.native_service"],
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 30
        while not Path(CONTROL_PATH).is_socket():
            if stopped.wait(.1):
                return 130
            if service.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("lite authentication service failed to start")
        # No credential is an argument or environment variable. The child owns
        # input/getpass on the same terminal as docker run -it.
        login = subprocess.Popen(["/usr/local/bin/vpnctl", "login"])
        while login.poll() is None:
            if stopped.wait(.1):
                return 130
            if service.poll() is not None:
                raise RuntimeError("lite service stopped during authentication")
        if login.returncode:
            return login.returncode
        deadline = time.monotonic() + 40
        while not stopped.is_set() and service.poll() is None:
            state = send("status", timeout=2)
            if state.get("data_plane_ready"):
                print("VPN target probe passed. Configure your app to use the published SOCKS5 TCP port.", flush=True)
                break
            if time.monotonic() >= deadline:
                print("VPN target probe is not ready; SOCKS requests remain blocked. Inspect with vpnctl status.", flush=True)
                break
            stopped.wait(.5)
        print("Lite runs in this terminal. Ctrl+C stops it; no authentication is retried.", flush=True)
        while service.poll() is None and not stopped.wait(.5):
            pass
        return 130 if stopped.is_set() else (service.returncode or 0)
    finally:
        _finish(login)
        _finish(service)
        for number, handler in previous.items():
            signal.signal(number, handler)
