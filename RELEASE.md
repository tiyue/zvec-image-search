# 发布指南

本项目发布原生 Python wheel 和 Windows 桌面程序，不发布 Docker 镜像、Compose 或 OCI 资产。旧 Docker Workspace 的处理见[迁移文档](./MIGRATION_FROM_DOCKER.md)。

## 本版桌面会话

- 新增正式应用图标和 Windows 系统托盘。
- 点击窗口关闭按钮只隐藏到托盘，后台任务和窗口状态保持不变。
- 单击托盘图标或再次启动 Zvec 会恢复原窗口，不会启动第二个实例。
- 只有页面“退出”或托盘“退出 Zvec”会进入完整关闭流程。
- 当前配置与原后端不一致时，先只读等待旧任务结束，再切换到当前配置。
- 本机会话令牌保存在 Windows 凭据管理器，不进入启动命令、实例登记 JSON 或日志。

升级前请从页面或系统托盘明确退出 Zvec。若仍有后台任务，请先等待任务完成，再安装新版本。
安装器会验证桌面窗口与持久后端均已退出；无法确认安全状态时会停止升级或卸载，不会终止后台任务。

## 本版搜索与界面

- 搜索结果改为每页 15 张，提供上一页、下一页和总页数。
- 当前页图片会在保持最低可读尺寸的前提下自动铺满结果区域；小窗口继续使用虚拟化滚动。
- 以图搜图新增图片投放区，支持选图、拖放图片或文件夹、剪贴板粘贴、目录选图和清除。
- 右侧大图预览改为居中裁切铺满，避免出现大面积空白边。
- “任务中心”并入“状态与诊断”，释放业务页面高度；长任务期间仍可查看日志和取消。
- “文字搜图”新增 AI 语义开关。关闭后只执行本地模糊标签搜索，不调用 embedding 模型。
- 标签查询支持别名、NFKC 和包含匹配；“原”或“神”均可命中“原神”。
- 标签结果显示“标签匹配”和实际命中的完整标签。
- 文字检索自动融合审核标签与图片向量；完成描述回填的图库再加入描述向量。
- 跨 Collection 搜索统一融合候选、按 SHA-256 去重并保留图库来源。
- 默认启用套图多样化：候选套图足够时，前 20 张中每套最多 2 张；不足时自动放宽，用户也可关闭并恢复原始顺序。
- 自包含桌面包只保留 `zh-Hans` 框架语言资源；不启用 WPF Trim、单文件或系统 .NET 依赖。

本版不启用方案第三阶段的人工质量匹配、阈值校准或 `search-quality.json`。原始分数和诊断字段不表示经过校准的概率。

## 本版 Collection schema v4

- 新增 `metadata_text`、`metadata_text_hash` 和 `metadata_embedding`。
- v1、v2、v3 Collection 可零 API 升级到 v4；已有图片向量原样复用。
- 迁移只写入空描述占位，不会在启动、迁移、索引或搜索时自动调用模型。
- “智能整理”新增“生成描述向量”。输入只包含人工标签、文件夹标签、已接受标签和已接受短描述，不发送原图。
- 回填按哈希断点续跑，单图失败跳过，内容变化时拒绝提交陈旧向量。
- 未回填图库保持原有标签与图片向量检索；描述通道不会额外增加查询 API 次数。

## 本版并发与容错边界

默认并发为：文件扫描 4、`qwen3-vl-embedding` 2、描述向量回填 2、自动标注 2。每个模型使用 48 RPM、80,000 TPM 的安全水位，并以 60 RPM、100,000 TPM 为硬上限；429 和临时网络错误按受控退避处理。

描述向量回填复用 embedding 并发配置和进程级限流。只有网络请求有限并发，Collection 校验与写入仍在单一主线程中按顺序完成。该任务必须由用户明确触发，会产生 Embedding API 请求和费用。

不同图库可并行运行；同一图库仍由单一工作线程串行提交 Zvec 和 SQLite。

索引页新增默认关闭的“同时生成智能标签（仅本次新增）”。开启后只提交一个 `index_and_auto_tag`：向量模型与当前主标注模型进入各自独立的有界请求阶段，新图片完成索引提交后即可进入标注，两个阶段可重叠推进。该能力不并发写入 Collection，不承诺精确的网络请求重叠，也不会绕过 RPM、TPM 或预算限制。用户必须选择模型、设置预算并确认第三方处理；旧后端或后端不可用时明确拒绝，不会拆成两个顺序任务。

单图输入错误不会终止整批。失败图片按 SHA-256 去重保存到 `<搜索结果目录>/failed-images/blobs`，任务清单保存到 `failed-images/jobs/<任务 ID>.jsonl`；桌面端“状态与诊断”可查看任务、取消任务和打开失败目录。鉴权、磁盘、SQLite 或 Zvec 等系统性错误会暂停任务并标记需要处理，不会把健康图片批量隔离，也不会冒险继续删除或写入数据。

## 本版模型配置

- 新增“模型配置”页，管理 `embedding`、`auto_tag_primary` 和 `auto_tag_escalation` 三个角色。
- 用户配置默认位于 `%LOCALAPPDATA%\zvec-image-search\models.json`；安装包在 `backend/model-catalog.default.json` 提供参考模板。
- 允许在 JSON 中增加阿里云 DashScope 兼容模型，再从页面选择；不接受其他 provider 或未知协议。
- API Key 不写入模型 JSON，仍由 Windows 凭据管理器保存。
- 配置原子写入；活动任务不会被中断，后端空闲后安全应用新配置。
- 更换标注模型不影响向量索引。更换向量模型后必须新建或重建 Collection，防止混用不同模型生成的向量。

## 发布产物

- `zvec_image_search-<version>-py3-none-any.whl`
- `requirements-lock.txt`（GitHub Release 原生运行时闭包）
- `Zvec-Desktop-<version>-win-x64[-unsigned].zip`
- `Zvec-Desktop-<version>-win-arm64[-unsigned].zip`
- `Zvec-Desktop-<version>-win-<arch>[-unsigned]-setup.exe`
- `Zvec-Desktop-<version>.spdx.json`
- `Zvec-Desktop-<version>.provenance.json`
- `desktop-release.json`、`SHA256SUMS.txt`
- 正式质量候选附带 `Zvec-Desktop-<version>.search-quality.json`

GitHub Release 候选还包含：

- `DESKTOP-SHA256SUMS.txt`
- `NATIVE-SHA256SUMS.txt`
- `RELEASE-ASSETS-SHA256SUMS.txt`
- `RELEASE-POLICY.json`

签名与质量认证是两组独立状态：`-unsigned` 表示未通过 Authenticode，`uncertified-preview` 表示搜索质量未认证。任一条件不满足，产物都不能进入稳定通道。

## 前置条件

- 版本符合 SemVer，并与 `pyproject.toml`、桌面项目和发布参数一致；当前版本为 `0.4.0`。
- 发布 worktree 必须干净；正式候选不得使用 `-AllowDirty`。
- 本地发布使用 Python 3.10+；GitHub Release 工作流固定使用 Python 3.12。
- .NET SDK `8.0.422`，由 `global.json` 固定。
- 构建安装器需要 NSIS；CI 固定为 `3.12.0`。
- 正式候选需要可重放的搜索质量报告；否则必须显式构建未认证 Preview。
- 稳定通道还需要准确的 `v<version>` tag、Authenticode 和仓库 LICENSE/LICENCE。

当前仓库没有 LICENSE/LICENCE，稳定通道因此保持关闭。

## 发布前验证

先安装锁定的开发依赖：

```powershell
python -m pip install --constraint .\requirements-lock.txt `
  --editable . --requirement .\requirements-dev.txt
```

执行 Python、PowerShell 和 WPF 门禁：

```powershell
python -m ruff check .
python -m ruff format --check .
python -m mypy image_vector_service image_service.py zvec_launcher.py zvec_logging.py
python -m mypy tests/search_quality scripts/verify_search_quality_gate.py
python -m unittest discover -s tests -v

powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\test_launcher.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\test_generate_sbom.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\test_publish_desktop.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\test_release_workflow.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\test_persistent_backend_installer_guard.ps1

dotnet build .\desktop\Zvec.Desktop\Zvec.Desktop.csproj `
  --configuration Release --warnaserror
dotnet run --project `
  .\desktop\Zvec.Desktop.ContractTests\Zvec.Desktop.ContractTests.csproj `
  --configuration Release
dotnet format .\desktop\Zvec.Desktop\Zvec.Desktop.csproj `
  --verify-no-changes --no-restore
dotnet format .\desktop\Zvec.Desktop.ContractTests\Zvec.Desktop.ContractTests.csproj `
  --verify-no-changes --no-restore
```

Windows、Ubuntu 原生 CI 和 Python 3.10/3.14 兼容矩阵必须全部通过。

## 构建 Python wheel

```powershell
$Version = "0.4.0"
$NativeOut = ".\dist\native\$Version"

python -m pip wheel --no-deps --wheel-dir $NativeOut .
python -m venv .\.release-venv
$Wheels = @(Get-ChildItem "$NativeOut\*.whl")
if ($Wheels.Count -ne 1) { throw "Expected exactly one wheel." }
$Wheel = $Wheels[0].FullName
.\.release-venv\Scripts\python.exe -m pip install `
  --constraint .\requirements-lock.txt $Wheel
.\.release-venv\Scripts\zvec.exe help
```

Linux 使用 `.release-venv/bin/python` 和 `.release-venv/bin/zvec`。

## 构建桌面候选

以下命令默认生成 x64、ARM64 ZIP、安装器、SBOM、provenance、元数据和校验和。输出目录必须不存在。

未取得正式搜索质量报告时，只能构建 Preview：

```powershell
$Version = "0.4.0"
.\scripts\publish-desktop.ps1 `
  -Version $Version `
  -OutputDirectory ".\dist\desktop\$Version-preview" `
  -AllowUncertifiedSearchQualityPreview `
  -RequireInstaller
```

正式质量候选：

```powershell
$Version = "0.4.0"
$QualityGate = ".\artifacts\search-quality-validation\comparison.json"

.\scripts\publish-desktop.ps1 `
  -Version $Version `
  -OutputDirectory ".\dist\desktop\$Version" `
  -SearchQualityGatePath $QualityGate `
  -RequireInstaller
```

签名候选增加证书指纹：

```powershell
.\scripts\publish-desktop.ps1 `
  -Version $Version `
  -OutputDirectory ".\dist\desktop\$Version-signed" `
  -SearchQualityGatePath $QualityGate `
  -RequireInstaller `
  -CertificateThumbprint "<40位证书指纹>"
```

`-SearchQualityGatePath` 与 `-AllowUncertifiedSearchQualityPreview` 互斥。发布脚本在临时目录完成构建、签名、SBOM、provenance 和校验后再原子移动结果；任一步失败都不会留下可误用的正式目录。

## 搜索质量门

本轮暂不执行第三阶段质量匹配，也没有启用新的质量阈值。当前功能候选应按未认证 Preview 发布；以下门禁保留给后续完成人工评测与阈值校准的版本。

正式报告必须绑定冻结的 validation holdout，并包含：

- 人工 source dataset、split manifest、seed 和策略
- validation dataset 指纹
- before/after 原始 run 的 SHA-256 与 canonical fingerprint
- 跨 Collection 公平性结果

独立重放：

```powershell
python .\scripts\verify_search_quality_gate.py `
  --report .\artifacts\search-quality-validation\comparison.json `
  --repository-root .
```

校验器会重新读取源工件、重建 split、重算指标和指纹，不信任报告内已有的 `passed` 字段。缺失、越界、被修改或不可重放的报告一律失败。智能整理模型生成的待审核标签不能代替搜索质量报告。

## Authenticode

GitHub 受保护环境使用：

- `ZVEC_AUTHENTICODE_PFX_BASE64`
- `ZVEC_AUTHENTICODE_PFX_PASSWORD`

两项必须同时存在。工作流临时导入 PFX，检查有效期、私钥和代码签名 EKU，只向发布脚本传递 thumbprint，结束后删除 PFX 和本次导入的证书。默认时间戳服务为 DigiCert。

正式签名只能从准确的 `v<version>` tag 进入受保护环境。当前 `Uninstall.exe` 尚未单独签名；生产证书、可信时间戳和证书轮换仍需运营实测。

## GitHub Release 工作流

`.github/workflows/release.yml` 仅支持手动 `workflow_dispatch`，输入包括：

- `version`、`prerelease`
- `search_quality_gate_path`
- `allow_uncertified_search_quality_preview`
- `require_authenticode`
- `create_draft_release`

工作流会校验版本、tag、LICENSE 和质量策略，复用 CI，构建 unsigned 候选与 wheel；准确 tag 可进入受保护签名环境。随后合并资产、重算总校验和并生成 `RELEASE-POLICY.json`。

稳定候选必须同时满足：

- 版本没有 prerelease 后缀
- 当前 ref 是准确的 `v<version>` tag，且 tag 指向 workflow SHA
- 桌面 ZIP 和安装器已通过 Authenticode
- 搜索质量已正式认证
- 仓库存在 LICENSE/LICENCE

工作流不会创建或覆盖 tag，不会覆盖已有 Release，也不会自动公开 Release。`create_draft_release` 只会从已存在且准确的 tag 创建 draft；稳定 SemVer tag 不能用于 Preview Release。

## 安装、升级与卸载

- 安装器为当前用户安装，不要求管理员权限。
- 安装目录带 ownership marker；桌面程序仍在运行、进程状态不明或目录归属不符时，安装、升级、回滚和卸载会安全停止。
- 升级前必须从页面或系统托盘明确退出旧版桌面程序；关闭窗口只会隐藏到托盘。
- 安装器不捆绑 Python。首次启动只检查环境；用户执行一键修复或首次启动后端任务时，才会按需使用 Python 3.10+ 在 `%LOCALAPPDATA%\zvec-image-search\runtime\venv` 创建隔离环境并安装锁定依赖。
- `win-arm64` 仅表示 WPF 为 ARM64。`zvec 0.5.1` 没有 Windows ARM64 Python wheel，后端必须使用 x64 CPython，由 Windows 仿真运行。
- 升级和卸载不会删除 Windows 凭据管理器中的 API Key、用户配置、Workspace、搜索结果或原图。
- 旧 Docker named volume 不会自动删除；迁移后再按[迁移文档](./MIGRATION_FROM_DOCKER.md)处理原数据。

## 供应链与发布后核验

`desktop-release.json` schema v3 记录版本、Git revision/dirty 状态、.NET SDK、RID、bundled framework、签名、搜索质量和每项产物的 SHA-256。供应链字段至少包括：

```text
python_lock.file = requirements-lock.txt
python_runtime.mode = native-venv
python_runtime.package_format = wheel
python_runtime.docker_required = false
```

SPDX 2.3 SBOM 和 SLSA provenance 的生成与验证规则见[供应链说明](./SUPPLY_CHAIN.md)。

在下载后的 Release 资产目录中至少核验：

```powershell
Get-Content .\RELEASE-ASSETS-SHA256SUMS.txt
$DesktopZip = Get-ChildItem .\Zvec-Desktop-0.4.0-win-x64*.zip |
  Select-Object -First 1
Get-FileHash $DesktopZip.FullName -Algorithm SHA256

python -m venv .\verify-venv
.\verify-venv\Scripts\python.exe -m pip install `
  --constraint .\requirements-lock.txt `
  .\zvec_image_search-0.4.0-py3-none-any.whl
.\verify-venv\Scripts\zvec.exe help
```

还需在干净 Windows 机器验证安装、首次启动、索引、单图库/跨 Collection 搜索、智能整理、升级回滚和卸载；Ubuntu 验证 CLI 初始化、索引、搜索、迁移和结果导出。

## 已知限制

- 仓库尚无 LICENSE/LICENCE，不能进入稳定通道。
- 安装器不携带 Python；首次准备环境需要可用的 Python 和软件包源。
- Windows ARM64 后端依赖 x64 CPython，ARM64 实机仍需完整回归。
- `Uninstall.exe` 尚未单独签名。
- 尚无自动升级、签名更新清单、断点续传和发布失败回滚服务。
- 第三阶段搜索质量匹配与阈值校准尚未执行；当前构建只能作为未认证 Preview。
- 生产 Authenticode、时间戳、证书轮换、受保护环境审批和公开 draft 流程仍需运营演练。

同一版本必须对应唯一、不可变的 Git revision。发现问题应发布新补丁版本，不得覆盖已分发资产。
