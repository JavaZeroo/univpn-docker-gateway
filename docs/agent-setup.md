# Agent 部署指南

适用于能操作 Windows 本机终端的 Agent。先选择版本，再准备公开镜像；账号、密码、短信只在真实交互 TTY 输入，系统授权由用户确认。[普通用户入口](../README.md)

## 1. 确认版本与连接范围

先问用户：需要 **Lite 应用 SOCKS5 代理**，还是 **Full 按原始 IP 透明接入**？应用支持代理且只用 IPv4 TCP 时可选 Lite；需要 UDP 或无需逐应用设置时选 Full。不要替用户安装不需要的 WireGuard。

一次补齐网关 `host:port`、明确的目标 IPv4 CIDR、其中一台真实 SSH 探针 IP/端口、实例名。目标由用户确认，不从网关地址推测，更不能扩大成默认路由。检查 Windows x64 的 Docker Desktop 是否运行 Linux 容器；Full 还需要 Compose。无需 Git、Python 或本机构建。

标签 `lite` / `full` 对应两个版本，`0.4.0-lite` / `0.4.0-full` 可固定版本；`latest` 与 `0.4.0` 仍是 Full。两版共用认证控制器，能力与验收范围不能混报。

## 2A. Lite：容器终端完成配置和登录

确认本机端口、容器名及配置卷不冲突，在真实 TTY 执行 [Lite 单条启动命令](lite.md#首次连接)。首次 wizard 只问网络参数，配置保存到 named volume 的 `/config/lite/vpn.json`；随后同一终端登录并前台运行。无需导出脚本或准备 Windows WireGuard。

Agent 无法提供用户可输入凭据的 TTY 时，把启动命令交给用户自己的终端，完成后再检查；不要代收凭据或伪造输入。`Ctrl+C` 停止，配置和已验证缓存保留。没有有效配置的后台启动不能替代首次 wizard。

```powershell
docker exec univpn-lite vpnctl status
curl.exe --proxy socks5h://127.0.0.1:1080 http://10.80.0.11:8080/
```

以用户已有的服务替换 HTTP 示例，不擅自部署测试服务。核对 `data_plane_ready: true`，再用实际应用的 SOCKS5 路径发请求；裸 SSH、HTTP 直连或宿主机路由都不能证明 Lite 代理可用。仅支持数字 IPv4 TCP，不能报告 UDP、IPv6 或域名解析已通过。

## 2B. Full：生成配置，再由用户完成认证与授权

检查已有 profile、VPN、Docker 网络、端口与网卡。已有 `univpn/private/<名称>/profile.json` 时复用它，不重新 Init 或生成密钥。新实例使用不同名称、UDP 端口和私有 `/30` 接入网段。

在选定的空工作目录导出 Full 启动器：

```powershell
docker run --rm -v "${PWD}:/output" javazero/univpn-docker-gateway:full export-client
```

将已确认的非凭据参数写入 `univpn/private/vpn-source-lab.json`（可创建 `private` 父目录）：

```json
{
  "gateway": "vpn.example.com",
  "port": 443,
  "allowed_cidrs": ["10.80.0.11/32"],
  "probe": {"host": "10.80.0.11", "port": 22, "banner_prefix": "SSH-2.0-"}
}
```

源 JSON 放在目标 profile 目录外，不提前创建 `univpn/private/lab`。自签证书只有独立核验后才填写 `approved_certificate_sha256`，不全局关闭检查。[证书边界](native-rpc-research.md)

```powershell
.\univpn\univpn.cmd -Mode Init -Name lab -GatewayConfig .\univpn\private\vpn-source-lab.json -Image javazero/univpn-docker-gateway:full -UdpPort 51829 -AccessSubnet 10.203.0.0/30
.\univpn\univpn.cmd -Name lab
```

Init 按需准备官方 Windows WireGuard、生成受限 profile、双端密钥和 Compose，再执行只读 Plan；Init 不登录或创建 tunnel，但首次依赖安装会修改 Windows。已有有效签名安装会复用，不升级或修复旧客户端。Connect 启动本实例、等待厂商组件获取/缓存、登录或续交短信，再安装或核验专属网卡。

没有真实 TTY 时完成 Init 后，把 Connect 命令交给用户终端。WireGuard 安装与专属 tunnel 创建可能分别要求 UAC，不能承诺只有一次；3010 表示需要重启，应告知用户，不自动重启或继续宣称就绪。只改 `univpn-*` 本实例，不能删除其他 VPN 或路由以求连通。

已有 profile 的只读预检及实际验收：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\univpn\windows-profile.ps1 -Mode Plan -Name lab
.\univpn\univpn.cmd -Name lab -Mode Status
Find-NetRoute -RemoteIPAddress 10.80.0.11
& "$env:ProgramFiles\WireGuard\wg.exe" show univpn-lab
```

`univpn.cmd` 没有 Plan 模式。核对 `data_plane_ready: true`、目标最佳路由属于 `univpn-lab`、近期握手和增长的收发计数，再做实际业务请求，例如 `curl.exe --noproxy "*" http://10.80.0.11:8080/`。Connect 的单个 SSH banner 不能证明所有目标、HTTP 或 UDP 都已通过。

## 3. 认证、停止和报告边界

两版都不把密码或 OTP 放入命令参数、环境变量、JSON、管道或日志；交互认证不要加 `-T`。`awaiting_sms` 使用现有挑战续交，不能反复 `login` 触发短信；失败、过期或取消后，仅由用户明确发起本实例的新尝试。

- **Lite：** 前台 `Ctrl+C` 停止；容器使用 `--rm`，网络配置和厂商缓存卷仍保留。需另一套 VPN 时另选名称、配置卷和本机代理端口。
- **Full Stop：** `-Mode Stop` 只停本实例容器，保留网卡、路由、密钥和缓存，目标可能仍指向该入口。
- **Full Remove：** `-Mode Remove` 卸载本实例 Windows tunnel 并执行 Compose down，仍保留密钥文件和缓存卷。

停止、移除或新认证尝试需要用户授权，不后台重试或自动重发短信。容器 Up、登录成功或状态命令返回成功，不能代替真实请求；Full 还需证明流量确实经过新网卡，Lite 需证明使用代理。报告版本、实例、配置位置、实际通过的业务与未验证项。Docker Desktop / WSL2 的平台开销也需保留在资源说明中。
