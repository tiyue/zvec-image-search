# SBOM 与构建来源

正式桌面构建由 Python 编排：CPython 3.12、PyInstaller 6.16.0、固定 NSIS 3.12。CI 与 Release 不安装 .NET，不调用 PowerShell/WPF 发布脚本，不使用 Chocolatey，也不依赖 Docker。

## 产物

`scripts/generate_python_release_materials.py` 离线生成：

- `Zvec-Desktop-<版本>.spdx.json`：SPDX 2.3 SBOM。
- `Zvec-Desktop-<版本>.provenance.json`：in-toto Statement v1，采用 SLSA provenance v1 predicate。
- `desktop-release.json`：版本、目标架构、签名状态、payload 和 sidecar 摘要。
- `SHA256SUMS.txt`：安装包、便携包与全部发布 sidecar 的 SHA-256。

payload 内的 `desktop-payload-manifest.json` 逐文件记录大小和 SHA-256。构建器在生成 ZIP 和安装器前验证该清单，并拒绝 PowerShell、C#/XAML、WPF/.NET 运行文件以及被排除的大型模型 SDK。

## 权威输入

- `pyproject.toml`：项目名称和版本。
- `requirements-lock.txt`：Python 运行时精确依赖及 PEP 508 条件。
- `requirements-packaging.txt`：固定 PyInstaller 及其传递构建依赖版本。
- `release/python_preview/zvec_python_preview.spec`：共享冻结图和三个入口。
- `assets/Zvec.AppIcon.ico`：正式桌面应用图标。
- `installer/Zvec.PythonDesktop.nsi`：主产品安装身份与安全升级边界。
- `scripts/provision_nsis.py`：下载并校验固定 NSIS 3.12 的 Python 准备器。
- Git commit、目标 `win-x64`、签名状态和可选的正式搜索质量报告。

生成器不联网查询许可证，不伪造 wheel 摘要。未知许可证统一使用 SPDX `NOASSERTION`。正式 Release 必须能够解析 Git commit；提供 `--expected-revision` 时，源码 revision 不一致会直接失败。

## 生成

```text
python -m pip install --requirement requirements-packaging.txt
python scripts/provision_nsis.py --output-directory build\tools\nsis-3.12
python scripts/build_python_desktop.py --output-root dist\desktop\0.4.0\win-x64 --makensis build\tools\nsis-3.12\payload\nsis-3.12\makensis.exe
python scripts/generate_python_release_materials.py --repository-root . --output-root dist\desktop\0.4.0\win-x64 --signing-status unsigned
```

固定 `SOURCE_DATE_EPOCH` 可稳定 sidecar 时间字段：

```text
set SOURCE_DATE_EPOCH=1783904523
python scripts/generate_python_release_materials.py --repository-root . --output-root dist\desktop\0.4.0\win-x64 --signing-status unsigned
```

## 签名

受保护的 GitHub Environment 通过以下 secrets 提供 PFX：

```text
ZVEC_AUTHENTICODE_PFX_BASE64
ZVEC_AUTHENTICODE_PFX_PASSWORD
```

签名构建只从环境读取凭据，临时 PFX 会在成功或失败后删除。三个入口 EXE 和安装器均使用 SHA-256 与 RFC 3161 时间戳签名，并由 `signtool verify /pa /all` 复核。未签名产物只能进入 Preview 通道。

## 验证

```text
python scripts/build_python_desktop.py --verify-only dist\desktop\0.4.0\win-x64\Zvec-Desktop
python -m unittest tests.test_python_preview_packaging tests.test_python_release_pipeline -v
```

旧 WPF、PowerShell 和 .NET 构建树已删除。旧 Docker named volume 的一次性导出仍由 Python CLI 显式调用 Docker；日常运行、测试和发布链不会连接 Docker Engine。

最佳实践：依赖、构建脚本或冻结清单变化时，应同时审查新 SBOM、provenance、安装冒烟和 SHA-256，不能复用旧版本产物。
