# YaoLens 项目文件与核心方法参考

> 版本：0.5（机器 SemVer：0.5.0）| 更新日期：2026-08-21

## 目录

1. [顶层入口文件](#顶层入口文件)
2. [image_vector_service（核心服务层）](#image_vector_service核心服务层)
3. [zvec_host（无界面应用服务）](#zvec_host无界面应用服务)
4. [zvec_webview（WebView 宿主）](#zvec_webviewwebview-宿主)
5. [zvec_lan（局域网服务）](#zvec_lan局域网服务)
6. [frontend（Vue 3 前端）](#frontendvue-3-前端)
7. [scripts / tools（辅助脚本）](#scripts--tools辅助脚本)

---

## 顶层入口文件

### `image_service.py`

CLI 主入口（命令 `zvec-image-search`），提供子命令：`index`、`sync`、`search`、`stats`、`roots`、`rebind-root`、`migrate-schema`、`metadata-backfill`、`cache-clear`、`clean-results`、`serve`。

| 核心方法 | 作用 |
|---------|------|
| `build_parser()` | 构建 argparse 命令行解析器，注册所有子命令及参数 |
| `main()` | CLI 主函数，解析参数后分发到对应子命令处理逻辑 |

### `zvec_launcher.py`

统一启动器（命令 `zvec`），负责原生配置管理、Docker 迁移、后端进程启动。

| 核心方法/类 | 作用 |
|------------|------|
| `NativeConfig` | 原生配置 dataclass，包含 config_home、workspace、token 等 |
| `validate_native_config()` | 校验配置文件合法性（schema 版本、路径安全） |
| `get_config_home()` | 获取配置主目录路径 |
| `LauncherError` | 用户可操作的启动错误基类 |

### `zvec_logging.py`

Zvec 引擎日志初始化。

| 核心方法 | 作用 |
|---------|------|
| `initialize_zvec(log_dir)` | 初始化 zvec 引擎的文件日志（轮转、7天保留），全局单例 |

---

## image_vector_service（核心服务层）

Python 包，提供所有业务逻辑。

### `service.py`（7499 行）

**顶层服务编排**，`ImageVectorService` 类是整个系统的核心协调者。

| 核心方法 | 作用 |
|---------|------|
| `index_folder()` | 索引图片文件夹：扫描→嵌入→写入向量集合 |
| `index_and_auto_tag_incremental()` | 增量索引+智能标注（watchdog 触发） |
| `search()` | 多模态搜索主入口（文本/图片/混合） |
| `sync_folder()` | 同步文件夹，删除失效记录 |
| `stats()` | 返回集合统计信息 |
| `prepare_search()` | 预计算搜索候选集（向量召回） |
| `rank_search()` | 对候选集执行置信度排序+多样性打散 |
| `export_results()` | 导出搜索结果到本地目录 |
| `manual_tag()` | 手动为图片添加/移除标签 |
| `run_auto_tag()` | 执行 AI 智能标注任务 |
| `build_clusters()` | 触发图片聚类 |
| `metadata_backfill()` | 补充描述嵌入向量 |

### `backend_server.py`（4096 行）

**HTTP API 服务器**，基于 `ThreadingHTTPServer`，端口 8765，Bearer Token 认证。

| 核心类/方法 | 作用 |
|------------|------|
| `BackendHTTPHandler` | HTTP 请求处理器，路由 `/v1/` 下所有 API |
| `run_backend_server()` | 启动 HTTP 服务主循环 |
| `_CAPABILITIES` | 服务端能力声明字典（前端据此启用功能） |
| `_JOB_PATH` | 异步任务轮询路径正则 `/v1/jobs/{job_id}` |
| `_MAX_REQUEST_BYTES` | 请求体上限 1 MiB |
| `_PROTOCOL_VERSION` | 协议版本号 2 |

### `config.py`（287 行）

**配置管理**。

| 核心类/方法 | 作用 |
|------------|------|
| `ServiceConfig` | 冻结 dataclass，包含所有服务配置（维度1024、COSINE、并发数、限流参数等） |
| `RuntimeCredentials` | 线程安全的运行时 API 凭据容器 |
| `default_config_home()` | 返回配置主目录（Windows: `%LOCALAPPDATA%/zvec-image-search`） |
| `ServiceConfig.validate()` | 校验配置合法性（维度、batch_size、限流水位等） |
| `ConfigurationError` | 配置错误异常 |

### `dashscope_client.py`（394 行）

**DashScope API 调用封装**（阿里云百炼多模态嵌入）。

| 核心类/方法 | 作用 |
|------------|------|
| `DashScopeEmbeddingClient` | 嵌入客户端，管理连接池、限流、重试 |
| `.embed_images(image_paths)` | 批量图片嵌入（最多 batch_size=5 张） |
| `.embed_text(text)` | 文本嵌入 |
| `EmbeddingResponse` | 嵌入响应 dataclass（vectors, request_id, usage） |
| `DashScopeError` | API 错误（含 status_code、是否可拆分） |
| `ImageInputError` | 图片输入错误（可拆分批次重试） |

### `hybrid_search.py`（314 行）

**混合检索意图识别**，在自然语言查询中检测标签意图。

| 核心类/方法 | 作用 |
|------------|------|
| `HybridTagIntent` | 标签意图检测结果（mode、fragments、matched_tags、权重） |
| `detect_hybrid_tag_intent(text, catalog)` | 从查询文本中提取已接受标签，计算 tag/vector 权重分配 |
| `hybrid_candidate_count()` | 计算混合搜索候选数量 |
| `HYBRID_TAG_WEIGHT / HYBRID_VECTOR_WEIGHT` | 标签 70% / 向量 30% 默认权重 |

### `rank_fusion.py`（1028 行）

**RRF 融合与置信度排序**。

| 核心方法 | 作用 |
|---------|------|
| `confidence_rank()` | 多路召回结果的置信度融合排序主函数 |
| `sort_confidence_hits()` | 按置信度对 hits 排序 |
| `confidence_candidate_limit(top_k)` | 动态计算候选池大小 |
| `normalize_sort_mode(value)` | 校验排序模式（relevance/confidence/diverse/legacy） |
| `sort_mode_uses_diversity()` | 判断排序模式是否启用多样性 |
| `ConfidenceRanking` | 排序结果 dataclass（hits、status、诊断信息） |
| `MINIMUM_RESULT_CONFIDENCE` | 最低置信度阈值 0.20 |

### `federated_search.py`（758 行）

**跨库联邦聚合搜索**。

| 核心方法/类 | 作用 |
|------------|------|
| `aggregate_federated_hits()` | 聚合多个库的候选集，执行跨库 RRF 融合 |
| `export_federated_search()` | 导出联邦搜索结果 |
| `LibraryCandidateSet` | 单库候选集（library + candidates） |
| `FederatedRanking` | 联邦排序结果 |

### `result_diversity.py`（144 行）

**结果去重与多样性打散**。

| 核心方法 | 作用 |
|---------|------|
| `diversify_search_hits()` | 按系列（文件夹）限制结果数量，防止同一系列霸屏 |
| `DiversityRanking` | 多样性排序结果（suppressed_count、relaxed） |
| `DEFAULT_MAX_RESULTS_PER_SERIES` | 每系列最多 2 条 |

### `metadata_search.py`（316 行）

**描述向量多路融合**。

| 核心方法 | 作用 |
|---------|------|
| `fuse_text_metadata_hits()` | 融合文本向量、描述向量、标签三路候选（加权 RRF） |
| `fuse_combined_metadata_hits()` | 图文混合搜索时的多路融合 |

### `tag_search.py`（440 行）

**标签匹配与查询计划**。

| 核心类/方法 | 作用 |
|------------|------|
| `TagSearchPlan` | 标签搜索计划（zvec_filter 表达式、matched_tags） |
| `TagCatalog` | 标签目录（所有已存储标签的索引） |
| `TagFragmentExpansion` | 单个查询片段的标签展开结果 |
| `normalize_tag_search_text()` | 标签文本归一化 |
| `matched_tags_for_result()` | 为搜索结果匹配命中的标签 |
| `TagExpansionTooBroadError` | 片段展开过宽错误 |

### `tags.py`（124 行）

**标签归一化与文件夹标签提取**。

| 核心方法 | 作用 |
|---------|------|
| `normalize_tags(tags)` | 标签列表去重、去空、去控制字符 |
| `clean_generated_tag(value)` | 清洗 AI 生成标签（去计数/大小元数据） |
| `folder_tags_for_relative_path()` | 从相对路径提取文件夹层级标签 |
| `build_tags_filter()` | 构建 zvec 标签过滤表达式 |
| `is_technical_metadata_tag()` | 判断是否为纯技术元数据标签 |

### `tag_aliases.py`（351 行）

**标签别名管理**。

| 核心类/方法 | 作用 |
|------------|------|
| `TagAliasGroup` | 一个规范标签及其别名组 |
| `TagAliasDictionary` | 别名字典（归一化查找） |
| `TagAliasStore` | 别名持久化存储（JSON 文件） |
| `normalize_tag_alias_text()` | 别名文本归一化 |

### `image_scanner.py`（885 行）

**文件系统扫描与图片检测**。

| 核心方法/类 | 作用 |
|------------|------|
| `scan_folder_to_staging()` | 扫描文件夹，将结果写入临时 SQLite staging |
| `inspect_image(path)` | 检测单张图片（尺寸、格式、SHA-256） |
| `inspect_query_image(path)` | 检测查询图片（含压缩） |
| `file_sha256(path)` | 计算文件 SHA-256 |
| `StagedScanResult` | 扫描结果摘要（scanned/supported/skipped） |
| `SUPPORTED_EXTENSIONS` | 支持的图片扩展名映射 |

### `scan_staging.py`（1537 行）

**扫描暂存层**，使用临时 SQLite 数据库存储扫描中间结果。

| 核心类/方法 | 作用 |
|------------|------|
| `ScanStaging` | 扫描暂存管理器（创建/读取/清理临时数据库） |
| `StagedImageRecord` | 暂存的图片记录 |
| `SeenScanDocument` | 已见文档去重 |
| `retire_orphaned_scan_staging()` | 清理孤立的暂存文件 |
| `cleanup_scan_staging()` | 清理过期暂存 |

### `image_clustering.py`（1608 行）

**确定性增量图片聚类**（存储/模型无关）。

| 核心类/方法 | 作用 |
|------------|------|
| `ImageClusterService` | 聚类服务主类 |
| `ClusterSnapshot` | 聚类快照（可序列化） |
| `IdentityEvidence` | 身份证据（category/value/source/confidence） |
| `SemanticNeighbor` | 语义邻居 |
| `apply_manual_cluster_rules()` | 应用手动聚类规则 |
| `cluster_detail()` | 获取聚类详情 |
| `read_cluster_snapshot() / write_cluster_snapshot()` | 快照读写 |
| `IDENTITY_CATEGORIES` | 身份类别：real_person, cosplayer, character, work |

### `large_cluster_adapter.py`（606 行）

**大型库聚类适配器**，桥接聚类引擎与库状态。

| 核心类/方法 | 作用 |
|------------|------|
| `LargeClusterAdapter` | 大库聚类适配器（分页读取、逐图计算感知哈希） |
| `should_use_large_cluster_engine()` | 判断是否使用大库引擎（阈值 10,000） |
| `DEFAULT_CLUSTER_TYPES` | 默认聚类类型：exact, perceptual |

### `large_image_clustering.py`

**大库聚类引擎**，纯算法层，不依赖库状态。

### `annotation_service.py`（5478 行）

**AI 智能标注服务**（qwen3-vl 视觉模型）。

| 核心类/方法 | 作用 |
|------------|------|
| `AutoTaggingCoordinator` | 智能标注协调器（并发控制、预算管理） |
| `StreamingAutoTagSession` | 流式标注会话 |
| `DEFAULT_AUTO_TAG_LIMIT` | 默认标注上限 200 张 |
| `MAX_AUTO_TAG_LIMIT` | 最大标注上限 10,000 张 |

### `vision_tagging_client.py`（1707 行）

**qwen3-vl-flash 视觉标注客户端**。

| 核心类/方法 | 作用 |
|------------|------|
| `DashScopeVisionTaggingClient` | 视觉标注 HTTP 客户端 |
| `VisionTaggingConfig` | 标注配置 |
| `VisionTaggingResponse` | 标注响应（proposed_tags、entities、description） |
| `TaggingBudget / TaggingBudgetTracker` | 标注预算管理 |
| `sanitize_generated_tag()` | 清洗生成的标签 |

### `active_learning.py`（676 行）

**主动学习审阅选择**（确定性、本地可复现）。

| 核心类/方法 | 作用 |
|------------|------|
| `ActiveLearningQueue` | 主动学习队列 |
| `ActiveLearningCandidate` | 待审阅候选项 |
| `ActiveLearningConfig` | 配置（budget=25、权重分配） |
| `build_active_learning_queue()` | 构建主动学习队列 |
| `record_learning_decision()` | 记录学习决策（accept/reject/edit/skip） |

### `active_learning_review_store.py`

**主动学习审阅持久化存储**。

| 核心类 | 作用 |
|--------|------|
| `ActiveLearningReviewStore` | 审阅项持久化（SQLite） |
| `ActiveLearningReviewItem` | 审阅项数据 |

### `search_learning_service.py`（924 行）

**搜索学习离线训练与版本控制**。

| 核心类/方法 | 作用 |
|------------|------|
| `SearchLearningServiceError` | 学习工作流错误 |
| `EvaluationGateResult` | 评测门禁结果 |
| `MIN_QUERY_SESSIONS` | 最少查询会话数 100 |
| `MIN_EXPLICIT_SAMPLES` | 最少显式样本数 300 |

### `search_learning_config.py`

**搜索学习配置加载**。

| 核心类/方法 | 作用 |
|------------|------|
| `SearchLearningBundle` | 学习配置包 |
| `load_search_learning()` | 加载搜索学习配置 |

### `search_learning_runtime.py`

**搜索学习运行时应用**。

| 核心方法 | 作用 |
|---------|------|
| `apply_search_learning()` | 将学习到的权重应用到排序 |

### `search_learning_store.py`

**搜索学习持久化存储**。

### `search_learning_evaluator.py`

**搜索学习固定评测**。

### `learning_ranker.py`（380 行）

**学习排序模型**。

| 核心类 | 作用 |
|--------|------|
| `RankingModel` | 排序模型（logistic/linear，含 intercept + weights） |
| `RANKING_MODEL_SCHEMA_VERSION` | 模型 schema 版本 1 |

### `search_features.py`（397 行）

**搜索特征抽取**。

| 核心常量/类 | 作用 |
|------------|------|
| `NUMERIC_FEATURE_NAMES` | 18 维数值特征名（vector_raw_score、tag_match_score 等） |
| `SearchFeatures` | 特征向量 dataclass |
| `SUPPORTED_QUERY_TYPES` | 支持的查询类型（text/image/combined/tag/identity 等） |

### `search_quality.py`（550 行）

**搜索质量配置与标注**。

| 核心方法/类 | 作用 |
|------------|------|
| `annotate_search_hits()` | 为搜索结果标注质量等级 |
| `load_search_quality()` | 加载搜索质量配置 |
| `CollectionCalibration` | 集合校准参数 |
| `QualityMode` | 质量模式：text/image/combined |

### `state.py`（3134 行）

**索引状态数据库**（SQLite），管理所有图片记录。

| 核心类/方法 | 作用 |
|------------|------|
| `IndexState` | 索引状态主类（CRUD 图片记录、根目录管理、变更队列） |
| `IndexStateReader` | 只读状态读取器 |
| `ENTRY_COLUMNS` | 记录字段列表（doc_id, root_id, relative_path, sha256 等） |
| `DOCUMENT_ANNOTATION_COLUMNS` | 标注字段列表 |

### `zvec_repository.py`（881 行）

**Zvec 向量集合仓库**，封装 zvec SDK 的 Collection 操作。

| 核心类/方法 | 作用 |
|------------|------|
| `ZvecImageRepository` | 向量仓库主类 |
| `.upsert_records()` | 批量写入图片记录+向量 |
| `.search_by_vector()` | 向量相似度搜索 |
| `.delete()` | 删除文档 |
| `.optimize()` | 集合压缩优化 |
| `.close()` | 释放 RocksDB 锁 |
| `COLLECTION_SCHEMA_VERSION` | 集合 schema 版本 4 |

### `collection_write_coordinator.py`（609 行）

**集合写入协调器**，保证 SQLite/Zvec 一致性。

| 核心类/方法 | 作用 |
|------------|------|
| `CollectionWriteCoordinator` | 写入协调器（有序执行 outbox 中的批次） |
| `PreparedCollectionUpsert` | 预备好的 upsert 操作 |
| `CollectionWriteRecoveryError` | 恢复失败错误 |

### `collection_write_outbox.py`（1398 行）

**持久化写前队列**（WAL），保证 SQLite/Zvec 双引擎一致性。

| 核心类/方法 | 作用 |
|------------|------|
| `CollectionWriteOutbox` | 写前队列（记录完整 payload，支持幂等重放） |
| `CollectionWriteItem` | 队列项 |
| `ReplayCollectionWrite` | 重放接口 |

### `file_watcher.py`（185 行）

**文件系统监听**（watchdog），自动增量索引触发。

| 核心类/方法 | 作用 |
|------------|------|
| `FileChangeWatcher` | 文件变更监听器（防抖、溢出检测） |
| `.start(roots)` | 开始监听指定根目录 |
| `.stop()` | 停止监听 |
| `.overflowed` | watchdog buffer 是否溢出 |

### `folder_deletion.py`（1265 行）

**文件夹删除管理**（两阶段：预览→确认删除）。

| 核心类/方法 | 作用 |
|------------|------|
| `FolderDeletionManager` | 文件夹删除管理器 |
| `FolderDeletionError` | 删除操作错误 |
| `_PREVIEW_TTL_SECONDS` | 预览有效期 15 分钟 |

### `library_browser.py`（220 行）

**只读文件夹/图片浏览器**。

| 核心类/方法 | 作用 |
|------------|------|
| `LibraryBrowser` | 库浏览器（list_folders、list_images） |
| `.list_folders()` | 列出文件夹（支持搜索、分页） |
| `.list_images()` | 列出文件夹内图片 |

### `library_config.py`（192 行）

**多库配置管理**。

| 核心类 | 作用 |
|--------|------|
| `LibraryDefinition` | 库定义（id、name、image_root、workspace、auto_index_enabled） |
| `LibraryCatalog` | 库目录（default_library_id、libraries 列表） |
| `load_library_catalog()` | 加载库配置清单 |

### `model_catalog.py`（536 行）

**模型配置目录**。

| 核心类/方法 | 作用 |
|------------|------|
| `ModelConfiguration` | 模型配置（embedding/auto_tag_primary/auto_tag_escalation） |
| `ModelPricing` | 模型定价 |
| `default_model_configuration()` | 默认模型配置 |
| `load_active_model_configuration()` | 加载用户自定义模型配置 |
| `EMBEDDING_ROLE / AUTO_TAG_PRIMARY_ROLE / AUTO_TAG_ESCALATION_ROLE` | 模型角色常量 |

### `rate_limiter.py`（431 行）

**线程安全、进程级 API 限流**。

| 核心类/方法 | 作用 |
|------------|------|
| `ModelRateLimiter` | 模型限流器（滑动窗口、并发控制、AIMD） |
| `RateLimitConfig` | 限流配置（RPM、TPM、并发数） |
| `get_process_rate_limiter()` | 获取进程级共享限流器 |
| `retry_after_seconds()` | 计算重试等待时间 |

### `result_exporter.py`（990 行）

**搜索结果导出**。

| 核心方法 | 作用 |
|---------|------|
| `export_results()` | 导出搜索结果到本地目录（含缩略图、manifest） |
| `search_report_payload()` | 搜索报告序列化 |
| `clean_result_directories()` | 清理过期结果目录 |
| `cleanup_search_results()` | 按天数清理搜索结果 |

### `source_resolver.py`（26 行）

**路径安全解析**。

| 核心类/方法 | 作用 |
|------------|------|
| `SourcePathResolver` | 源路径解析器（root_id + relative_path → 绝对路径） |
| `.resolve_fields()` | 从字段字典解析绝对路径 |
| `.resolve_hit()` | 从搜索命中解析绝对路径 |

### `logical_paths.py`（39 行）

**逻辑路径工具**。

| 核心方法 | 作用 |
|---------|------|
| `normalize_path(path)` | 路径归一化（normcase） |
| `normalize_relative_path(value)` | 相对路径归一化（POSIX 风格） |
| `logical_document_id(root_id, relative_path)` | 生成逻辑文档 ID（SHA-256） |
| `resolve_under_root(root_path, relative_path)` | 安全解析路径（防目录逃逸） |

### `process_lock.py`（69 行）

**跨进程文件锁**。

| 核心类/方法 | 作用 |
|------------|------|
| `ProcessLock` | 非阻塞跨进程锁（Windows: msvcrt / Unix: fcntl） |
| `.acquire()` | 获取锁 |
| `.release()` | 释放锁 |

### `failure_sink.py`（352 行）

**索引失败持久化**。

| 核心类/方法 | 作用 |
|------------|------|
| `FailureSink` | 失败沉淀器（内容寻址 blob + JSONL manifest） |
| `IndexFailureSink` | 索引失败专用沉淀器 |
| `FailureCapture` | 失败捕获记录 |

### `activity_store.py`（1765 行）

**任务历史与活动日志**（全局 SQLite）。

| 核心类 | 作用 |
|--------|------|
| `ActivityStore` | 活动存储（任务历史、结构化日志、敏感信息脱敏） |

### `metadata_backfill.py`（388 行）

**描述嵌入补充**（可恢复执行）。

| 核心类/方法 | 作用 |
|------------|------|
| `MetadataBackfillRunner` | 补充执行器（并发嵌入、逐条提交） |
| `MetadataBackfillItem` | 待补充项（doc_id、text、text_hash） |
| `MetadataBackfillReport` | 补充报告 |

### `metadata_text.py`

**元数据文本构建**。

| 核心方法 | 作用 |
|---------|------|
| `build_metadata_text()` | 从标签、描述等构建嵌入用文本 |

### `optimize_policy.py`（933 行）

**Zvec 优化调度策略**（低阻塞、持久化）。

| 核心类/方法 | 作用 |
|------------|------|
| `OptimizePolicyStore` | 优化策略存储（SQLite 计数器） |
| `OptimizeRuntimeAdapter` | 运行时适配器 |
| `evaluate_optimize_gate()` | 评估是否执行优化 |
| `DEFAULT_CHANGE_THRESHOLD` | 变更阈值 2,000 |

### `data_migration.py`（1749 行）

**数据迁移**（schema/root/legacy_config/docker_workspace）。

| 核心类/方法 | 作用 |
|------------|------|
| `DataMigrationError` | 迁移错误基类 |
| `migrate_schema()` | Schema 迁移 |
| `MIGRATION_CONFIRMATION_PHRASE` | 确认短语 "MIGRATE" |

### `workspace_backup.py`（522 行）

**工作空间备份**。

| 核心方法 | 作用 |
|---------|------|
| `create_migration_backup()` | 创建迁移前备份 |
| `plan_migration_backup()` | 规划备份 |
| `resolve_backup_library_directory()` | 解析备份目录（路径安全） |

### `path_migration.py`

**路径迁移**（旧 schema 升级）。

### `image_data_uri.py`

**图片 Data URI 编码**（含压缩）。

### `auto_tag_cache.py`

**智能标注缓存**（避免重复调用视觉模型）。

| 核心类 | 作用 |
|--------|------|
| `SharedAutoTagCache` | 共享标注缓存 |

### `cluster_operation_store.py`

**聚类操作持久化存储**。

### `search_result_store.py`

**搜索结果存储**（JSON 文件）。

### `backend_instance_lock.py`

**后端实例锁**（防止多实例启动）。

### `large_library_policy.py`

**大库策略**（大库特殊处理策略快照）。

### `rank_fusion.py` 中的搜索学习集成

通过 `apply_search_learning()` 将学习到的权重注入排序。

### `__init__.py`

包初始化，导出 `ImageVectorService`。

---

## zvec_host（无界面应用服务）

WebView 与 LAN 宿主共用的 Python 服务层，不包含 Tkinter、系统托盘或窗口入口。

### `backend_host.py`（894 行）

| 核心类/方法 | 作用 |
|------------|------|
| `BackendHost` | 后端进程生命周期管理（启动/停止/健康检查） |
| `BackendBusyError` | 后端忙碌错误（有任务运行时拒绝关闭） |

### `backend_api.py`

| 核心类 | 作用 |
|--------|------|
| `BackendApiClient` | 后端 HTTP API 客户端（同步） |
| `BackendApiError / BackendHttpError` | API 错误 |

### `search_service.py`

| 核心功能 | 作用 |
|---------|------|
| 搜索服务 | WebView/LAN 搜索逻辑封装 |

### `library_tasks.py`

| 核心功能 | 作用 |
|---------|------|
| 多库任务 | 库任务提交、轮询、取消和结果校验 |

### `model_settings.py` / `credentials.py`

| 核心功能 | 作用 |
|---------|------|
| 模型配置 | 模型选择、API 凭据管理 |

### `configuration_service.py`

| 核心功能 | 作用 |
|---------|------|
| 配置服务 | WebView/LAN 宿主配置读写 |

### `result_catalog.py`

| 核心功能 | 作用 |
|---------|------|
| 结果目录 | 搜索结果目录管理 |

---

## zvec_webview（WebView 宿主）

轻量 WebView 容器（pywebview），命令 `zvec-webview-preview`。

### `app.py`（224 行）

| 核心方法 | 作用 |
|---------|------|
| `main()` | WebView 预览入口（验证 Windows x64、启动运行时、创建窗口） |
| `build_parser()` | 参数解析（--width、--height、--debug-webview） |

### `runtime.py`（69 行）

| 核心类/方法 | 作用 |
|------------|------|
| `PreviewRuntime` | 运行时协调器（先启动 UI 网关，再后台初始化后端） |
| `.start()` | 启动网关 + 异步启动后端 + LAN 访问 |
| `.close()` | 安全关闭（检查活跃任务） |

### `native_bridge.py`（653 行）

| 核心类/方法 | 作用 |
|------------|------|
| `NativeBridge` | Windows 原生桥接（文件对话框、资源管理器、剪贴板、批量导出） |
| `.select_directory()` | 选择文件夹对话框 |
| `.select_json_file()` | 选择 JSON 文件 |
| `.export_images()` | 批量导出图片到目标文件夹 |

### `facade.py`

| 核心类 | 作用 |
|--------|------|
| `PreviewFacade` | WebView 外观层（封装后端启动、LAN、配置） |

### `server.py`

| 核心类 | 作用 |
|--------|------|
| `GatewayServer` | 本地 UI 网关服务器（静态资源 + API 代理） |
| `GatewayAddress` | 网关地址 |

### `lan_access.py`

| 核心功能 | 作用 |
|---------|------|
| LAN 访问集成 | 启动/停止局域网服务 |

### `image_registry.py`

| 核心类 | 作用 |
|--------|------|
| `ImageRegistry` | 图片注册表（缩略图缓存） |

### `diagnostics.py`

| 核心功能 | 作用 |
|---------|------|
| 诊断工具 | 文本脱敏、诊断信息收集 |

---

## zvec_lan（局域网服务）

为安卓/移动设备提供局域网访问。

### `discovery.py`（174 行）

| 核心类/方法 | 作用 |
|------------|------|
| `DiscoveryServer` | UDP 服务发现响应器（端口 38521） |
| `.start()` | 开始监听 UDP 探测 |
| `DiscoveryAddress` | 发现地址（host + port） |
| `DISCOVERY_PORT` | 发现端口 38521 |

### `http_server.py`（1643 行）

| 核心类/方法 | 作用 |
|------------|------|
| LAN HTTP 服务器 | 认证后的数据平面（端口 38522） |
| `API_PORT` | API 端口 38522 |
| 搜索/浏览/图片服务 | 为安卓端提供搜索、库浏览、图片流 |

### `pairing.py`（892 行）

| 核心类/方法 | 作用 |
|------------|------|
| `PairingManager` | 设备配对管理器（审批制） |
| `AuthenticatedClient` | 已认证客户端 |
| `PAIRING_TTL_SECONDS` | 配对有效期 5 分钟 |
| `MAX_PAIRING_RECORDS` | 最大配对记录 128 |

### `uploads.py`

| 核心类 | 作用 |
|--------|------|
| `QueryImageStore` | 查询图片上传存储 |

### `models.py`

| 核心类 | 作用 |
|--------|------|
| `LanSearchBackend` | LAN 搜索后端协议 |
| `SearchRequest / SearchPage` | 搜索请求/分页 |
| `LibraryInfo` | 库信息 |
| `MediaResolver / MediaSource` | 媒体解析 |

---

## frontend（Vue 3 前端）

Vue 3.5 + TypeScript 5.9 + Vite 8 SPA。

### 入口与全局

| 文件 | 作用 |
|------|------|
| `src/main.ts` | 应用入口，挂载 Vue 实例 |
| `src/App.vue` | 根组件（路由、布局） |
| `src/diagnostics.ts` | 前端诊断工具 |

### API 层（`src/api/`）

| 文件 | 作用 |
|------|------|
| `client.ts` | HTTP 客户端封装（Bearer Token、错误处理） |
| `gateway.ts` | 网关 API 调用（搜索、索引、任务、设置等） |

### 公共组件（`src/components/`）

| 文件 | 作用 |
|------|------|
| `GalleryGrid.vue` | 图片网格（masonry 瀑布流、dense 填充） |
| `ImageCard.vue` | 图片卡片（缩略图、标签、操作） |
| `ImagePreview.vue` | 图片预览（大图、元数据） |
| `PaginationBar.vue` | 分页栏 |
| `GalleryContextMenu.vue` | 图片右键菜单 |
| `StatusToast.vue` | 状态提示 |
| `AppIcon.vue` | 应用图标 |

### Composables（`src/composables/`）

| 文件 | 作用 |
|------|------|
| `useSearch.ts` | 搜索逻辑组合式函数（查询、分页、排序） |
| `useNativeImageActions.ts` | 原生图片操作（打开、导出、复制） |

### 功能模块（`src/features/`）

#### search-learning（搜索学习）

| 文件 | 作用 |
|------|------|
| `SearchLearningSettingsCard.vue` | 搜索学习设置卡片 |
| `useSearchFeedback.ts` | 搜索反馈逻辑 |
| `api.ts / types.ts` | API 与类型定义 |

#### organize（图片整理）

| 文件 | 作用 |
|------|------|
| `OrganizePage.vue` | 整理页面 |
| `ActiveLearningPanel.vue` | 主动学习面板（4列网格） |
| `SimilarityGroupsPanel.vue` | 相似组面板 |
| `useOrganize.ts` | 整理逻辑 |
| `useOrganizeIntelligence.ts` | 整理智能逻辑 |
| `galleryCapacity.ts` | 画廊容量计算 |

#### settings（设置）

| 文件 | 作用 |
|------|------|
| `SettingsPage.vue` | 设置页面 |
| `LanAccessSection.vue` | 局域网访问设置 |
| `DataMigrationSection.vue` | 数据迁移设置 |
| `useSettings.ts / useLanAccess.ts / useDataMigration.ts` | 设置逻辑 |

#### tasks（任务）

| 文件 | 作用 |
|------|------|
| `TasksPage.vue` | 任务页面 |
| `useJobs.ts` | 任务轮询逻辑 |

#### activity（活动中心）

| 文件 | 作用 |
|------|------|
| `ActivityLogTable.vue` | 活动日志表格 |
| `JobHistoryTable.vue` | 任务历史表格 |
| `useActivityCenter.ts` | 活动中心逻辑 |

#### cleanup（清理）

| 文件 | 作用 |
|------|------|
| `useSearchResultsCleanup.ts` | 搜索结果清理逻辑 |

---

## scripts / tools（辅助脚本）

### `scripts/`

| 文件 | 作用 |
|------|------|
| `build_webview_preview.py` | 构建 YaoLens Windows 包 |
| `assemble_python_release.py` | 组装 Windows + Android 发布资产 |
| `prepare_python_release.py` | 准备发布材料 |
| `provision_nsis.py` | 配置 NSIS 安装包 |
| `webview_preview_packaging.py` | Windows 打包契约 |
| `verify_search_quality_gate.py` | 搜索质量门禁验证 |
| `verify_webview_preview_release.py` | Windows 发布资产验证 |
| `run_large_library_gates.py` | 大库门禁测试 |

### `tools/`

| 文件 | 作用 |
|------|------|
| `tools/search_learning/generate_eval_pack.py` | 从历史搜索结果生成固定评测包 |

---

## 数据存储文件

| 路径 | 作用 |
|------|------|
| `image_collection/` | zvec 向量集合（RocksDB） |
| `image_collection.meta.json` | 集合元数据 |
| `image_collection.state.sqlite3` | 索引状态数据库（含 fs_change_queue 表） |
| `search_results/` | 搜索结果导出目录 |
| `search-learning/` | 搜索学习产物 |
| `fixed-evaluation.json` | 固定评测包 |
| `model-catalog.default.json` | 默认模型配置 |
| `optimize-policy.sqlite3` | 优化策略数据库 |
| `folder_deletions.sqlite3` | 文件夹删除记录 |

---

## 配置文件

| 文件 | 作用 |
|------|------|
| `pyproject.toml` | 项目元数据、依赖、CLI 入口、Ruff/mypy 配置 |
| `requirements.txt` | 运行时依赖 |
| `requirements-dev.txt` | 开发依赖 |
| `requirements-lock.txt` | 锁定依赖 |
| `.env.example` | 环境变量示例 |
| `frontend/package.json` | 前端依赖与脚本 |
| `frontend/vite.config.ts` | Vite 构建配置 |
| `frontend/tsconfig.json` | TypeScript 配置 |
