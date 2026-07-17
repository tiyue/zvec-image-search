# Zvec Desktop

`Zvec.Desktop` 是 .NET 8 WPF 图形界面。它直接启动宿主机上的 Python 后端，不依赖 Docker Desktop、Docker Engine、Compose、WSL 或容器网络。每个图库仍拥有独立的 Collection 和 Workspace；索引与同步明确选择单图库，搜索可以选择一个图库或全部启用图库。

## 功能

- 图库初始化、新增、编辑、启停、移除和默认图库设置。
- 每个图库独立配置 `image_root` 与 `workspace_directory`，禁止多个图库共享 Workspace。
- 单 Collection 索引、同步、同步预演、统计、缓存清理、旧结果清理和路径重绑。
- 文字、图片、图文联合搜索；支持 Top-K、标签 all/any、联合权重和诊断结果，并自动融合审核标签、图片向量和可用的描述向量。
- 以图搜图支持选择文件、拖放图片或目录、粘贴剪贴板图片和从目录选图。
- 搜索结果每页 15 张，在空间足够时自动铺满画廊；小窗口保持最低卡片尺寸并滚动。
- 跨 Collection 并行召回、统一结果融合、SHA-256 去重和图库归属展示。
- 候选套图足够时，默认限制前 20 张中每套最多 2 张；套图不足时自动放宽，也可关闭“优先展示不同套图”查看原始顺序。
- 可重新连接的原生 Python 后端、任务进度与取消；同一 Collection 的写任务串行执行。
- Windows 凭据管理器保存 API Key；桌面端通过本机回环地址和 Bearer 会话把 Key 注入后端内存。
- 智能整理：文件夹标签补全、视觉标注费用估算、图片外发确认、预算上限、缓存、人工审核和显式描述向量回填。

当前图片索引、语义查询和描述向量均使用配置的 embedding 模型，默认是 `qwen3-vl-embedding`。智能整理的 `qwen3-vl-flash`/`qwen3-vl-plus` 是独立视觉理解模型，只生成待审核标签，不替换向量模型。

## 运行要求

- Windows 10/11
- 已安装 .NET 8 Desktop Runtime；自包含发布包不需要另装 .NET
- 可运行后端依赖的 Python 3.10 或更高版本；Windows ARM64 当前需使用 x64 CPython，因为 `zvec 0.5.1` 尚无 Windows ARM64 wheel
- `zvec==0.5.1`、Pillow、numpy 和项目 wheel 安装在隔离虚拟环境中

桌面安装器不捆绑 Python。窗口首次启动时自动检查 Python 3.10+，并在 `%LOCALAPPDATA%\zvec-image-search\runtime\venv` 创建当前用户独立虚拟环境、安装锁定依赖和验证导入；无需手工把 `python_executable` 指向 venv。可以选择一个基础解释器或通过 `ZVEC_PYTHON_EXECUTABLE` 覆盖。准备失败时，环境检查页会显示任务日志、恢复建议和重新修复入口。

## 从源码运行

```powershell
python -m pip install --constraint .\requirements-lock.txt `
  --editable . --requirement .\requirements-dev.txt

dotnet run --project .\desktop\Zvec.Desktop\Zvec.Desktop.csproj
```

构建与契约测试：

```powershell
dotnet build .\desktop\Zvec.Desktop\Zvec.Desktop.csproj `
  --configuration Release --warnaserror

dotnet run `
  --project .\desktop\Zvec.Desktop.ContractTests\Zvec.Desktop.ContractTests.csproj `
  --configuration Release
```

## 原生配置

默认配置目录：

```text
%LOCALAPPDATA%\zvec-image-search
```

可通过 `ZVEC_CONFIG_HOME` 覆盖；旧环境变量 `ZVEC_DOCKER_CONFIG_HOME` 仅作为迁移兼容别名。schema v3 示例：

```json
{
  "schema_version": 3,
  "python_executable": null,
  "results_directory": "D:/ZvecData/results",
  "default_library_id": "lib-main",
  "libraries": [
    {
      "id": "lib-main",
      "name": "人物图库",
      "image_root": "D:/Pictures/Portraits",
      "workspace_directory": "D:/ZvecData/portrait-workspace",
      "enabled": true
    }
  ]
}
```

已弃用的 `image_name`、`workspace_type`、`workspace_source` 不会写入新配置。旧 bind Workspace 可以直接迁移；旧 named volume 必须先导出到宿主机目录。桌面端遇到未迁移的 named volume 会明确停止，不会创建空 Workspace 或重新生成已有向量。详见 [从 Docker 迁移](../MIGRATION_FROM_DOCKER.md)。

## 标签行为

人工标签只应用到本次新增图片或用户明确选择的图片。标签来源分别保存：

- 人工标签
- 文件夹标签
- 待审核的模型建议
- 已接受的自动标签

文件夹标签从最近的有效父目录生成，会过滤 `120P`、`80张`、`1.2GB`、`850MB` 等数量和大小信息。若直接父目录只包含这些技术信息，则向上查找到 `image_root` 为止。例如：

```text
原神-刻晴-120P-1.2GB/001.jpg  → 原神-刻晴
原神/120P-1.2GB/001.jpg       → 原神
```

搜索框中的标签默认按包含关系匹配。已有完整标签“原神”时，输入“原”或“神”都可命中。英文匹配忽略大小写；all/any 语义保留，且没有匹配到标签时返回空结果，不会绕过过滤条件。

## 搜索策略与 Collection schema v4

文字搜索默认融合已审核标签与图片向量。Collection 完成描述向量回填后，搜索自动加入第三个本地通道：

1. 审核标签。
2. 图片视觉向量。
3. 审核标签与已接受短描述生成的描述向量。

跨 Collection 搜索先在各图库召回，再统一融合、去重和排序。没有描述向量的图库继续使用原有两路检索，不会被当作低相关结果。

Collection schema v4 新增 `metadata_text`、`metadata_text_hash` 和 `metadata_embedding`。v1、v2、v3 可通过 `zvec migrate-schema` 升级；迁移保留已有图片向量，`api_requests` 为 `0`，也不会在启动时自动生成描述向量。

“智能整理”页的“生成描述向量”是显式、可恢复的付费操作。它只发送已审核标签和短描述文本，不发送原图；已完成记录自动跳过，单图失败留待下次继续，标签在请求期间变化时拒绝写入旧结果。默认并发为 2，与其他 embedding 请求共用 48 RPM、80,000 TPM 的安全水位；Collection 校验与写入保持串行。

本轮不启用方案第三阶段的人工质量匹配、阈值校准或 `search-quality.json`。界面中的原始分数与诊断字段不等于校准后的概率。

## 智能整理与身份规则

智能整理页提供以下流程：

1. 选择图库和处理范围。
2. 预览文件夹标签、缓存命中、待调用图片数和预计费用。
3. 选择 `qwen3-vl-flash`，或由用户手动改为 `qwen3-vl-plus`。
4. 设置最大预算并确认图片将发送到第三方视觉服务。
5. 查看任务进度、Token、费用、失败项和模型建议。
6. 接受、编辑后接受或拒绝每张图片的标签。
7. 需要时单独点击“生成描述向量”，将已审核内容加入描述检索通道。

同一图片内容按 SHA-256、模型、提示词和 schema 版本缓存；相同内容位于不同文件夹时复用模型结果，但各自保留文件夹标签。视觉请求失败不会让图片索引失败，也不会覆盖人工标签。

真人姓名和 Cosplayer 名称只有在人工标签、文件夹、文件名、sidecar、可读 OCR 或水印等显式证据存在时才能确认，不能只凭面部或外观识别。角色名和作品名允许视觉推断；低证据结论标为建议，来源冲突标为冲突并进入审核。优先级为：

```text
人工标签 > 文件夹/文件名/sidecar/OCR/水印 > 视觉推断
```

## 后端与凭据安全

桌面端使用只监听 `127.0.0.1` 随机端口的本机 Python 后端。窗口关闭后，运行中的任务可以继续；再次打开时会连接原后端，并恢复最近的任务中心记录。空闲后端会安全退出，活动任务不会被强制终止。

若当前配置与原后端不一致，桌面端进入只读等待：允许查看旧任务，不允许提交新任务；旧任务结束后再按当前配置切换后端。这样可以避免两个后端同时打开同一 Collection。

API Key 与本机会话令牌保存在 Windows 凭据管理器。会话令牌不写入启动命令、实例登记 JSON 或日志；API Key 不写入后端配置、任务结果或日志。

后端不可用时，单图库兼容操作可以调用 `scripts/zvec.ps1`；跨 Collection 搜索不会静默退化成默认图库。配置更新遵循停止后端、原子保存、重新启动的顺序，失败时恢复旧配置。

## 发布与安装器

```powershell
.\scripts\publish-desktop.ps1 `
  -Version 0.4.0 `
  -SearchQualityGatePath .\artifacts\search-quality-validation\comparison.json `
  -RequireInstaller
```

输出包括 `win-x64`、`win-arm64` 自包含 ZIP、NSIS 每用户安装器、SPDX 2.3 SBOM、in-toto/SLSA provenance、质量报告、`desktop-release.json` 和校验和。没有正式质量报告时必须显式使用 `-AllowUncertifiedSearchQualityPreview`，产物会标为未认证 Preview。

安装、升级和卸载不会删除 API Key、配置目录、Workspace 或图片。升级前应先退出所有旧版桌面窗口；若仍有后台任务，先等待任务完成并让后端空闲退出。安装器检测到桌面程序仍在运行、安装目录所有权无法确认或架构 payload 不匹配时会安全停止。
安装器也会检查持久后端登记和本机端口；后台仍活跃或状态无法确认时，升级与卸载都会停止，不会强制结束任务。

`WorkspaceMigrationService` 为界面提供独立的备份预演、创建备份、迁移预演和执行迁移数据模型。它调用 `workspace-backup` 与 `migrate-docker-workspace` 的 JSON 接口；默认只备份小型关键状态，完整向量备份必须由用户显式选择。`LauncherConfigService.InspectAsync` 可以在正常配置尚未加载成功前只读识别旧 schema、named volume 名称、默认导出目录、默认备份根目录和 `api_requests: 0`。旧 bind 配置会委托原生服务完成备份、schema migration、rebind 和零 API 验证；named volume 则先明确阻断，等待用户确认一次性导出。

## 已知限制

- 当前窗口同一时间只运行一个前台任务。
- ARM64 安装器已验证 WPF payload 架构，但仍需 ARM64 Windows 实机启动回归；其 Python 后端使用 x64 CPython 仿真，不应选择 ARM64 Python。
- 安装器不携带 Python；用户仍需安装 Python 3.10+，但隔离 venv 与锁定依赖由桌面端首次启动自动准备和修复。后续可把固定 Python 运行时作为独立签名组件随安装器分发。
- 智能整理不是 OCR 管线；只有已提供或模型可明确读取的文字证据才能用于身份确认。
- 当前没有自动升级器，生产 Authenticode 证书和公开 Release 流程仍需运营验证。

最佳实践：让桌面端管理 `%LOCALAPPDATA%\zvec-image-search\runtime\venv`，系统 Python 仅作为首次创建或损坏重建时的基础解释器。
