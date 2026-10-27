import ipaddress
import json
import re
from pathlib import Path


def load(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    gateway = data["gateway"]
    if not re.fullmatch(r"[a-zA-Z0-9.-]+", gateway):
        raise ValueError("gateway must be an IPv4 address or DNS name")
    port = data.get("port", 443)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    interface = data.get("vpn_interface", "cnem_vnic")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,15}", interface):
        raise ValueError("invalid vpn_interface")
    networks = [ipaddress.IPv4Network(n, strict=True) for n in data["allowed_cidrs"]]
    if not networks or any(n.prefixlen == 0 for n in networks):
        raise ValueError("explicit non-default IPv4 destinations are required")
    client = ipaddress.IPv4Interface(data["wireguard_client"])
    if client.network.prefixlen != 32:
        raise ValueError("wireguard_client must be a single /32 address")
    if any(client.ip in n for n in networks):
        raise ValueError("WireGuard client address overlaps target destinations")
    probe = data.get("probe")
    if probe:
        host = ipaddress.IPv4Address(probe["host"])
        if not any(host in n for n in networks):
            raise ValueError("probe must be within allowed_cidrs")
        if type(probe.get("port")) is not int or not 1 <= probe["port"] <= 65535:
            raise ValueError("invalid probe port")
        if not probe.get("banner_prefix"):
            raise ValueError("probe needs a real banner_prefix, e.g. SSH-2.0-")
    pin = data.get("approved_certificate_sha256")
    if pin is not None and (not isinstance(pin, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", pin)):
        raise ValueError("approved_certificate_sha256 must contain 64 hexadecimal characters")
    for key, default in (("tunnel_mode", 2), ("gm_algorithm", 0), ("route_cover", 1)):
        value = data.get(key, default)
        choices = range(1, 4) if key == "tunnel_mode" else (0, 1)
        if type(value) is not int or value not in choices:
            raise ValueError("invalid " + key)
        data[key] = value
    data.update(port=port, vpn_interface=interface,
                allowed_cidrs=[str(n) for n in networks], wireguard_client=str(client))
    return data
