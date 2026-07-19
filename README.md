# Zvec 图片检索

Zvec 是面向本地图库的检索与整理工具。它使用 Zvec 保存向量索引，调用阿里云百炼模型生成图片向量和结构化标签，支持文字搜图、以图搜图、图文联合、模糊标签和跨图库查询。

桌面版仅支持 Windows x64。运行时不需要 PowerShell、.NET、Docker、WSL、Node.js 或单独安装 Python。

## 下载与运行

Windows x64 构建采用以下命名：

```text
Zvec-Desktop-<version>-win-x64-unsigned-setup.exe
Zvec-Desktop-<version>-win-x64-portable.zip
Zvec-Webview-Preview-<version>-win-x64-portable.zip
Zvec-Webview-Preview-<version>-win-x64-unsigned-setup.exe
```

安装包按当前用户安装，不要求管理员权限。便携包必须完整解压后运行，不能直接在 ZIP 内启动，也不要拆散同目录的 EXE、DLL 和 `_internal`。

Vue 3 界面当前以独立 Preview 交付。完整解压后运行：

```text
Zvec.WebviewPreview.exe
```

Preview 复用现有 `config.json`、`models.json`、Workspace、Collection 和 Windows 凭据，不安装或覆盖旧桌面版。关闭窗口不会强制终止正在运行的索引或标注任务。

当前候选未进行 Authenticode 签名。Windows SmartScreen 出现提示时，应先确认下载来源和 SHA-256，再决定是否选择“更多信息”→“仍要运行”。

具体功能是否已进入某个安装包，以该包对应的 Release 说明、构建时间和 SHA-256 清单为准。

## 主要功能

| 页面 | 功能 |
|---|---|
| 图片搜索 | 文字、图片、图文联合、模糊标签、跨图库搜索；每页 15 张；显示命中标签、来源、分数和置信度 |
| 图库任务 | 建立索引、同步、索引并标注、独立智能标注、费用估算、取消、持久任务历史和操作日志 |
| 批量标签 | 按非空文件夹浏览；动态分页和多选；批量修改人工标签；支持撤销、标签来源、别名词典和安全文件夹清理 |
| 设置 | 首次建库、编辑图库与结果目录、模型选择、JSON 配置和 API Key |

搜索结果默认采用 5×3 画廊。小窗口自动减少列数并启用滚动。缩略图和右侧预览始终完整显示，不裁切原图。独立结果摘要栏已移除，结果数和状态收进画廊标题，查询方式、耗时和候选数进入操作日志。

搜索按钮和输入框回车分别触发同一条受控搜索链路。单次本地请求默认 30 秒超时，完整搜索默认 120 秒看门狗；超时、取消或失败后按钮会自动恢复，迟到响应不会覆盖新结果。前端诊断日志位于：

```text
%LOCALAPPDATA%\zvec-image-search\logs\frontend-diagnostics.jsonl
```

日志只记录阶段、状态、耗时和计数，不记录查询正文、图片路径或 API Key。

任务历史与可检索的结构化操作日志保存在：

```text
%LOCALAPPDATA%\zvec-image-search\activity.sqlite3
```

该数据库与图库 Workspace、Collection 和失败图片目录分离，使用 WAL 模式。默认保留 30 天或最多 50,000 条操作日志、10,000 条任务历史；活动任务不会被保留清理误删。API Key、Bearer Token、Cookie、密码、图片二进制和完整模型 Prompt/响应不会写入其中。

交互方式：

- 单击图片：查看标签和详细信息。
- 双击图片：调用 Windows 默认程序打开原图。
- “系统打开”：调用默认程序打开当前图片。
- “所在文件夹”：打开 Windows Explorer 并选中该文件。
- 左侧“清理搜索结果”：默认保留最近 3 次，只删除具备有效归属标记和结果清单的搜索结果目录。
- 不提供应用内全屏查看。

搜索结果清理不会删除缩略图缓存、查询缓存、失败图片、原图、Workspace、Collection 或用户自行创建的目录。

## 智能标注与批量整理

“图库任务”可对已有索引单独执行智能标注。默认范围为“未标注或源图片已变化”；已有当前标注和缓存命中的图片会跳过。该任务不会调用 `qwen3-vl-embedding`，也不会重建索引。页面提供候选数、缓存命中、预计请求、Token 和费用估算；选择“全部图片重新处理”需要额外确认。

“批量标签”只列出至少有一张成功入库图片的文件夹，并按首次成功入库时间从新到旧排列。图片数量根据画廊实际宽高动态计算；窗口缩放后保留当前位置。单击选择，双击调用 Windows 默认程序打开原图。

文件夹清理采用两阶段流程：

1. 预览路径、图片数、文件数、大小和安全阻断项。
2. 明确确认后，将目标暂存，再同步删除 Collection 向量和 SQLite 中的图片、标签与标注记录，最后清理暂存文件。

程序异常退出后会从删除日志恢复未完成操作。清理不调用模型，不处理预览范围外的文件，也不会删除搜索结果、缓存、失败图片副本或其他用户目录。路径已变化、受保护目录、链接或状态不一致时会停止提交，而不是扩大删除范围。

## 首次使用

1. 打开“设置”，创建或编辑图库。
2. 确认原图目录、Workspace 和结果目录互不嵌套。
3. 保存阿里云百炼 DashScope API Key。
4. 确认向量模型和标注模型。
5. 在“图库任务”建立索引；已有索引可直接选择“智能标注”。
6. 在“图片搜索”验证检索结果。
7. 如启用智能标注，在“批量标签”查看来源、补充人工标签或维护别名。

建议首次处理 10～50 张图片，确认目录、费用和搜索效果后再扩大批次。

## 模型与凭据

默认模型均来自阿里云百炼：

| 角色 | 默认模型 | 用途 |
|---|---|---|
| `embedding` | `qwen3-vl-embedding` | 图片索引、语义搜索和描述向量 |
| `auto_tag_primary` | `qwen3-vl-flash` | 常规智能标注 |
| `auto_tag_escalation` | `qwen3-vl-plus` | 疑难图片复核 |

模型可在设置页修改，也可编辑：

```text
%LOCALAPPDATA%\zvec-image-search\models.json
```

API Key 保存在当前用户的 Windows 凭据管理器，不写入 JSON、前端资源或日志。

更换标注模型不需要重建索引。更换向量模型后必须新建或重建 Collection，不能混用不同模型生成的向量。

通过策略校验的模型标签会自动批准，不要求逐项确认身份标签；来源和继承关系仍会保留，便于后续批量检查和修正。

## 缓存与性能

Preview 使用两类界面缓存：

- 结果清单缓存位于内存中。首次完整校验 `results.json`，后续翻页只读取当前 15 条；清单修改、替换或删除后自动失效。
- 缩略图缓存位于：

```text
%LOCALAPPDATA%\zvec-image-search\cache\thumbnails-v1
```

缩略图缓存有容量和数量上限，可跨重启复用。程序限制同时生成缩略图的数量，避免大量高分辨率照片同时解码造成内存峰值。

这些缓存只保存搜索展示数据和缩略图：

- 不调用大模型。
- 不重新生成向量。
- 不修改 Collection、SQLite 或原图。

需要清理缩略图缓存时，应先退出 Zvec，再删除 `thumbnails-v1`。程序会按需重建。CLI 的 `cache-clear` 清理的是查询向量缓存，与缩略图缓存不是同一项。

## 并发与错误隔离

界面、图片解码、后端任务和模型请求分开执行。不同图库可以并行，同一图库保持单写入者，避免损坏 Zvec 和 SQLite 状态。

默认安全水位：

- 文件扫描与哈希：并发 4。
- `qwen3-vl-embedding`：并发 2。
- 智能标注：并发 2。
- 每个模型 48 RPM、80,000 TPM；不会超过 60 RPM、100,000 TPM。

单张图片失败不会终止整批任务。最终失败图片按 SHA-256 去重保存到：

```text
<结果目录>\failed-images\blobs
```

任务错误清单保存到：

```text
<结果目录>\failed-images\jobs\<任务ID>.jsonl
```

## 数据与迁移

用户配置默认位于：

```text
%LOCALAPPDATA%\zvec-image-search
```

安装、升级和卸载不会删除原图、Workspace、Collection、结果目录或 Windows 凭据。

原生 schema v3 配置可直接复用。旧 bind Workspace 可原地迁移；旧 Docker named volume 需要先导出。Schema migration 和 rebind-root 保留已有向量，不调用模型。详见 [MIGRATION_FROM_DOCKER.md](./MIGRATION_FROM_DOCKER.md)。

迁移前先备份并执行预演：

```text
zvec.exe migrate-docker-workspace --dry-run --destination "D:\ZvecData\workspace"
zvec.exe migrate-schema --dry-run
```

迁移报告中的 `api_requests` 应为 `0`。

## 随包 CLI

安装包和便携包包含 `zvec.exe`，与桌面端共用配置：

```text
zvec.exe help
zvec.exe doctor
zvec.exe index
zvec.exe sync
zvec.exe search "海边日落" --tk 15
zvec.exe stats
```

普通使用不需要 CLI。

## 从源码开发

Python 后端需要 CPython 3.10+。Windows x64 发布构建固定使用 CPython 3.12。

```text
python -m venv .venv
.venv\Scripts\python.exe -m pip install --constraint requirements-lock.txt --editable . --requirement requirements-dev.txt
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m mypy image_vector_service zvec_desktop zvec_webview image_service.py zvec_launcher.py zvec_logging.py scripts
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Vue 前端开发需要 Node.js 20.19+。Node 只用于开发和构建，不进入发布包。

```text
cd frontend
npm.cmd ci
npm.cmd run typecheck
npm.cmd test
npm.cmd run dev
npm.cmd run build
```

`npm.cmd run build` 将经过类型检查的静态资源写入：

```text
zvec_webview\frontend_dist
```

联调前先构建前端，再启动 Python 宿主：

```text
python -m zvec_webview.app
```

## 构建 Windows x64 Preview

构建机需要 Windows x64、CPython 3.12、Node.js 20.19+ 和锁定依赖。构建脚本会依次执行 `npm ci`、类型检查、前端测试和 Vite 构建，再冻结 Python 宿主。

```text
python -m pip install --requirement requirements-webview-preview-lock.txt
python scripts/provision_nsis.py --output-directory build/tools/nsis-3.12
python scripts/build_webview_preview.py --dry-run
python scripts/build_webview_preview.py --makensis build/tools/nsis-3.12/nsis-3.12/Bin/makensis.exe
```

输出位于：

```text
dist\webview-preview\<version>\win-x64
```

省略 `--makensis` 时只生成便携包。发布包只包含 `frontend_dist` 的编译产物，不包含 `frontend/src`、Node.js 或 `node_modules`。完整发布边界见 [RELEASE.md](./RELEASE.md)。

最佳实践：原图、Workspace、结果目录和备份目录应彼此独立。
