# 快速开始

先安装并启动 Windows x64 的 [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/)（Linux 容器 / WSL2）。启动器会按需安装官方 WireGuard；已安装且签名有效时直接复用，不升级。无需 Git、Python 或本机构建。

希望交给本机 Agent 操作时，使用[Agent 部署指南](agent-setup.md)；它支持先生成配置，再由用户完成终端认证。

## 首次连接：两条命令

在 Windows 新建空文件夹，在该文件夹打开 PowerShell：

```powershell
docker run --rm -v "${PWD}:/output" javazero/univpn-docker-gateway:latest export-client
.\univpn\univpn.cmd
```

第一条将启动器导出到当前 Windows 文件夹的 `univpn` 子目录；第二条在同一个 PowerShell 窗口运行这个 `.cmd` 脚本。`.\` 表示当前文件夹，全部命令在 Windows 执行。

依次填写网关（如 `vpn.example.com:443`）、目标 CIDR（如 `10.80.0.11/32`）和用于检查真实连通的 SSH 服务器。随后在终端输入 VPN 账号、密码与本次短信；安装 WireGuard 和创建专属 Windows 网卡可能分别需要 UAC 确认。若系统要求重启，先完成重启，再继续连接；启动器不会自动重启电脑。

启动器自动生成双端密钥与配置、启动容器、等待官方组件准备、保留短信会话、安装或复用本实例网卡，并验证 Windows 到目标的实际 banner。探针用 SSH，转发也支持 HTTP 和 UDP。

镜像首次从官方下载并校验固定版本组件，约 30 MB；以后使用 Docker 卷中的已验证缓存。私钥留在受限目录，账号密码和验证码不写配置。当前支持 Linux amd64 / UniVPN **10781.21.0.0831**。

## 连接后的常用命令

```powershell
.\univpn\univpn.cmd                   # 连接；已有短信挑战会续交
.\univpn\univpn.cmd -Mode Status      # 状态和最佳路由
.\univpn\univpn.cmd -Mode Stop        # 停容器，保留网卡、密钥和缓存
.\univpn\univpn.cmd -Mode Remove      # 移除本实例网卡和容器，保留文件及缓存
```

连接后，应用填写服务器原始 IP 和端口，例如 `ssh your-user@10.80.0.11` 或 `http://10.80.0.11:8080/`。应用自身配置的 HTTP/SOCKS 代理仍需另查。

## 再加一套 VPN

新实例必须使用不同的名称、UDP 端口及接入网段：

```powershell
.\univpn\univpn.cmd -Name work -UdpPort 51830 -AccessSubnet 10.204.0.0/30
```

以后加 `-Name work` 即可操作该实例；端口和网段已保存。远端目的 IP 重叠时不能靠容器隔离同时区分。

## Windows 安装范围

WireGuard 位于 Windows 的标准安装目录 `C:\Program Files\WireGuard`；`univpn-*` 是独立的隧道服务与网卡。容器内的 `wg0` 是另一端的 Linux 接口。启动器不停止或重装既有 WireGuard；部分安装、异常同名服务会先报错。目标 CIDR 重叠时仍会影响实际选路。

## 遇到问题

- **短信过期或失败：** 不自动重发；显式 `Stop` 后重新连接。存在有效挑战时会执行 `sms`，不会重复 `login`。
- **证书不受信任：** 独立核实叶证书 SHA256 后，写入本实例 `vpn.json` 的 `approved_certificate_sha256`，再显式重新连接。不全局关闭检查。[证书边界](native-rpc-research.md)
- **已有网关 JSON：** 首次运行可加 `-GatewayConfig .\vpn.json`，保留其网关、目标、探针和已核验的证书配置。
- **首次下载失败：** 检查容器访问官方站点的网络；失败不会使用未校验文件。[缓存、固定版本与升级](image-release.md)
- **容器已通、Windows 未通：** 查看 `Status` 的最佳路由和本实例网卡；启动器不会覆盖其他 VPN。`Stop` 保留路由并阻断本实例流量，需要释放入口则用 `Remove`。

完整实现和依赖见[架构](architecture.md)，手工构建仅供开发者，见[镜像说明](image-release.md)。
