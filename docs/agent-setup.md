# Agent 部署指南

适用于 Windows 主机上的本地 Agent。使用公开预构建镜像，自动完成环境检查、配置生成、启动和验收；账号密码、短信及系统授权由用户参与。普通用户入口见[README](../README.md)。

## 1. 先检查现状，补齐连接参数

检查 Docker Desktop 是否运行 Linux 容器、Compose 是否可用，以及 Windows WireGuard 是否已安装。缺少依赖时使用[Docker 官方安装说明](https://docs.docker.com/desktop/setup/install/windows-install/)或[WireGuard 官方安装器](https://www.wireguard.com/install/)，按本机权限完成准备；不要另外安装 Git、Python 或构建镜像。

一次收集缺少的非凭据参数：网关 `host:port`、明确目标 IPv4 CIDR、其中一台真实 SSH 探针的 IP/端口，以及实例名。示例中的地址均需替换，不根据网关地址推测目标网段。

先检查当前目录是否已有导出的 `univpn`，以及 `univpn/private/<名称>/profile.json`。已有 profile 时重用它，不重新 Init 或生成密钥。新实例使用不冲突的名称、本机 UDP 端口和私有 `/30` 接入网段；检查现有 VPN、Docker 网络、端口和网卡。目标范围由用户确认，不能扩大成默认路由。

## 2. 导出启动器，非交互生成配置

在用户选定的本地工作目录执行；已有导出目录时跳过此步：

```powershell
docker run --rm -v "${PWD}:/output" javazero/univpn-docker-gateway:0.2.0 export-client
```

将已确认参数写入 `univpn/private/vpn-source-lab.json`（可创建 `private` 父目录），例如：

```json
{
  "gateway": "vpn.example.com",
  "port": 443,
  "allowed_cidrs": ["10.80.0.11/32"],
  "probe": {"host": "10.80.0.11", "port": 22, "banner_prefix": "SSH-2.0-"}
}
```

文件只保存连接参数，不含账号、密码或 OTP。将源 JSON 放在目标 profile 目录外；不要预先创建 `univpn/private/lab`，Init 会拒绝已有目录。自签证书需要独立核验后填写 `approved_certificate_sha256`，沿用现有[证书策略](native-rpc-research.md)。

下面示例从导出目录的父目录运行；端口与网段需先确认不冲突：

```powershell
.\univpn\univpn.cmd -Mode Init -Name lab -GatewayConfig .\univpn\private\vpn-source-lab.json -Image javazero/univpn-docker-gateway:0.2.0 -UdpPort 51829 -AccessSubnet 10.203.0.0/30
```

Init 会生成受限的 profile、双端密钥和 Compose，并执行只读 Plan，不启动 VPN、不安装网卡。无需手填 WireGuard 公私钥或单独维护 AllowedIPs。后续操作从保存的元数据读取端口、地址和镜像，通常只需实例名。

已有 profile 的独立只读预检使用以下脚本，`univpn.cmd` 没有 Plan 模式：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\univpn\windows-profile.ps1 -Mode Plan -Name lab
```

## 3. 连接并保留当前认证会话

先确认工具能提供真实交互 TTY、让用户输入凭据；不具备此能力时，完成 Init 后把下面的命令交给用户终端执行，再继续验收。

```powershell
.\univpn\univpn.cmd -Name lab
```

Connect 会启动本实例、等待首次官方组件获取/缓存准备、登录或续交短信、安装或复用专属 Windows 网卡，并检查一个真实目标 banner。只有 `idle` 才发起登录，`awaiting_sms` 续交当前挑战；不要为了获取验证码反复执行登录。

登录需要真实 TTY。Agent 的工具若不能让用户在交互终端输入凭据，就完成 Init 后，将上面的 Connect 命令交给用户在自己的终端执行，再继续检查状态。不要把密码/验证码塞进命令参数、环境变量、JSON、管道或日志；不要用 `-T` 执行交互登录。首次网卡安装所需的 UAC 由用户在系统弹窗确认。

失败、过期或取消的会话需要用户明确发起新尝试；届时仅对本实例 Stop 后再 Connect，不后台重试或自动重发短信。已有异常同名网卡会拒绝覆盖，应先诊断并向用户说明，不删除其他 VPN 以求连通。

## 4. 验收后再报告成功

```powershell
.\univpn\univpn.cmd -Name lab -Mode Status
Find-NetRoute -RemoteIPAddress 10.80.0.11
& "$env:ProgramFiles\WireGuard\wg.exe" show univpn-lab
```

检查 `data_plane_ready: true`、目标最佳路由属于 `univpn-lab`、近期握手和增长的收发计数。Status 命令本身退出成功、容器 Up 或网卡存在，都不能代替这些检查。

Connect 自带 Windows 普通 TCP banner 检查。再按用户的实际服务做请求；例如已有 HTTP 服务时用 `curl.exe --noproxy "*" http://10.80.0.11:8080/` 排除应用代理，不为了测试擅自部署服务。一个 SSH 探针不能证明所有目标、HTTP、UDP 都通过。

报告实例名、配置位置、实际选路、已通过的业务及未验证项；若仍在等用户输入或授权，明确报告等待阶段。只改本实例命名资源，保留其他 VPN、网卡、密钥及认证会话；不使用 host 网络或接管默认路由。

## 停止与移除

- `-Mode Stop`：停本实例容器，保留专属网卡、路由、密钥和缓存，目标可能继续指向该不可用入口。
- `-Mode Remove`：卸载本实例 Windows tunnel 并执行 Compose down，仍保留密钥文件和缓存卷。

仅在用户请求停止或移除对应实例时执行。更多边界见[快速开始](quickstart.md)、[架构](architecture.md)和[镜像说明](image-release.md)。
