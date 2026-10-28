# UniVPN 容器网关改造方案

> **历史设计草案。** 下文保留 2026-10-27 准备阶段的判断与待办，不代表当前实现或部署状态。当前默认后端、依赖及真实 Windows 验证请看[架构与依赖](architecture.md)和[实测记录](subnet-gateway-validation.md)。

日期：2026-10-27。状态：网关转发已通过独立 Linux 模拟隧道测试，真实 Windows/VPN 端到端待验证。默认实现已切换纯原生 CLI，短信认证受厂商 CLI 限制，见 [实测](cli-mfa-proof.md)。本文后续涉及 GUI 的段落只记录早期实验思路，不是当前默认实现。

## 目标

让 Windows 按目标 CIDR 访问多套 UniVPN 内网，应用直接使用原始 IP 和服务端口。不再为每个 SSH、HTTP、数据库端口增加 socat 实例。

认证后端与网络出口分开实现。默认仅使用 UniVPNCS，不引入图形依赖；短信环境需要研究真正不依赖 GUI 的认证接口。

## 已确认的边界

- 当前 connect.exp 只有账号、密码和连接结果，没有验证码交互。不能把未观察到的 OTP 提示当作厂商 CLI 已支持的功能。
- 图形认证、业务可达是两道检查；Ubuntu 22.04 + 新客户端 + 对齐配置的组合曾成功，无法据此锁定先前失败的唯一原因。
- Docker Desktop 的 Linux 容器 IP 不能直接当作 Windows 路由下一跳。
- 当前本机 Docker 引擎不可连接，因此此次没有新建容器或进行转发验证。

## 网络结构

Windows WireGuard → localhost 发布的 UDP 端口 → VPN 网络命名空间中的 wg0 → 受限 FORWARD / SNAT → cnem_vnic → 内网。

每套 VPN 独立命名空间。WireGuard 辅助服务可使用 network_mode: service:vpn，端口发布和 ip_forward sysctl 配置放在网络命名空间拥有者 vpn 服务上。不得把不同 VPN 实例放在 host 网络或共享同一命名空间。

优先验证 WSL 内核 WireGuard 能力；不可用时才评估用户态实现，不能预设内核模块一定存在。复用 Windows 已安装的 WireGuard，但新建独立 tunnel，不改现有 huawei-new。

容器网关只负责转发，不要求 DNS、桌面、浏览器等服务常驻。

## 配置约定

- VPN_ALLOWED_CIDRS：用户明确允许访问的 IPv4 目标前缀。先用目标 /32 验证，再按实际路由合并。
- WG_CLIENT_ADDRESS：Windows 接入地址，每套实例独立；容器对端 peer 的 AllowedIPs 只含该客户端接入 /32。
- Windows peer 的 AllowedIPs：目标前缀及必要的网关诊断地址。不配置 0.0.0.0/0。
- 私钥：运行时挂载文件，权限受限；不进入镜像、日志、博客或 Git。
- Windows 接入端口默认仅发布到 127.0.0.1；远端客户端接入属于额外功能，不能默认暴露到局域网。
- DNS：首版支持直接 IP；随后实现内网域名的条件解析，不全局替换 Windows DNS。

## 转发规则要求

1. 只匹配 wg0 入站、客户端源地址、明确目标 CIDR、cnem_vnic 出站的流量。
2. 在 UniVPN 出口做 MASQUERADE；回复仅允许对应出口、源网段以及 ESTABLISHED/RELATED 状态。
3. wg0 的其他转发流量默认丢弃。即使 cnem_vnic 消失、路由回落到 eth0，也不能绕行。
4. 使用独立命名链；启动、if-up、if-down、退出操作幂等；只清理本项目拥有的规则，不 flush 系统其他链。
5. 更新网段先校验 CIDR、重叠和路由，再原子更新本项目规则。健康探测不通过不启用 Windows 新路由。
6. 把网关 UDP endpoint、VPN 网关自身的路由排除在隧道回送之外；验证与现有 Mihomo TUN、原生 HiDev 路由及 WireGuard tunnel 的实际选路。
7. 显式处理 IPv6：首版只配置 IPv4 目标，不宣称已覆盖 IPv6。

## 认证及恢复

CLI 保持默认轻量后端；GUI 使用独立镜像构建目标，保留官方 GUI 与最小 X 服务。VNC/noVNC 按需启用，不安装完整桌面。

认证状态至少包括 connecting、awaiting_user_auth、connected_unverified、ready、retry_backoff。需要验证码或密码错误时停止自动认证重试；普通断线使用有上限的指数退避。日志不能回显账号密码或验证码。

不能把网卡出现当作 ready；网卡、下发路由和配置的真实业务探针都通过后才就绪。会话失效后提示人工认证；不循环发送短信。登录后先验证关闭 VNC/noVNC 是否保持隧道；不能预设停止 X 服务或 GUI 本身也能保持连接。

## 实施和验证顺序

1. 测试实例保留现有 VPN 和 SSH 入口。检查 WireGuard 内核能力、发布 UDP 的回环连通性、选取未冲突的接入地址。
2. 单个目标 /32：Windows 直连目标原 IP 的 SSH banner；核对容器抓包双向流量及源地址转换。
3. 验证真实 HTTP、UDP（目标提供对应服务时）、大文件传输。SSH 完成不能代替 UDP 证明；无服务时记录未验证。
4. 故意断开 VPN，确认目标数据包被阻断且 eth0 无明文目标流量；确认普通互联网、原生 HiDev 和 A5 回连不受影响。
5. 重连、容器重启、Docker 重启后复测；记录 Windows 路由恢复、重复规则和 stale conntrack 行为。
6. 第二套 VPN 验证不重叠目标；重叠网段明确报告冲突，不静默选择某一套。
7. 与原方案按相同空闲时长/流量测量容器内存、CPU、Docker/WSL 整体内存，以及吞吐和延迟。分别报告平台固定成本和网关增量。
8. 前述通过后再迁移别名、删除端口转发。GUI 瘦身单独实验，避免把认证和路由变量同时改变。

## 参考

- https://github.com/jesusdf/huawei-vpn
- https://docs.docker.com/desktop/features/networking/networking-how-tos/
- https://www.wireguard.com/netns/
- https://www.wireguard.com/quickstart/
