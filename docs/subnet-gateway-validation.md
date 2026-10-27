# 网段网关验证（2026-10-28）

## 验收范围

本轮已验证 Linux WireGuard 客户端 → Docker 网关 → 真实 UniVPN → HiDev。Windows 入口仍待管理员安装与端到端验证。正式 UniVPN 会话未重启，也没有重新发送短信。

真实目标地址、网关凭据、密钥和短信内容仅保留在本地忽略目录中；下文仅记录实验方法和结果。

## 真实 VPN 数据面

正式容器原有 `NET_ADMIN`、IPv4 转发、localhost UDP 端口发布及按目标 CIDR 的 FORWARD/NAT 规则。当前健康状态为已认证且实际目标探针通过。向只读配置目录添加本地 `wg0.conf`，复制到容器 `/run/univpn` 后执行 `wg-quick up`，成功热加载，无需重启官方引擎。

创建临时、单独网络命名空间的 Linux 客户端容器，使用真实 WireGuard peer 和四个精确目标 `/32`。它的 UDP endpoint 直连 Docker bridge 内的网关地址，因此**这一阶段不包含 Windows 网络栈或 Windows localhost UDP 发布路径**。

在一台已有授权的目标上临时启动只返回固定测试数据的 HTTP/UDP 服务，限定运行时间并在结束后清理，不读取业务文件。对照客户端使用原始 TCP/UDP socket 和 Python `http.client`，未经过 HTTP/SOCKS 代理。

| 测试 | 结果 |
| --- | --- |
| 4 台服务器的 SSH banner | 4/4 正常 |
| HTTP 下载 | 1,048,576 字节，内容摘要匹配 |
| HTTP 上传 | 1,048,576 字节，服务端长度与摘要匹配 |
| UDP echo | 32、1200、4096 字节测试内容均正确返回；另有测试前缀 |
| 源地址转换 | 远端观察到正式容器 UniVPN 地址，非 Windows 原生 VPN 地址 |
| WireGuard | 真实握手及双向收发计数增长 |

真实 `cnem_vnic` MTU=1300；测试 peer 使用 MTU=1280。大于该 MTU 的 UDP 内容在本次路径中分片并正常往返，不能据此推断所有远端路径都支持相同数据包大小。

## 健康门控失效与恢复

保持 WireGuard 激活，仅在正式容器添加带独立注释的临时 OUTPUT 规则，拒绝已配置 VPN 探针的 TCP 端口。厂商认证连接和其他目标路由没有被关闭。测试控制器在 `finally` 删除该规则，另有独立限时清理兜底。

守卫检测探针失败后，将专属转发链改成 DROP。四个 SSH 目标、HTTP 和三种 UDP 内容全部不可达。恢复探针后，守卫自动恢复转发，完整协议测试再次通过，原认证保持，无需新验证码。

这证明“业务探针失败后转发关闭并恢复”，**尚不等于厂商 VPN 真正断开、重新认证或 Docker 重启后的恢复验证**。

## 可重复的真实 WireGuard 隔离测试

`tests/wireguard_integration.py` 在外层新建 network/mount namespace，再建立实际 kernel WireGuard 客户端和网关接口。只有 VPN 段和普通出口由 veth 模拟，不运行厂商客户端、不访问真实环境。

一次性隔离容器内运行通过，耗时约 6 秒：

- SSH-like TCP、完整 1 MiB HTTP 下载及 SHA256、UDP 内容与 SNAT 来源均通过。
- 未列入的目标被阻断，手动 disable 后阻断、enable 后恢复。
- 先临时用窄规则证明 WireGuard 客户端能经普通出口收到响应，再撤除窄规则，确认正式出口限制阻断 TCP/UDP 回落；恢复 VPN 路由后再次连通。
- 重复 enable 不重复插入 hook，其他命名链保持不变。
- 最后检查真实 WireGuard 握手及收发量，避免只靠接口名认定使用了 WireGuard。

测试脚本使用限时预算及 finally 清理命名空间、子进程，私钥通过 0600 临时文件及 stdin 传递，不输出。运行需要 root、WireGuard 内核支持及网络命名空间权限；只能放在专用一次性测试环境，不能针对正式 VPN namespace 运行。

## Windows 边界

当前独立客户端配置、只读计划、安装与专属回滚脚本已准备。Windows PowerShell 5.1 和 PowerShell 7 的 16 项模拟检查通过，未在测试中安装服务。

Windows 真实验收需管理员创建命名的 WireGuard 服务，并同时核对：四条 `/32` 最佳路由命中新网卡、真实 UDP/WireGuard 握手、容器 WireGuard 和 UniVPN 转发计数、远端看到容器 VPN 源地址，以及 HTTP/UDP/大包的真实响应。现有 Windows VPN 仍可提供旧路径，因此“SSH 能连上”本身不能作为新入口的成功证据。

Windows 结果确认前，保留现有访问方式。DNS 分流、多个 VPN 的重叠目标网段、真实断线重连和 Docker 重启恢复仍未验收。
