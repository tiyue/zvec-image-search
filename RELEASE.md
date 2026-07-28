# YaoLens 0.1

本版提供 Windows x64 原生 Python 后端和 Vue 3 桌面界面。发布包冻结 Python、WebView2 桥接和前端静态资源；用户运行时不需要 Node.js、PowerShell、.NET、Docker、WSL 或单独安装 Python。

本次使用机器版本 `0.1.0`、公开展示版本 `0.1` 和稳定标签 `v0.1.0`。

## 本版更新

### 图库扫描、智能标注与任务进度修复

- 修复中文及其他非 ASCII 路径可能被错误识别为非法路径、导致新增图片无法被扫描发现的问题。
- 根据真实接口并发测试，将智能标注默认并发数从 2 调整为 4；在吞吐明显提升的同时避免 6 路并发触发额外重试。
- 修复“索引 + 智能标注”组合任务跨阶段时进度条先到 100% 再回退的问题；索引和标注现在映射到同一条单调递增的总进度。
- 组合任务结束时明确落盘 100% 终态，任务历史与实时任务视图保持一致。

### 配对可靠性热修复

- Windows 批准结果支持幂等恢复；批准响应短暂丢失后，重复查询同一请求仍可完成连接。
- Android 在等待和领取批准结果时复用同一配对请求，临时网络故障不会悄然创建另一组验证码。
- 自动发现不可用时可通过私网 IP 直接连接，连接前会明确校验目标地址与端口。
- 配对诊断进一步区分待批准、已批准、已拒绝、已过期和凭据恢复失败，便于定位“电脑已批准但手机无反应”。

### Windows 登录后常驻

- 安装版为当前用户注册 `AtLogon` 计划任务，使用 `InteractiveToken`、`LeastPrivilege`、`IgnoreNew`、无限执行时间和固定有限次数的失败重启，不要求管理员权限或保存用户密码。
- 用户登录后 YaoLens 隐藏常驻；关闭窗口只隐藏界面，后台任务、Android LAN 服务和自动增量索引 watcher 继续运行。再次启动会唤醒已有窗口。
- 完全停止必须使用“设置 → 应用 → 退出 YaoLens”；存在活动任务时退出请求会被拒绝，不会中断已接受的索引或标注任务。
- 便携包不注册计划任务；用户手工启动后只在当前登录会话内常驻，二次启动同样唤醒已有窗口。
- backend 子进程加入仅由宿主持有的 Windows Job Object。它只在宿主异常死亡时兜底清理孤儿进程；正常退出仍先执行现有的 idle-only 安全关闭协议。

### Vue 3 桌面界面

- 前端迁移至 Vue 3、TypeScript 和 Vite。
- Python 负责窗口、后台、文件操作和生命周期；Vue 只负责显示与交互。
- 四个页面统一为现代化响应式布局：图片搜索、图库任务、批量标签和设置。
- 发布包仅包含编译后的 HTML、CSS 和 JavaScript，不包含 Node.js、源码或 `node_modules`。
- 所有页面资源随包提供，不加载 CDN、远程字体或示例图片。

### Android 局域网查看

- 新增 Android 8.0 及以上局域网客户端。Windows 继续保存图库、运行本地向量引擎和调用模型。
- 支持 UDP 自动发现和手工私网 IP；配对无需二维码，两端核对相同 6 位验证码后由 Windows 批准。
- 支持选择图库、文字/标签/图片/图文搜索、分页查看、原图预览、保存和分享。
- 查询图片不设固定文件大小上限，按流上传；原图支持 Range、断点续传和多路并发流式传输。
- 对外 LAN 网关与本机控制接口隔离，只开放配对、搜索和原图读取，不开放设置、索引、迁移或图库删除。
- 撤销或重新配对会先持久化旧 Token 的哈希拒绝记录；即使凭据文件写入失败并重启，旧 Token 也不会恢复有效。
- APK 由固定 JDK、Android SDK 和 Gradle Wrapper 完成测试、lint、装配及 SHA-256 校验。

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
YaoLens-0.1-win-x64-portable.zip
YaoLens-0.1-win-x64-setup.exe
YaoLens-0.1-android.apk
```

登录常驻没有新增独立发布资产；GitHub Release 资产矩阵仍为上述 Windows 安装包、便携包、Android APK 及对应校验和、验证报告和策略元数据。

GitHub Release 标题固定为 `YaoLens 0.1`，并从精确指向工作流提交的稳定标签 `v0.1.0` 发布。

Windows 应用入口：

```text
YaoLens.exe
```

WebView 使用独立安装身份，不安装或覆盖已废弃的旧 Desktop 产品。冻结包包含内部组件 `zvec-backend.exe`、`zvec.exe`、Python 运行库、WebView2 桥接和 `zvec_webview/frontend_dist`。

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
4. 运行 `YaoLens.exe`。
5. 关闭窗口只会隐藏界面；再次运行会唤醒当前会话内的已有窗口。
6. 升级前等待任务结束，并使用“设置 → 应用 → 退出 YaoLens”明确退出。

便携版不会注册登录计划任务，只在用户手工启动后的当前登录会话内常驻。它与安装版复用 `%LOCALAPPDATA%\zvec-image-search` 下的配置和缓存；删除便携目录不会删除原图、Workspace 或 Collection。

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
.venv\Scripts\python.exe -m mypy image_vector_service zvec_host zvec_lan zvec_webview image_service.py zvec_launcher.py zvec_logging.py scripts
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

Android：

```text
cd android
gradlew.bat testDebugUnitTest assembleDebug lintDebug
```

Android 构建使用 JDK 17、SDK Platform 34 和 Build Tools 34.0.0。CI 生成 APK 和 SHA-256，并在发布装配时复核固定公开文件名。

完整 Windows x64 构建由 `.github/workflows/release.yml` 使用固定 Python、NSIS 和前端依赖执行。本地发布契约检查：

```text
python -m unittest tests.test_release_pipeline -v
```

构建脚本必须验证 Vite manifest、资源闭包、冻结入口、x64 WebView2、payload 清单、便携 ZIP 和 NSIS 安装器；冻结自检还会导入搜索学习、主动学习、聚类持久层、活动记录、安全删除和图库目录模块，并实际验证 SQLite WAL 及本地学习状态库可写。

## 已知限制

- Windows 可能显示 SmartScreen 提示；安装前应核对发布页提供的 SHA-256。
- YaoLens 只发布 Windows x64；不提供 macOS、Linux、ARM64 或 x86 版本。
- Android 客户端不提供应用商店分发、自动更新或 iOS 版本。
- Android 局域网连接当前使用未加密 HTTP，不提供端到端传输加密；只应在用户自己控制的家庭专用网络使用。
- Android 与电脑必须位于可互访的私网；访客 Wi-Fi、AP 隔离、VPN 或防火墙可能阻止自动发现，手工 IP 可作为备用。
- 尚无自动更新。
- 阿里云模型需要网络、有效 API Key 和可用额度。
- Node.js 只属于开发与构建环境，不属于用户运行环境。

最佳实践：每次发布都提高版本号并重新生成 SHA-256，不覆盖已经交付的同版本文件。

## 已废弃产物

- **Pure-Python Desktop（纯 Python 桌面端）**：源码入口、Tkinter/pystray 依赖和发布打包链均已移除。后续 Release 只保留 YaoLens Windows、Android 及校验元数据；Python wheel 仅用于 CI 安装验证。
