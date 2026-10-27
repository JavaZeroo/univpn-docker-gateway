# 纯命令行短信认证实测

日期：2026-10-27。

## 条件

- 官方 Linux amd64 UniVPN 10781.21.0.0831，使用实际要求短信认证的 HiDev 账号。
- 第一轮：WSL Ubuntu 的独立 network/mount namespace，slirp4netns 出站。
- 第二轮：本项目新构建的 Ubuntu 22.04 纯 CLI Docker 镜像，独立 bridge 网络。两轮认证结果相同。
- 只提取并运行 serviceclient/UniVPNCS 与 promote/UniVPNPromoteService。
- 未运行 GUI、Qt、Xvfb、DBus、VNC 或 noVNC。
- 一次账号密码提交，不自动重试；不保存密码或验证码；下列用户名已脱敏。

## 观察到的输出

```text
1:Connect
...
Connect success.
Tip:  auth type is normal .
Please input the login user name
<account>
Please input the login user password
The dynamic token code is required.
ssl connect failed! reason: login failed
-------------------------------------------------
Welcome to UniVPN!
1:New Connection
2:Exit
<Connection Name List>
3:PROOF
```

提交密码后，客户端直接返回失败和主菜单，没有继续等待验证码。前面的 `Connect success.` 是认证前连接阶段的提示，不能当作已登录或隧道就绪。

## 结论边界

已证实这个版本、这个网关上的官方原生 CLI 无法完成现有短信认证流程。仅补 Expect 提示匹配、输入框或验证码环境变量无法解决这一点。该结果与厂商《终端用户接入指南》10.2.2 的“Linux CLI 的 SSL VPN 仅支持用户名/密码”一致。

这不证明 UniVPN 协议本身无法纯终端实现短信认证。进一步开发需要确认一个不依赖 GUI 的认证接口、SDK 或经验证的兼容实现；当前没有这样的已验证后端。不能把隐藏 GUI 的终端操作桥称为纯命令行客户端。

测试结束后终止测试客户端及辅助服务，并验证原 Windows HiDev 的 SSH 连接仍可用。本次测试没有建立第二个已认证 VPN 会话。

## 镜像与资源检查

- `cli` 构建目标成功；只复制官方 serviceclient、promote 和必需的密码库，不复制 GUI 主程序。
- 包清单检查：Qt、Xvfb、DBus、AT-SPI、VNC/noVNC 包均未安装。
- 菜单空闲时进程为 tini、Python 运行管理器、UniVPNPromoteService、UniVPNCS。
- 单次 `docker stats --no-stream` 菜单空闲样本：12.54 MiB / 256 MiB，CPU 0.95%。这不是已连接短信 VPN 的稳态数据，也不包含 Docker Desktop/WSL 平台成本。
- 首轮 CLI 构建时 Docker 报告镜像大小 49,921,111 字节；磁盘共享、缓存及运行内存须分别评估。
