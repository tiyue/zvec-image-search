# Zvec Desktop 0.4.0 候选版

本版提供 Windows x64 原生 Python 后端和 Vue 3 桌面界面。发布包冻结 Python、WebView2 桥接和前端静态资源；用户运行时不需要 Node.js、PowerShell、.NET、Docker、WSL 或单独安装 Python。

当前状态：Windows x64、未签名候选。尚未达到已签名稳定版发布门槛。

## 本版更新

### Vue 3 桌面界面

- 前端迁移至 Vue 3、TypeScript 和 Vite。
- Python 负责窗口、后台、文件操作和生命周期；Vue 只负责显示与交互。
- 四个页面统一为现代化响应式布局：图片搜索、图库任务、批量标签和设置。
- 发布包仅包含编译后的 HTML、CSS 和 JavaScript，不包含 Node.js、源码或 `node_modules`。
- 所有页面资源随包提供，不加载 CDN、远程字体或示例图片。

### 搜索与图片操作

- 搜索数量由用户输入正整数，不再限制为固定选项。
- 修复用户修改搜索数量后，数字值被当作文本调用 `.trim()` 导致请求未发出的故障。
- 搜索按钮改为显式点击，搜索框回车独立处理，不再依赖隐式表单提交。
- 本地请求具备 30 秒硬超时，完整搜索具备 120 秒看门狗；失败后可直接重试，迟到响应不会覆盖新结果。
- 前端错误、未处理 Promise 和搜索阶段写入脱敏轮转日志；日志失败不影响搜索。
- 搜索结果固定每页 15 张，正常窗口采用 5×3 画廊，小窗口自动滚动。
- 删除独立结果摘要栏；结果数和状态收进画廊标题，耗时、查询方式和候选数写入操作日志。
- 缩略图和预览使用完整显示，不裁切人物、横图或方图。
- 右侧面板显示命中标签、全部标签、图库、来源、置信度、分数、尺寸、文件大小和相对路径。
- 双击画廊图片可调用 Windows 默认程序打开原图。
- “系统打开”调用默认程序；“所在文件夹”打开 Explorer 并选中文件。
- 左侧可清理历史搜索结果，默认保留最近 3 次。
- 清理只处理具备有效归属标记和结果清单的搜索结果目录；缓存、失败图片、原图和用户目录保持不变。
- 不提供应用内全屏查看。

### 搜索质量与本地学习

- 搜索结果支持明确标记“相关”或“不相关”；打开、复制和导出可作为可关闭的低权重反馈。
- 反馈、训练样本、候选模型和版本记录只保存在本机，不调用额外大模型 API。
- 达到最低样本量后可后台训练候选排序版本；训练失败或取消不会影响正常搜索。
- 固定评测包可从设置页导入。没有评测包时，候选版本保持待评测，不能绕过质量门禁。
- 设置页显示 Precision@15、Recall@15、无答案误报率、跨图库偏置、P95 延迟和额外 API 调用，并给出未通过原因。
- 通过门禁的版本可先做影子验证，再正式启用或切回上一版本；常驻后端会安全重载活动配置。
- 模型文件损坏、特征版本不兼容或加载失败时，搜索自动回退到固定排序。

### 批量标签与自动标注

- 新增独立“智能标注”任务，可处理已有索引但尚未标注的图片，不重建索引，也不调用向量模型。
- 默认只处理未标注或源图片已变化的图片；支持最近索引、失败重试和全部重跑范围。
- 标注前可估算候选数、缓存命中、API 请求、Token 和费用；全部重跑要求额外确认。
- 按图库、根目录和文件夹浏览图片，支持单选、Ctrl/Shift 多选、当前页和整文件夹选择。
- 空文件夹不进入可选目录；文件夹按首次成功入库时间倒序排列。
- 画廊按实际可用空间计算每页数量，小窗口回退到可滚动布局；双击缩略图调用 Windows 默认程序打开原图。
- 批量添加、移除或替换人工标签，并可撤销最近一次批量操作。
- 标签显示人工、文件夹、模型和同文件夹继承来源；别名词典可直接新增或更新。
- 通过策略校验的模型标签自动批准，真人、Cosplayer、角色和作品身份不再逐项确认。
- 文件夹清理采用“预览确认—安全暂存—数据库提交—最终清理”流程；异常退出后可恢复未完成操作。
- 清理同步移除对应 Collection 向量和 SQLite 图片、标签、标注记录，不调用模型，也不触碰范围外目录、缓存、搜索结果或失败图片副本。

### 相似分组与主动学习

- 使用已有 SHA-256、感知哈希和图片向量生成精确重复、近似重复和语义相似分组，不重新生成 embedding；为避免大图库默认逐图执行向量查询，内容相似改为按需手动开启。
- 支持人工合并分组、拆出错误成员、整组添加身份标签，以及撤销最近一次整理。
- 身份标签只允许真人、Cosplayer、角色和作品；动作、神态、场景、镜头和构图不会被整组传播。
- 人工合并和拆分分别持久化为 must-link 与 must-not-link 规则，重新聚类后仍可复用。
- 主动学习审核会产生真实标签修改；批次和单项结果持久保存，支持撤销最近一次审核。
- 单项失败、冲突或源图片变化不会终止整批，失败原因会单独记录。
- 聚类快照、人工规则、操作前后状态和撤销记录写入图库 SQLite；进程异常退出后，未完成操作标记为需处理，不伪装成功。
- 分组列表、成员和内部边使用 SQLite 有界分页，不再为查看一页而加载完整 JSON 快照。

### 任务历史与操作日志

- 图库任务页改为紧凑的任务历史表和操作日志表，宽屏并排，中小窗口在工作区内上下排列。
- 支持状态、类型、图库、级别、类别、关键词和任务编号筛选；历史与日志使用游标分页，每页最多 50 条。
- 点击任务错误数会按 `job_id` 筛选日志并展开错误图片；单图失败仍继续整批任务。
- 支持安全取消、实时跟随与暂停、复制选中日志，以及当前筛选页 JSONL/CSV 导出。
- 任务历史和结构化日志写入 `%LOCALAPPDATA%\zvec-image-search\activity.sqlite3`，后端重启后仍可查询；未完成的旧任务标记为已中断，不伪装为成功。
- 默认保留 30 天或最多 50,000 条日志、10,000 条任务历史。密钥、Token、Cookie、密码、图片数据和完整模型输入输出不会写入活动库。

### 图库设置

- 图库名称、原图目录、Workspace、启用状态和默认图库可从设置页修改。
- 全局结果目录可编辑。
- 路径必须是 Windows 宿主绝对路径；原图、Workspace 和结果目录不能危险嵌套。
- `library_id` 在路径修改后保持不变。
- 配置采用原子保存；失败不会破坏旧配置。
- 有活动任务时，界面会提示配置需要稍后重启生效。

### 结果与缩略图缓存

- 桌面搜索采用 `results.sqlite3` 保存有序结果，界面内存只保留当前 15 条；CLI 继续兼容原有导出行为。
- 结果页可以直接跳转到较后页，不需要先解析或复制前面的全部图片。
- Manifest 按规范化路径、修改时间和大小自动失效；修改、替换和删除均能重新发现。
- 并发翻页共享同一结果存储，避免重复扫描大结果集。
- 缩略图使用内存与磁盘两级缓存，并限制同时渲染数量。
- 缩略图磁盘缓存位于 `%LOCALAPPDATA%\zvec-image-search\cache\thumbnails-v1`。
- 清理这些界面缓存不会调用模型、重建向量或修改 Collection。

### 大图库读写

- 每个图库的待处理队列有固定容量，搜索使用高优先级；队列已满时返回可重试的 429，不会无限占用内存。
- 长任务定期协作让出执行权，索引或标注期间仍可响应同图库搜索与状态请求。
- 索引扫描使用磁盘 staging，Collection 与 SQLite 状态按 256 条批量提交；扫描临时库在中断后安全重建，不作为恢复日志。
- Collection Outbox 只重放已经持久化的向量和元数据，不重新调用 embedding 模型。
- 标签查询在 SQLite 中直接完成 Top-N；元数据回填、路径迁移和大图库聚类使用有界分页及批量读取。
- 超过 10,000 张且没有人工聚类规则的图库使用流式 SQLite 聚类；人工规则场景继续使用兼容引擎。
- `Collection.optimize()` 改为累计维护：达到 2,000 次变更、500 次删除、删除比例或最长 12 小时时，等待图库连续空闲 2 秒后执行；失败按指数退避重试。
- 同文件夹拒标继承使用 500 条分页和全局内存预算，不再保留整个文件夹的成员对象或无限审计详情。
- CI 新增十万级大图库门禁和每日百万级回归；源码架构检查会拒绝全表读取、直接 Collection 写入、任务尾部同步压缩和无界桌面结果。
- `/health`、`/version` 与后端启动事件暴露无路径、无凭据的运行时策略快照及 SHA-256，可用于排查队列、分页、批写和空闲维护参数漂移。

### 并发与容错

- 界面线程不执行长时间后端任务。
- 不同图库可并行；同一图库保持单写入者。
- 单图失败会跳过并记录，不终止整批任务。
- 429、超时和临时网络错误采用受控退避。
- 最终失败图片保存到 `failed-images/blobs`，任务清单保存到 `failed-images/jobs`。

### 模型与凭据

- 默认向量模型：`qwen3-vl-embedding`。
- 默认标注模型：`qwen3-vl-flash`。
- 疑难复核默认可使用 `qwen3-vl-plus`。
- 模型可从设置页或 `%LOCALAPPDATA%\zvec-image-search\models.json` 修改。
- API Key 保存在 Windows 凭据管理器，不进入 JSON、前端或日志。

## 预期发布产物

以下是完成最终构建和校验后采用的文件名。本说明不代表这些文件已经由当前源码重新生成；实际交付必须同时提供 SHA-256 和验证报告。

```text
Zvec-Desktop-0.4.0-win-x64-unsigned-setup.exe
Zvec-Desktop-0.4.0-win-x64-portable.zip
Zvec-Webview-Preview-0.4.0-win-x64-portable.zip
Zvec-Webview-Preview-0.4.0-win-x64-unsigned-setup.exe
```

Vue Preview 使用独立入口：

```text
Zvec.WebviewPreview.exe
```

Preview 不安装或覆盖旧桌面版。冻结包包含 `zvec-backend.exe`、`zvec.exe`、Python 运行库、WebView2 桥接和 `zvec_webview/frontend_dist`。

包内不得出现：

- `node_modules`
- `frontend/src`
- 旧 `zvec_webview/assets`
- macOS、Linux、ARM64 或 x86 运行时
- WPF、Tk 或 PowerShell 启动链

## 便携包使用

1. 将 ZIP 完整解压到普通可写目录。
2. 不要直接在 ZIP 内运行。
3. 不要移动单个 EXE，也不要删除 `_internal`。
4. 运行 `Zvec.WebviewPreview.exe`。
5. 升级前等待任务结束，并从程序内明确退出。

便携版与安装版复用 `%LOCALAPPDATA%\zvec-image-search` 下的配置和缓存。删除便携目录不会删除原图、Workspace 或 Collection。

## 升级与兼容

- 继续使用现有 schema v3 配置、Workspace、Collection、SQLite、结果目录和 Windows 凭据。
- Collection schema migration 保留已有向量，不调用模型。
- 旧 bind Workspace 可迁移后直接使用。
- 旧 Docker named volume 只在一次性导出时需要 Docker；日常运行不需要。
- 更换标注模型不需要重建索引。
- 更换向量模型后必须新建或重建 Collection。

迁移步骤见 [MIGRATION_FROM_DOCKER.md](./MIGRATION_FROM_DOCKER.md)。

## 开发与验证

Python：

```text
python -m venv .venv
.venv\Scripts\python.exe -m pip install --constraint requirements-lock.txt --editable . --requirement requirements-dev.txt
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m mypy image_vector_service zvec_desktop zvec_webview image_service.py zvec_launcher.py zvec_logging.py scripts
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Vue：

```text
cd frontend
npm.cmd ci
npm.cmd run typecheck
npm.cmd test
npm.cmd run build
```

完整 Windows x64 Preview 构建：

```text
python -m pip install --requirement requirements-webview-preview-lock.txt
python scripts/provision_nsis.py --output-directory build/tools/nsis-3.12
python scripts/build_webview_preview.py --dry-run
python scripts/build_webview_preview.py --makensis build/tools/nsis-3.12/nsis-3.12/Bin/makensis.exe
```

构建脚本必须验证 Vite manifest、资源闭包、冻结入口、x64 WebView2、payload 清单、便携 ZIP 和 NSIS 安装器；冻结自检还会导入搜索学习、主动学习、聚类持久层、活动记录、安全删除和图库目录模块，并实际验证 SQLite WAL 及本地学习状态库可写。

## 已知限制

- 当前产物未签名，SmartScreen 可能提示风险。
- 只发布 Windows x64；不提供 macOS、Linux、ARM64 或 x86 版本。
- 尚无自动更新。
- 阿里云模型需要网络、有效 API Key 和可用额度。
- Node.js 只属于开发与构建环境，不属于用户运行环境。

最佳实践：每次发布都提高版本号并重新生成 SHA-256，不覆盖已经交付的同版本文件。
