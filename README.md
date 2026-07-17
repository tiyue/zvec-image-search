# Zvec 图片检索

Zvec 使用阿里云百炼 `qwen3-vl-embedding` 和 Zvec 为本地图库建立索引，支持文字搜图、以图搜图、图文联合检索、本地模糊标签搜索，以及 Windows 桌面端的单图库和跨 Collection 查询。文字检索可融合审核标签、图片向量和已生成的描述向量。

日常运行采用宿主机原生 Python，不需要 Docker、Compose、WSL 或硬件虚拟化。Docker 只用于从旧 named volume 一次性导出 Workspace。

## 系统要求

| 场景 | 要求 |
|---|---|
| CLI | Windows 10/11 或近期 Ubuntu；Python 3.10+ |
| Windows 桌面端 | Windows 10/11；安装包不捆绑 Python |
| 桌面端源码构建 | .NET SDK 8.0.422 |
| 索引与语义搜索 | 阿里云百炼 `DASHSCOPE_API_KEY` |

`zvec==0.5.1` 提供 Windows x64、Linux AMD64 和 Linux ARM64 wheel。包管理器只下载与当前 Python 和 CPU 匹配的 wheel。Windows ARM64 暂无原生 wheel，后端必须使用 x64 CPython，由 Windows 仿真运行；桌面程序本身提供 `win-x64` 和 `win-arm64` 包。

## Windows 桌面端首次使用

1. 安装与电脑架构匹配的桌面包并启动程序。
2. 在“图库设置”中填写图库名称、图片目录、Workspace、搜索结果目录和 DashScope API Key。
3. 等待首次检查；运行环境未就绪时点击“自动修复运行环境”。
4. 保存配置，在“索引与同步”建立索引，然后进入“搜索”。如需边索引边生成待审核标签，可选择“同时生成智能标签（仅本次新增）”。

首次检查只报告环境状态。点击“自动修复运行环境”或首次执行需要后端的操作时，程序会按需在 `%LOCALAPPDATA%\zvec-image-search\runtime\venv` 创建当前用户专用环境并验证依赖。通常无需指定 Python；自动检测失败时可在高级设置中选择 Python 3.10+，Windows ARM64 必须选择 x64 Python。API Key 保存在当前用户的 Windows 凭据管理器中。

桌面端支持图库管理、单 Collection 索引与维护、跨 Collection 搜索、分页预览、任务进度与取消、智能整理审核，以及无可靠结果和低置信度诊断展示。以图搜图支持选图、拖放、剪贴板粘贴和从目录选图；结果每页 15 张，在可读尺寸允许时自动铺满画廊，小窗口自动改用滚动布局。详见 [Zvec Desktop](./desktop/README.md)。

## 后台任务与重新打开

点击窗口右上角关闭按钮只会隐藏到系统托盘，不会停止索引、同步、搜索或智能整理。单击托盘图标，或再次启动 Zvec，即可恢复同一个窗口，不会另起进程争用 Collection。

只有页面右上方的“退出”或托盘菜单“退出 Zvec”会完整结束程序。退出时，本地前台操作会先确认并取消；可恢复的后端任务会继续运行，下次启动后在“状态与诊断”中恢复。若新窗口使用了不同配置，程序会先以只读方式等待旧任务结束，再切换到当前配置。

本机后端的会话令牌保存在 Windows 凭据管理器中，不进入启动命令、实例登记 JSON 或日志。

升级前请从页面或系统托盘明确退出 Zvec。如仍有后台任务，请先等待任务完成，再运行新安装包。
安装器会检查桌面窗口和持久后端；任一仍在运行时都会停止升级或卸载，不会强制结束任务。

## 并发、限流与失败处理

程序采用有界并发，默认值如下：

| 阶段 | 默认并发 |
|---|---:|
| 文件扫描、校验与 SHA-256 | 4 |
| `qwen3-vl-embedding` | 2 |
| 描述向量回填 | 2 |
| 自动标注 | 2 |

每个模型按 48 RPM、80,000 TPM 的安全水位运行，60 RPM、100,000 TPM 为硬上限。429、超时和服务端临时错误会进入受控退避，不会通过无限增加线程绕过限流。

不同图库拥有独立工作线程，可以并行执行任务；同一图库保持单写者，Zvec 和 SQLite 提交始终按顺序完成。

索引页的“同时生成智能标签（仅本次新增）”默认关闭。开启后，桌面端提交单一 `index_and_auto_tag` 任务：向量模型与当前主标注模型使用各自独立、受限的请求工作阶段，新图片完成索引提交后即可进入标注，因此两个有界阶段可以重叠推进。该模式不并发写入 Collection，也不保证每个请求在网络层精确重叠；实际吞吐仍受 RPM、TPM、预算、图片大小和响应时间限制。运行前必须设置模型与预算，并明确确认图片会发送到第三方视觉服务；旧后端或后端不可用时会直接拒绝，不会退化为两个顺序任务。

描述向量回填复用 `qwen3-vl-embedding` 的并发配置和进程级 RPM/TPM 限流。网络请求有限并发，Collection 校验与写入保持串行。回填不会随启动、迁移、索引或搜索自动执行；只有用户明确触发时才会调用 API 并产生费用。

单图输入错误会跳过，不中断整批。最终失败图片按 SHA-256 去重复制到 `<搜索结果目录>/failed-images/blobs`，完整记录写入 `failed-images/jobs/<任务 ID>.jsonl`；重试型网络错误只记录，不复制健康图片。鉴权、磁盘或索引存储等系统性错误会暂停任务并标记“需要处理”，不会继续删除或冒险写入数据。

桌面端“状态与诊断”页可查看进度、取消任务、打开失败目录和运行日志；长任务期间该页面始终可用。

## CLI 初始化

Windows 可安装当前用户的 `zvec` 命令入口：

~~~powershell
.\scripts\install-zvec-command.cmd
zvec init "D:\Pictures"
zvec doctor
zvec index
~~~

安装后若当前终端找不到 `zvec`，请重新打开终端。`zvec init` 会在交互终端询问 API Key，也可提前设置 `DASHSCOPE_API_KEY`；使用 `--workspace` 和 `--results` 可指定数据目录。

也可以手动创建虚拟环境：

~~~powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --constraint .\requirements-lock.txt .
.\.venv\Scripts\zvec.exe init "D:\Pictures"
~~~

Ubuntu 使用 `.venv/bin/python` 和 `.venv/bin/zvec`。配置目录默认为 Windows 的 `%LOCALAPPDATA%\zvec-image-search` 或 Linux 的 `~/.zvec-image-search`，可用 `ZVEC_CONFIG_HOME` 覆盖。

## CLI 常用命令

~~~powershell
# 索引与同步
zvec index 人物 写真
zvec sync --dry-run
zvec sync

# 搜索
zvec search "海边日落" --tk 10
zvec search "神" --search-mode tags --tk 10
zvec search-image "D:\Queries\example.jpg" --tk 10
zvec search-mix "D:\Queries\example.jpg" "红色礼服" --tk 10

# 状态与维护
zvec stats
zvec roots
zvec results
zvec clean 7
zvec cache-clear
zvec doctor
zvec rebind-root <root-id> "D:\Pictures"
zvec migrate-schema --dry-run
zvec raw metadata-backfill --max-images 200
~~~

运行 `zvec help` 查看完整入口；`zvec raw --help` 查看底层命令。

## 多图库

每个图库拥有独立的图片根目录和 Workspace。CLI 使用 `--library` 选择一个图库；桌面端可选择“全部图库”执行跨 Collection 搜索。

~~~powershell
zvec library-list
zvec library-add "工作图库" "D:\WorkPictures"
zvec index --library "工作图库"
zvec search "白发角色" --library "工作图库"
~~~

跨图库会分别召回候选，再统一融合、按 SHA-256 去重并保留图库来源。默认启用套图多样化：候选套图足够时，前 20 张中每个套图最多展示 2 张；套图不足时自动放宽。桌面端可关闭该选项以恢复原始相关性顺序。

## 搜索与标签

文字、图片和图文搜索均支持 Top-K 与标签过滤。文字搜索默认自动融合已审核标签和图片向量；完成描述向量回填后，再加入描述文本通道。桌面端“文字搜图”可关闭 AI 语义搜索；关闭后只在本地模糊匹配已有标签，不调用 embedding 模型，也不产生模型费用。以图搜图可选择文件、拖入图片或文件夹、粘贴剪贴板图片，或先选目录再选图。搜索结果按每页 15 张显示，可前后翻页，并在窗口空间足够时自动铺满画廊；小窗口自动保留可读卡片尺寸并滚动。低置信度候选只在用户明确启用诊断模式时显示。

~~~powershell
zvec search "紫色角色" --tags 原
zvec search "神" --search-mode tags
zvec search "角色立绘" --tags 原 神 --tag-mode all
zvec search "角色立绘" --tags 原 崩 --tag-mode any
~~~

- `zvec index` 后输入的人工标签只应用到本次新增图片，不覆盖已有图片。
- 文件夹标签在本地生成，不调用模型，也不产生费用。
- `--tags` 使用 Unicode NFKC 标准化后的包含匹配，英文忽略大小写。
- `--search-mode tags` 将查询文字作为标签片段；例如“原”或“神”都可命中“原神”，且不会请求大模型。
- `all` 要求所有输入片段命中；`any` 只要求一个片段命中。

文件夹名中的数量和大小信息会被清理：

~~~text
原神-刻晴-旗袍-120P-1.2GB  →  原神-刻晴-旗袍
作品/120P-1.2GB/001.jpg     →  作品
~~~

## Collection schema v4 与描述向量

Collection schema v4 新增 `metadata_text`、`metadata_text_hash` 和 `metadata_embedding`。描述文本只取人工标签、文件夹标签、已接受的模型标签，以及审核通过的短描述；待审核或已拒绝内容不会进入检索。

旧 Collection 可原地升级：

~~~powershell
zvec migrate-schema --dry-run
zvec migrate-schema
~~~

v1、v2、v3 升级到 v4 会保留已有图片向量，创建备份并校验文档数，报告中的 `api_requests` 为 `0`。迁移只为描述通道建立空占位，不会自动调用模型。

升级后，可在桌面端“智能整理”点击“生成描述向量”，或显式运行：

~~~powershell
zvec raw metadata-backfill --max-images 200
~~~

回填按内容哈希断点续跑，成功一张即提交一张；单图失败会跳过并留待下次处理，运行中标签发生变化时不会写入陈旧向量。默认有限并发为 2，并与其他 embedding 请求共用 48 RPM、80,000 TPM 的安全水位。该操作会调用阿里云 Embedding API，可能产生费用；程序不会在启动、迁移、索引或搜索时自动执行。

未回填描述向量的图库继续使用原有标签和图片向量检索。文字与图文搜索复用同一个查询文本向量，不会为了描述通道额外增加查询 API 次数。

本轮暂不执行方案第三阶段的人工质量匹配、阈值校准或 `search-quality.json` 启用。现有诊断信息保持原行为，不应视为经过人工校准的概率。

## 智能整理

桌面端“智能整理”默认使用 `qwen3-vl-flash` 生成待审核标签；只有身份或字段冲突、关键结果置信度不足时才升级到 `qwen3-vl-plus`。主模型和升级模型可在“模型配置”页切换。

运行前会显示图片数、缓存命中、费用估算和预算上限，并要求确认图片将发送到第三方视觉服务。同图按 SHA-256、模型、提示词和 schema 版本复用缓存；只有人工接受的建议才进入有效标签。

当前标签体系面向人物、Cosplay、写真和二次元图库，使用 17 个稳定字段及受控词表，覆盖人数、景别、动作、神态、视线、外观、服装、道具、场景和摄影特征。真人姓名和 Cosplayer 名称只能依据人工标签、目录、文件名、sidecar、程序实际读取到的 OCR 或水印等显式证据确认，不能仅凭外观识别；证据不足时显示“无法确认”，不会强行猜测。

视觉模型价格可能变化，正式使用前请核对阿里云最新价格；配置示例见 [.env.example](./.env.example)。

## 模型配置

所有可选模型均限定为阿里云 DashScope。桌面端“模型配置”页可切换三个角色：

- `embedding`：索引与语义搜索。
- `auto_tag_primary`：常规智能标注。
- `auto_tag_escalation`：疑难图片复核。

用户配置默认保存在配置目录的 `models.json`：Windows 为 `%LOCALAPPDATA%\zvec-image-search\models.json`，Linux 为 `~/.zvec-image-search/models.json`。`ZVEC_CONFIG_HOME` 可更改配置目录；`ZVEC_MODELS_CONFIG` 可直接指定文件。页面可直接打开该文件。向 `models` 数组加入兼容模型后重新载入，即可在页面和智能整理任务中选择。只支持以下协议：

- `dashscope_multimodal_embedding`
- `dashscope_multimodal_conversation`

API Key 不进入 JSON，仍保存在 Windows 凭据管理器或运行环境中。保存模型配置不会中断正在运行的任务；后端空闲后会安全切换。向量模型必须输出 1024 维。更换向量模型后，已有 Collection 会拒绝混用旧向量，需要新建或重建索引；更换标注模型不需要重建向量索引。

仓库中的 [model-catalog.default.json](./model-catalog.default.json)（桌面包路径为 `backend/model-catalog.default.json`）提供与内置默认值一致的参考模板；实际用户配置仍是 `models.json`。未知 provider、字段、角色、协议、重复模型、无效价格或不兼容维度会被拒绝。

## Workspace 备份与 Docker 迁移

迁移、升级或人工维护前，先预演再备份：

~~~powershell
zvec workspace-backup --dry-run --destination "D:\ZvecBackups\before-migration"
zvec workspace-backup --destination "D:\ZvecBackups\before-migration"
~~~

默认 metadata 备份保存配置、Collection 元数据、SQLite 状态和小型质量配置，不复制大型向量 Collection；完整备份需显式增加 `--full`。

旧 bind Workspace 可直接迁移，不需要 Docker。旧 named volume 必须在仍能访问原 volume 的 Docker Engine 上显式导出：

~~~powershell
zvec migrate-docker-workspace --dry-run --destination "D:\ZvecData\workspace" --library "默认图库"
zvec migrate-docker-workspace --destination "D:\ZvecData\workspace" --backup-directory "D:\ZvecBackups\docker-migration" --library "默认图库"
zvec verify-native --library "默认图库"
~~~

迁移保留原数据并复用已有向量，同时可将 Collection 升级到 schema v4。迁移与 schema 升级报告中的 `api_requests` 必须为 `0`；描述向量回填是迁移完成后的独立、付费操作。完整步骤和回滚边界见 [从 Docker 迁移](./MIGRATION_FROM_DOCKER.md)。

## 开发与测试

~~~powershell
python -m pip install --constraint .\requirements-lock.txt --editable . --requirement .\requirements-dev.txt
python -m ruff check .
python -m ruff format --check .
python -m mypy image_vector_service image_service.py zvec_launcher.py zvec_logging.py
python -m unittest discover -s tests -v

powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\test_launcher.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\test_release_workflow.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\test_persistent_backend_installer_guard.ps1

dotnet build .\desktop\Zvec.Desktop\Zvec.Desktop.csproj --configuration Release --warnaserror
dotnet run --project .\desktop\Zvec.Desktop.ContractTests\Zvec.Desktop.ContractTests.csproj --configuration Release
~~~

从源码启动桌面端：

~~~powershell
dotnet run --project .\desktop\Zvec.Desktop\Zvec.Desktop.csproj
~~~

CI 在 Windows 和 Ubuntu 上验证原生 Python 包、CLI、迁移流程和桌面发布资产；默认不构建 Docker 镜像。

## 发布入口

本轮未执行第三阶段质量匹配，应生成未认证 Preview：

~~~powershell
.\scripts\publish-desktop.ps1 -Version 0.4.0 -AllowUncertifiedSearchQualityPreview -RequireInstaller
~~~

后续完成人工评测和阈值校准后，才可生成正式质量门禁候选：

~~~powershell
.\scripts\publish-desktop.ps1 -Version 0.4.0 -SearchQualityGatePath .\artifacts\search-quality-validation\comparison.json -RequireInstaller
~~~

发布脚本可生成 `win-x64`、`win-arm64` 自包含 ZIP、NSIS 每用户安装器、SHA-256 清单、SPDX SBOM、provenance 和发布元数据。可构建候选不等于已公开发布稳定版；完整门禁见 [发布说明](./RELEASE.md) 与 [供应链说明](./SUPPLY_CHAIN.md)。

最佳实践：将 Workspace 和搜索结果目录放在图片根目录之外，并纳入独立备份；迁移或重绑前先运行 `--dry-run`。
