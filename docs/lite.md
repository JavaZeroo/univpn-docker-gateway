# Lite：只用 Docker 的应用代理

Lite 不包含 `wireguard-tools`、Windows 脚本或桌面，也不会安装 WireGuard、创建 Windows 网卡或修改其路由。它把容器 VPN 提供为本机 SOCKS5 代理，应用需要自行设置代理。

## 首次连接

启动 Docker Desktop 的 Linux 容器环境，在 Windows PowerShell 执行：

```powershell
docker run --rm -it --log-driver none --name univpn-lite `
  --cap-add NET_ADMIN --device /dev/net/tun `
  -p 127.0.0.1:1080:1080 `
  -v univpn-lite-config:/config `
  -v univpn-vendor-cache:/var/cache/univpn `
  javazero/univpn-docker-gateway:lite
```

首次依次填写：网关 `host:port`、允许访问的 IPv4 CIDR、其中一台 SSH 探针的 `IP:port`；只有一个 `/32` 目标时可使用默认探针。可选证书 SHA256 必须事先独立核实，留空使用正常 CA 验证，不自动信任自签证书。

随后在同一终端输入 VPN 账号密码及最新短信。服务在前台运行，`Ctrl+C` 停止；再次执行同一命令复用网络配置并显式登录，不保存凭据或自动重发短信。

网络配置保存在 `univpn-lite-config` 卷的 `/config/lite/vpn.json`，目录与文件权限分别为 root 0700 / 0600，已有配置只读复用。厂商组件首次从官方获取、校验后缓存于另一 named volume，无需本机配置文件或手工构建。固定镜像版本可用 `:0.4.0-lite`。

## 应用如何使用

将应用的代理类型设为 **SOCKS5**，地址 `127.0.0.1`，端口 `1080`。例如访问数字 IPv4 的 HTTP 服务：

```powershell
curl.exe --proxy socks5h://127.0.0.1:1080 http://10.80.0.11:8080/
```

这里只接受数字 IPv4；`socks5h` 不意味着本项目支持目标域名解析。SSH 请使用支持 SOCKS5 的客户端，在其会话中配置代理；裸 `ssh user@10.80.0.11` 不会自动通过 Lite。需要所有应用直接使用原始 IP、或需要 UDP 时，选择 [Full](quickstart.md)。

运行时可从另一终端检查真实就绪状态或续交当前短信挑战：

```powershell
docker exec univpn-lite vpnctl status
docker exec -it univpn-lite vpnctl sms
```

`status` 只有真实探针就绪才退出成功。`sms` 只适用于尚未过期的现有挑战；失败、过期或取消后，停止本次容器，再显式运行首次命令发起新尝试。

## 范围与隔离

- Python 标准库实现 **SOCKS5 CONNECT-only / IPv4 TCP**。不支持 UDP ASSOCIATE、BIND、IPv6 或真正的目标域名；域名字段中的规范数字 IPv4 可以使用。
- 只连接 `allowed_cidrs` 内的目标，拒绝默认路由。认证、隧道与真实探针均就绪后才放行，失效时关闭代理流量；每个出站 socket 绑定 VPN 接口并核对路由，不能回落到 `eth0`。
- 多套 Lite 使用不同容器名、配置卷及本机代理端口；应用选择哪个代理，就使用哪套 VPN。可以复用已验证的厂商缓存卷，认证会话不共享。
- Lite 与 Full 共用原生认证控制器。此前真实短信与 Full 数据链路已通过；新增 Lite 代理入口仍待真实 VPN 登录后的端到端验收，协议或模拟路由测试不能替代它。
- 省去 WireGuard 和 Windows 网卡不等于没有平台成本：Docker Desktop / WSL2 仍会占用资源。当前厂商引擎仅支持 Linux amd64。
