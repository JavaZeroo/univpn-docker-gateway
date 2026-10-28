# 快速开始：Windows 通过 Docker 访问 UniVPN 内网

这条路径面向 **Windows + Docker Desktop/WSL2 + Linux amd64**。先在新克隆的目录中准备配置，再一次启动容器、登录、接入 Windows。已有实例不要覆盖 `.env`、私钥或当前配置。

示例网关 `vpn.example.com`、目标 `10.80.0.11` 均需替换。示例接入网段 `10.203.0.0/30` 必须与本地、其他 VPN 和 Docker 网络不冲突。

## 1. 准备软件和厂商安装包

主机需要 Git、Docker Desktop 的 Linux 容器/Compose，以及[官方 WireGuard for Windows](https://www.wireguard.com/install/)。不需要在 Windows 另装 Python，也不需要额外的 Windows 虚拟机。

厂商安装包需要自行取得：[联软下载页](https://www.leagsoft.com/doc/article/103107.html)。当前运行时只接受已验证的 **10781.21.0.0831 / Linux amd64** 引擎；其他版本不能通过修改摘要强行宣称兼容。仓库不分发厂商安装包或含厂商程序的预制镜像。

```powershell
git clone https://github.com/JavaZeroo/univpn-docker-gateway.git
Set-Location univpn-docker-gateway
New-Item -ItemType Directory -Force vendor, private | Out-Null
Copy-Item .env.example .env
Copy-Item examples/vpn.json private/vpn.json
```

将下载并解出的安装程序放到 `vendor/univpn.run`。下面把本地文件的 SHA256 填入 `.env`，不需要手工抄摘要：

```powershell
$installerSha = (Get-FileHash vendor/univpn.run -Algorithm SHA256).Hash.ToLowerInvariant()
$envText = (Get-Content .env -Raw).Replace('REPLACE_WITH_OFFICIAL_INSTALLER_SHA256', $installerSha)
[IO.File]::WriteAllText((Join-Path (Get-Location) '.env'), $envText, [Text.UTF8Encoding]::new($false))
```

默认 `.env` 使用 `VPN_CONFIG_DIR=./private`、`WG_PORT=51829`。摘要固定本地构建输入，运行时还会核对引擎和辅助程序；它不是任意新版本都能使用的兼容开关。

## 2. 配置 VPN 和真实目标

编辑 `private/vpn.json`：

```json
{
  "gateway": "vpn.example.com",
  "port": 443,
  "vpn_interface": "cnem_vnic",
  "allowed_cidrs": ["10.80.0.11/32"],
  "wireguard_client": "10.203.0.2/32",
  "probe": {"host": "10.80.0.11", "port": 22, "banner_prefix": "SSH-2.0-"}
}
```

`gateway` 只填写 IP 或域名，不带 `https://` 或端口；端口单独填写。`allowed_cidrs` 先用确认需要的 `/32`，`probe` 必须在其中。这里用 SSH banner 检查 VPN，实际转发不限于 SSH。

账号、密码和短信不填在 JSON、`.env` 或命令参数中。网关若使用非公共 CA 的证书，需要独立核验后填写可选的 `approved_certificate_sha256`；不要为让登录通过而全局关闭证书检查。独立预检不是引擎侧同一连接的 pinning，详见[认证研究](native-rpc-research.md)。

## 3. 生成两端 WireGuard 配置

以下 PowerShell 命令适用于表中的默认接入地址与 UDP 端口。它从 `vpn.json` 读取目标 CIDR，避免再手工抄一份 `AllowedIPs`；多实例的地址和端口另见文末。

| 字段 | 容器端 | Windows 端 |
| --- | --- | --- |
| 地址 | `10.203.0.1/30` | `10.203.0.2/32` |
| peer AllowedIPs | 仅 Windows 接入 `/32` | `vpn.json` 的目标 CIDR |
| endpoint | 不固定客户端来源 | `127.0.0.1:51829` |
| MTU | `1280` | `1280` |
| 文件 | `private/wg0.conf` | `private/univpn-lab.conf` |

先限制新目录的权限。这里只操作当前项目的 `private`，允许当前用户、SYSTEM 和管理员读取；不要对其他目录照搬执行：

```powershell
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
icacls.exe private /inheritance:r /grant:r "*${currentSid}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw '无法限制 private 权限，请先处理后继续。' }
```

生成配置时私钥只保存在内存变量与受限文件中，不输出到终端：

```powershell
$wg = Join-Path $env:ProgramFiles 'WireGuard/wg.exe'
if (-not (Test-Path -LiteralPath $wg)) { throw '请先安装官方 WireGuard for Windows。' }
if ((Test-Path private/wg0.conf) -or (Test-Path private/univpn-lab.conf)) { throw '已有密钥配置，停止以免覆盖。' }
$vpnConfig = Get-Content private/vpn.json -Raw | ConvertFrom-Json
if ($vpnConfig.wireguard_client -ne '10.203.0.2/32') { throw '本段示例仅使用默认接入地址，其他地址请同步修改两端模板。' }
$serverPrivate = (& $wg genkey).Trim()
$clientPrivate = (& $wg genkey).Trim()
$serverPublic = ($serverPrivate | & $wg pubkey).Trim()
$clientPublic = ($clientPrivate | & $wg pubkey).Trim()
foreach ($key in @($serverPrivate, $clientPrivate, $serverPublic, $clientPublic)) {
  if ($key -cnotmatch '^[A-Za-z0-9+/]{43}=$') { throw '密钥生成未成功，不写入配置。' }
}
$serverText = (Get-Content examples/wg0.conf -Raw).Replace('REPLACE_WITH_CONTAINER_PRIVATE_KEY', $serverPrivate).Replace('REPLACE_WITH_WINDOWS_PUBLIC_KEY', $clientPublic)
$windowsText = (Get-Content examples/windows.conf -Raw).Replace('REPLACE_WITH_WINDOWS_PRIVATE_KEY', $clientPrivate).Replace('REPLACE_WITH_CONTAINER_PUBLIC_KEY', $serverPublic).Replace('10.80.0.11/32', ($vpnConfig.allowed_cidrs -join ', '))
$encoding = [Text.UTF8Encoding]::new($false)
[IO.File]::WriteAllText((Join-Path (Get-Location) 'private/wg0.conf'), $serverText.Replace("`r`n", "`n"), $encoding)
[IO.File]::WriteAllText((Join-Path (Get-Location) 'private/univpn-lab.conf'), $windowsText.Replace("`r`n", "`n"), $encoding)
Remove-Variable serverPrivate, clientPrivate, serverText, windowsText, key
```

Windows 配置的 endpoint 必须与 `.env` 的 `WG_PORT` 对应；这里都为 `51829`。容器内部固定监听 `51820/udp`。私钥只属于自己这一端，peer 中填写对端公钥，不要交换私钥。[WireGuard 密钥说明](https://www.wireguard.com/quickstart/#key-generation)

## 4. 一次构建，在终端登录

两端文件已准备好后，第一次启动就会自动加载容器的 `wg0.conf`，无需再手动热加载：

```powershell
docker compose build
docker compose up -d
docker compose exec vpn vpnctl login
docker compose exec vpn vpnctl status
```

交互登录不要加 `-T`。输入账号密码；若出现短信挑战，在同一终端输入最新验证码。`status` 需显示 `data_plane_ready: true` 并退出 0，才进入 Windows 安装步骤。新源码由后台统一检查，状态读取有轮询和探针超时延迟。

若先返回终端，稍后再提交本次短信，用 `login --no-otp`，然后 `docker compose exec vpn vpnctl sms`。约 180 秒的挑战只允许一次提交，不重新登录或重发短信。过期、失败或取消后，重启此容器，再显式发起新登录。

## 5. 先计划，再安装 Windows 入口

下面命令默认只读，不创建网卡：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/windows-gateway.ps1 `
  -TunnelConfig private/univpn-lab.conf -GatewayConfig private/vpn.json `
  -Endpoint 127.0.0.1:51829
```

确认目标、客户端地址、endpoint 与现有路由，再安装：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/windows-gateway.ps1 -Mode Install `
  -TunnelConfig private/univpn-lab.conf -GatewayConfig private/vpn.json `
  -Endpoint 127.0.0.1:51829 -Elevate
```

创建系统服务、网卡及路由需要管理员权限；由用户处理 UAC。后台看不到弹窗时，在自己打开的管理员 PowerShell 中执行同一条命令并去掉 `-Elevate`。`ExecutionPolicy Bypass` 只影响这个脚本进程，不修改持久策略。

安装器只管理 `univpn-*` 命名的独立 tunnel，拒绝覆盖已有同名服务或网卡；核验本次新接口身份后设置其 IPv4 ActiveStore metric=0，并检查每个目标的最佳路由。部分失败可能意味着服务已经创建，但路由未就绪；不要反复重装或修改其他 VPN。

## 6. 用原始 IP 验证应用

```powershell
Find-NetRoute -RemoteIPAddress 10.80.0.11
Get-Service 'WireGuardTunnel$univpn-lab'
docker compose exec vpn ip route get 10.80.0.11
docker compose exec vpn wg show wg0 latest-handshakes
docker compose exec vpn wg show wg0 transfer
docker compose exec vpn vpnctl status
ssh your-user@10.80.0.11
curl.exe --noproxy "*" http://10.80.0.11:8080/
```

换成自己实际提供服务的目标和端口。Windows 最佳路由应选 `univpn-lab`，容器出口应为 `cnem_vnic`，WG 握手和双向计数应更新。SSH 首次连接需核对目标主机指纹。UDP 需要远端有对应服务，仅看到 SSH 响应不能替代 UDP 验证。

此后 WindTerm 使用目标 IP、端口 `22`，HTTP 使用原 IP 和服务端口。应用显式指定的代理仍按应用设置运行，Windows 路由不会取消它。完整实测与未覆盖项见[验证记录](subnet-gateway-validation.md)。

## 已认证实例的会话内迁移

只有已经登录、后来才加入 `wg0.conf` 的实例才需要热加载。确认容器内还没有 `wg0`，且目标白名单未改变：

```powershell
docker compose exec vpn sh -c 'install -m 600 /config/wg0.conf /run/univpn/wg0.conf && wg-quick up /run/univpn/wg0.conf'
```

这不会重新创建官方引擎。运行时目前只在启动时读取 `vpn.json`；修改 CIDR 并不自动刷新转发白名单，需要另行维护，不能只改 Windows 的 AllowedIPs。

## 停止、回滚和更新

```powershell
# 仅移除这个 Windows tunnel；此操作需要管理员权限。
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/windows-gateway.ps1 `
  -Mode Uninstall -TunnelConfig private/univpn-lab.conf -Elevate
docker compose stop vpn
```

卸载不删除其他 VPN。若旧 VPN 在线，回滚后可能再次接管目标，需用 `Find-NetRoute` 核对。停止 Docker 而保留 Windows tunnel 时，目标路由可能仍指向当前不可用的入口。

仅重新启动同一个容器时，执行 `docker compose up -d`，然后显式登录。更新源码并应用新镜像需要构建和重新创建，会中断当前认证，可能需要新短信：

```powershell
git pull --ff-only
docker compose build
docker compose up -d --force-recreate
docker compose exec vpn vpnctl login
```

不要把 `docker compose restart` 当作换镜像；它重启原容器。Windows 服务或系统重启后，ActiveStore 优先级是否保留仍需验收，不能承诺“一次设置永久生效”。

## 多实例要同步的字段

用独立 env 文件与 `docker compose --env-file ... -p vpn-a ...` 区分实例。`VPN_CONFIG_DIR`、`WG_PORT`、Windows tunnel 名、接入地址段和两套密钥分别独立；更换客户端地址时，同步 `vpn.json`、容器 peer AllowedIPs 与 Windows Address，更换端口时同步 env 和 Windows Endpoint。

目标 CIDR 在 Windows peer 与 `vpn.json` 中必须一致。不同 VPN 的目标地址重叠时，当前无法同时区分；不要仅依靠两个容器已有隔离就同时导入重叠的 Windows 目标路由。详见[架构](architecture.md)和[简化分析](simplification.md)。

## 常见现象

| 现象 | 先看什么 |
| --- | --- |
| 正在等待短信 | 用当前会话 `vpnctl sms` 续交，别重复登录 |
| AUTH 成功但不 ready | 真实探针、`cnem_vnic`、目标路由；网卡存在不是业务可达 |
| Windows 显示已连接却走旧 VPN | `Find-NetRoute`，比较相同前缀的 route + interface metric |
| CIM 的 metric 为 null | 不直接推断为 0；用只读 `netsh interface ipv4 show interfaces interface=univpn-lab level=verbose store=active` 交叉核对 |
| 没有 Docker 日志 | 默认 `logging.driver=none`；使用脱敏 `vpnctl status` 和网络状态诊断 |
| 小请求通、大包卡住 | 核对两段 MTU 与实际传输；不要只看一次握手 |
| 证书不受信任 | 核对可信 CA 或已独立批准的精确叶摘要，不全局关闭校验 |
