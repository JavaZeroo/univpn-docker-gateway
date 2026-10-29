# Lite / Full：怎样选更简单

0.4.0 已实现 Lite 应用代理与 Full 网段入口。两版共用原生终端认证，公开镜像都不包含厂商软件，首次从官方获取并校验后缓存；无需用户本机构建或另开 Windows 虚拟机。

| 需求 | 选择 | 需要接受的边界 |
| --- | --- | --- |
| 只想启动 Docker，应用支持设置 SOCKS5，只用 IPv4 TCP | [Lite](lite.md) | 每个应用选择代理；不解析目标域名，不支持 UDP/BIND/IPv6；裸 SSH 不自动走代理 |
| 应用直接填写原始 IP，或需要 UDP | [Full](quickstart.md) | Windows 需要 WireGuard、专属网卡与路由；启动器按需准备，系统授权仍由用户完成 |

标签为 `lite` / `0.4.0-lite` 和 `full` / `0.4.0-full`。`latest` / `0.4.0` 为兼容仍指 Full，不因 Lite 新增而改变老用户入口。

## 已省掉什么

- **共同：** 无桌面、VNC 或额外 Web 登录服务；Python 标准库通过本地 RPC 续交同一短信挑战。固定包自动获取、缓存校验与独立运行副本已经实现。
- **Lite：** 不安装 WireGuard、不导出 Windows 启动器、不改 Windows 网卡或路由。一个 `docker run -it` 在终端完成网络配置和登录；应用使用本机 SOCKS5 端口。
- **Full：** `Init` 自动生成双端密钥和配置，目标 CIDR 不再重复抄写；`Connect` 串接 Compose、TTY 登录、Windows 安装/复用和一个真实 banner 检查。已有有效 WireGuard 不升级，部分安装或异常服务先停止，不擅自修复。

两版都是每套 VPN 一个容器，不需要 WG sidecar 或逐业务端口转发。Lite 的每个出站 TCP socket 强制绑定 VPN 接口并核对实际路由，真实探针就绪才接受业务；Full 则以过滤链、SNAT 和明确出口约束内核转发。实现与依赖见[架构](architecture.md)。

## 这次简化改变了访问方式

Lite 做到了“不手装 Windows 网络组件”，代价是应用必须使用代理。SOCKS5 中的域名字段只接受规范数字 IPv4，`socks5h` 也不会让真正域名自动可用；它不是透明网段网关。其代理没有额外账号密码，入口只发布到 `127.0.0.1`，VPN 凭据仍在容器终端输入。

Full 保留原始 IP 的 TCP/UDP 路径。[Docker Desktop 的 Linux bridge 不直接向 Windows 暴露](https://docs.docker.com/desktop/features/networking/networking-how-tos/#known-limitations)，[Desktop host networking 只提供 L4 能力](https://docs.docker.com/engine/network/drivers/host/#limitations)；仅删除 WG、添加一条指向容器 IP 的路由不能替代当前入口。自动安装 WG 减少人工操作，仍是 Windows 本机引导，不是 Docker 跨越权限边界。

Full 的不同 VPN 若目标地址重叠，会竞争 Windows 选路。Lite 可以让应用选择不同代理端口，避开同一系统路由表的歧义，但多实例重叠目标仍未独立验收；两版都不能绕过网关对同一账号的并发限制。

## 没有省掉的事

账号、密码、短信及真实目标探针仍不可缺；认证失败、过期或取消后需要明确发起新尝试，不自动重发短信。原生引擎只验证当前固定 Linux amd64 版本，厂商协议和使用许可不会因为容器封装而改变。

Lite 停止后释放代理端口，但应用的代理配置不会自动清除；Full 的 `Stop` 保留网卡和目标路由，`Remove` 才释放该入口。配置与缓存卷保留，认证会话不保留。

不声称 Lite 在任何负载下都更省内存或更快：它省去 WG 组件和宿主机引导，但 TCP 业务经过 Python relay。既有 18.8 MiB 是一次旧容器样本，不是 Lite 测量或整机成本；Docker Desktop/WSL 的平台开销仍需单独测量。

## 接下来先验收

既有 Full 已有真实短信、4/4 SSH、双向 1 MiB HTTP、UDP 与探针故障恢复证据。Lite 的协议测试和一次性隔离容器已验证真实内核绑定、1 MiB HTTP、就绪撤销/恢复及可用普通出口下的回落阻断，VPN 段仍由 veth 模拟。

**新 Lite 入口的真实 UniVPN 登录和完整业务仍待验收。** 新 Full launcher 的整体流程、Windows 重启选路、真实断线重认证也继续分开验证；现存旧容器未重建，源码中的健康查询修复尚未在该会话生效。先完成这些门槛，再讨论条件 DNS、IPv6、重叠映射或新增管理界面。
