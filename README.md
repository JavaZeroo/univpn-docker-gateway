# univpn-docker-gateway

**实验项目，尚不是开箱即用的 VPN 产品。** 目标是在 Docker 中连接多套 UniVPN，让 Windows 按网段直接访问内网 IP，而非逐个映射 SSH 端口。

**默认 Compose 现使用纯 Python 终端前端 + 官方 UniVPNCS 的 CSDK/RPC 后端**，不安装或运行 Qt、X11、Xvfb、DBus、VNC/noVNC。无图形原型已完成一次真实 HiDev 短信认证、启动 VPN 隧道，并收到 4 台内网服务器的真实 SSH banner。

正式 native 服务模块当前 **114 项单元测试通过**。默认 Compose 容器也已完成真实短信认证、隧道启动和 **4/4 台服务器的 SSH banner** 验证，状态中的 `authenticated`、`tunnel_started`、`data_plane_ready` 均为 `true`。**Windows WireGuard 网段接入仍待端到端实测**。详见 [原生后端研究](docs/native-rpc-research.md) 和 [验证记录](docs/validation.md)。

```text
Windows 应用（SSH / HTTP / UDP）
    → WireGuard 虚拟网卡（仅配置的目标 CIDR）
    → localhost UDP → Docker wg0 → 转发与 SNAT → UniVPN → 内网
```

## 当前状态

| 功能 | 状态 |
| --- | --- |
| 纯 Python → 官方 CSDK，无 GUI 的短信原型 | 真实 HiDev 短信认证、隧道启动和 4 台 SSH banner 通过 |
| 正式 native 服务、配置、会话和传输控制 | 当前 114 项单元测试通过；默认 Compose 容器真实短信、隧道及 4/4 台服务器 SSH banner 通过 |
| 按 CIDR 的双向转发/SNAT、普通出口回落阻断 | 独立 Linux namespace 集成测试通过；使用 veth 模拟隧道，非真实 VPN |
| Windows WireGuard → Docker → 真实 VPN 网段 | 配置模板已提供；整条链路尚待端到端验收 |
| 旧厂商 CLI 交互菜单 | 此版本实测没有短信续交流程；不再是默认 Compose 后端 |
| 早期 Qt 控件终端交互实验 | 仅保留研究代码，不在默认路径执行 |

正式容器连接后的单次 `docker stats --no-stream` 样本为 **18.8 MiB / 384 MiB、CPU 1.26%**。进程只有 tini、Python、官方辅助服务和 UniVPNCS；Qt/Xvfb/DBus/AT-SPI/VNC 包检查为空。单次样本不是稳态或峰值保证，也不包含 Docker Desktop/WSL 的固定平台开销。

旧菜单收到“需要动态口令”后返回主菜单的[实测](docs/cli-mfa-proof.md)仍然成立。新后端绕开菜单交互，通过同一官方引擎的另一条本地控制路径完成短信续交；没有启动隐藏 GUI。

## 构建（Linux amd64）

1. 从[厂商官方页面](https://www.leagsoft.com/doc/article/103107.html)自行下载 Linux 客户端。当前构建目标为 Ubuntu 22.04 / x86_64；其他版本、ARM 未验证。
2. 解压取得安装程序，放到 `vendor/univpn.run`。安装包不提交到 Git，不提供厂商二进制镜像下载。
3. 计算 SHA256，将 `UNIVPN_SHA256=<64位摘要>` 写入本地 `.env`。
4. `docker compose build`。安装包摘要用于固定本地构建输入；运行时还校验已验证的 **10781.21.0.0831** 引擎和辅助程序摘要。其他厂商版本需要重新验证，不能直接忽略校验。

```powershell
Get-FileHash vendor/univpn.run -Algorithm SHA256
docker compose build
```

`vendor/`、`private/`、`.env` 被 Git 忽略。容器不使用 privileged，不共享宿主机网络；只授予 NET_ADMIN 和 TUN 设备。

## 配置与终端登录

将 `examples/vpn.json` 复制为 `private/vpn.json`，填写实际网关、目标 CIDR、Windows 接入地址和真实服务探针。最初用单机 `/32`，不要猜测整个 `/8` 都属于内网。当前就绪探针要求目标主动发送固定前缀的 banner，例如 SSH；这只用于检查 VPN，不限制转发协议为 SSH。

先不创建 `private/wg0.conf`，构建并启动默认 native 后端：

```powershell
docker compose build
docker compose up -d
docker compose exec vpn vpnctl login
```

按提示输入账号、密码。若网关要求短信，保持这个终端，在同一登录会话内输入最新验证码。不要使用 `-T`；交互输入需要 TTY。密码和验证码不通过 Docker 启动参数、环境变量或配置文件传递。

若希望收到短信挑战后先返回终端，再单独提交验证码：

```powershell
docker compose exec vpn vpnctl login --no-otp
docker compose exec vpn vpnctl sms
```

`sms` 续交现有会话，不再次提交账号密码或重发短信。挑战最多等待约 180 秒，每次挑战只允许一次验证码提交。`login --no-otp` 返回成功可能只表示正在等待验证码；认证成功也不等于网络已就绪。检查实际数据面：

```powershell
docker compose exec vpn vpnctl status
```

只有真实目标探针通过、`data_plane_ready` 为 `true` 时，`vpnctl status` 才以状态码 0 退出。探针同时检查 VPN 接口、到目标的路由和业务 banner；例如目标可配置为 `10.80.0.11:22`，前缀为 `SSH-2.0-`。后台转发守卫独立使用这一结果，失败时阻断转发。

过期、失败或取消的认证上下文不能再次登录。重启容器，再显式发起一次新登录：

```powershell
docker compose restart vpn
docker compose exec vpn vpnctl login
```

可用 `docker compose exec vpn vpnctl cancel` 取消当前待完成会话。项目不自动重新认证、不自动重发短信；断网恢复后也不会在后台消耗验证码。

证书默认使用正常 CA 链及主机名验证。只有你已独立核验并明确批准当前网关叶证书的 SHA256 时，才在本地 `private/vpn.json` 配置可选的 `approved_certificate_sha256`（64 位十六进制）。匹配后，例外只用于该网关、端口和本次登录请求，不修改系统信任库。**独立 TLS 预检与引擎后续连接不是同一连接，不能将其称为引擎侧证书 pinning**；摘要不符时直接失败，不降级。

旧 `cli` 和 `gui-experimental` 构建目标保留作研究对照，不是默认 Compose 路径。

## 网段转发实验

这部分 Windows 到真实 VPN 的整条链路仍待验收。认证和实际目标探针通过后，再配置接入链路：

1. 本地分别生成 Windows 和容器 WireGuard 密钥。可使用本机 WireGuard 的 `wg genkey` / `wg pubkey`。不要提交私钥。
2. 参考 `examples/wg0.conf` 写入 `private/wg0.conf`，容器 peer AllowedIPs 只允许 Windows 接入 `/32`。
3. 新建 Windows WireGuard 配置，参考 `examples/windows.conf`，AllowedIPs 填所需目标网段；与 vpn.json 一致。接入地址必须与现有局域网、VPN、Docker 网络无冲突。
4. 重建此测试容器，导入并启用**新的** Windows tunnel。现有其他 VPN、Mihomo、WireGuard tunnel 保留。
5. 验证真实 IP 的 SSH、HTTP、UDP、大文件，确认路由、MTU，以及断开 UniVPN 后无绕行。未完成这些测试前，不移除原有 SSH 入口。

多个实例用不同 Compose project、UDP 端口、接入地址和密钥。目标网段重叠需要另行设计地址映射，当前不支持同时区分重叠目的地址。首版只处理 IPv4 和直接 IP，不提供内网条件 DNS。

容器只有实际 VPN 路由和目标 banner 探针同时通过才启用转发；探针失败后在下一次轮询阻断。正常流量还必须命中 wg0 → 指定 VPN 接口的过滤规则。其他目的地或从普通 eth0 出站的转发被丢弃。

## 开发与证据

```sh
python -m unittest discover -s tests -v
```

当前 114 项单元测试覆盖原生传输分帧、同引擎短信续交、超时/取消/禁止重复提交、证书策略、认证与健康状态分离，以及配置和规则生成。它们使用模拟对象；正式容器的真实短信、隧道和 4/4 台服务器 SSH banner 另行完成实测，不能由单元测试替代。

独立 Linux 网络测试已通过真实 TCP/HTTP/UDP、SNAT、目标阻断、普通出口回落阻断及重复启动规则检查，接口由 veth 模拟，不包含 WireGuard、UniVPN 或 Windows。历史 Qt/AT-SPI 集成测试使用自建登录窗口，不属于默认 native 后端的认证证据。镜像内存上限不是实际内存消耗，Docker Desktop/WSL 的平台开销应另测。

架构背景见 [docs/design.md](docs/design.md)。网络、认证及资源结果将以实测记录更新；不能从程序启动或网卡存在推断 VPN 可用。

## 来源与许可

本项目受 [jesusdf/huawei-vpn](https://github.com/jesusdf/huawei-vpn) 的容器封装方式启发。本仓库自行编写的源码采用 GPL-3.0-only；厂商 UniVPN 客户端不属于本项目的开源代码，不包含在仓库中，其使用和再分发受厂商许可约束。本项目不发布包含厂商程序的安装包或镜像，用户自行取得安装程序并在本地构建。

参考：[Docker Desktop 网络限制](https://docs.docker.com/desktop/features/networking/networking-how-tos/)、[WireGuard 网络命名空间](https://www.wireguard.com/netns/)、厂商安装包附带《终端用户接入指南》10.2.2（Linux CLI 仅用户名/密码）。
