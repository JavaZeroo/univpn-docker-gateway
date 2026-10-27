# 验证记录

2026-10-27，初始实验版本。

- Windows Python 3.12：15 项单元测试通过，覆盖认证控制、配置校验和防火墙规则生成。
- 这些测试不执行厂商认证，不能证明短信登录可用。
- Linux 网络集成测试使用独立 namespace 和 veth 模拟隧道接口，测试 TCP、HTTP、UDP/SNAT、目标白名单及普通出口回落阻断。测试通过也不代表 WireGuard 或 UniVPN 协议已通过。
- GitHub Actions 首轮 unit/network 均通过：[运行记录](https://github.com/JavaZeroo/univpn-docker-gateway/actions/runs/40493776513)。网络测试包括真实 SSH banner、HTTP、UDP/SNAT、白名单阻断、网关停用、普通出口回落阻断及无关规则保留。
- 加入真实 Qt/DBus/AT-SPI 集成测试后，unit/network/accessibility 三项均通过：[运行记录](https://github.com/JavaZeroo/univpn-docker-gateway/actions/runs/40493970548)。Qt 使用自建窗口，已验证密码编辑、验证码窗口识别及提交；不代表官方 UniVPN 暴露相同控件或能够短信认证。
- 厂商安装包只在本地 vendor 中存在，Git 忽略；版本来源为先前从厂商取得的 Linux 21 安装程序。公开仓库没有安装包或运行凭据。
- 后续启动 Docker 时引擎一度卡住；随后恢复可用，纯 CLI 镜像已构建并运行。未验证厂商 GUI 控件或 Windows 虚拟网卡端到端连接。
- 已在 WSL 独立网络和挂载命名空间执行官方 10781.21.0.0831 的纯 CLI HiDev 认证：`The dynamic token code is required.` → `ssl connect failed! reason: login failed` → 主菜单。没有验证码输入提示。详见 [纯 CLI 实测](cli-mfa-proof.md)。
- 同一结果已在新建 Ubuntu 22.04 纯 CLI Docker 镜像中复现；两个环境均未启动图形组件。菜单空闲资源、进程和包清单检查记录在上述实测文档。

后续更新必须区分：测试替身、隔离的 Linux 转发集成、厂商真实认证、Windows 真实访问、资源测量。不能把某一层通过表述为整体已可用。
