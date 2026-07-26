# Zvec Image Search - 项目规格文档

> 版本：0.5.0-rc.4 | 最后更新：2026-07-26

## 概述

Zvec 是一个本地多模态图片检索系统，基于阿里云百炼大模型（DashScope）实现向量嵌入与语义搜索，支持文本搜图、图片搜图、图文混合搜图，并提供桌面端、Web 端和安卓端三种客户端形态。

## 系统架构

```
┌─────────────────────────────────────────────────────────┐
│                      客户端层                            │
├──────────────┬──────────────┬──────────────┬────────────┤
│ zvec_desktop │ zvec_webview │   frontend   │  android   │
│ (Python GUI) │ (WebView宿主)│ (Vue 3 SPA) │ (Kotlin)   │
└──────┬───────┴──────┬───────┴──────┬───────┴─────┬──────┘
       │              │              │             │
       ▼              ▼              ▼             ▼
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
| `image_clustering.py` | 图片聚类 |
| `image_scanner.py` | 文件系统扫描 |
| `file_watcher.py` | 文件系统监听（watchdog），自动增量索引触发 |
| `model_catalog.py` | 模型配置目录 |
| `rate_limiter.py` | API 限流 |
| `result_exporter.py` | 搜索结果导出 |
| `library_browser.py` | 只读文件夹/图片浏览 |
| `source_resolver.py` / `logical_paths.py` | 路径安全解析 |

### zvec_desktop（桌面端）

Python GUI 应用，入口 `zvec_desktop/app.py`（命令 `zvec-desktop`）：
- 系统托盘常驻（`tray.py`）
- 后端进程管理（`backend_host.py`、`runtime_controller.py`）
- 图库浏览与搜索 UI（`gallery_layout.py`、`search_service.py`）
- 图片整理功能（`organize_panel.py`、`organize_runtime.py`）
- 多库管理（`library_tasks.py`）
- 模型与凭证配置（`model_settings.py`、`credentials.py`）
- 单实例锁（`single_instance.py`）

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

### zvec_lan（局域网服务）

为安卓/移动设备提供局域网访问：
- UDP 服务发现（`discovery.py`，端口 DISCOVERY_PORT）
- HTTP API（`http_server.py`，端口 API_PORT）
- 设备配对（`pairing.py`）
- 查询图片上传（`uploads.py`）

### android（安卓客户端）

Kotlin + Gradle 构建的安卓应用，通过 LAN API 与桌面端通信。

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
| 桌面端 | Python（pystray 系统托盘） |
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

### 自动增量索引与标注

基于 watchdog 文件系统监听实现自动增量索引，避免每次索引时全量扫描目录树：

- **开关位置**：设置 → 图库与路径 → 每个图库编辑器“保存图库设置”按钮下方的 toggle 开关（`auto_index_enabled`）
- **工作流程**：watchdog 后台监听已索引 root 目录 → 文件变化事件持久化到 `fs_change_queue` 表 → 5 秒防抖等待无新事件 → 自动提交增量索引+智能标注任务（`index_and_auto_tag_incremental`）
- **增量路径**：只处理变更队列中的文件（created/modified → inspect + embed + auto_tag；deleted → 删除记录），绕过全量 `scan_folder_to_staging`
- **启动行为**：信任上次索引结果，不做启动时全量扫描；watcher 直接接管新变化
- **兜底机制**：watchdog buffer 溢出 → 标记需全量扫描；程序关闭期间变化 → 下次启动 watcher 接管，提供手动全量索引兜底
- **配置项**：`watcher_debounce_seconds`（默认 5 秒）、`watcher_overflow_triggers_full_scan`（默认 True）

## 发布与构建

- **触发方式**：GitHub Actions `workflow_dispatch` 手动触发 `release.yml`
- **桌面端**：PyInstaller 打包 → NSIS 生成 .exe 安装程序
- **安卓端**：Gradle 构建 .apk
- **产物发布**：GitHub Release 附件
- **质量门禁**：搜索质量验证（可通过 `allow_uncertified_search_quality_preview=true` 绕过）

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
