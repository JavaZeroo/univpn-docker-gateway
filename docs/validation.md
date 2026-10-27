# 验证记录

2026-10-27，初始实验版本。

- Windows Python 3.12：15 项单元测试通过，覆盖认证控制、配置校验和防火墙规则生成。
- 这些测试不执行厂商认证，不能证明短信登录可用。
- Linux 网络集成测试使用独立 namespace 和 veth 模拟隧道接口，测试 TCP、HTTP、UDP/SNAT、目标白名单及普通出口回落阻断。测试通过也不代表 WireGuard 或 UniVPN 协议已通过。
- GitHub Actions 首轮 unit/network 均通过：[运行记录](https://github.com/JavaZeroo/univpn-docker-gateway/actions/runs/40493776513)。网络测试包括真实 SSH banner、HTTP、UDP/SNAT、白名单阻断、网关停用、普通出口回落阻断及无关规则保留。
- 厂商安装包只在本地 vendor 中存在，Git 忽略；版本来源为先前从厂商取得的 Linux 21 安装程序。公开仓库没有安装包或运行凭据。
- 本机 Docker 引擎当前未运行，桌面控制工具不可连接；已请求启动 Docker，真实镜像构建、AT-SPI 控件与短信登录、Windows 虚拟网卡端到端验证待进行。

后续更新必须区分：测试替身、隔离的 Linux 转发集成、厂商真实认证、Windows 真实访问、资源测量。不能把某一层通过表述为整体已可用。
