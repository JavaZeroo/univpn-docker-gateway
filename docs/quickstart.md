# 快速开始

先安装并启动 Windows x64 的 [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/)（Linux 容器 / WSL2）。无需 Git、Python 或本机构建。

| 需求 | 版本与教程 |
| --- | --- |
| 只运行 Docker，应用可以配置 SOCKS5，只用 IPv4 TCP | [Lite：一条命令](lite.md) |
| 应用直接填写内网 IP，需要 TCP / UDP 或无需逐应用设置 | 下方 Full 教程 |

镜像标签为 `lite` / `full`，固定版本为 `0.4.0-lite` / `0.4.0-full`；`latest` 和 `0.4.0` 兼容指向 Full。希望交给 Agent 时，使用[部署指南](agent-setup.md)。

## Full 首次连接：两条命令

在 Windows 新建空文件夹，在该文件夹打开 PowerShell：

```powershell
docker run --rm -v "${PWD}:/output" javazero/univpn-docker-gateway:full export-client
.\univpn\univpn.cmd
```

第一条将启动器导出到当前文件夹的 `univpn` 子目录；第二条在同一个 PowerShell 窗口运行它。启动器按需准备官方 WireGuard，已有签名有效的安装会复用，不升级。

依次填写网关（如 `vpn.example.com:443`）、目标 CIDR（如 `10.80.0.11/32`）和真实 SSH 探针，再在终端输入账号、密码与本次短信。WireGuard 安装和专属网卡创建可能分别需要 UAC；若要求重启，先完成重启，启动器不会自动重启电脑。

启动器生成密钥与配置、启动容器、登录或续交短信、安装或复用本实例网卡，并检查一个 Windows 到目标的真实 SSH banner。厂商组件首次从官方获取并校验，缓存保留在 Docker 卷中；凭据不写配置。当前支持 Linux amd64 / UniVPN **10781.21.0.0831**。

## 日常使用与多实例

```powershell
.\univpn\univpn.cmd                   # 连接；已有短信挑战会续交
.\univpn\univpn.cmd -Mode Status      # 状态和最佳路由
.\univpn\univpn.cmd -Mode Stop        # 停容器，保留网卡、密钥和缓存
.\univpn\univpn.cmd -Mode Remove      # 移除本实例网卡和容器，保留文件及缓存
.\univpn\univpn.cmd -Name work -UdpPort 51830 -AccessSubnet 10.204.0.0/30
```

最后一条创建另一套 VPN；新实例需不同名称、UDP 端口和接入网段，以后加 `-Name work` 即可操作它。目标网段由用户明确选择，不使用默认路由。

连接后直接使用 `ssh your-user@10.80.0.11` 或 `http://10.80.0.11:8080/`。Windows WireGuard 位于 `C:\Program Files\WireGuard`，本项目只新增 `univpn-*` 隧道；容器内 `wg0` 是另一端接口。原有隧道保持运行，但目标 CIDR 重叠仍会改变实际选路，不能同时区分两套 VPN 中相同的目标 IP。

## 遇到问题

- **短信过期或失败：** 不自动重发；显式 `Stop` 后重新连接。有效挑战续交 `sms`，不要反复 `login`。
- **证书不受信任：** 独立核实叶证书 SHA256 后，写入本实例 `vpn.json` 的 `approved_certificate_sha256`，再显式重新连接。不全局关闭检查。[证书边界](native-rpc-research.md)
- **已有网关 JSON：** 首次加 `-GatewayConfig .\vpn.json`，保留网关、目标、探针和已核验的证书配置。
- **首次下载失败：** 检查容器访问官方站点的网络；不使用未校验文件。[组件缓存](image-release.md)
- **容器已通、Windows 未通：** 查看 `Status` 的最佳路由和本实例网卡。`Stop` 保留路由，需释放入口时用 `Remove`。

Full 的真实数据链路证据见[验证记录](subnet-gateway-validation.md)，实现见[架构](architecture.md)。Lite 的应用代理与测试边界单独见[Lite 指南](lite.md)；Docker Desktop / WSL2 的平台开销不会因版本拆分消失。
