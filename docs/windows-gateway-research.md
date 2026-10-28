# Windows 网段网关接入：机制、实测与执行边界

2026-10-28。本文件记录官方机制及本机准备、安装和选路冲突；实际实验见[网段网关验证](subnet-gateway-validation.md)。Windows 指定接口到真实 VPN 已通过，普通应用的默认选路仍需修正。

## 现在可准备什么

沿用 [Windows 模板](../examples/windows.conf)和[容器模板](../examples/wg0.conf)，另建 `univpn-hidev`，不复用 `huawei-new` 的密钥。审阅版保留密钥占位符；正式部署时在本地生成两套新密钥，只交换公钥，私钥保存在权限受限目录，不输出到终端、Git 或日志。

| 项目 | 审阅值 |
| --- | --- |
| Windows 接入地址 | `10.203.0.2/32` |
| 容器 wg0 地址 | `10.203.0.1/30` |
| Windows endpoint | `127.0.0.1:51829`，须与实际发布端口一致 |
| Windows AllowedIPs | 先用 `10.80.0.11/32` 示例；实际四个业务目标分别写四条 `/32`，并与 `vpn.json` 白名单一致 |
| 容器 peer AllowedIPs | 仅 Windows 接入地址 `10.203.0.2/32` |
| DNS / 默认路由 | 不设置 `DNS`、`0.0.0.0/0` 或 `/1`；首版直接用业务 IP |

Compose 已显式发布 localhost UDP。Docker Desktop 通过后台进程把宿主机发布端口送入 Linux VM；`/udp` 不能省略。这能提供网段隧道的入口，并不把容器桥接 IP 变成 Windows 下一跳。[Docker Desktop 网络](https://docs.docker.com/desktop/features/networking/)、[端口发布规则](https://docs.docker.com/engine/network/port-publishing/)

安装前快照：WireGuard 1.1.1 已安装；`huawei-new`、Mihomo、Windows UniVPN TAP 均在运行；localhost UDP 51829 由 Docker 后台监听。拟用接入地址与已检查的本机专用路由、Docker bridge 无重叠。此时目标走旧 Windows UniVPN 的宽前缀。安装后的新证据和同长度路由竞争见下文，不能继续用安装前快照判断选路。

## 管理员执行边界

当前进程不是管理员，申请 `SC_MANAGER_CREATE_SERVICE` 权限实测返回 `5 / Access denied`。配置审阅、查路由不需提权；创建新 tunnel 服务需管理员进程。UAC 同意或管理员凭据只能由用户在系统界面完成。[Microsoft 服务权限](https://learn.microsoft.com/en-us/windows/win32/services/service-security-and-access-rights)、[UAC 工作方式](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/user-account-control/how-it-works)

用户已通过辅助脚本的 Install 模式创建新服务，新网卡已 Up；自动化进程本身仍未提权。以下保留的是另一种维护窗口命令草案；其中 `Set-Service ... Manual` 并未在本轮执行：

```powershell
# 在已由用户打开的管理员 PowerShell 中；配置必须已完成本地审阅。
$TunnelConfig = (Resolve-Path '.\private\univpn-hidev.conf').Path
& 'C:\Program Files\WireGuard\wireguard.exe' /installtunnelservice $TunnelConfig
Set-Service -Name 'WireGuardTunnel$univpn-hidev' -StartupType Manual
```

安装命令会立即启动新 tunnel，官方实现默认还会设置自动启动；上面的草案另将这个新服务改为手动，适合仅在维护窗口试验。辅助脚本的安装模式保持官方默认行为，配置的目标会在每次服务启动后被接管，直到 Docker/VPN 就绪才恢复业务。直接安装服务引用的配置路径应保持可用。另一种官方方式是管理员导入管理器，由其加密保存配置；不要混用两种配置管理方式。[WireGuard 服务说明](https://git.zx2c4.com/wireguard-windows/about/docs/enterprise.md)、[安装实现](https://git.zx2c4.com/wireguard-windows/tree/manager/install.go)、[Set-Service](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.management/set-service)

当前 [runtime](../univpn_gateway/native_service.py)仅在启动时自动读取 `/config/wg0.conf`；重启会重置短信会话。本轮已实测手工热加载 `wg0` 并保留已认证引擎，见[具体步骤和结果](../README.md#网段转发实验)。目标 allowlist 仍由运行时启动时加载，热加载接口不代表目标配置会自动刷新。

## 共存、验收与回滚

WireGuard Windows 会从 AllowedIPs 创建路由；Windows 优先最长匹配，因此业务 `/32` 可覆盖旧 VPN 的宽路由。先检查同长度路由和地址冲突，不删除旧路由、不调整全局 metric。管理器启动 tunnel 时可能停止地址或路由相交的其他 WireGuard tunnel，故新旧配置应先核对不重叠。[WireGuard 路由行为](https://git.zx2c4.com/wireguard-windows/about/docs/netquirk.md)、[Microsoft 路由选择](https://learn.microsoft.com/en-us/windows-hardware/customize/desktop/unattend/microsoft-windows-tcpip-interfaces-interface-routes-route-metric)、[管理器实现](https://git.zx2c4.com/wireguard-windows/tree/manager/ipc_server.go)

本轮安装后实际出现旧 VPN 和新 WG 都有 `/32` 的情况，route metric 同为 0，但旧接口 metric=1、新 WG 自动 metric=5，`Find-NetRoute` 仍选旧接口。最长前缀相同时还需比较 route + interface metric。WireGuard Windows 1.1.1 自身在默认路由场景使用 interface metric=0，证明 0 是受支持值；本轮拟仅将新 WG 改为 0，以优先于旧接口的 1。[Microsoft interface metric](https://learn.microsoft.com/en-us/powershell/module/nettcpip/set-netipinterface#-interfacemetric)、[WireGuard 1.1.1 接口配置](https://git.zx2c4.com/wireguard-windows/plain/tunnel/addressconfig.go?h=v1.1.1)

```powershell
# 管理员执行：只改本次新建的接口，随后重新查询每个目标的 Find-NetRoute。
Set-NetIPInterface -InterfaceAlias 'univpn-hidev' -AddressFamily IPv4 -AutomaticMetric Disabled -InterfaceMetric 0 -PolicyStore ActiveStore
# 撤销此接口优先级调整，恢复自动选择。
Set-NetIPInterface -InterfaceAlias 'univpn-hidev' -AddressFamily IPv4 -AutomaticMetric Enabled -PolicyStore ActiveStore
```

ActiveStore 表示当前活动配置，不应承诺跨重启持久。非默认路由的 WG 配置不会主动把自动 metric 改回 true，但服务停止/启动会关闭和重建网卡；必须另外验证重启后的实际值，不能固定缓存接口编号。[Microsoft PolicyStore](https://learn.microsoft.com/en-us/powershell/module/nettcpip/set-netipinterface#-policystore)、[WG 服务生命周期](https://git.zx2c4.com/wireguard-windows/plain/tunnel/service.go?h=v1.1.1)

只读预检及启用后的复核命令：

```powershell
$Target = '10.80.0.11' # 替换为已授权的实际业务目标
Find-NetRoute -RemoteIPAddress $Target |
    Select-Object IPAddress,DestinationPrefix,InterfaceAlias,NextHop,RouteMetric
Get-NetIPInterface -AddressFamily IPv4 |
    Select-Object InterfaceAlias,NlMtu,InterfaceMetric
Get-Service -Name 'WireGuardTunnel$huawei-new','WireGuardTunnel$univpn-hidev' -ErrorAction SilentlyContinue
```

`Find-NetRoute` 同时返回源地址与最佳路由。启用后须确认命中新 tunnel，并结合握手/收发计数、容器 `cnem_vnic` 选路与实际 SSH/HTTP/UDP 请求；“仍能 SSH”单独不能证明走了新入口。检查计数时不使用会导出配置或密钥的 `showconf` / `dump`。[Find-NetRoute](https://learn.microsoft.com/en-us/powershell/module/nettcpip/find-netroute)

MTU 要分段实测：WireGuard 在容器解封装后才转入 UniVPN，不应从 1500 机械叠扣两种开销。WireGuard Windows 默认 MTU 根据普通默认出口减 80，未必反映下游 VPN 瓶颈。本机已读值为 Mihomo 1500、旧 TAP 1400、`huawei-new` 1420，均不能代替新链路测量。先读取新接口与容器 VPN MTU，再用较大业务响应/下载验证；目标允许 ICMP 时，可辅助执行 `ping.exe -4 -f -l 1200 $Target` 并调整尺寸，失败也可能是 ICMP 被禁，不能据此断言 MTU 错误。[WireGuard MTU 规则](https://git.zx2c4.com/wireguard-windows/about/docs/netquirk.md)、[Microsoft DF ping](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/ping)

故障阻断测试应保持新 Windows tunnel 激活，只令测试容器的 UniVPN 出口或探针失败，确认转发被阻断；不要停现有 Windows UniVPN、`huawei-new` 或 Mihomo。关闭新 tunnel 后更具体路由消失，旧 VPN 恢复接管属于回滚，不能当作容器阻断失败。

回滚只卸载本次独立名称，由 WireGuard 移除其网卡和路由，再用同一组只读命令核对旧选路恢复；不卸载管理器或驱动，不清空路由表：

```powershell
# 管理员执行；只影响本次新建 tunnel。
& 'C:\Program Files\WireGuard\wireguard.exe' /uninstalltunnelservice univpn-hidev
```

DNS 保持现状：精确业务路由不提供内网域名解析，后续若需要域名，应另行设计按域名后缀解析，而非全局替换 DNS。非 `/0` tunnel 仍受 Windows 常规路由与多网卡 DNS 行为管理。[WireGuard DNS/网卡生命周期](https://git.zx2c4.com/wireguard-windows/about/docs/netquirk.md)
