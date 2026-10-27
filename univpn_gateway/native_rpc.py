"""Bounded transport for the observed native CSDK control protocol.

The six-word control header is distinct from helper bootstrap announcements.
This module does not infer authentication success or replay credentials.
"""
from dataclasses import dataclass, field
import socket
import struct
import time

CONTROL_HEADER = struct.Struct("!6I")
MAX_PAYLOAD = 1024 * 1024


class ProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class Message:
    source: int
    destination: int
    connection_type: int
    message_class: int
    opcode: int
    payload: bytes = field(repr=False)

    def encode(self):
        if len(self.payload) > MAX_PAYLOAD:
            raise ProtocolError("payload too large")
        return CONTROL_HEADER.pack(self.source, self.destination, self.connection_type,
                                   self.message_class, self.opcode, len(self.payload)) + self.payload


def _remaining(deadline):
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise socket.timeout("control channel deadline exceeded")
    return remaining


def _apply_deadline(channel, deadline):
    if deadline is not None:
        channel.settimeout(_remaining(deadline))


def receive_exact(channel, size, deadline=None):
    """Read exactly size bytes within an optional absolute monotonic deadline.

    A deadline bounds the complete read, including fragmented messages. Restore
    the persistent socket's timeout policy afterward. A partially read frame
    cannot be resumed after an error; its caller must discard that channel.
    """
    gettimeout = getattr(channel, "gettimeout", None)
    restore_timeout = deadline is not None and callable(gettimeout)
    original_timeout = gettimeout() if restore_timeout else None
    try:
        _remaining(deadline)
        chunks = bytearray()
        while len(chunks) < size:
            _apply_deadline(channel, deadline)
            data = channel.recv(size - len(chunks))
            _remaining(deadline)
            if not data:
                raise ProtocolError("control channel closed during a message")
            chunks.extend(data)
        return bytes(chunks)
    finally:
        if restore_timeout:
            channel.settimeout(original_timeout)


def receive(channel, deadline=None):
    header = CONTROL_HEADER.unpack(receive_exact(channel, CONTROL_HEADER.size, deadline))
    if header[-1] > MAX_PAYLOAD:
        raise ProtocolError("peer announced an oversized payload")
    return Message(*header[:-1], receive_exact(channel, header[-1], deadline))


def request(endpoint, opcode, payload=b"", *, message_class=3, capacity=None, timeout=10):
    if capacity is not None:
        if not len(payload) <= capacity <= MAX_PAYLOAD:
            raise ProtocolError("invalid payload capacity")
        payload = payload.ljust(capacity, b"\0")
    message = Message(2, 4, 1, message_class, opcode, payload)
    if timeout is not None and timeout < 0:
        raise ValueError("timeout must be nonnegative")
    deadline = None if timeout is None else time.monotonic() + timeout
    with socket.socket(socket.AF_UNIX) as channel:
        if deadline is None:
            channel.settimeout(None)
        else:
            _apply_deadline(channel, deadline)
        channel.connect(endpoint)
        _apply_deadline(channel, deadline)
        channel.sendall(Message(2, 4, 1, 1, 0x10006, b"").encode())
        _apply_deadline(channel, deadline)
        channel.sendall(message.encode())
        response = receive(channel, deadline)
        if response.source != 4 or response.destination != 2:
            raise ProtocolError("unexpected response routing")
        if response.connection_type != 1:
            raise ProtocolError("unexpected response connection type")
        return response
