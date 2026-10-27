"""Own only named chains. Block all other forwarded WireGuard traffic."""
import subprocess


def run(*args, check=True):
    return subprocess.run(args, check=check, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True)


def ipt(*args, check=True):
    return run("iptables", "-w", "5", *args, check=check)


def chain(table, name):
    ipt("-t", table, "-N", name, check=False)


def jump(table, base, name, *match):
    args = ("-t", table, "-C", base, *match, "-j", name)
    if ipt(*args, check=False).returncode:
        ipt("-t", table, "-I", base, "1", *match, "-j", name)


def block():
    # Hooks are installed before WireGuard comes up and remain on VPN failure.
    chain("filter", "UVPN_IN")
    chain("filter", "UVPN_OUT")
    subprocess.run(["iptables-restore", "--wait", "5", "--noflush"], input=(
        "*filter\n-F UVPN_IN\n-F UVPN_OUT\n"
        "-A UVPN_IN -j DROP\n-A UVPN_OUT -j DROP\nCOMMIT\n"),
        text=True, check=True)
    jump("filter", "FORWARD", "UVPN_IN", "-i", "wg0")
    jump("filter", "FORWARD", "UVPN_OUT", "-o", "wg0")


def enable(config):
    block()
    # Replace only our chains in a single filter-table COMMIT. --noflush
    # preserves unrelated rules and the fail-closed hooks above.
    lines = ["*filter", "-F UVPN_IN", "-F UVPN_OUT"]
    src = config["wireguard_client"]
    dev = config["vpn_interface"]
    for network in config["allowed_cidrs"]:
        lines += [
            f"-A UVPN_IN -s {src} -d {network} -o {dev} -j ACCEPT",
            f"-A UVPN_OUT -i {dev} -s {network} -d {src} "
            "-m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
        ]
    lines += ["-A UVPN_IN -j DROP", "-A UVPN_OUT -j DROP", "COMMIT", ""]
    subprocess.run(["iptables-restore", "--wait", "5", "--noflush"],
                   input="\n".join(lines), text=True, check=True)
    chain("nat", "UVPN_NAT")
    # NAT rule replacement can fail closed but never changes the filter allowlist.
    ipt("-t", "nat", "-F", "UVPN_NAT")
    for network in config["allowed_cidrs"]:
        ipt("-t", "nat", "-A", "UVPN_NAT", "-s", src, "-d", network,
            "-o", dev, "-j", "MASQUERADE")
    jump("nat", "POSTROUTING", "UVPN_NAT")


def disable():
    block()
