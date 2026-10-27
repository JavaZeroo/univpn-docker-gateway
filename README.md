# univpn-docker-gateway

**实验项目，尚不是开箱即用的 VPN 产品。** 目标是在 Docker 中连接多套 UniVPN，让 Windows 按网段直接访问内网 IP，而非逐个映射 SSH 端口。

**当前默认镜像已改为纯原生 CLI**：不安装或运行 Qt、X11、Xvfb、DBus、VNC/noVNC。HiDev 短信认证已用官方 10781.21.0.0831 的原生 CLI 实测失败，客户端返回需要动态口令后直接回主菜单，没有验证码输入流程。详见 [实测记录](docs/cli-mfa-proof.md)。**此项目目前不能完成纯命令行短信认证。**

```text
Windows 应用（SSH / HTTP / UDP）
    → WireGuard 虚拟网卡（仅配置的目标 CIDR）
    → localhost UDP → Docker wg0 → 转发与 SNAT → UniVPN → 内网
```

## 当前状态

| 功能 | 状态 |
| --- | --- |
| 配置校验、单次短信会话状态、禁止自动重复提交 | 有单元测试；不等于真实厂商登录已通过 |
| 按 CIDR 的双向转发/SNAT、普通出口回落阻断 | 独立 Linux namespace 集成测试通过；使用 veth 模拟隧道，非真实 VPN |
| Windows WireGuard 配置 | 模板；待端到端验证 |
| 默认纯原生 CLI 镜像 | 已构建并运行；Qt/Xvfb/DBus/VNC 包检查为空 |
| 原生无图形 UniVPNCS 的短信认证 | 10781.21.0.0831 + HiDev 实测失败；返回动态口令错误后回主菜单 |
| 早期 Qt 控件终端交互实验 | 仅保留研究代码；不在默认镜像内执行，不属于纯原生 CLI 短信支持 |

默认 CLI 镜像停在登录菜单时，单次样本约 12.5 MiB；这不是已连接 VPN 的稳态测量，不包含 Docker Desktop/WSL 的平台开销。

厂商 GUI 和命令行程序是两种不同的认证入口。隐藏 GUI 的终端操作桥不等于纯命令行客户端。现在以**完全不依赖图形栈**为默认目标；短信需要另行确认可用的非 GUI 认证接口、SDK 或兼容客户端，不承诺现有 CLI 能通过增加 OTP 参数实现。

## 构建（Linux amd64）

1. 从[厂商官方页面](https://www.leagsoft.com/doc/article/103107.html)自行下载 Linux 客户端。当前构建目标为 Ubuntu 22.04 / x86_64；其他版本、ARM 未验证。
2. 解压取得安装程序，放到 `vendor/univpn.run`。安装包不提交到 Git，不提供厂商二进制镜像下载。
3. 计算 SHA256，将 `UNIVPN_SHA256=<64位摘要>` 写入本地 `.env`。
4. `docker compose build`。摘要检查只是固定输入；仍应核对下载来源及厂商公布的校验值。

```powershell
Get-FileHash vendor/univpn.run -Algorithm SHA256
docker compose build
```

`vendor/`、`private/`、`.env` 被 Git 忽略。容器不使用 privileged，不共享宿主机网络；只授予 NET_ADMIN 和 TUN 设备。

## 配置与首次实验

将 `examples/vpn.json` 复制为 `private/vpn.json`，填写实际网关、目标 CIDR、Windows 接入地址和真实服务探针。最初用单机 `/32`，不要猜测整个 `/8` 都属于内网。当前就绪探针要求目标主动发送固定前缀的 banner，例如 SSH；这只用于检查 VPN，不限制转发协议为 SSH。

先不创建 `private/wg0.conf`，启动原生 CLI：

```powershell
docker compose up -d
docker compose attach vpn
```

在厂商 CLI 菜单中选择 `GATEWAY` 连接并登录。仅适用于账号密码认证；遇到动态口令要求时停止，不要把验证码盲发到主菜单。保持会话运行，可用 `Ctrl-P`、`Ctrl-Q` 分离终端而不停止容器。运行状态检查：

```powershell
docker compose exec vpn vpnctl status
```

凭据在厂商终端中输入，不写入 Docker 启动参数或环境变量。厂商 CLI 会回显账号，默认关闭 Docker 日志持久化。当前没有自动断线重认证。`vpnctl login` 和 `inspect-ui` 仅适用于早期图形实验，默认 CLI 镜像会拒绝这两个操作。

早期 `gui-experimental` 构建目标保留作研究参考，不是默认 Compose 路径；其自建 Qt 控件测试通过不代表厂商短信已验证，也不符合“完全无图形依赖”的使用要求。

## 网段转发实验

认证和实际目标探针通过后，再配置接入链路：

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

单元测试覆盖过期验证码、重复提交、未收到真实业务响应、证书提示、目标白名单及规则隔离；不替代真实认证与数据面验证。独立 Linux 网络测试已通过真实 TCP/HTTP/UDP、SNAT、目标阻断、普通出口回落阻断及重复启动规则检查，接口由 veth 模拟，不包含 WireGuard、UniVPN 或 Windows。Qt/AT-SPI 集成测试使用自建登录窗口，不代表厂商窗口已验证。镜像内存上限不是实际内存消耗，Docker Desktop/WSL 的平台开销应另测。

架构背景见 [docs/design.md](docs/design.md)。网络、认证及资源结果将以实测记录更新；不能从程序启动或网卡存在推断 VPN 可用。

## 来源与许可

本项目受 [jesusdf/huawei-vpn](https://github.com/jesusdf/huawei-vpn) 的容器封装方式启发。仓库自身脚本采用 GPL-3.0-only；厂商 UniVPN 客户端不属于本项目的开源代码，不包含在仓库中，其使用和再分发受厂商许可约束。

参考：[Docker Desktop 网络限制](https://docs.docker.com/desktop/features/networking/networking-how-tos/)、[WireGuard 网络命名空间](https://www.wireguard.com/netns/)、厂商安装包附带《终端用户接入指南》10.2.2（Linux CLI 仅用户名/密码）。
