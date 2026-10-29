# 简化现状与取舍

核对日期：2026-10-29。当前重点已从“设计配置生成器”变为验收已有连续入口，不能继续把已实现的 `Init`、`Plan` 和 `Connect` 列作计划。

## 已实现的简化

| 用户原本要处理的事情 | 当前实现 |
| --- | --- |
| 拉源码、安装 Python 依赖、本机构建 | 默认使用 `javazero/univpn-docker-gateway` 预构建镜像；镜像导出 CMD 入口与三个配套 PowerShell 脚本 |
| 手工下载、解包厂商安装器 | 首次容器启动从官方固定 URL 获取并校验；之后复用 named volume 内的固定 ZIP，不执行安装脚本 |
| 生成两端密钥、交换公钥、重复抄目标 CIDR | `windows-profile.ps1 Init` 生成受限本地 profile、双端 WG 配置；由一份 VPN 配置生成 Windows AllowedIPs，并执行只读 Plan |
| 分别调用 Compose、登录、网卡安装和检查 | `univpn.cmd` 调用默认 Connect，串起初始化、计划、镜像/容器、TTY 登录或短信续交、专属 WG 安装/复用及普通 TCP banner 检查 |
| 开桌面、VNC 或额外 Web 登录服务 | Python 标准库直接控制官方本地 CSDK/RPC，在终端续交同一短信挑战 |
| 单独部署 WG 或逐业务做端口映射 | 一个容器内放 `wg0` 和 UniVPN，内核转发原始 IPv4；没有 WG sidecar、用户态 WG 守护进程或逐服务 socat |

公开镜像不含厂商软件；首次获取发生在用户机器上。缓存和可变运行目录分离，容器之间不共享认证配置。默认不同 Compose 项目各有自己的缓存卷，并非全机只下载一次。来源及固定摘要见[镜像说明](image-release.md)。

## 用户现在面对的入口

首次只需预装 Docker Desktop 的 Linux 容器环境；导出客户端到新空目录后，在同一个 Windows PowerShell 中运行 `univpn.cmd`。0.3.0 的 Windows 引导会复用有效 WireGuard 安装，仅完全缺失时才校验官方固定 MSI 并安装；用户不需要手动安装它。用户仍需提供网关、明确目标、真实 banner 探针，并在终端输入账号密码和短信；WireGuard 安装与新建 Windows 接口可能分别要求 UAC。CMD 只为本次 PowerShell 进程设置执行策略，不改持久策略。

| 动作 | 当前语义 |
| --- | --- |
| `Init` | 检查/准备宿主机 WireGuard，再生成 profile 与 Plan；不登录 VPN 或创建隧道网卡，拒绝覆盖已有密钥 |
| `Connect` | 默认动作；缺 profile 才初始化；等待容器就绪，按当前认证状态登录或续交 SMS；检查路由与一个真实 TCP banner |
| `Status` | 查看容器认证/就绪状态与 Windows 当前最佳路由；不重新登录 |
| `Stop` | 只停止本实例容器，保留 Windows tunnel、密钥与缓存；目标路由可能继续指向不可用入口 |
| `Remove` | 卸载本实例 Windows tunnel，再 `compose down`；保留 profile 文件和缓存卷 |

这减少了手工文件配对和命令拼接，但没有首次使用耗时的前后对照，不能承诺“几分钟完成”或按入口数量计算提速。Connect 的最终检查是配置中的一个 TCP banner，完整 HTTP/UDP、全部目标和故障恢复仍需对应业务验收。

## 能否只运行一个容器

这里区分“少装一个软件”和“同样支持 Windows 普通应用按原始 IP 访问”。0.3.0 已省去用户手动装 WireGuard 的步骤，但仍会在 Windows 建立网络入口。

| 方式 | Windows 额外准备 | 访问范围 | 本项目状态 |
| --- | --- | --- | --- |
| 程序也在 VPN 容器或共享其网络的容器里 | 可只用 Docker | 容器内应用使用 VPN，Windows 应用不会自动获得相同路由 | Docker 平台能力；当前启动器未封装独立容器工作流 |
| 容器提供 HTTP/SOCKS 代理 | 应用设置代理，SSH 还需相应代理入口 | 取决于应用和代理协议，不能直接等同所有 TCP/UDP/系统 DNS | 当前镜像未提供代理模式，不能按已有功能宣传 |
| Windows 普通应用按目标原始 IP 访问 | 宿主机网卡与路由；当前由启动器准备 WireGuard | 已配置 IPv4 目标的 SSH、HTTP、UDP 等 | 当前采用的路径 |

[Docker Desktop 网络限制](https://docs.docker.com/desktop/features/networking/networking-how-tos/#known-limitations)说明 Linux bridge 不直接暴露给 Windows 宿主机；[Desktop host networking](https://docs.docker.com/engine/network/drivers/host/#limitations)是 L4 能力，不能据此让 Windows 自动得到容器 VPN 路由。因此只启动一个 Linux 容器，不能完整实现第三行的用户体验。自动安装宿主机组件可以减少人工步骤，但仍是 Windows 引导程序的行为。

独立 `univpn-*` 服务保护已有隧道配置；已安装 WireGuard 时不再运行 MSI。目标 CIDR 与旧隧道重叠，仍会参与 Windows 选路，必须实查，不能宣称完全无影响。

## 保留哪些依赖，为什么

- **官方引擎和 MFA：** 当前由 UniVPNCS、辅助服务及厂商库承担真正的 VPN 协议；终端前端没有替代远端认证。失败后需用户明确重新连接，不自动试密码或重发短信。
- **Windows 三层入口：** 普通应用要直接访问内网 IP，需要可达的 IP 接口与路由。新网卡和服务仍需管理员权限，包装器只能集中请求，不能绕过。
- **容器隔离、白名单和真实探针：** 接口存在或认证成功不足以判断业务可达；仅允许指定目标从 `cnem_vnic` 转发，阻止普通出口回落。
- **Docker Desktop/WSL 平台：** 当前容器内的工具已较少，但 Linux 平台成本不会随它们一起消失。既有 18.8 MiB 单次容器样本不包含整机平台内存，也不是稳态或峰值承诺。

本轮保留 WireGuard：[Docker Desktop 官方说明](https://docs.docker.com/desktop/features/networking/networking-how-tos/#known-limitations)指出 Windows 宿主机不能直接路由到其 Linux bridge 容器；Desktop 的 [host networking 只提供 L4 能力](https://docs.docker.com/engine/network/drivers/host/#limitations)。[WSL mirrored networking](https://learn.microsoft.com/en-us/windows/wsl/networking#mirrored-mode-networking)改善兼容性，但没有承诺自动把 Linux VPN 接口及 Docker 私有路由反向提供给 Windows。

原生 Linux 主机可另行研究直接路由到容器；这不是当前 Windows 入口的等价删组件操作。SSH 端口转发和 HTTP/SOCKS 代理可以满足更窄的需求，不能替代本次“普通应用按目标 IP 访问”的交付目标。

## 下一步优先验收，不继续堆功能

1. 新镜像已通过全新缓存的官方在线准备，以及第二个无网络新容器命中缓存的检查；公开镜像已发布并核验匿名拉取；接下来验收新包装器的真实短信、新 Windows 安装/已有接口复用和普通应用路径。代码交付不等于整条新流程已经实测。
2. 在维护窗口部署健康查询竞争修复并重新认证。现存旧容器未重启，修复尚未在该会话生效；原有 4/4 SSH、双向 1 MiB HTTP、UDP 与探针故障恢复证据保留，但不能替代新版验收。
3. 分别验证 Windows 服务/系统重启、Docker 重建、缓存损坏与下载失败提示。确认只影响本实例，Stop 与 Remove 的保留/卸载语义清楚。

DNS 分流、IPv6、重叠目标映射和厂商真实断线重认证继续作为独立范围，不先扩大 MVP。暂不增加常驻 Web 管理端、数据库或另一套代理协议；如果继续优化资源，先分别测量平台空闲成本和单/多实例增量。
