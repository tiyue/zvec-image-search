# SBOM 与构建来源材料

`scripts/generate-sbom.ps1` 从本地文件生成两份 JSON sidecar，全程不访问网络：

- `Zvec-Desktop-<版本>.spdx.json`：SPDX 2.3 JSON SBOM。
- `Zvec-Desktop-<版本>.provenance.json`：in-toto Statement v1，predicate 使用 SLSA provenance v1 结构。

`publish-desktop.ps1` 会把 sidecar 的名称、大小和 SHA-256 写入 `desktop-release.json`，并纳入 `SHA256SUMS.txt`。provenance subject 覆盖已生成的 ZIP、安装器、正式搜索质量报告和 SPDX SBOM；未认证 Preview 不会伪造质量报告 subject。

## 权威输入

生成器只读取构建时已有的本地输入：

- `requirements-lock.txt`：原生 Python 运行时闭包的精确 `name==version` 锁定；仅允许使用受限的 `python_version` 环境标记表达互斥的 Python 兼容版本。生成器拒绝范围约束、无条件重复包名、重复条件和缺失的直接依赖，并把每个条件写入 SBOM 与 provenance。
- `pyproject.toml`：项目名称、版本、`requires-python`、运行时依赖和构建依赖。
- `zvec_launcher.py`、`image_service.py` 与 `scripts/zvec.ps1`：原生 CLI、后端和隔离虚拟环境启动入口。
- `desktop/Zvec.Desktop/Zvec.Desktop.csproj`：WPF 项目与目标框架。
- 发布后的 `Zvec.Desktop.runtimeconfig.json`：每个 RID 实际捆绑的 `Microsoft.NETCore.App` 和 `Microsoft.WindowsDesktop.App` 版本。
- Git revision/state、实际执行 `dotnet --version` 的结果、发布脚本和质量门禁脚本的本地 SHA-256。

仓库不再提供 Dockerfile、Compose、OCI 镜像和 Docker constraints，它们也不是桌面 SBOM 或发布 provenance 的输入。SBOM 中的原生运行时组件明确记录：

```text
isolation = venv
package_format = wheel
requires_python = pyproject.toml 中的值
```

只独立运行生成器而不提供发布后的 runtimeconfig 清单时，.NET bundled framework 版本会明确写为 `unknown`；由发布脚本调用时记录实际 self-contained 版本。

## 依赖摘要与未知值

仓库当前没有 LICENSE 文件，生成器也不通过网络查询第三方许可证或下载 wheel，因此：

- SPDX 的 `licenseDeclared`、`licenseConcluded`、supplier 和 copyright 使用标准 `NOASSERTION`。
- 没有本地 wheel 文件时，不伪造包摘要；provenance 写入 `package_artifact_digest: unknown`。
- `requirements-lock.txt` 自身记录真实 SHA-256，各锁定依赖记录精确版本。
- Git 不可用时 revision 为 `unknown`、state 为 `unavailable`；正式发布仍由 clean-worktree 门禁拒绝这种来源。
- sidecar 当前未签名。即使桌面二进制通过 Authenticode，sidecar 的 `signature_status` 仍是 `unsigned`。

`SHA256SUMS.txt` 能验证完整性，但不能替代独立签名、透明日志或受信构建服务提供的来源认证。

## 独立生成与可重复性

```powershell
.\scripts\generate-sbom.ps1 -OutputDirectory .\dist\sbom
```

自动化构建应显式传入 Git revision、SDK、RID 和 bundled-framework manifest。固定全部输入并传入相同 `-GeneratedUtc` 时，输出字节可重复；也可以使用标准 `SOURCE_DATE_EPOCH`：

```powershell
$env:SOURCE_DATE_EPOCH = "1783904523"
.\scripts\generate-sbom.ps1 -OutputDirectory .\dist\sbom
Remove-Item Env:SOURCE_DATE_EPOCH
```

## 发布元数据

`desktop-release.json` 的 `supply_chain` 包含：

- SPDX sidecar 的文件名、格式、大小和 SHA-256。
- provenance sidecar 的 predicate 类型、大小和 SHA-256。
- `requirements-lock.txt` 的文件名、SHA-256 和精确锁定策略。
- `python_runtime.mode = native-venv`。
- `python_runtime.package_format = wheel`。
- `python_runtime.docker_required = false`。

发布工作流还单独构建项目 wheel，在全新 Python 3.12 虚拟环境中使用 `requirements-lock.txt` 安装，并执行 `zvec help` 冒烟测试。该 wheel 和 `NATIVE-SHA256SUMS.txt` 与桌面产物一起进入候选 Release；默认工作流不生成 OCI archive。

## 校验发布目录

```powershell
$release = Get-Content .\desktop-release.json -Raw | ConvertFrom-Json

Get-FileHash $release.supply_chain.sbom.file -Algorithm SHA256
$release.supply_chain.sbom.sha256

Get-FileHash $release.supply_chain.provenance.file -Algorithm SHA256
$release.supply_chain.provenance.sha256

Get-FileHash .\requirements-lock.txt -Algorithm SHA256
$release.supply_chain.python_lock.sha256

Get-Content .\SHA256SUMS.txt
```

契约测试：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\tests\test_generate_sbom.ps1

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\tests\test_publish_desktop.ps1

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\tests\test_release_workflow.ps1
```

这些测试验证离线边界、固定输入的可重复输出、精确 Python pins、.NET bundled framework、Git/SDK、真实材料摘要、原生 wheel 发布链和未知许可证策略，并明确拒绝默认 CI/Release 重新引入 Docker 构建依赖。

最佳实践：锁文件变化应与依赖升级原因、跨平台测试结果和新 SBOM 一起审查，不能只修改版本号。
