# 网段网关验证（2026-10-28 至 2026-10-29）

## 验收范围

本轮已验证 Linux WireGuard 客户端及 Windows 普通连接 → Docker 网关 → 真实 UniVPN → HiDev。Windows 新服务已安装，同前缀选路竞争已解决；localhost UDP 发布路径、默认选路和真实业务均已通过。正式 UniVPN 会话未重启，也没有重新发送短信。当前范围仅为已配置的四个目标 `/32`。

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

## Windows 独立验收与选路竞争

用户在管理员 PowerShell 执行安装后，`WireGuardTunnel$univpn-hidev` 为 Running，网卡 Up、地址为 `10.203.0.2/32`、MTU=1280；原有另一个 WireGuard 服务仍 Running。仅有这些状态不能证明默认业务流量使用新入口。

安装后读到同一目标的两条 `/32`，route metric 都为 0：原 VPN 网卡的 interface metric=1，新 WireGuard 自动 metric=5。`Find-NetRoute` 确认普通连接仍选择原 VPN，不能把直接 SSH 成功当作新入口通过。

先在 Windows 测试 socket 设置 `IP_UNICAST_IF` 为实时查询的新接口编号，并绑定其源地址，绕开默认选路歧义，仅影响测试连接：

| Windows 指定接口测试 | 结果 |
| --- | --- |
| SSH banner | 4/4 正常 |
| HTTP 下载、上传 | 各 1,048,576 字节，长度与摘要正确 |
| UDP echo | 32、1200、4096 字节测试内容均正确返回 |
| 真实入口 | Windows → `127.0.0.1:51829/udp` → Docker WireGuard；握手有效，双向计数增长超过传输数据量 |
| 远端来源 | 正式容器 UniVPN 地址 |
| 探针失败/恢复 | Windows 指定接口的 SSH、HTTP、UDP 全部阻断，恢复后全部通过；未重新认证 |

这一步覆盖真实 Windows 网络栈和 localhost UDP 发布路径，但还不能证明普通连接已默认切换。随后由用户在管理员窗口仅将新网卡设为 IPv4 ActiveStore metric=0、关闭自动 metric；自动化进程自身仍未提权，没有修改其他网卡。

### 2026-10-29：普通 Windows 默认选路验收

四个目标的 `Find-NetRoute` 均命中 `univpn-hidev` 及其精确 `/32`。本机 CIM 对 metric=0 呈现为 null，因此另用只读 `netsh interface ipv4 show interfaces interface=univpn-hidev level=verbose store=active` 独立确认 `Metric: 0`、`Use Automatic Metric: disabled`、MTU=1280；不能将其他场景的 null 一律当作零。

本轮移除测试中的 `IP_UNICAST_IF` 和源地址绑定，使用默认 `socket.create_connection`、`http.client` 与普通 UDP socket，未使用 HTTP/SOCKS 代理：

| 普通 Windows 默认连接 | 结果 |
| --- | --- |
| SSH banner | 4/4 正常，系统自动选择 WireGuard 源地址 |
| HTTP 下载、上传 | 各 1,048,576 字节，长度与摘要匹配 |
| UDP echo | 32、1200、4096 字节测试内容及前缀均正确返回 |
| 真实路径 | 四个最佳路由均为新接口；容器 WireGuard 有近期握手及双向传输增长；远端看到正式容器 UniVPN 地址 |
| 探针失败 | 普通 SSH、HTTP、UDP 全部失败，没有经仍在线的旧 Windows VPN 返回响应 |
| 探针恢复 | 所有测试再次通过；四条最佳路由仍为新接口，无需新验证码 |

这证明已配置四个原始目标 IP 的多协议默认访问，不需要逐服务映射 localhost 端口。应用显式指定代理的行为不由这次普通 socket 测试覆盖，临时远端测试服务在结束后清理。

Windows 服务重启、系统重启后是否保留接口优先级尚未验收。DNS 分流、其他 VPN 的重叠目的地、真实厂商断线重连和 Docker 重启恢复也仍未验收。

## 安装器和健康探测的回归修复

安装器原版仅创建服务，不能仅凭这一结果判断普通应用已改走新链路。修正版只接受安装前不存在、本次新建且身份确认的 WireGuard 网卡；设置该接口的 IPv4 ActiveStore metric=0 后，读回设置并逐目标检查最佳选路。已有同名服务或网卡不自动覆盖，部分失败明确报告“可能已安装但选路未就绪”。Windows PowerShell 5.1/7 的 21 项模拟检查通过；本轮已存在服务仍由单独的管理员命令调整，不伪称重新执行过修正版的完整安装。

同时在受控内存测试中复现健康检查竞争：状态请求先占用探针锁，后台将“探针正忙”返回的 false 当作失败，触发不必要的 disable/enable。原 dispatcher 在重复强制并发中 20/20 复现，期间实际探针始终返回成功。该重配会清零规则计数，但不能据此认定本机每次计数归零都由同一竞争造成。

源码修复让状态请求只读已完成的探针结果，由后台独占主动探测。新增 6 项真实 dispatcher/守卫 call-site 回归测试，涵盖并发状态查询、真实探针失败、探针期间引擎死亡及原认证命令分发；旧 dispatcher 对照会触发回归失败。全量 Python 单元测试共 120 项通过。

状态因此存在后台轮询及探针超时带来的更新延迟。真实失败仍会阻断，接口出口限制继续阻止普通 eth0 回落。**当前已连接的旧容器尚未替换，此源码修复还未在该会话中生效**；应用新镜像需要维护窗口重建容器并重新认证。
