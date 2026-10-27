# syntax=docker/dockerfile:1
FROM ubuntu:22.04 AS install
ARG UNIVPN_SHA256
COPY vendor/univpn.run /tmp/univpn.run
RUN test -n "$UNIVPN_SHA256" \
    && echo "$UNIVPN_SHA256  /tmp/univpn.run" | sha256sum -c - \
    && chmod +x /tmp/univpn.run \
    && (/tmp/univpn.run S > /tmp/install.log 2>&1; \
        test -x /usr/local/UniVPN/UniVPN || { cat /tmp/install.log; exit 1; })

FROM ubuntu:22.04 AS gui-experimental
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pyatspi at-spi2-core dbus-x11 xvfb \
    libgl1 libegl1 libxcb-xinerama0 libxcb-cursor0 libxkbcommon-x11-0 \
    libnss3 libasound2 libdbus-1-3 libxcb-icccm4 libxcb-keysyms1 libxcb-shape0 \
    libqt5gui5 libqt5widgets5 libqt5dbus5 \
    iproute2 iptables wireguard-tools ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*
COPY --from=install /usr/local/UniVPN /usr/local/UniVPN
COPY --from=install /lib/libgmcrypto.so /lib/libgmcrypto.so
COPY univpn_gateway /opt/gateway/univpn_gateway
COPY scripts/vpnctl /usr/local/bin/vpnctl
RUN chmod 755 /usr/local/bin/vpnctl
ENV USER=root HOME=/root DISPLAY=:1 QT_X11_NO_MITSHM=1 \
    VPN_BACKEND=gui \
    QT_ACCESSIBILITY=1 QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1 \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/univpn/bus \
    XDG_RUNTIME_DIR=/run/user/0 PYTHONPATH=/opt/gateway \
    LD_LIBRARY_PATH=/usr/local/UniVPN/lib
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD vpnctl status
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/bin/python3", "-m", "univpn_gateway.runtime"]

# Default build: official native CLI only, with no Qt/X11/DBus/VNC packages.
FROM ubuntu:22.04 AS cli
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 iproute2 iptables wireguard-tools ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*
COPY --from=install /usr/local/UniVPN/serviceclient /usr/local/UniVPN/serviceclient
COPY --from=install /usr/local/UniVPN/promote /usr/local/UniVPN/promote
COPY --from=install /lib/libgmcrypto.so /lib/libgmcrypto.so
COPY --from=install /usr/local/UniVPN/sysconfig.ini /usr/local/UniVPN/sysconfig.ini
COPY --from=install /usr/local/UniVPN/sysconfig.ini /opt/gateway/vendor-sysconfig.ini
COPY univpn_gateway/__init__.py univpn_gateway/cli.py univpn_gateway/config.py univpn_gateway/gateway.py univpn_gateway/profile.py univpn_gateway/runtime_cli.py univpn_gateway/native_rpc.py univpn_gateway/native_messages.py univpn_gateway/challenge.py univpn_gateway/certificate_policy.py univpn_gateway/native_control.py univpn_gateway/native_engine.py univpn_gateway/native_auth.py univpn_gateway/native_service.py /opt/gateway/univpn_gateway/
COPY scripts/vpnctl /usr/local/bin/vpnctl
RUN chmod 755 /usr/local/bin/vpnctl
ENV USER=root HOME=/root PYTHONPATH=/opt/gateway VPN_BACKEND=cli \
    LD_LIBRARY_PATH=/usr/local/UniVPN/serviceclient
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD vpnctl status
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/bin/python3", "-m", "univpn_gateway.runtime_cli"]

# Native CSDK terminal frontend: verified SMS MFA without graphical libraries.
FROM cli AS native
ENV VPN_BACKEND=native
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/bin/python3", "-m", "univpn_gateway.native_service"]
