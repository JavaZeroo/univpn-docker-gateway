<h1 align="center">UniVPN Docker Gateway</h1>
<p align="center"><strong>让多个 UniVPN 共存，把配置交给你的 Agent。</strong></p>
<p align="center">
  <a href="https://hub.docker.com/r/javazero/univpn-docker-gateway"><img alt="Docker Hub" src="https://img.shields.io/docker/v/javazero/univpn-docker-gateway?label=Docker%20Hub&sort=semver" /></a>
  <a href="https://github.com/JavaZeroo/univpn-docker-gateway/actions/workflows/test.yml"><img alt="CI" src="https://github.com/JavaZeroo/univpn-docker-gateway/actions/workflows/test.yml/badge.svg?branch=main" /></a>
  <a href="LICENSE"><img alt="GPL-3.0-only" src="https://img.shields.io/badge/license-GPL--3.0--only-59bba7" /></a>
</p>

## 交给你的 Agent

把下面这段发给能操作本机终端的 Agent：

```text
请按 https://raw.githubusercontent.com/JavaZeroo/univpn-docker-gateway/main/docs/agent-setup.md 在本机部署 UniVPN Docker Gateway。
先了解我的使用需求，帮我选择 Full 或 Lite，再完成环境准备、配置和连接。
缺少 VPN 网关、内网目标等信息时问我；账号、密码和短信验证码由我在终端输入。
保留已有 VPN 和密钥，并用我需要的 SSH 或网页访问验证结果。
```

## 想先了解一下？

[阅读完整教程：版本选择、SSH 与网页访问、架构和资源对比 →](https://blog.javazero.top/2026/10/19/docker-vpn-ssh-remote-dev/)

[Docker Hub](https://hub.docker.com/r/javazero/univpn-docker-gateway) · [反馈问题](https://github.com/JavaZeroo/univpn-docker-gateway/issues) · [参与贡献](CONTRIBUTING.md)

项目受 [jesusdf/huawei-vpn](https://github.com/jesusdf/huawei-vpn) 启发。自有代码采用 [GPL-3.0-only](LICENSE)，厂商程序遵循其自身许可。
