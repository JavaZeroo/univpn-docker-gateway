"""Independently encoded native ABI messages for the pinned amd64 client.

Login and site payload fields are little endian. The transport header, tunnel
start payload, and login response values are big endian. These layouts must not
be silently reused for another vendor release/architecture.
"""
import re
import struct

AUTH_OK = 0x00020000
SMS_REQUIRED = 0x0003000A
SYSTEM_CONFIG_OK = 0x000D0002
TUNNEL_START_OK = 0x00070000
LOGIN_RESPONSE_SIZE = 32


def fixed_text(value, capacity, label):
    if not isinstance(value, str) or "\0" in value:
        raise ValueError(label + " must be NUL-free text")
    encoded = value.encode("utf-8")
    if len(encoded) >= capacity:
        raise ValueError(label + " exceeds the native field capacity")
    return encoded.ljust(capacity, b"\0")


def endpoint(gateway, port):
    if not re.fullmatch(r"[A-Za-z0-9.-]+", gateway):
        raise ValueError("gateway must be an IPv4 address or DNS name")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return f"{gateway}:{port}"


def first_connect(gateway, port):
    address = endpoint(gateway, port)
    payload = bytearray(0x9D0)
    struct.pack_into("<I", payload, 0, 1)  # English UI locale, no UI process.
    payload[4:0x104] = fixed_text(address, 0x100, "endpoint")
    payload[0x108:0x188] = fixed_text(gateway, 0x80, "gateway")
    struct.pack_into("<I", payload, 0x188, port)
    struct.pack_into("<I", payload, 0x9C8, 1)  # One explicitly configured site.
    struct.pack_into("<i", payload, 0x9CC, -1)  # Default gateway index.
    return bytes(payload)


def password_login(gateway, port, username, password, *, verify_gateway_certificate=True):
    if type(verify_gateway_certificate) is not bool:
        raise ValueError("verify_gateway_certificate must be a boolean")
    payload = bytearray(0x7BC)
    # Certificate verification is enabled unless the caller explicitly accepts
    # the current gateway certificate. Normal auth, no remembered password.
    struct.pack_into("<5I", payload, 0, int(verify_gateway_certificate), 0, 0, 1, 1)
    payload[0x414:0x514] = fixed_text(endpoint(gateway, port), 0x100, "endpoint")
    payload[0x514:0x694] = fixed_text(username, 0x180, "username")
    payload[0x694:0x714] = fixed_text(password, 0x80, "password")
    return bytes(payload)


def sms_code(code):
    if not isinstance(code, str) or not code.isascii() or not code.isdigit() or not 4 <= len(code) <= 10:
        raise ValueError("SMS code must contain 4 to 10 ASCII digits")
    return b"\0" * 4 + fixed_text(code, 0x80, "SMS code")


def tunnel_start(tunnel_mode, gm_algorithm, route_cover):
    if type(tunnel_mode) is not int or not 0 <= tunnel_mode <= 0xFFFFFFFF:
        raise ValueError("tunnel_mode must be an unsigned 32-bit integer")
    for value, label in ((gm_algorithm, "gm_algorithm"), (route_cover, "route_cover")):
        if type(value) is not int or value not in (0, 1):
            raise ValueError(label + " must be 0 or 1")
    return struct.pack("!III", tunnel_mode, gm_algorithm, route_cover)


def decode_login_response(payload):
    """Return the response's seven u32 and two u16 values without field guesses."""
    if not isinstance(payload, (bytes, bytearray)) or len(payload) != LOGIN_RESPONSE_SIZE:
        raise ValueError("login response must contain exactly 32 bytes")
    return struct.unpack("!7I2H", payload)
