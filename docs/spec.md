# 曜镜（YaoLens）项目规格文档

> 版本：0.1（机器 SemVer：0.1.0）| 最后更新：2026-07-28

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
│  索引 | 搜索 | 标签 | 聚类 | 联邦检索 | 结果导出        │
└──────────────────────────┬──────────────────────────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │ zvec引擎 │ │ DashScope│ │  zvec_lan│
        │(向量存储)│ │ (嵌入API)│ │(局域网服务)│
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
| `dashscope_client.py` | DashScope API 调用封装 |
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
- API 层（`src/api/`）：`client.ts`、`gateway.ts`
- 构建产物嵌入 `zvec_webview/frontend_dist/`
- 缩略图显示策略：所有图片网格统一使用 `object-fit: contain` 确保全图可见不裁切；图库浏览 4 列 + dense 自动填充，待学习 2 列 + dense 自动填充，搜索结果 5×3 固定布局；横图（宽高比 > 1.5）自动跨 2 列；网格采用 masonry 瀑布流布局（`grid-auto-rows: 10px` + 动态 `grid-row: span N`），每张图的行跨度由宽高比和列宽自动计算，竖图高窄、横图矮宽，dense 模式自动填缝

### zvec_webview（WebView 宿主）

轻量 WebView 容器，入口 `zvec_webview/app.py`（命令 `zvec-webview-preview`）：
- 承载前端静态资源
- 原生桥接（`native_bridge.py`）
- LAN 访问集成（`lan_access.py`）
- 运行时管理（`runtime.py`）

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

### android（安卓客户端）

Kotlin + Gradle 构建的安卓应用，通过 LAN API 与 Windows WebView 宿主通信。

- 启动器使用橙黑相机搜索标识，提供白色背景的 Android 自适应图标以及各屏幕密度的传统、圆形兼容资源。

## CLI 命令

入口：`zvec-image-search`（`image_service.py`）

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
| `ZVEC_IMAGE_WORKSPACE` | 工作空间路径 |
| `ZVEC_IMAGE_RESULTS_DIR` | 搜索结果输出目录 |
| `ZVEC_IMAGE_LOG_DIR` | 日志目录 |
| `ZVEC_CONFIG_HOME` | 配置主目录 |
| `ZVEC_BACKEND_TOKEN` | HTTP 后端 Bearer Token |
| `ZVEC_LIBRARIES_CONFIG` | 多库配置清单路径 |

## 数据存储

| 路径 | 内容 |
|------|------|
| `image_collection/` | zvec 向量集合（RocksDB） |
| `image_collection.meta.json` | 集合元数据 |
| `image_collection.state.sqlite3` | 索引状态数据库（含 `fs_change_queue` 表：文件变更队列） |
| `search_results/` | 搜索结果导出目录 |
| `search-learning/` | 搜索学习产物（聚类快照、主动学习队列） |
| `fixed-evaluation.json` | 搜索学习固定评测包（离线排序质量评估基准） |
| `model-catalog.default.json` | 默认模型配置 |

集合写入前会对字段做安全校验（`collection_write_outbox`）：拒绝凭据、绝对路径（Windows 盘符 / UNC / Unix `/` 开头）和 NUL 字符。`relative_path` 必须是 POSIX 风格的相对路径，支持 CJK 字符及多级子目录（如 `作品/子目录/1.jpg`）。

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
- **工作流程**：watchdog 后台监听已索引 root 目录 → 文件变化事件持久化到 `fs_change_queue` 表 → 5 秒防抖等待无新事件 → 自动提交增量索引+智能标注任务（`index_and_auto_tag_incremental`）
- **增量路径**：只处理变更队列中的文件（created/modified → inspect + embed + auto_tag；deleted → 删除记录），绕过全量 `scan_folder_to_staging`
- **启动行为**：信任上次索引结果，不做启动时全量扫描；watcher 直接接管新变化
- **兜底机制**：watchdog buffer 溢出 → 标记需全量扫描；程序关闭期间变化 → 下次启动 watcher 接管，提供手动全量索引兜底
- **配置项**：`watcher_debounce_seconds`（默认 5 秒）、`watcher_overflow_triggers_full_scan`（默认 True）
- **持久化约束**：保存图库设置后，`auto_index_enabled` 必须在接口返回值和 `config.json` 重载结果中保持一致

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
- **发布版本**：机器版本 `0.1.0`，公开展示版本 `0.1`，稳定标签 `v0.1.0`
- **Windows 版本资源**：发布载荷内三个可执行文件的产品名均为 `YaoLens`，文件版本与产品版本均展示为 `0.1`，固定数值版本为 `0.1.0.0`
- **发布矩阵**：仅发布 YaoLens Windows、Android 及对应校验、验证报告和策略元数据
- **公开附件**：`YaoLens-0.1-win-x64-portable.zip`、`YaoLens-0.1-win-x64-setup.exe`、`YaoLens-0.1-android.apk`
- **矩阵约束**：登录常驻不新增独立附件或产品，现有 GitHub Release 资产矩阵保持不变
- **禁止产物**：不发布 Zvec-Desktop 安装包、便携包或 Python wheel
- **内部组件**：`zvec.exe`、`zvec-backend.exe` 仅随 WebView 包交付
- **质量门禁**：搜索质量验证；显式使用 `allow_uncertified_search_quality=true` 时记录为未提供正式认证

## API 协议

后端 HTTP API（`/v1/`）：
- 认证：`Authorization: Bearer <token>`
- 协议版本：`_PROTOCOL_VERSION = 2`
- 任务管理：`/v1/jobs/{job_id}` 异步任务轮询
- 请求体上限：1 MiB（`_MAX_REQUEST_BYTES`）

## 开发规范

- Python 代码风格：Ruff，行宽 88 字符
- 类型检查：mypy（Python 3.10 target）
- 前端类型检查：vue-tsc + tsc
- 测试：pytest（Python）、vitest（前端）
- 提交前检查：`python -m ruff format . && python -m ruff check --fix .`
