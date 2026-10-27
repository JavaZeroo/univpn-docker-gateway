# syntax=docker/dockerfile:1
FROM ubuntu:22.04 AS install
ARG UNIVPN_SHA256
COPY vendor/univpn.run /tmp/univpn.run
RUN test -n "$UNIVPN_SHA256" \
    && echo "$UNIVPN_SHA256  /tmp/univpn.run" | sha256sum -c - \
    && chmod +x /tmp/univpn.run \
    && (/tmp/univpn.run S > /tmp/install.log 2>&1; \
        test -x /usr/local/UniVPN/UniVPN || { cat /tmp/install.log; exit 1; })

FROM ubuntu:22.04 AS terminal
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
    QT_ACCESSIBILITY=1 QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1 \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/univpn/bus \
    XDG_RUNTIME_DIR=/run/user/0 PYTHONPATH=/opt/gateway \
    LD_LIBRARY_PATH=/usr/local/UniVPN/lib
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD vpnctl status
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/bin/python3", "-m", "univpn_gateway.runtime"]
