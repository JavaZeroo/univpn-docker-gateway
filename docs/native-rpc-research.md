# 纯终端后端：独立引擎与 Python 前端原型

2026-10-27，官方 Linux amd64 10781.21.0.0831。

## 已验证

1. 原交互菜单要求短信时直接失败，见 [CLI 短信实测](cli-mfa-proof.md)。
2. 同一个 UniVPNCS 可通过启动参数进入另一条 CSDK/RPC 初始化路径，不运行交互菜单或 GUI。
3. 在项目纯 CLI 镜像内，仅启动官方辅助服务、UniVPNCS 和独立编写的 Python 前端，收到引擎启动通知，核心进程保持运行。
4. 官方引擎日志出现 `CSDK Startup biz succeed`；本地 IPC 使用 Unix domain socket，日志里的“port”对应 `/tmp/<数字>` 路径，不是 TCP 监听。
5. 本轮不提供 VPN 账号密码、不触发短信、不修改官方二进制。

可复现脚本：[tools/probe_native_rpc.py](../tools/probe_native_rpc.py)。它检查厂商二进制 SHA256，只接受实测版本；只解析启动通知，**不是通用 RPC 客户端，也没有实现短信认证**。应在全新临时容器中运行，不挂载用户配置，不共享宿主机 PID 或网络。

```powershell
docker run --rm --cap-add NET_ADMIN --device /dev/net/tun --mount "type=bind,source=$((Resolve-Path tools/probe_native_rpc.py).Path),target=/probe.py,readonly" --entrypoint python3 univpn-docker-gateway:cli /probe.py
```

期望关键结果：

```json
{
  "native_engine_alive": true,
  "python_frontend_received_engine_announcement": true,
  "gui_started": false,
  "credentials_submitted": false,
  "sms_authentication_implemented": false
}
```

## 后续门槛

- 确认前端到 CSDK 的请求格式、消息分帧和会话关联。不能将一次 socket recv 当作一个完整消息。
- 先实现无凭据的状态查询及返回码校验，再接入正常登录、证书校验和短信 challenge/submit/cancel。
- 保留原厂引擎进行认证及隧道处理；不通过跳过 MFA、证书检查或修改成功返回码来伪造可用性。
- 验证同一会话收到验证码后能继续认证，再进行真实业务探测和资源测量。
- 已发现的短信处理字符串属于离线线索，不证明某个消息格式、公开 SDK 或调用稳定性。当前接口未找到公开承诺，升级客户端须重新验证。

分析使用现有 ELF 工具与独立容器运行结果。仓库不包含反编译源码、厂商二进制或真实认证会话数据。
