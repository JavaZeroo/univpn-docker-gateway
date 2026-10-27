# 验证记录

2026-10-27。本记录区分原型、正式模块、模拟网络和真实接入链路。

## 当前结论

| 验证层 | 实际结果 | 尚未证明的内容 |
| --- | --- | --- |
| 正式模块单元测试 | 当前 114 项通过；包括原生传输、引擎生命周期、单次短信会话、证书策略、控制接口、配置与规则生成 | 使用模拟对象；正式容器的真实认证证据另列 |
| 无 GUI 原型真实 HiDev 认证 | 一次账号密码提交后，在同一引擎会话续交用户短信验证码，收到 `0x00020000`；请求隧道启动后收到 `0x00070000` | 原型与正式容器的实测记录分别保留 |
| 原型真实 VPN 数据面 | 4 台内网服务器的真实 SSH banner 通过 | 不证明 Windows WireGuard 接入或 HTTP/UDP、MTU、大文件链路已经验证 |
| 正式 native 容器认证与数据探针 | 默认 Compose 路径真实短信通过；`authenticated`、`tunnel_started`、`data_plane_ready` 均为 `true`，最后响应 class `5`、code `0x00070000`；配置的 SSH 业务探针及 4/4 台服务器 SSH banner 成功 | 不代表 Windows WireGuard 网段整条链路或 HTTP/UDP、MTU、大文件已经验证 |
| Linux 独立网络集成 | namespace/veth 模拟隧道，TCP/HTTP/UDP、SNAT、白名单、普通出口回落阻断等通过 | 不包含真实 WireGuard、UniVPN 或 Windows |
| Windows WireGuard 网段接入 | 配置模板已提供 | 整条真实链路仍待端到端实测 |
| 原型资源 | 单次 `docker stats --no-stream` 样本 14.99 MiB | 不包括 Docker Desktop/WSL 固定平台成本，不是稳态、峰值或正式镜像保证 |
| 正式容器资源 | 已连接后的单次样本 18.8 MiB / 384 MiB、CPU 1.26%；Qt/Xvfb/DBus/AT-SPI/VNC 包为空，仅 tini/Python/官方辅助服务/UniVPNCS | 不包括 Docker Desktop/WSL 固定平台成本，不是稳态或峰值保证 |

原型只运行独立 Python 前端、官方辅助服务与 UniVPNCS，没有 GUI、Qt、X11、Xvfb、DBus、VNC/noVNC。没有修改厂商二进制、伪造成功返回码或跳过短信流程。真实认证结果见[原生后端研究](native-rpc-research.md)；公开记录不保留真实账号、密码、验证码、网关或服务器地址。

## 当前运行与判定方式

默认 Compose 已使用 `native` 后端，不是旧 CLI 菜单。用户在本地取得并校验 **10781.21.0.0831** 安装程序后构建：

```powershell
docker compose build
docker compose up -d
docker compose exec vpn vpnctl login
docker compose exec vpn vpnctl status
```

`login` 在同一 TTY 等待短信验证码。也可用 `login --no-otp` 返回后，再运行 `docker compose exec vpn vpnctl sms` 续交现有挑战；不会重新发送密码或自动重发短信。过期、失败或取消后，运行 `docker compose restart vpn`，再显式 `login` 开始一次新会话。

认证返回成功、网卡存在和引擎存活都不是业务就绪的充分条件。只有真实业务探针通过，`vpnctl status` 的 `data_plane_ready` 才为 `true` 并以状态码 0 退出；后台转发守卫也依据该健康结果启停转发。例如可使用文档示例地址 `10.80.0.11:22` 的 `SSH-2.0-` banner，不需在公开仓库记录真实目标。

证书默认验证 CA 链和主机名。可选的 `approved_certificate_sha256` 只接受用户明确批准的 64 位十六进制叶证书摘要，端点与摘要一致时才允许本次登录 payload 的证书例外；不修改系统证书。独立 TLS 预检与后续引擎连接之间仍有连接绑定边界，**这不是已经实现的引擎侧 pinning**。

## 历史对照证据

- 初始 Windows Python 3.12 的 15 项单元测试通过，覆盖早期认证控制、配置和防火墙规则。这是历史数量，当前模块汇总为上表的 114 项。
- GitHub Actions 首轮 unit/network 均通过：[运行记录](https://github.com/JavaZeroo/univpn-docker-gateway/actions/runs/40493776513)。独立网络测试包括真实 TCP banner、HTTP、UDP/SNAT、白名单阻断、网关停用、普通出口回落阻断及无关规则保留；隧道由 veth 模拟。
- 历史 Qt/DBus/AT-SPI 集成测试加入后，unit/network/accessibility 三项均通过：[运行记录](https://github.com/JavaZeroo/univpn-docker-gateway/actions/runs/40493970548)。Qt 使用自建窗口，只验证早期控件桥；不属于默认 native 后端的短信认证证据。
- 官方 10781.21.0.0831 的旧 CLI 菜单分别在 WSL 隔离命名空间和 Ubuntu 22.04 Docker 内实测：要求动态口令后失败并返回主菜单，没有验证码输入流程，见[旧菜单实测](cli-mfa-proof.md)。这不否定后续 CSDK 路径的无 GUI 短信成功。
- 安装包仅在本地 `vendor/` 中存在，Git 忽略；运行时核对已验证版本的引擎和辅助程序 SHA256。本项目不发布含厂商二进制的安装包或镜像，本仓库自行编写的源码采用 GPL-3.0-only。

后续更新仍须区分：测试替身、隔离的 Linux 转发集成、原型真实认证、正式容器真实认证、Windows 真实访问和资源测量。某一层通过不能代替其余各层的验收。
