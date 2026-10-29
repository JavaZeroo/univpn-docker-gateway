# 参与项目

欢迎提交可复现的兼容性报告、操作体验改进和测试。当前优先验证新启动器的完整接入，以及重启/真实断线恢复，详见[简化分析](docs/simplification.md)。

## 报告问题

说明操作系统、Docker/WSL 后端、厂商客户端版本、相关命令和脱敏后的错误。区分认证失败、容器目标不可达、Windows 选路错误，以及应用自己配置了代理的情况。

可以提供引擎版本和安装包摘要、`vpnctl status` 中必要状态字段、脱敏后的路由与握手时间。删除账号、密码、OTP、私钥、真实网关/内网地址，以及 status 的 attempt/challenge 标识；不要粘贴完整配置、`wg showconf`、`wg dump` 或厂商原始日志。

不要上传厂商安装包、二进制、反编译源码或包含厂商程序的镜像。新协议适配必须保持独立实现，并描述验证版本及其证据。

## 提交修改

```powershell
python -m unittest discover -s tests -v
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/windows_gateway_test.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/windows_profile_test.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/windows_client_test.ps1
```

Python 测试使用受控对象；Windows 测试模拟系统调用，不触发 UAC 或修改网络。网络集成测试只能放在一次性隔离环境执行，不在正在认证的正式容器命名空间运行。

网络改动请给出能失败的对照、完整业务响应、实际出口和恢复结果，不能只贴端口监听、网卡 Up 或 WireGuard 握手。没有对应服务时，明确标记 HTTP/UDP 尚未验证。

会话改动需覆盖超时、取消、单次提交与并发状态查询；不得增加凭据回显或自动重复认证。配置工具默认不覆盖现有密钥和服务，只修改本实例的网络资源，并提供单独回滚路径。

更新文档时保持 README、[快速开始](docs/quickstart.md)、[架构](docs/architecture.md)和[实测记录](docs/subnet-gateway-validation.md)一致。分清模拟测试、真实验证和源代码已修复但存量容器尚未更新的状态。

项目自写代码使用 GPL-3.0-only；厂商软件不在此许可范围内。提交前检查差异和 Git 忽略目录，不将任何本地环境文件加入提交。
