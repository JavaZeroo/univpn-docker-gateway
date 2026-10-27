"""Container-owned terminal MFA runtime, with no graphical dependencies."""
import json
import os
from pathlib import Path
import signal
import socketserver
import threading

from . import gateway
from .cli import probe
from .config import load
from .native_auth import NativeAuth
from .native_control import CONTROL_PATH, MAX_CONTROL_MESSAGE
from .native_engine import NativeEngine


def main():
    os.umask(0o077)
    config = load(os.environ.get("VPN_CONFIG", "/config/vpn.json"))
    Path("/run/univpn").mkdir(mode=0o700, parents=True, exist_ok=True)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    engine = None
    server = None
    worker = None
    try:
        gateway.block()
        engine = NativeEngine(config).start()
        auth = NativeAuth(engine, config, probe=probe)
        if Path("/config/wg0.conf").is_file():
            import subprocess
            path = Path("/run/univpn/wg0.conf")
            path.write_bytes(Path("/config/wg0.conf").read_bytes())
            subprocess.run(["wg-quick", "up", str(path)], check=True)

        def dispatch(request):
            command = request.get("command")
            if command == "status":
                status = auth.status()
                status["data_plane_ready"] = auth.health()
                return status
            if command == "login":
                return auth.login(request["username"], request["password"])
            if command == "sms":
                return auth.sms(request["attempt_id"], request["challenge_id"], request["code"])
            if command == "cancel":
                return auth.cancel(request["attempt_id"])
            raise ValueError("unknown control command")

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(100)
                try:
                    line = self.rfile.readline(MAX_CONTROL_MESSAGE + 1)
                    if len(line) > MAX_CONTROL_MESSAGE or not line.endswith(b"\n"):
                        raise ValueError("invalid control frame")
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise ValueError("control request must be an object")
                    result = dispatch(request)
                except Exception as error:
                    # Never serialize arbitrary exceptions, request data or tracebacks.
                    result = {"control_error": "operation failed: " + type(error).__name__,
                              "restart_required": True}
                try:
                    self.wfile.write(json.dumps(result).encode() + b"\n")
                except OSError:
                    pass  # A caller disconnect does not resend/replay its operation.

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
            block_on_close = False

            def handle_error(self, request, client_address):
                pass  # socketserver's default traceback may contain user input.

        socket_path = Path(CONTROL_PATH)
        if socket_path.exists():
            socket_path.unlink()
        server = Server(CONTROL_PATH, Handler)
        os.chmod(CONTROL_PATH, 0o600)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        print("Native runtime ready. Authenticate with: vpnctl login", flush=True)
        previous = False
        while not stop.wait(2):
            if not engine.alive:
                print("Native engine stopped; manual authentication is required after restart.", flush=True)
                break
            ready = auth.health()
            if ready != previous:
                gateway.enable(config) if ready else gateway.disable()
                print("VPN data probe ready" if ready else "VPN data probe failed; forwarding blocked",
                      flush=True)
                previous = ready
    finally:
        gateway.disable()
        if server is not None:
            server.shutdown()
            server.server_close()
        if engine is not None:
            engine.close()
        if worker is not None:
            worker.join(timeout=2)


if __name__ == "__main__":
    main()
