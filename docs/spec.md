# 曜镜（YaoLens）项目规格文档

> 版本：0.3（机器 SemVer：0.3.0）| 最后更新：2026-08-04

## 概述

曜镜（YaoLens）是一个本地多模态图片检索系统，基于阿里云百炼大模型（DashScope）实现向量嵌入与语义搜索，支持文本搜图、图片搜图、图文混合搜图，并提供 Windows WebView 和安卓两种客户端形态。

### 品牌与对外命名

- 对外产品名称为“曜镜”，英文名称为“YaoLens”。
- Windows 应用与宣传页共用同一品牌图形；`docs/assets/images/yaolens-logo.png` 是原始 Logo，`frontend/src/assets/yaolens-logo.png` 与 `assets/Zvec.AppIcon.ico` 是面向对应运行环境的派生资产。
- 宣传页使用橙色与石墨色品牌视觉。
- 宣传页界面展示区提供搜索、任务、整理和设置四个可切换的静态微缩页面；展示控件不连接后端，也不执行真实业务操作。
- `zvec`、`zvec_webview`、`zvec_host` 等内部代码、协议和辅助组件名称继续保留；公开主程序 `YaoLens.exe`、Windows 安装身份和三个产品附件均已迁移到 YaoLens。

## 系统架构

```
┌─────────────────────────────────────────────────────────┐
│                      客户端层                            │
├──────────────────┬──────────────────┬──────────────────┤
│   zvec_webview   │     frontend     │     android      │
│  (WebView 宿主)  │   (Vue 3 SPA)    │    (Kotlin)      │
└────────┬─────────┴────────┬─────────┴────────┬─────────┘
         │                  │                  │
         ▼                  ▼                  ▼
┌─────────────────────────────────────────────────────────┐
│              zvec_host（无界面应用服务层）               │
└──────────────────────────┬──────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────┐
│              image_vector_service (核心服务层)            │
│  backend_server.py - HTTP API (端口 8765, Bearer Token) │
├─────────────────────────────────────────────────────────┤
│  索引 | 搜索 | 标签 | 聚类 | 联邦检索 | 推荐 | 结果导出 │
└──────────────────────────┬──────────────────────────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │ zvec引擎 │ │ 模型服务 │ │  zvec_lan│
        │(向量存储)│ │(阿里云适配)│ │(局域网服务)│
        └──────────┘ └──────────┘ └──────────┘
```

## 模块说明

### image_vector_service（核心服务）

Python 包，提供所有业务逻辑：

| 子模块 | 职责 |
|--------|------|
| `service.py` | 顶层服务编排（索引、搜索、同步） |
| `backend_server.py` | HTTP API 服务器（ThreadingHTTPServer） |
| `config.py` | 配置管理（ServiceConfig dataclass） |
| `model_services/contracts.py` | 供应商无关的嵌入、视觉标注、错误与诊断契约 |
| `model_services/factory.py` | 单一活动模型供应商的装配边界 |
| `model_services/tagging.py` | 视觉标注领域类型、预算与严格解析入口 |
| `model_services/aliyun/` | 阿里云百炼嵌入与视觉 HTTP 适配器 |
| `dashscope_client.py` / `vision_tagging_client.py` | 旧导入路径兼容层；不参与业务装配 |
| `hybrid_search.py` | 混合检索（标签意图识别 + 多路召回） |
| `rank_fusion.py` | RRF 融合与置信度排序 |
| `federated_search.py` | 跨库联邦聚合 |
| `result_diversity.py` | 结果去重与多样性打散 |
| `tag_search.py` / `tags.py` | 标签匹配与归一化 |
| `folder_name_tags.py` | 文件夹名称标签规则、黑名单与父目录回退 |
| `image_clustering.py` | 图片聚类 |
| `image_scanner.py` | 文件系统扫描 |
| `file_watcher.py` | 文件系统监听（watchdog），自动增量索引触发 |
| `model_catalog.py` | 模型配置目录 |
| `rate_limiter.py` | API 限流 |
| `result_exporter.py` | 搜索结果导出 |
| `library_browser.py` | 只读文件夹/图片浏览 |
| `source_resolver.py` / `logical_paths.py` | 路径安全解析 |
| `recommendations.py` / `recommendation_store.py` | 固定配额推荐选择与按 viewer 隔离的批次、曝光事件存储 |

### zvec_host（无界面应用服务）

WebView 与 LAN 宿主共用的 Python 服务层，不包含 Tkinter、系统托盘或窗口代码：
- 后端生命周期、Windows Job Object 与 HTTP 客户端（`backend_host.py`、`windows_job.py`、`backend_api.py`）
- 配置与凭证（`configuration_service.py`、`credentials.py`）
- 图库任务与搜索（`library_tasks.py`、`search_service.py`）
- 模型设置与分页结果（`model_settings.py`、`result_catalog.py`）

### frontend（Web 前端）

Vue 3 + TypeScript + Vite SPA：
- 组件：`GalleryGrid`、`ImageCard`、`ImagePreview`、`PaginationBar` 等
- 功能模块（`src/features/`）：search-learning、organize、settings、tasks、activity、cleanup
- 图片推荐模块（`src/features/recommendations/`）：推荐批次、缩略图预加载、shown/action 事件同步、单张右键菜单与独立推荐详情
- API 层（`src/api/`）：`client.ts`、`gateway.ts`
- 构建产物嵌入 `zvec_webview/frontend_dist/`
- 缩略图显示策略：所有图片网格统一使用 `object-fit: contain` 确保全图可见不裁切；图库浏览 4 列 + dense 自动填充，待学习 2 列 + dense 自动填充，搜索结果 5×3 固定布局；横图（宽高比 > 1.5）自动跨 2 列；网格采用 masonry 瀑布流布局（`grid-auto-rows: 10px` + 动态 `grid-row: span N`），每张图的行跨度由宽高比和列宽自动计算，竖图高窄、横图矮宽，dense 模式自动填缝

### zvec_webview（WebView 宿主）

轻量 WebView 容器，入口 `zvec_webview/app.py`（命令 `zvec-webview-preview`）：
- 承载前端静态资源
- 原生桥接（`native_bridge.py`）
- LAN 访问集成（`lan_access.py`）
- 运行时管理（`runtime.py`）
- production `ImageRegistry` 使用 4 个有界 render slot；通用 registry 默认值仍为 2。2/3/4/6/8/12/16 的同输入压力矩阵中，4 是首个通过冷前 6 张 1.5 秒门禁且没有让搜索缩略图、preview 或 bootstrap P95 回退超过 10% 的档位；6 对关键 P95 只再改善 2.96%，却显著增加 CPU/RSS，因此不采用更高值。4 档压力峰值 RSS 比 2 档高 231.191 MiB（58.33%），真实源码版联合冒烟必须继续检查这一明确瞬时内存代价。
- Windows UI 大批量导出由 `NativeBridge._export_worker` 后台逐项执行 capability 解析、重名避让、`copy2`、错误清单和进度更新，production 复制并发保持 1。当前同盘 SATA SSD 的 1/2/4 矩阵中，2 虽提高中位吞吐约 61%，但搜索缩略图、preview、bootstrap 和纯推荐选择 worst P95 均回退超过 10%，因此不得启用；`copy_files` 只写 CF_HDROP 剪贴板列表，不属于应用内复制并发。

### ARW 选片模块（raw_selection）

独立于现有图库索引、向量生成、搜索、推荐、偏好、曝光或模型调用的 Sony A7M4 ARW 选片模块：
- 后端包：`image_vector_service/raw_selection/`（db.py、importer.py、decoder.py、cache.py、scheduler.py、jobs.py、creative_look.py、service.py）
- 前端模块：`frontend/src/features/raw-selection/`
- 独立 SQLite 数据库存储于 `raw-selection/projects.sqlite3`，不触及现有图库状态库
- 支持 .jpg/.jpeg/.png（通过 Pillow）和仅由 Sony ILCE-7M4 产生的 .arw；ARW 导入先通过轻量 TIFF 身份读取做型号门禁，`rawpy==0.27.0` 作为受控回退/完整解码依赖
- 三级原位渐进加载：缩略图 → 按实际显示尺寸快速生成的 ARW 内嵌 JPEG → 完整最佳预览；同一成员的显示阶段只升不降，相同容器尺寸不得重启渐进流程，分辨率替换前按预加载尺寸预先计算 fit，替换前后显示边界一致且不使用 transform 动画。高清替换保留缩放/平移，派生缓存原子写入并校验源版本。ARW 内嵌 JPEG 缺少 EXIF 方向时使用 TIFF 容器方向，元数据、缩略图和预览一致，历史方向缓存通过管线版本失效
- 项目管理：创建/重命名/删除项目、按导入顺序取首张图片缩略图作为封面、空项目双导入入口、评级/色标、筛选/排序、统一导出、永久删除（两阶段确认模态）
- 文件夹导入渐进登记并在后台生成缩略图；导入/导出使用可查询、可取消的后台任务，逐项记录进度、错误和操作日志
- 胶片栏虚拟化（仅渲染视口 ± 2 屏）、双图对比（同步缩放/平移、双侧评级）
- Sony 创意外观：当前只开放 `as_shot` 并使用相机内嵌 JPEG；未经可信 A7M4 参考输出校准的 ST/PT/NT/VV/VV2/FL/IN/SH/BW/SE 明确阻断，禁止用近似滤镜占位；JPG/PNG 不应用外观
- WebView 网关路由：`api/raw-selection/*`（项目 CRUD、成员列表、任务查询/取消、源状态、缩略图/预览、评级、导出、删除）
- 目标 Python 3.12 锁定闭包已经验证一致；该环境下的非正式基准（384 项：192 ARW + 192 JPG，无 PNG）当前可测硬门禁通过：冷首 24/100 张为 525.142/2067.703ms，冷内嵌预览为 98.971ms，重启热首 24 张为 43.538ms。由于未满足至少 300 ARW、1000 项、PNG 和完整 A7M4 RAW 模式清单，仍不能视为正式性能验收

### Windows 登录后常驻生命周期

- NSIS 安装版为当前用户注册 `AtLogon` 计划任务。任务使用 `InteractiveToken` 登录类型、`LeastPrivilege` 运行级别、`IgnoreNew` 多实例策略、无限执行时间，并只配置固定有限次数的失败重启；不得保存用户密码、切换到 SYSTEM 或请求管理员权限。
- 计划任务命令输出按 Windows OEM 代码页解码；查询精确任务名时，“文件不存在”或“路径不存在”均表示任务尚未创建，必须保持全新安装和重复卸载幂等，其他查询失败仍应阻止安装或卸载。
- 登录任务以隐藏状态启动 YaoLens。关闭 WebView 窗口仅隐藏界面，不停止宿主；已接受的后台任务、Android LAN 服务和自动增量索引 watcher 必须继续运行。
- 用户二次启动时必须唤醒已有窗口，不得创建第二个宿主、BackendHost 或 LAN listener。
- 用户完全停止应用的唯一产品入口是“设置 → 应用 → 退出 YaoLens”。存在活动任务时必须拒绝退出；任务空闲后才依次安全停止 backend、LAN、Gateway 和宿主。
- portable 包不得注册或修改计划任务。用户手工启动后仅在当前登录会话内隐藏常驻，关闭窗口和二次启动语义与安装版一致。
- Windows 下宿主为自己创建的 backend 子进程持有启用 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的 unnamed Job Object。Job Object 只负责宿主异常死亡时清理孤儿 backend；正常停止仍必须先走 authenticated `shutdown_if_idle`，不得用 Job Object 绕过 active-job 保护。
- Windows 专属 `ctypes` 调用必须保持非 Windows 导入安全，并通过 Windows 与 Linux 两侧的 Mypy 检查。

### zvec_lan（局域网服务）

为安卓/移动设备提供局域网访问：
- UDP 服务发现（`discovery.py`，端口 DISCOVERY_PORT）
- HTTP API（`http_server.py`，端口 API_PORT）
- 设备配对（`pairing.py`）
- 查询图片上传（`uploads.py`）
- 推荐批次及 shown/action 事件（`/api/v1/recommendations`）

### android（安卓客户端）

Kotlin + Gradle 构建的安卓应用，通过 LAN API 与 Windows WebView 宿主通信。

- 启动器使用橙黑相机搜索标识，提供白色背景的 Android 自适应图标以及各屏幕密度的传统、圆形兼容资源。
- 底部栏提供“推荐”入口；推荐界面采用 Material3，随系统使用 light/dark 配色。推荐原图查看器关闭或系统返回后恢复推荐 Tab、原批次与网格滚动位置；列表卡片不提供反馈按钮，喜欢/不喜欢仅位于推荐原图长按菜单并在提交前立即收起。

### 搜索查询语义

- 标签搜索将空格、半角/全角逗号、顿号、半角/全角分号、`|`、`/` 和 `\` 视为标签分隔符；归一化后稳定去重。
- 多标签默认使用 AND（`tag_mode=all`），要求结果同时命中所有输入片段；WebView 搜索设置可显式切换为 OR（`tag_mode=any`），此时命中任一片段即可召回。标签搜索保持本地执行，不调用嵌入模型。
- 普通语义句保持单次嵌入和原有搜索路径。仅当文本出现半角 `|` 时，按非空片段拆分、稳定去重，最多接受 8 段。
- 多语义查询对每段分别生成文本向量，并分别执行向量、描述元数据及可用标签通道召回；各段结果以等权 RRF 做 OR 并集融合，同一图片命中多段时自然获得更高融合分数。
- 多语义融合使用 `semantic_rrf` 排序模式；当前搜索学习排序器未针对该特征训练，因此该模式不执行学习重排。导出的查询、结果清单和搜索质量诊断记录语义片段及融合方式。

## CLI 命令

入口：`zvec-image-search`（`image_service.py`）

CLI 入口将配置创建、迁移/后端服务、需要 `ImageVectorService` 的命令和搜索分派分别隔离；迁移与后端服务不会创建图片服务实例，其他命令由入口统一关闭服务并映射退出码。

| 命令 | 说明 |
|------|------|
| `index <folder> [tags...]` | 索引图片文件夹（支持标签、`--clear-tags`） |
| `sync <folder>` | 同步文件夹并删除失效记录（`--dry-run`） |
| `search --text/--image` | 多模态搜索（`--tags`、`--tag-mode`、`--search-mode`） |
| `stats` | 集合统计信息 |
| `roots` | 列出已注册的图片根目录 |
| `rebind-root <id> <folder>` | 重新绑定根目录路径 |
| `migrate-schema` | 数据迁移（`--dry-run`） |
| `metadata-backfill` | 补充描述嵌入（`--max-images`） |
| `cache-clear` | 清除查询嵌入缓存 |
| `clean-results` | 清理过期结果目录（`--days`） |
| `serve` | 启动 HTTP 后端服务（`--host`、`--port`、`--token`） |

### 开发工具

| 工具 | 说明 |
|------|------|
| `tools/search_learning/generate_eval_pack.py` | 从 `search_results/` 历史结果生成固定评测包（`fixed-evaluation.json`），用于搜索学习排序模型的离线评估；参数：`--search-results-dir`、`--output`、`--max-cases`、`--max-candidates` |

## 技术栈

| 层 | 技术 |
|----|------|
| 向量引擎 | zvec 0.5.1（1024维，COSINE） |
| 嵌入模型 | DashScope multimodal-embedding |
| 后端 | Python 3.10+，标准库 HTTP Server |
| 前端 | Vue 3.5 + TypeScript 5.9 + Vite 8 |
| Windows 客户端 | CPython 3.12 + PyInstaller + pywebview |
| 安卓端 | Kotlin + Gradle |
| 代码质量 | Ruff（行宽88）、mypy |
| CI/CD | GitHub Actions |
| 安装包 | NSIS（Windows .exe）、Gradle（Android .apk） |

## 配置与环境变量

| 变量 | 说明 |
|------|------|
| `DASHSCOPE_API_KEY` | 阿里云百炼 API 密钥（必需） |
| `DASHSCOPE_API_URL` | API 地址（可选，有默认值） |
| `DASHSCOPE_VISION_API_URL` | 视觉标注 API 地址（可选，有默认值） |
| `ZVEC_IMAGE_WORKSPACE` | 工作空间路径 |
| `ZVEC_IMAGE_RESULTS_DIR` | 搜索结果输出目录 |
| `ZVEC_IMAGE_LOG_DIR` | 日志目录 |
| `ZVEC_CONFIG_HOME` | 配置主目录 |
| `ZVEC_BACKEND_TOKEN` | HTTP 后端 Bearer Token |
| `ZVEC_LIBRARIES_CONFIG` | 多库配置清单路径 |

### 模型角色与并发

- “设置 → 模型角色”同时维护嵌入模型、智能标注主模型、疑难升级模型，以及全局向量并发和智能标注并发。
- 两项并发仅允许 `1`、`2`、`4`、`6`；向量并发默认 `2`，智能标注并发默认 `4`。智能标注主模型与疑难升级模型共享同一个标注并发上限。
- 并发值与角色分配一同保存在 `models.json`。旧配置缺少并发字段时必须继续使用 `2`、`4`，下一次保存时写出规范化字段；显式空值、布尔值、字符串及其他整数必须拒绝。
- 后端启动时将模型配置中的并发值注入 `ServiceConfig`。运行中的后端不热加载该文件，保存后必须明确返回重启状态，不得中断活动任务进行静默重启。
- 模型并发只控制外部模型请求；单个图库的 SQLite 与向量 Collection 仍由单一所有者线程按最多 256 条一批顺序写入。
- 自动标注运行结果使用 `AutoTagRunReport` 固定字段类型；并发基准直接消费其中的数值指标，Mypy 必须能够检查吞吐量和成本计算，防止报告字段类型漂移。

### 模型服务边界与替换约束

- 当前采用“单一活动供应商、内部可替换”方案。业务服务只依赖 `EmbeddingProvider`、`VisionTaggingProvider`、`ModelProviderError` 和 `ModelProviderFactory`；不得导入、构造或读取阿里云适配器的具体类型、限流器或请求计数属性。
- 阿里云专属的 URL、环境变量回退、图片请求编码、DashScope envelope、HTTP 重试、`Retry-After` 和错误码解释集中在 `model_services/aliyun/`。替换模型服务时应重写适配器和工厂装配，不得修改索引、搜索、标注编排或图库存储逻辑。
- 供应商错误跨边界时必须携带稳定类别与实际 HTTP `attempts`。稳定类别包括配置、认证、输入、内容策略、限流、瞬态故障、非法响应、取消和未知；业务层不得依赖供应商错误文案或 HTTP 状态码决定隔离、重试和告警策略。
- 嵌入结果必须验证数量、唯一连续索引、向量维度以及所有元素的有限数值性；供应商响应必须有大小上限。模型服务测试必须离线运行，使用内存替身或回环服务，并用网络守卫阻止意外外联。
- `models.json` 继续使用 schema v1，现有 provider、protocol、角色、并发字段及其含义不变；`DASHSCOPE_*` 环境变量、`dashscope_api_key` 请求字段和 Windows 凭证目标保持兼容。本次边界调整不改变 Prompt、自动标注缓存 schema、LAN 协议、存储 schema 或发布矩阵。
- `dashscope_client.py` 与 `vision_tagging_client.py` 的旧公开导入保留一个发布周期；新业务代码必须使用 `model_services` 契约或工厂。
- 任何会改变嵌入向量空间的操作——包括更换嵌入模型、维度、向量生成语义或不兼容的预处理——都必须新建或完整重建图片向量及描述向量索引。禁止在同一 Collection 中混用不同向量空间，也不得把“仅重写供应商适配器”误认为可免重建索引。

## 数据存储

| 路径 | 内容 |
|------|------|
| `image_collection/` | zvec 向量集合（RocksDB） |
| `image_collection.meta.json` | 集合元数据 |
| `image_collection.state.sqlite3` | 索引状态数据库（含 `fs_change_queue` 表：文件变更队列） |
| `recommendations.sqlite3` | 推荐批次、批次项、事件和 viewer 内容曝光计数；不存路径、向量或 token |
| `search_results/` | 搜索结果导出目录 |
| `search-learning/` | 搜索学习产物（聚类快照、主动学习队列） |
| `fixed-evaluation.json` | 搜索学习固定评测包（离线排序质量评估基准） |
| `model-catalog.default.json` | 默认模型配置 |

集合写入前会对字段做安全校验（`collection_write_outbox`）：拒绝凭据、绝对路径（Windows 盘符 / UNC / Unix `/` 开头）和 NUL 字符。`relative_path` 必须是 POSIX 风格的相对路径，支持 CJK 字符及多级子目录（如 `作品/子目录/1.jpg`）。

### 图片推荐

- 每批目标为 15 张，固定池配额为：技术质量（`quality`）5、低曝光（`low_exposure`）6、随机发现（`random`）4。新批次不得生成最近入库（`recent`）来源；某个池不足时，只能以其他合格候选随机补位，并标记 `quota_degraded`。
- 每个已启用图库正常先采样最多 100 条技术质量候选和 100 条稳定随机候选；只有首轮无法在完整 240 条历史下组成 15 张，或固定 5/6/4 配额发生降级时，才对该请求扩到每类 256 并完整重选一次。首轮与扩容共用同一历史和请求 seed，只持久化最终批次；不再执行按入库时间倒序的最近入库采样，也不执行全库加载或全库随机排序。
- 选择过程按 SHA-256 去重；同一图集（`library_id + root_id + parent_directory`）最多 3 张，同一已确认角色最多 5 张。推荐不含作者字段，也不施加作者维度的限制。
- 角色只读取与当前 SHA-256 匹配、状态同时为 accepted 和 confirmed 的标注，并且只使用其 `accepted_auto_tags`。
- 每个 viewer 最多读取最近 240 张成功 shown 的 SHA-256，并依次使用 240、210、180、150、120、90、60、30、0 的排除窗口；只有候选不足以组成完整批次时才逐级放宽，最终不足时不复制图片并返回 `partial` 批次及原因。
- `low_exposure` 与 `random` 每次选择都严格限定在当前合格候选的最低 viewer 曝光层级，再分别按 `mtime_ns` 降序或请求种子稳定随机排序；MMR 不得越过该曝光层级重新选回高曝光候选。
- 向量多样性只使用现有索引向量，不得为推荐临时调用模型。每次选择只对候选向量执行一次合法性检查和归一化、只建立一次各槽位稳定基础排序；每选中一张后增量维护其余候选相对已选集合的最大相似度惩罚。MMR 将余弦相似度 `>= .95` 视为强惩罚，`.85–.95` 采用渐进软惩罚。跨图库仅在 `model + dimension + metric` 完全一致时比较向量；否则返回 `incompatible_vector_spaces`。
- 当前电脑端实例只有一套显式推荐偏好：Windows 与所有已授权 Android 设备的 `like/dislike` 跨 viewer 合并，同一 SHA-256 以 sequence 最大的最后成功事件为最终状态。`shown`、`open` 和 `export` 不推断为偏好；已有最终偏好的原图从所有新批次永久排除，既有批次回放则显示当前最终偏好。
- 个性化画像按 sequence 倒序只扫描最近最多 4096 条 `like/dislike` 事件，在该有界窗口内按 SHA-256 首次出现取最终状态并最多保留 256 张，再由各图库 worker 按已知 doc ID 读取现有向量。后端推荐单线程缓存已归一化正向/负向质心、有效数、向量空间摘要和最后显式反馈 sequence；重启后首批重建，连续换批复用。`shown/open/export`、同状态重复反馈和幂等回放不失效，只有某 SHA-256 最终 `like/dislike` 状态实际变化时才使下一批重建。缓存不调用模型，不保存到推荐数据库，也不持有路径、原始向量或偏好明细；极端重复反馈、删除、缺失、读取失败、SHA 不匹配或空间不兼容继续安全降级。
- 至少 10 个最终偏好具有可用且兼容的现有向量后才启用个性化。排序调整强度为 `min(0.25, effective_count × 0.005)`，候选调整限制在 `[-0.25, +0.25]`；只作用于 `quality` 和 `low_exposure`，`random` 保持稳定随机探索。冷启动、向量不可用或向量空间不兼容时回退到非个性化排序。
- Windows viewer 与每台 Android viewer 的推荐批次、shown 历史和曝光计数继续完全隔离。只有客户端成功显示并提交 `shown` 后才计入该 viewer 曝光；创建批次本身不计曝光。
- `recommendations.sqlite3` 仍仅含 `batches`、`items`、`events`、`content_stats` 四表，并保存不透明标识与计数，不保存文件路径、向量或 token。共享偏好直接从既有事件计算；增加精确候选偏好查询使用的 `idx_items_sha256`、`idx_events_item_action_sequence`，以及有界画像事件倒序读取使用的部分覆盖索引 `idx_events_preference_sequence`，不新增偏好表或数据迁移。
- LAN 契约为已认证的 `POST /api/v1/recommendations`（仅 `{request_id}`）、`POST /api/v1/recommendations/{batch_id}/shown`（仅 `{event_id}`）和 `POST /api/v1/recommendations/{batch_id}/actions`（`event_id`、`item_id`、`action`，export 可带 metadata）。`request_id` 使批次创建幂等；`event_id` 使 shown 与 action 幂等。
- 推荐响应的每个 item 可含最终共享 `preference`（`like`、`dislike` 或 `null`），顶层 `personalization` 返回 `applied`、`effective_count` 和安全 `reason`。降级 reason 为 `insufficient_preferences`、`vectors_unavailable`、`incompatible_vector_spaces`；幂等批次回放使用 `replayed`，并仍重新读取 item 的当前最终偏好。action 成功响应也返回 `recorded` 与原子读取的当前最终 `preference`，客户端不得把较旧 event ID 的幂等回放误显示为新的跨设备偏好。响应不包含 viewer/device ID 或完整反馈历史。
- 升级前已经持久化的批次允许在幂等回放时继续返回 `bucket=recent`，不得删除或迁移旧批次；Windows 将该旧来源显示为通用“推荐”，Android 继续安全读取，不再向用户显示“最近入库”。
- Windows 同时启动一批全部 15 张缩略图预载，固定前 6 张成功后即可把含 15 个图片槽位的批次提交为可见并同步 shown；Android 的固定关键组为前 4 张。关键组失败时保留旧批次且不得 shown；尾部失败只汇总一次，不回滚已可见批次。隐藏期间不得提交新批次，恢复可见后同一批只 shown 一次。shown/action 同步失败时必须复用原 `event_id`；Android shown 使用有上限的指数退避自动重试。
- 当前批 shown 成功且全部缩略图 settle 后，两端都只允许在内存中静默准备一个下一批。未消费的预热批次不算展示、不增加曝光、不进入 240 条历史；点击消费完整或在途预热不得重复 create/preload。推荐页或 document/Activity 隐藏、断开/重新配对和卸载会取消并丢弃预热；最终 `like/dislike` 状态相对操作前实际变化时也会失效，open/export、最终状态相同的反馈和幂等回放不失效。跨设备偏好在页面持续可见期间变化时，最多影响这一批已生成快照，不新增轮询 API。Android 仅在保存原图成功后提交 `export`（`metadata.channel=save`）；分享不记录 export 事件。
- LAN 推荐 item 的 `thumbnail_url` 使用经 Bearer、session owner capability 和撤销校验的 `/api/v1/media/{media_id}/thumbnail`，返回共享 `ImageRegistry` 的 640px 持久化 JPEG 缩略图并支持 GET/HEAD、private cache、ETag/304；`preview_url` 与详情、保存、分享继续走 `/original`。推荐 capability 创建只绑定 registry ID 与精确文件版本，不为列表逐项读取完整原图；首次 original 请求才按稳定版本 single-flight 计算并缓存强 SHA-256。响应不泄露路径、token 或内部 registry ID。
- Windows 推荐卡片仅显示图片及图片右下角的推荐来源角标，不显示文件名、图库名或“打开/喜欢/导出/不喜欢”底部按钮；单击图片继续打开当前图片。右键菜单仅作用于当前单图，推荐详情及既有操作保持可用，菜单提供详情、系统打开、所在文件夹、喜欢/不喜欢、复制图片、复制文件、复制路径和导出；推荐反馈保持 `like/dislike`，搜索菜单仍使用“相关/不相关”且保留原多选语义。菜单受窗口边界约束，并在点击外部、Escape、窗口缩放或离开推荐页时收起。
- 新批次在后端记录候选首轮/扩容读取、偏好画像、选择、结果回填和总耗时，以及候选读取次数和画像缓存命中。Windows 本地 bridge 仅白名单透传这些非负数值和布尔诊断字段；字段不得含路径、viewer/device ID、请求/批次/图库标识或偏好内容，LAN/Android 推荐契约保持不变。后端阶段耗时不含 HTTP 与缩略图预载；Windows 端到端性能测量另包含 create HTTP、15 张缩略图预载和 shown 同步，DOM commit/paint 只做独立视觉冒烟，不计入计时。
- TC-006 的 2026-08-03 历史基线为：768×1024 纯选择 P95 615.905 ms、真实图库冷启动首批 2662.453 ms、连续真实新批次后端 P95 536.604 ms，但包含 15 张首次缩略图的 Windows 端到端 P95 仍为 2995.028 ms，其中约 2371.225 ms 位于共享缩略图生成/HTTP 预载。该 `2.995 s` 只作为 TC-007 前值，不再描述当前交付路径。
- TC-007 production 采用共享 render=4、Android HTTP=10；扫描保持 4，Windows 导出复制保持串行 1。Windows 20 个非预热真实新批次的 create、前 6、全 15、shown P95 分别为 601.126/1751.808/2130.634/39.687 ms；20 个完整预热命中点击提交/全就绪 P95 为 317/341 ms。相对历史 2995.028 ms，前 6 原始口径减少 41.51%（1.71x），加 shown 的保守口径减少 40.18%（1.67x），全 15 减少 28.86%（1.41x），预热点击减少 89.42%（9.45x）。冷首批前 6/全 15 为 1510/1850 ms；冷首批和预热门禁通过，非预热前 6 `<=1.5 s` 与全 15 `<=2 s` 未通过。
- API 34 模拟器连接真实 LAN、production 10x4 的 20 个非预热批次 create/t4/t15 P95 为 856.960/2427.430/2601.598 ms，预热命中状态提交 P95 为 0.087 ms（不含 Compose paint）；600 张缩略图合计 27,601,611 bytes，即使保守对比历史 300 张原图 1,634,786,151 bytes 仍减少 98.31%。预热点击与网络字节门禁通过，非预热 t4 `<=1.5 s`、t15 `<=2.2 s` 未通过；剩余瓶颈为首次原图 decode、640px render 与模拟器 Coil 解码，不得用全热或预热结果冒充冷路径达标，也不得启用已被公平性/资源矩阵淘汰的更高并发。

本图片推荐章节不改变现有发布矩阵。

### 文件夹名称批量标签

- 入口位于“批量标签”页的“从文件夹名生成”，支持当前文件夹（含子文件夹）或整个当前图库。
- 操作必须先执行只读预览，再明确确认提交后台任务；预览显示待更新图片数、变化文件夹数和最多 100 个文件夹样例。
- 标签写入只替换 `folder_tags` 来源，不修改人工 `tags`、模型 `accepted_auto_tags` 或 `inherited_tags`。
- 规则在本地确定性执行：NFKC 归一化，过滤数量、容量、图片扩展名及内置黑名单；纯英文/数字或无有效标签的目录向上查找父目录，但不得越过图库根目录。
- 应用任务按 128 张图片分页读取状态，复用现有图片向量，通过 Collection 写协调器同步更新 Zvec 与 SQLite；不调用模型、不重新生成图片向量，支持安全取消与重复执行幂等。
- 后续正常索引使用同一规则生成 `folder_tags`，避免再次扫图库时回退为旧格式。
- 后端命令为 `folder_name_tag_estimate`（不进入任务历史）和 `folder_name_tag_apply`（进入任务历史并报告进度与失败项）。

### 自动增量索引与标注

基于 watchdog 文件系统监听实现自动增量索引，避免每次索引时全量扫描目录树：

- **开关位置**：设置 → 图库与路径 → 每个图库编辑器“保存图库设置”按钮下方的 toggle 开关（`auto_index_enabled`）
- **工作流程**：watchdog 后台监听已索引 root 目录 → 文件变化事件持久化到 `fs_change_queue` 表 → 5 秒防抖等待无新事件 → 自动提交增量索引+智能标注任务（`index_and_auto_tag_incremental`）。每批最多领取本次智能标注上限对应的事件，成功后自动续跑下一批，直至积压清空
- **授权语义**：用户为图库启用 `auto_index_enabled` 即持续授权该图库的自动增量索引与智能标注流水线；watcher 提交内部任务时必须携带外部处理确认。手动智能标注任务仍须逐次明确确认
- **任务隔离**：watcher 提交的是不依赖用户任务 ID 的内部批处理调用，不进入手动任务的取消与进度查询链路，但仍随图库 worker 关闭而停止
- **增量路径**：只处理变更队列中的文件（created/modified → inspect + embed + auto_tag；deleted → 删除记录），绕过全量 `scan_folder_to_staging`。事件先进入“已领取”状态，索引和智能标注流水线完整结束后才确认完成；异常、取消或确认失败必须恢复为待处理
- **启动行为**：信任上次索引结果，不做启动时全量扫描；watcher 直接接管新变化，并恢复进程中断时遗留的“已领取”事件、把悬空索引批次收尾为失败，再对持久化积压重新执行防抖调度
- **兜底机制**：watchdog buffer 溢出 → 标记需全量扫描；程序关闭期间变化 → 下次启动 watcher 接管，提供手动全量索引兜底
- **配置项**：`watcher_debounce_seconds`（默认 5 秒）、`watcher_overflow_triggers_full_scan`（默认 True）
- **持久化约束**：保存图库设置后，`auto_index_enabled` 必须在接口返回值和 `config.json` 重载结果中保持一致

### 全量图片扫描并发

- 全量扫描继续使用 `scan_concurrency=4` 的有界 `ThreadPoolExecutor`，默认 in-flight 上限为 worker 的两倍。发现 sequence 在提交 worker 前分配；worker 可以乱序完成，但结果和 staging 消费继续按稳定 sequence/path/SHA 契约输出，不能因并发改变去重、错误事实或进度。
- 当前 D: `CT2000MX500SSD1` SATA SSD 上，对同一 340 张、1,846,689,211 bytes 隔离真实图片快照做 4/6/8/12 各三轮扫描，P50 吞吐分别为 185.113/194.882/197.106/194.808 images/s。6 相对 4 只提高 5.28%，却使搜索缩略图和 preview worst P95 回退 12.17%/20.13%；更高档 CPU/RSS 继续增加，因此 production 保持 4。该结果受一次性 snapshot 复制和内容摘要预热 Windows 文件缓存影响，不泛化到 HDD、其他 SSD 或严格冷盘。
- 中途取消会等待当前有界 inspection 集合收尾；本机 4 档实测约 777.961 ms，随后无 scanner thread 或 staging 残留。若取消恰在 final drain 完成后到达，scanner 会正常返回 ready staging，service 紧接着的 `cancel_check` 再终止任务并清理；不得声称 scanner 在已经完成的 final drain 内观察到了取消。

### 任务历史

- 仅持久化会改变数据或执行后台处理的任务；`cluster_list`、`cluster_detail` 等只读分页查询不进入任务历史。
- 旧版本已写入的聚类只读查询记录在读取任务历史时隐藏，不删除数据库行。
- 运行不足一秒的任务耗时显示为“< 1 秒”。

## 发布与构建

- **触发方式**：GitHub Actions `workflow_dispatch` 手动触发 `release.yml`
- **发布入口**：`prepare_python_release.py` 与 `assemble_python_release.py` 必须支持从仓库根目录直接执行，以匹配 GitHub Actions 调用方式
- **Windows WebView**：PyInstaller 打包 → NSIS 生成 .exe 安装程序
- **安装版常驻**：NSIS 注册当前用户登录任务；卸载时移除该任务
- **portable 常驻**：不注册计划任务，仅手工启动后的当前登录会话内常驻
- **安卓端**：Gradle 构建 .apk
- **发布版本**：机器版本 `0.3.0`，公开展示版本 `0.3`，稳定标签 `v0.3.0`
- **Windows 版本资源**：发布载荷内三个可执行文件的产品名均为 `YaoLens`，文件版本与产品版本均展示为 `0.3.0`，固定数值版本为 `0.3.0.0`
- **发布矩阵**：仅发布 YaoLens Windows、Android 及对应校验、验证报告和策略元数据
- **公开附件**：`YaoLens-0.3-win-x64-portable.zip`、`YaoLens-0.3-win-x64-setup.exe`、`YaoLens-0.3-android.apk`
- **矩阵约束**：登录常驻不新增独立附件或产品，现有 GitHub Release 资产矩阵保持不变
- **禁止产物**：不发布 Zvec-Desktop 安装包、便携包或 Python wheel
- **内部组件**：`zvec.exe`、`zvec-backend.exe` 仅随 WebView 包交付
- **质量门禁**：搜索质量验证；显式使用 `allow_uncertified_search_quality=true` 时记录为未提供正式认证
- **冻结依赖门禁**：Windows 构建环境固定安装并校验 `watchdog==6.0.0`；冻结包自检必须成功导入 `image_vector_service.file_watcher` 与 `watchdog.observers`，防止自动索引监听依赖遗漏后进入发布包

## API 协议

后端 HTTP API（`/v1/`）：
- 认证：`Authorization: Bearer <token>`
- 协议版本：`_PROTOCOL_VERSION = 2`
- 任务管理：`/v1/jobs/{job_id}` 异步任务轮询
- 请求体上限：1 MiB（`_MAX_REQUEST_BYTES`）

## 开发规范

- 开发流程统一参考 `docs/development-workflow-guide.md`，按风险选择轻量、普通或高风险流程；实施前明确目标、范围、成功标准和预计涉及的文件或模块，不得修改无关部分。仅当架构、长期行为、数据格式、兼容范围或发布约束变化时更新对应 Spec
- Python 代码风格：Ruff，行宽 88 字符
- 类型检查：mypy（Python 3.10 target）
- 前端类型检查：vue-tsc + tsc
- 测试：pytest（Python）、vitest（前端）
- 前端组件测试固定使用 `@vue/test-utils` 2.2.7，避免 2.4.x 引入存在已知高危漏洞的 `js-beautify`/`glob` 开发依赖链；升级时必须同时通过 `npm audit`、前端测试、类型检查和构建。
- 提交前检查：`python -m ruff format . && python -m ruff check --fix .`
