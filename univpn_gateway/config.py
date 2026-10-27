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
    data.update(port=port, vpn_interface=interface,
                allowed_cidrs=[str(n) for n in networks], wireguard_client=str(client))
    return data
