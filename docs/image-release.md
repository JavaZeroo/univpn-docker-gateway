# 公开镜像的构建与发布

0.4.0 分为 Lite 与 Full，平台为 `linux/amd64`。普通用户从 [README](../README.md) 选择版本；本页记录构建、缓存与发布证据。

| 标签 | 版本 | 内容 |
| --- | --- | --- |
| `lite`、`0.4.0-lite` | Lite | SOCKS5 IPv4 TCP 入口；无 WireGuard 工具和 Windows 脚本 |
| `full`、`0.4.0-full` | Full | Windows WireGuard 原始 IPv4 接入；指定目标 TCP / UDP |
| `latest`、`0.4.0` | Full 兼容别名 | 保留此前默认镜像的使用方式 |

## 0.4.0 验证记录（2026-10-29）

- 同一份认证控制器、两个 Docker 构建 target；Lite 不执行 WireGuard 或内核转发规则管理，Full 保留原有路径。
- 192 项 Python 测试在 Windows 通过（8 项平台相关跳过）。隔离 Linux 网络测试实际验证 SOCKS5 SSH、1 MiB HTTP、真实 `SO_BINDTODEVICE`、白名单、就绪撤销/恢复，以及普通出口可用时禁止回落。VPN 在此测试中由 veth 模拟。
- Lite 镜像已在无网络的一次性容器内完成真实 TTY 配置、0700/0600 权限检查、固定官方缓存校验及官方引擎 idle 初始化；未认证的 SOCKS 请求被拒绝，镜像内没有 `wg`、`wg-quick` 或 Windows 客户端文件。
- 新 Lite 完整真实 VPN 认证及应用端到端接入仍待独立验收；未为发布重新触发短信或重启现存 VPN。Full 既有真实数据链路证据见[验证记录](subnet-gateway-validation.md)。
- [8 项 CI 检查通过](https://github.com/JavaZeroo/univpn-docker-gateway/actions/runs/40834447078)，分别构建 Lite / Full，验证两条网络路径及 Windows 引导。镜像源码 revision：`8767e242a5a67cee88f5d111ee4165905c312660`。
- 六个标签均已 push 并通过匿名 Registry manifest / 配置核验；Lite 的 OCI index 为 `sha256:da6fe3f9ee165bee3e8dfe2d087c853d6fcc9b7f4efcc8861de20e2545596182`，Full 及其兼容别名为 `sha256:d07a1d1e01f0e48b5a46a440a35bc75a8a97b032a4113723d5009abf10b59b70`。
- Linux/amd64 层压缩合计分别为 43,632,061 字节（Lite）和 43,795,158 字节（Full）；这是下载量，不是运行内存。两版仍依赖 Docker Desktop / WSL 的平台资源，不能由镜像大小推断内存收益。

## 0.3.0 发布记录（2026-10-29）

- Docker Hub `0.3.0` 与当时的 `latest` 已发布并通过匿名 manifest 核验，OCI index：`sha256:9fde5286dfef51c09c0eff3b432efbe5938a986b56f4c838ef71988efdf75209`。

- 新增 Windows WireGuard 按需准备，用户只需预装 Docker Desktop；MSI 与隧道创建的 UAC 分开处理。
- 源码 revision：`ad5d8008ca75c2eb281730e66e52281243f143ad`。镜像包含 CMD 和四个 PowerShell 文件，不包含 Windows MSI 或 UniVPN 二进制。
- [7 项 CI 检查通过](https://github.com/JavaZeroo/univpn-docker-gateway/actions/runs/40811966051)，其中一次性 Windows runner 实际运行官方 MSI，返回 `Status=Installed, Ready=true, Reused=false, RestartRequired=false`。
- 本机真实检查走 `Existing / Reused=true`，未重装或升级正在使用的 WireGuard；镜像导出的 CMD 已实际完成 Init、真实密钥生成和 Compose 预检。
- 27 项前置依赖检查覆盖错误摘要/签名、部分安装、提权、受保护副本及 3010 分支；非管理员 UAC 点击本身仍是模拟覆盖，未为此修改本机既有客户端。新入口整体真实短信与网络接入仍另行验收。

## 0.2.0 发布记录（2026-10-29）

- 0.2.0 发布时，该版本与当时 latest 指向同一 OCI index（latest 为可变标签）：`sha256:7d51acf542116f6d01eeda6645ee04cd7d646e35939d39382263fa033b168e25`。
- 镜像源码 revision：`006ce32659cf479acc4b264c975ec4f05bb649ed`；Linux/amd64 层压缩合计 43,671,687 字节，约 44 MB 下载，不代表运行内存。
- 已通过匿名 Registry manifest/配置校验，并用空凭据配置实际执行 Docker CLI pull；不需要 Docker Hub 登录即可取得镜像。
- 真实验收包含冷缓存官方下载、无网络新容器复用缓存、隔离官方引擎启动并停在未认证的 idle 状态，以及镜像导出 CMD 的实际密钥生成/只读 Plan/Compose 校验。
- 新入口完整短信认证、新建 Windows 网卡与最终业务检查仍未作为整体真实验收；原有链路证据和新包装器模拟测试分开报告。现存 VPN 未重启。

## 镜像包含什么

[Dockerfile.public](../Dockerfile.public) 两版共用 Ubuntu 22.04、Python 标准库、tini、iproute2、iptables、CA 和终端前端；Full 额外包含 `wireguard-tools` 和 Windows 配置脚本。**两版均不包含官方 UniVPN 安装器、引擎、厂商共享库或用户配置。** [专用构建忽略规则](../Dockerfile.public.dockerignore) 排除 `vendor/`、`private/` 和 `.env`。

用户无需在本机编译镜像。默认 [compose.yml](../compose.yml) 拉取公开镜像；首次 `run` 或 `prepare` 才在用户本机容器内从官方取得固定版本，校验后复制最小运行文件。`run` 接着启动认证服务，`prepare` 到此退出；二者都不执行厂商安装器的 shell 脚本。

`export-client` 提前返回，只导出 `univpn.cmd` 和四个 PowerShell 文件，不下载厂商程序、不启动 VPN，也不需要 `NET_ADMIN` 或 TUN。仍需 Docker 使用权限和输出目录写权限。以下示例假定镜像已可获取，且当前目录还没有 `univpn` 子目录；本地无镜像时，Docker 自身仍需先拉取镜像：

```powershell
docker run --rm --network none -v "${PWD}:/output" javazero/univpn-docker-gateway:0.3.0 export-client
.\univpn\univpn.cmd
```

导出不会覆盖已有目录。`univpn.cmd` 启动本地向导，仅本次 PowerShell 进程使用 `ExecutionPolicy Bypass`，不修改持久执行策略；启动器在 Windows 检查并按需安装 WireGuard，再创建网络入口；两个动作可能分别请求管理员确认。入口的实际分支见 [image_entrypoint.py](../univpn_gateway/image_entrypoint.py)。导出成功不等于 VPN 已连接。

## 0.3.0 的 Windows 引导

用户只需预装 Docker Desktop。新增 `windows-prerequisites.ps1`；已有有效签名安装直接复用，完全缺失才从[官方目录](https://download.wireguard.com/windows-client/)取得 `wireguard-amd64-1.1.1.msi`，实际文件 3,293,184 字节、SHA256 `7bfed60ad61b785c914b38b61555a975488e1d3ec472dbfb2fcdf498fca75242`。本轮已用 Windows Authenticode 实测其签名 Valid、发布者 WireGuard LLC，未在本机卸载或重装现有 WireGuard。

安装路径为 Windows 标准 Program Files；MSI 不随 Linux 镜像打包。调用 `msiexec /i ... /qn /norestart DO_NOT_LAUNCH=1`，正常权限使用 UAC，已提权进程直接安装；3010 要求用户重启并停止本次连接。安装行为依据[WireGuard 官方企业部署](https://git.zx2c4.com/wireguard-windows/about/docs/enterprise.md)和[Microsoft msiexec](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/msiexec)。部分安装或已有异常服务不会被自动修复或升级。

## 固定官方版本与校验

来源为[联软官方下载页](https://www.leagsoft.com/doc/article/103107.html)的 Linux x86 `10781.21.0.0831`；固定 ZIP 地址是[官方下载直链](https://download.leagsoft.com/download/UniVPN/linux/univpn-linux-64-10781.21.0.0831.zip)。2026-10-29 匿名完整 GET 返回 200，实际值如下：

| 层级 | 大小（字节） | SHA256 |
| --- | ---: | --- |
| ZIP | 29951944 | `2315e88eb3259f78215e77a41768048d10f6c5540970b042ba76a1f12107cd9f` |
| `univpn-linux-amd64-10781.21.0.0831.run` | 30068863 | `92b07b10ed92a08cdb67ee622a83bd3e576fd14903229d23dcb728a011a8d42a` |

官网下载页标示的 MD5 `23B129334DFA999B976B61028848083B` 与实际 ZIP 的 `c249e38ca130d7eb21d2147d5d511919` 不一致，原因未确认。运行时使用上表实测 SHA256，不能照抄该网页 MD5 或静默接受新摘要。ZIP 内另有 `.run.cms`、`.run.crl`；当前路径不宣称已验证其签名链。

[vendor_runtime.py](../univpn_gateway/vendor_runtime.py) 还逐项固定以下六个文件的大小与 SHA256；表中目的路径相对容器根目录：

| 归档条目 → 目的路径 | 大小 | SHA256 |
| --- | ---: | --- |
| `sysconfig.ini` → `usr/local/UniVPN/sysconfig.ini` | 403 | `828ce10134190151c7fdf5882f87efa4785a77439d03dce901ef0382ca57118a` |
| `promote/UniVPNPromoteService` → `usr/local/UniVPN/promote/UniVPNPromoteService` | 242352 | `388d05e3e3a5aa0e7f4c80f6b642953b8b0c8fa99701f9c6e94926f1273960c0` |
| `serviceclient/UniVPNCS` → `usr/local/UniVPN/serviceclient/UniVPNCS` | 6775624 | `f26948ea1d3892f31a0692d4f771dcbc40365bfb2a39a509b9f3174d323781c4` |
| `serviceclient/libgmssl.so` → 同目录 | 502088 | `178143e5123c475b9cc8490f21ed0b63ca157e584d5e6ce94e9ea2bfae93ba9c` |
| `serviceclient/legacy.so` → 同目录 | 4123184 | `5c708db4059d8b8ca1446b9201438a7a92790218dbc02589decfe3d27089d501` |
| `serviceclient/libgmcrypto.so` → `lib/libgmcrypto.so` | 3411264 | `37c10deccde29c5d997414ad06541bd89ba316b79680cc35cf27a025d22c0afe` |

其中“同目录”指 `usr/local/UniVPN/serviceclient/`。另复制同一 `sysconfig.ini` 到 `opt/gateway/vendor-sysconfig.ini`，共七个输出、15055318 字节，供引擎删除可变配置后的初始化使用。解包只读取精确白名单，不使用 `extractall`；拒绝重复、越界及特殊条目，文件权限归一为 0755/0644，不保留 setuid。

## 缓存与离线迁移

Compose 将 named volume `vendor-cache` 挂载到 `/var/cache/univpn`，固定缓存文件名为 `univpn-10781.21.0.0831-amd64.zip`。默认实际卷名带 Compose project 前缀，不同 project 不会自动共用。只有显式挂同一卷时才共享归档，并通过跨进程锁协调首次下载。

缓存保存固定归档，不共享每个实例的引擎目录、可变配置或认证状态。安装文件全部匹配时不联网；否则重新校验缓存并生成本实例文件。已有损坏缓存会明确失败，不静默换源或替换摘要。不要在仍使用该卷时盲目删除缓存；`docker compose down -v` 还会删除该项目的缓存卷。

本轮集成已验证两项准备流程：新缓存容器从官方在线 `prepare` 成功；第二个 `--network none` 新容器复用同一缓存，返回 `cache_hit: true` 并成功。它们不替代公开 push、新包装器的真实短信认证或新 Windows WireGuard 安装验收。

可以只迁移公开镜像：

```powershell
docker image save -o univpn-gateway-0.3.0.tar javazero/univpn-docker-gateway:0.3.0
# 在另一台机器上：
docker image load -i univpn-gateway-0.3.0.tar
```

`save/load` **不携带 named volume、厂商缓存、用户私钥或认证会话**。本地已有可信 ZIP 时，可按固定文件名放入目标机器实际使用的缓存卷；`prepare` 仍会核验全部摘要，可在禁止联网的情况下完成本地准备。此处不代表已验证离线全流程：真实 VPN 认证仍需访问网关，短信策略也仍需用户参与。不要将含厂商缓存的卷或改装后的镜像当作本项目公开制品分发。

## 维护者发布模板

以下为当前版本发布流程的可复现模板，只供有仓库发布权限的维护者显式执行。发布前先运行相应测试，并确认镜像不含厂商文件、私有配置和凭据。`latest` 可变；需要精确复现时，记录实际发布后的 digest，不能以本地 image ID 代替。

```powershell
$revision = (git rev-parse HEAD).Trim()
foreach ($edition in @('lite', 'full')) {
  docker buildx build --platform linux/amd64 -f Dockerfile.public --target $edition `
    --build-arg VERSION=0.4.0 --build-arg "REVISION=$revision" `
    -t "javazero/univpn-docker-gateway:0.4.0-$edition" `
    -t "javazero/univpn-docker-gateway:$edition" --load .
  if ($LASTEXITCODE -ne 0) { throw "Build failed: $edition" }
}

docker login
foreach ($tag in @('0.4.0-lite', 'lite', '0.4.0-full', 'full')) {
  docker push "javazero/univpn-docker-gateway:$tag"
  if ($LASTEXITCODE -ne 0) { throw "Push failed: $tag" }
}
docker tag javazero/univpn-docker-gateway:0.4.0-full javazero/univpn-docker-gateway:0.4.0
docker tag javazero/univpn-docker-gateway:0.4.0-full javazero/univpn-docker-gateway:latest
docker push javazero/univpn-docker-gateway:0.4.0
docker push javazero/univpn-docker-gateway:latest
docker buildx imagetools inspect javazero/univpn-docker-gateway:0.4.0-lite
docker buildx imagetools inspect javazero/univpn-docker-gateway:0.4.0-full
```

更新固定厂商版本须重新核对官方下载、外层 ZIP、内层 RUN、精确条目及六文件摘要，并做初始化、原生认证和数据面回归；不能只改版本字符串。公开镜像构建本身不应下载厂商包。

传统 [Dockerfile](../Dockerfile) 与 [compose.build.yml](../compose.build.yml) 仍供用户用本地 `vendor/univpn.run` 构建，需设置 `UNIVPN_SHA256` 并使用 Compose override。该结果**包含厂商程序，仅用于用户自己的本地环境，不得当作本项目公开镜像 push**：

```powershell
docker compose -f compose.yml -f compose.build.yml build vpn
docker compose -f compose.yml -f compose.build.yml up -d
```

## 许可边界

自有前端采用项目的 [GPL-3.0-only](../LICENSE)；基础系统及开源工具保留各自许可证。官方 UniVPN 不因此成为本项目的 GPL 代码。

对上述固定包的离线检查发现：79 个 tar 成员中没有独立 EULA/LICENSE；中英文 `language/2052/l_UILabel.ini`、`language/1033/l_UILabel.ini` 含保留一切权利及未经授权复制警告。未找到可支持公开重打包厂商程序的授权，故公开镜像只提供依赖与自有前端，厂商文件由用户本地从官方取得。公开下载直链不等于再分发许可，也不代表厂商对本项目兼容性或自动获取方式作出支持承诺。
