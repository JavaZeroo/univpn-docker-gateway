"""Local root-only runtime control. Credentials never enter argv or files."""
import json
import errno
import socket
import time

CONTROL_PATH = "/run/univpn/control.sock"
MAX_CONTROL_MESSAGE = 65536


def send(command, *, timeout=180, path=CONTROL_PATH, **fields):
    data = json.dumps({"command": command, **fields}).encode() + b"\n"
    if len(data) > MAX_CONTROL_MESSAGE:
        raise ValueError("control message exceeds limit")
    ready_deadline = time.monotonic() + min(timeout, 15)
    while True:
        channel = socket.socket(socket.AF_UNIX)
        channel.settimeout(timeout)
        try:
            channel.connect(path)
            break
        except OSError as error:
            channel.close()
            # Nothing has been sent yet; waiting for boot cannot replay login.
            if error.errno not in (errno.ENOENT, errno.ECONNREFUSED) or time.monotonic() >= ready_deadline:
                raise RuntimeError("native runtime is not ready") from None
            time.sleep(0.1)
    with channel:
        channel.sendall(data)
        result = bytearray()
        while not result.endswith(b"\n"):
            chunk = channel.recv(4096)
            if not chunk:
                raise RuntimeError("runtime control connection closed")
            result.extend(chunk)
            if len(result) > MAX_CONTROL_MESSAGE:
                raise RuntimeError("runtime response exceeds limit")
    response = json.loads(result)
    if not isinstance(response, dict):
        raise RuntimeError("invalid runtime response")
    if response.get("control_error"):
        # Runtime sends only sanitized, purpose-written error messages.
        raise RuntimeError(response["control_error"])
    return response
