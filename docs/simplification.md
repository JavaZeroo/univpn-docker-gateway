# 还能怎样简化

核对日期：2026-10-29。本文区分当前源码、已完成的实测和待实现的建议；不把建议写成现成功能。审查范围为默认 `native` Compose 路径，不包含历史 GUI 研究路径。

## 结论

现阶段更值得简化的是**配置和首次接入步骤**。当前数据链路已经收敛为一个容器：Windows WireGuard 网卡 → 本机 UDP 端口 → 容器 `wg0` → 按目标放行及 SNAT → 官方 UniVPN 隧道。短信在终端完成，没有额外桌面容器。

保留这条已验证的链路，优先做到“填写一份配置，生成双端文件，一次 Windows 管理员确认”。直接移除 WireGuard 会重新引入 Windows 到 Docker Desktop Linux 网络的三层入口问题，目前没有证据表明这样更简单。

## 当前实证

以下状态来自 [网段网关实测](subnet-gateway-validation.md)，不是本文件重新运行的测试：

- Windows 普通 socket 按默认选路经过新 WireGuard 入口，已通过 4/4 SSH banner、双向各 1 MiB HTTP、UDP，以及探针失败时阻断、恢复后放行。
- 只配置四个精确目标 `/32`；不能据此声称任意网段、DNS 分流或重叠 VPN 都已支持。
- 本次 UniVPN MTU 为 1300，WireGuard 使用 1280 并通过上述传输测试；其他路径需要重新验证。
- Windows 服务/系统重启、Docker 重启恢复、真实厂商断线后的重新认证、内网 DNS 和重叠目的地尚未验收。
- 源码已修复并发 `status` 与后台探针争锁导致的误阻断；**当前已连接的旧容器尚未替换，修复未在该会话生效**。部署需要维护窗口重建并重新认证。

README 中单次容器样本为 18.8 MiB、CPU 1.26%。它不是峰值或稳态结论，**不包含 Docker Desktop、WSL2 虚拟机、Windows WireGuard 驱动及宿主机缓存的总成本**。不能把约 19–21 MiB 描述为整套方案的 Windows 内存占用。

### 当前依赖各自做什么

| 层次 | 当前实现使用的依赖 | 作用与边界 |
| --- | --- | --- |
| Windows 容器平台 | Docker Desktop、当前环境的 WSL2/Linux 内核后端、Docker Compose | 运行 Linux 容器及发布 localhost UDP。WSL2 是当前部署后端，不是所有 Docker Desktop 部署都唯一可选的后端；换后端需重新验收。 |
| Windows 接入 | 官方 WireGuard for Windows 的驱动、隧道服务和命令行工具 | 为普通 Windows 应用提供按目标选路的 IP 接口。日常使用不要求保持 WireGuard 管理界面打开。 |
| 容器基础 | Ubuntu 22.04 amd64 及其基础系统库/工具 | 当前已验证的厂商执行环境。Ubuntu 版本和架构是兼容性边界，不能直接换 Alpine 或 ARM 并宣称等价。 |
| 容器控制 | Python 3 标准库、`tini` | Python 负责本地 RPC、交互认证和健康门控；`tini` 负责 PID 1 信号与进程回收。没有 Python 第三方运行时包。 |
| 容器网络 | `iproute2`、`iptables`/`iptables-restore`、`wireguard-tools` 及基础 shell 工具 | 检查实际出口、管理接口、限制转发和 SNAT；`wg-quick` 读取本地配置建立 `wg0`。 |
| 内核能力 | Linux kernel WireGuard、TUN、IPv4 转发、网络命名空间 | WireGuard 的数据面在 Docker Linux 内核中；安装 `wireguard-tools` 本身不等于内核已支持 WireGuard。UniVPN 使用 TUN；Compose 显式授予 `NET_ADMIN` 和 `/dev/net/tun`。 |
| 官方 VPN 引擎 | `UniVPNCS`、`UniVPNPromoteService`、厂商服务目录/所需共享库（含 `libgmcrypto.so`）和原始 `sysconfig.ini` 模板 | 连接真正的 UniVPN 网关并执行认证、创建隧道。Python 实现的是独立终端前端，没有独立重写 VPN 协议。 |
| TLS 与状态 | `ca-certificates`、只读本地配置、临时运行目录 | 正常 CA 验证及用户明确批准的单网关证书例外；运行状态放在临时目录。厂商可能删除安装目录内的配置，因此镜像保留稳定模板供下次初始化。 |

依据：[Dockerfile](../Dockerfile)、[compose.yml](../compose.yml)、[pyproject.toml](../pyproject.toml)、[native_engine.py](../univpn_gateway/native_engine.py)、[native_service.py](../univpn_gateway/native_service.py) 和 [gateway.py](../univpn_gateway/gateway.py)。这里列的是直接运行依赖及其职责，不是完整的 ELF/系统包传递依赖清单；目前复制的厂商目录也没有逐文件证明全部必需。

构建时需要用户自行取得的官方安装器及 SHA256；它们不是额外常驻进程。`setuptools` 是 Python 打包构建依赖，默认容器通过 `PYTHONPATH` 执行复制的源码，不需要运行时 `pip install`。测试夹具、浏览器和历史分析工具不在正式运行链路内。

### 已经完成的简化

- 默认 Compose 选择 `native`，不安装 Qt、X11、Xvfb、DBus、AT-SPI 或 VNC；短信不依赖 GUI。
- 一个容器同时承载官方 VPN 和内核 WireGuard 接口，没有 WireGuard sidecar，也没有逐个 SSH/HTTP 服务的 `socat` 转发进程。
- Python 运行依赖为空；认证在交互终端输入，不需要额外 Web 服务、数据库或消息队列。
- 容器只发布一个绑定到 `127.0.0.1` 的 UDP 端口；按配置的 IPv4 目标转发，不给每个业务端口建立映射。
- [Windows 辅助脚本](../scripts/windows-gateway.ps1) 已有只读预检、明确请求 UAC 后安装、目标最佳路由校验，以及仅卸载新命名隧道的回滚入口。它拒绝覆盖既有隧道，不修改其他 VPN 的 metric。

Windows 辅助脚本会将**本次新建**接口的 IPv4 ActiveStore metric 设为 0，并校验选路。重建网卡后的持久性仍未验证；脚本安装成功也不等于端到端业务已通过。它不负责生成双端配置，不能把这部分也算作已经自动化。

## 调整前的首次路径与本轮文档减步

下面记录调整前 README 分阶段诊断路径中的 **11 个操作检查点**，不是必须独立执行的 11 条命令。前提是已经安装 Docker Desktop/WSL2 和官方 Windows WireGuard；尚未安装时，还要增加这两项准备工作。

1. 下载、解出官方安装器，放到指定目录，计算摘要并写本地 `.env`。
2. 构建 `native` 镜像。
3. 填写 `private/vpn.json`：网关、目标 CIDR、客户端接入地址和真实探针。
4. 启动容器，交互登录并输入短信，确认数据面就绪。
5. 本地生成 Windows 和容器的两组 WireGuard 密钥。
6. 根据模板填写容器 `wg0.conf`。
7. 根据模板填写独立的 Windows 配置，并限制私钥文件 ACL；再次填写目标 CIDR。
8. 在已认证的容器中热加载 `wg0.conf`。
9. 运行 Windows 只读预检。
10. 显式安装新 Windows 隧道，完成一次管理员确认并检查最佳路由。
11. 核对实际目标响应、WireGuard 握手/计数及故障阻断，随后使用普通应用。

其中第 4 阶段包含账号、密码和验证码交互，不能承诺只敲一次命令或无需等待短信。已经认证的迁移场景需要保留第 8 阶段，避免无故重启短信会话。

### 本轮文档已经减少的操作

旧文档的构建节和登录节都出现 `docker compose build`。新版快速开始只构建一次，源码与镜像未改变时不重复构建。

新版[快速开始](quickstart.md)先准备好全部配置，再第一次启动容器：服务已经会自动读取 `/config/wg0.conf`。这样第 8 个检查点的手工热加载可省，Windows 隧道仍在 VPN 认证及探针通过后激活。这是现有代码支持的文档顺序调整，不是本轮重新实测的完整安装流程。已认证会话的迁移仍单独保留热加载分支。

README 主路径只展示 `native` 和一条连续操作路径。详细教程的 PowerShell 片段生成两组密钥、由 `vpn.json` 的目标生成 Windows AllowedIPs，再填充双端模板，减少手工抄写；它只针对默认接入地址，不等于已交付通用 profile 初始化器。历史目标和会话迁移留在分支说明，不让首次用户先选认证后端。

## 建议实现：减少配置，保留已验证的数据链路

P0 文档项已在本轮完成；其余为待实现建议，命令名称与界面尚未确定。

| 优先级 | 最小交付 | 实际减少什么 | 主要约束与验收 |
| --- | --- | --- | --- |
| P0：文档（已完成） | 合并快速开始，去掉重复构建，区分首次安装与会话内迁移 | 不重复构建；首次安装不必手工热加载；提供默认地址的配置生成片段 | 保留只读计划、交互认证、管理员边界和回滚；完整的新用户安装仍需独立走通。 |
| P1：配置生成器 | 从一份 `vpn.json` 加实例名/UDP 端口，生成新的双端密钥和配置、限制文件权限 | 去掉手工交换两组公钥、重复抄写 CIDR 和多个模板占位符 | 默认不覆盖现有私钥；Windows `AllowedIPs` 从 `allowed_cidrs` 生成，容器 peer 仍只允许客户端 `/32`；生成后调用现有只读预检。 |
| P1：连续入口 | 一个明确命名的 profile，串起构建/启动、终端登录、就绪等待、Windows 计划与现有安装脚本 | 不再让用户手工拼目录、项目名、端口和多组命令 | 复用现有安装器，不另写 Windows 网络配置器；展示计划后才请求一次 UAC。口令和验证码不落配置、日志或参数。 |
| P2：收尾检查 | 同一 profile 下提供状态、诊断和卸载指引 | 减少“已经安装但访问不通”时的猜测 | 分别报告引擎认证、探针、新接口最佳路由、握手与业务响应；失败不动其他 VPN，回滚仅作用于本实例。 |

配置生成器和连续入口做好以后，可以把首次操作**目标设为 4 个阶段**：

1. 初始化实例：提供官方安装器路径、网关、目标 CIDR、真实探针；程序生成并展示本地配置。
2. 启动并在终端登录：输入账号、密码、短信，等待真实数据面就绪。
3. 审阅 Windows 路由计划，完成一次管理员确认；复用现有独立隧道安装与校验。
4. 完成真实业务检查，然后直接在应用中使用目标 IP。

这是命令入口的分组目标，**尚未实现，也没有首次安装耗时对照数据**。不能拿 11 个检查点与 4 个入口分组直接计算减少了多少用户操作；实现后应比较手工编辑文件数、重复输入字段、终端切换及真实首次安装耗时。软件安装、官方安装器获取和 MFA 仍需用户参与。正常日常连接不应反复生成密钥或重装网卡；重启选路持久性未验证，不能先承诺“一次设置永久可用”。

不建议为本轮 MVP 引入常驻 Web 管理端、数据库、第二个代理协议、跨平台大框架，或为了省少量 Python 源码重写控制层。厂商目录继续裁剪可以另做兼容性实验，但必须先追踪配置与共享库依赖；目前没有其可节省总内存的测量依据。

## WireGuard 能否直接去掉

| 替代方式 | 官方资料能确认的事实 | 对本项目的判断 |
| --- | --- | --- |
| Windows 静态路由直接指向 Docker bridge IP | Docker Desktop 的 `docker0` 位于 Linux VM 内；官方说明宿主机不能直接路由到 Linux bridge 容器，也不提供这类逐容器 IP 接入。[Docker Desktop 限制](https://docs.docker.com/desktop/features/networking/networking-how-tos/#known-limitations) | 当前 Windows 部署不能把容器 IP 当成一个现成、可达的三层下一跳，删除 WG 后只加 route 不是已成立的替换方案。 |
| Docker Desktop `network_mode: host` | Desktop 的 host networking 只提供第四层 TCP/UDP 能力，容器不能直接访问宿主机接口；与原生 Linux 的 host namespace 语义有差别。[Docker host networking](https://docs.docker.com/engine/network/drivers/host/#limitations) | 不能据此让 Windows 自动获得容器中的 `cnem_vnic` 或它的路由；多 VPN 还需要各自网络隔离。 |
| WSL mirrored networking | Windows 11 22H2 及以上可启用镜像模式，以改善 localhost、IPv6、VPN 兼容和 LAN 接入；入站还涉及 Hyper-V 防火墙。[Microsoft WSL 网络](https://learn.microsoft.com/en-us/windows/wsl/networking#mirrored-mode-networking) | 官方描述不等于把 Linux VPN 隧道或 Docker 私有 bridge 反向镜像成 Windows 下一跳。是否能另建直连路由方案需要独立实验；它改变全局 WSL 网络环境，本轮没有验证，不能承诺更省步骤。 |
| 原生 Linux 主机上直接路由到容器 | Docker Engine 的 bridge 默认允许 Docker 主机访问容器；网络命名空间和 OS 路由可由管理员管理。[Docker bridge driver](https://docs.docker.com/engine/network/drivers/bridge/) | 对原生 Linux 客户端，设计直连容器网关并去掉额外 WG 是合理实验方向；仍须配置转发、返回路径和防火墙。它不等于 Windows Docker Desktop 也可直接照用，本仓库未实现此入口。 |
| 只用 SSH 转发或 HTTP/SOCKS 代理 | 逐端口转发和应用代理具有各自的应用配置/协议范围。 | 可以满足更窄的访问需求，但不能交付“普通应用直接访问目标 IP 的按网段入口”；不能拿它们冒充本次网段网关目标。 |

因此，**当前 Windows MVP 保留 WireGuard**。它在 Linux 内核里提供一个接口和 UDP 传输，不需要再起一个用户态 WireGuard 守护进程或侧车。[WireGuard 官方命名空间说明](https://www.wireguard.com/netns/)也展示了接口与容器网络命名空间的组合方式。这个选择基于现有可运行路径；不是声称所有操作系统都无法不用 WireGuard。

## 不能靠包装省掉的边界

- **官方 VPN 引擎与网关认证。** 当前协议兼容由厂商程序提供；尚无经过验证的独立协议替代。短信是网关策略，包装工具可以少切换终端，不能跳过验证或静默重发。
- **Windows 新网络接口的管理员操作。** 可以把操作集中为一次明确的 UAC，不能由普通权限进程绕过。官方 WireGuard 支持独立隧道服务及安装/卸载命令，因而不必依赖常开的管理界面。[WireGuard Windows 企业部署](https://git.zx2c4.com/wireguard-windows/about/docs/enterprise.md)
- **可达的三层入口、转发限制和返回路径。** 要让普通 SSH、HTTP、UDP 都直接访问目标 IP，就需要相应 IP 路由和隔离；去掉某个组件之前必须给出能替代它的真实数据路径。
- **配置冲突检查。** 实例使用独立的 Compose 项目、UDP 端口、密钥和接入地址；目标必须明确，不能自动扩大成 `/8` 或默认路由。目的网段重叠时不能靠更低 metric 宣称解决全部歧义。

## MVP 发布前最少再验什么

1. 在维护窗口部署健康检查修复，重新认证；并发状态查询不应造成误阻断，真实探针失败和引擎死亡仍应阻断。
2. 用全新配置走一次连续首次安装：私钥不泄露、目录权限正确，旧 VPN/Mihomo 保留；普通应用的最佳路由、握手/流量计数及真实响应一致。
3. 分别测试 Windows 隧道服务重启、系统重启与 Docker 重建，验证接口优先级、稳定模板恢复和必须人工重新认证的提示；失败时只回滚本实例。
4. 在新增目标/路径上复核 MTU和双向大文件、UDP，以及未允许目标和普通出口回落的阻断。DNS、IPv6、重叠目的地继续明确标记未支持或未验证，不扩大 MVP 范围。

资源优化另设受控测量：同一台机器分别记录 Docker Desktop 未运行、平台空闲、连接一套和两套 VPN 时的宿主机内存/CPU，并单独记录容器样本。先把平台固定成本和每实例增量分开，再决定是否值得更换部署后端。
