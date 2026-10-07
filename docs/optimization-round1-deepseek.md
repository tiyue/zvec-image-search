# YaoLens 第一轮体验与查询优化：DeepSeek 实施任务

日期：2026-10-07。实施基线：`3ad6af37979829bab8fcdb24c039e9a8a2d38bb0`。

## 1. 目标、分工与边界

用户已授权本轮实施。主协调 Codex 编写任务、准备独立工作副本、审查实际差异、二次测试、视觉验收、组织纠正和最终提交；DeepSeek 只负责明确范围内的代码实施和定向自测。DeepSeek 不得启动其他模型或执行器，不得提交、推送、发布。

本轮交付两个能力：

1. 搜索和整理页共享三档缩略图密度，列数与分页容量适配实际窗口，并保留用户选择。
2. 文件夹图片排序利用匹配的索引，文件夹列表减少重复重关联与统计，保证所有结果和统计语义一致。

范围不包含安全加固、应用内大图查看器、导航重做、页面懒加载、图片调度改造、人脸、本地模型、ARW 功能、Android 功能、搜索排序算法、推荐算法、发布矩阵或依赖升级。不得修改无关部分；已有 `.qoder/better-harness/` 不属于本次任务。

必须阅读项目 `AGENTS.md`、`docs/spec.md`、`docs/development-workflow-guide.md`。使用解决本任务所需的最少代码，不新增无关配置、抽象或兜底分支。用户已授权本轮方案中的普通实施与兼容性索引更新，无需反复申请同一授权。发现真实阻断时报告具体证据，不交付占位结果。

## 2. 工作副本与运行约束

- 原工作区：`D:\code\zvec`。DeepSeek 不得在原工作区写代码。
- 数据库独立副本：`D:\CodexTools\deepseek-worker\work\projects\zvec-round1-20261007`。
- UI 独立副本：`D:\CodexTools\deepseek-worker\work\projects\zvec-round1-ui-20261007`。两个任务在各自副本执行，协调者合并并独立验收，不能相互写入。
- 调用入口：`D:\CodexTools\deepseek-worker\worker.ps1`，PowerShell 7，`Mode=workspace-write`，每次使用精确的 `AllowedFiles`。
- Python：`D:\code\zvec\.venv\Scripts\python.exe`。从独立副本目录调用 `-B`，设置 `PYTHONDONTWRITEBYTECODE=1`，避免在仓库创建编译缓存。不要安装依赖。
- 前端通过独立副本 `frontend/node_modules` 使用现有依赖；不要执行依赖升级或修改锁文件。
- 测试只使用临时、可重复创建的数据，不连接用户图库，不调用付费模型，不触及真实图片。
- 自测用 `npm.cmd test -- ...`、`npm.cmd run typecheck`。生产构建及 `zvec_webview/frontend_dist/` 更新由协调者验收时执行，不在 DeepSeek 白名单中。
- 范围检测以文件内容为准。只运行当前任务指定的命令；需要扩大范围时先在结果报告中说明原因。

## 3. 任务 A：数据库查询

### 允许修改的精确文件

- `image_vector_service/state.py`
- `image_vector_service/library_browser.py`
- `tests/test_library_browser.py`
- `tests/test_bounded_state_reads.py`
- `tools/benchmark_gallery_queries.py`（确有需要时新增可复用的查询基准工具）
- `docs/spec.md`（仅本轮数据库行为与限制）

不得修改任务 B 文件或其他无关文件。

### 已核对的事实

- `IndexState` 已有 WAL 与独立读连接，不要重复实现。
- `_page_folder_entries` 使用 `relative_path COLLATE NOCASE, doc_id` 排序，现有 `idx_entries_folder_page` 的路径列使用默认排序规则；原查询在临时数据库的执行计划出现额外排序，匹配 `NOCASE` 的索引能够消除同目录分页的额外排序。
- `_list_folders` 的总数和分页分别执行带多表关联、GROUP BY 的查询；图片数量、标签来源覆盖、失败数量与首次/末次索引时间必须保持一致。
- `IndexStateReader` 为短生命周期只读连接。不要假设跨请求缓存已经存在，也不要仅添加一个无法有效复用的实例缓存。
- 搜索结果存储已经按 rank 范围分页；不改它，不全局替换分页机制。

### 实施要求

1. 保存当前查询计划、代表性数据的输出与耗时基线，再改 SQL。
2. 为同目录图片分页提供匹配现有排序语义的索引，兼容已有库重开；评估新增/替换索引的写入和空间成本。不得改变文件排序和 schema 的对外语义。
3. 文件夹列表优先采用更轻的总数查询，以及先确定当前页文件夹、再为当前页聚合的方案，减少全量重复重关联。用实际 SQL 计划和数据验证选择，不强行增加缓存、持久化统计表或全局缓存框架。
4. 覆盖全部图库/单 Root、搜索词、同名目录、直接图片/后代图片、空 Root、标注失败、手工/模型/继承标签和目录删除后的结果。
5. 如 SQL 改写无法保持原语义，保留已验证的索引优化并明确报告统计部分阻断，不编造完成情况。

### 自测与验收

- `python -B -m unittest tests.test_library_browser tests.test_bounded_state_reads`，以及实际变更所需的定向状态测试。
- 新旧输出逐项相同，排序稳定；空页和深页结果正确；旧库重开可应用索引且无需重建向量。
- 基准至少涵盖 10,000 张/100 个目录、100,000 张/1,000 个目录的合成元数据；报告单目录分页、深页、目录列表的多次 P50/P95，明确是合成元数据，不宣称真实照片端到端达标。
- 记录索引构建耗时、数据库大小、代表性批量写入的对比；不能只报告优化后的暖缓存最好成绩。
- 协调者独立复跑与审查后才算通过。

## 4. 任务 B：自适应密度与分页

### 允许修改的精确文件

- `frontend/src/App.vue`
- `frontend/src/styles.css`
- `frontend/src/components/GalleryGrid.vue`
- `frontend/src/components/ImageCard.vue`
- `frontend/src/composables/useSearch.ts`
- `frontend/src/composables/useGalleryDensity.ts`（新增，共享密度与纯布局计算，保持简单）
- `frontend/src/types/contracts.ts`
- `frontend/src/api/client.ts`
- `frontend/src/features/organize/OrganizePage.vue`
- `frontend/src/features/organize/galleryCapacity.ts`
- `frontend/src/features/organize/useOrganize.ts`
- `frontend/src/__tests__/App.spec.ts`
- `frontend/src/__tests__/App.search-submit.spec.ts`
- `frontend/src/__tests__/App.search-feedback.spec.ts`
- `frontend/src/components/__tests__/GalleryGrid.spec.ts`
- `frontend/src/components/__tests__/ImageCard.spec.ts`
- `frontend/src/composables/__tests__/useSearch.spec.ts`
- `frontend/src/composables/__tests__/useSearch.resilience.spec.ts`
- `frontend/src/composables/__tests__/useGalleryDensity.spec.ts`（新增）
- `frontend/src/features/organize/__tests__/galleryCapacity.spec.ts`
- `frontend/src/features/organize/__tests__/OrganizePage.spec.ts`
- `tests/test_webview_gateway.py`（仅可变分页的既有接口回归）
- `tests/test_host_result_catalog.py`（仅可变分页回归）
- `docs/spec.md`（仅本轮 UI/分页行为与限制）

后端现有结果分页与提交接口已支持可变 page_size，应复用。任务 B 暂不允许修改后端生产代码；若核对出确切缺口，向协调者报告所需文件与理由。不得修改任务 A 的实现或任何无关部分。

### 产品行为

1. 密度选项为“紧凑 / 标准 / 大图”，默认标准；搜索与整理共享选择并在本机持久化。沿用现有主题 token，用小型原生选择控件，避免新增设置页面。
2. 以实际容器宽高计算布局。初始建议目标卡片宽度分别为 144/192/272 px，间距沿用 8 px；具体高度按搜索无文字卡片与整理含文字卡片分别计算。无尺寸时暂用现有默认容量，真实尺寸到达后再更新。
3. 搜索按稳定行序排列，保持全图可见；整理保留现有图片/标签信息和横图跨列语义。窄窗口必须减少列数，不能继续固定 5 列。容器不足时允许纵向滚动，所有本页结果都必须可达，禁止 overflow hidden 吞图。
4. 分页容量随容器和密度变化，前端上限 60，整理可保留 32 的现有有界上限。只改变展示容量，不改变 top_k 的语义、默认检索数量或排序结果。
5. 调整密度/窗口时优先保留选中图片；否则以原页首图片的绝对位置作为锚点，换算新页码。选中项与可见位置不能无故跳回第一页。
6. 切换容量只重新读取已有结果页，不重新提交语义搜索、不调用嵌入。缓存与预取必须区分页容量；旧容量、旧查询、旧来源的迟到响应不能覆盖当前页。
7. 复用 ResizeObserver，对 resize 去抖且忽略隐藏页面的零尺寸；只在容量真正变化时读取数据，避免尺寸/内容互相触发循环。
8. 首页、最新结果、搜索结果和历史结果都必须使用一致的分页行为；普通翻页、跳页、选择、Ctrl/Shift 多选与现有反馈保持工作。
9. 原图系统打开、右键菜单、详情、导出、任务及其他页面维持现有功能。不要顺手重写 App、统一所有页面或增加新导航。

### 自测与验收

- 纯布局：三档、窄/宽、零尺寸、极小尺寸与容量上限；密度持久化和异常旧存储值。
- 搜索：容量改变时锚点、缓存与预取隔离；旧请求延迟、快速切换、历史结果；不能重新提交搜索；结果数/页数和页边界一致。
- 整理：不再固定返回 6；窗口/密度变化后正确 offset/limit，保留当前位置及现有横竖图规则。
- 定向 Vitest、完整 TypeScript 检查、至少一次功能冒烟。新增参数校验须有非法输入测试。
- 协调者最终运行前端全套测试与 production build，并以真实 Vue 页面配合隔离测试后端做浏览器视觉检查：1366×768、1920×1080、窄窗口；light/dark/system；三档密度和连续 resize。测试数据只能是验收夹具，禁止把假数据加入生产页面。

## 5. 报告与返工

DeepSeek 最终报告必须列出：实际修改文件、每项需求的完成情况、运行命令与结果、基线/优化后数据、未验证项及具体限制。`completed` 仅表示进程结束，不表示验收通过。

协调者检查 `report.json`、`last-message.md`、实际 Git diff、白名单越界和 `runtime_cleanup`，复跑测试与视觉检查；发现问题时将明确复现、期望和范围写入返工任务，再交给 DeepSeek 纠正。所有修复仍需二次验收。

通过后，协调者把已验收变更整合回原工作区，保留用户已有修改，更新本规格关联文档；检查同项目活动会话后按项目规则提交一次 Git commit，不自动推送。不得以剩余时间不足为理由跳过验收或交付未完成结果。

## 6. 实际交付与二次验收记录

本轮实施及返工由全局 DeepSeek 执行器完成，模型为 `deepseek-flash`，配置强度为 `low`。公共入口没有单次强度参数；本轮未修改全局工具配置。协调者独立审查、复现、组织返工、整合，并修正文档及一处目录存在性说明注释。

### 已交付

- 搜索首页、结果及历史页、整理图库共享紧凑/标准/大图，持久化选择，按容器调整列数和分页容量。密度变化保留主选中项与绝对位置；普通翻页沿用原选择规则；仅容量真正变化才重读已有结果页。
- 搜索请求与预取处理容量往返、迟到响应、初始化及历史恢复竞争；整理按已呈现页计算连续调整的锚点，并用真实列数与跨列计算图片高度。
- 搜索结果与整理区域提供随系统的深浅色样式，390px 窄窗口标题和控件合理换行；其他页面的新增样式影响已通过限定选择器消除。
- 同目录分页新增 NOCASE 复合索引；目录列表先选当前页，再聚合这一页的统计。旧库重开自动应用索引，现有排序、标签统计与 Root 语义保留。
- 同步项目规格与嵌入的 `zvec_webview/frontend_dist/`，未构建或发布安装包。

### 返工与范围审查

首轮数据库进程超时，产生的 `build/` 基准输出和 Ruff 缓存被范围检测标为越界；这些开发产物未合入主项目。随后两次数据库返工修正写入成本对比、测量命名、临时目录归属、连接准备公平性及完整字段等价比较。

UI 返工修正结果页入口、主选中项、A→B→A 竞争、初始化与历史恢复、整理高度和连续锚点，随后收窄样式作用范围、修正窄屏标题与相同容量下的高度更新。最后修正动态分页测试夹具，保留并加强页边界断言。其余六次执行报告无白名单越界；七次运行的 `runtime_cleanup` 均为 `removed`。原有 `.qoder/better-harness/` 未修改、未纳入提交。

### 协调者独立验证

- 主项目 `npm.cmd test`：43 个文件、346 项全部通过；`npm.cmd run typecheck` 通过；`npm.cmd run build` 通过。
- 主项目 `python -B -m unittest tests.test_library_browser tests.test_bounded_state_reads tests.test_webview_gateway tests.test_host_result_catalog tests.test_annotation_state_queries tests.test_perceptual_hash_state`：89 项全部通过。
- 所有修改的 Python 文件执行 `ruff check --no-cache` 通过，`git diff --check` 通过。
- 额外独立分页审计 4 项通过；真实生产 Vue 页面配隔离后端的浏览器验收 156 项通过，无浏览器异常。覆盖 1366×768、1920×1080、760×700、390×844，三档密度、light/dark 系统切换、共享保存、历史恢复、选中位置、连续 resize、不重复提交搜索及完整图片显示。已人工查看截图；验收夹具未进入生产代码。
- 首次将构建与依赖构建产物的网关测试并行，触发一次构建目录短暂缺文件；已改为构建完成后测试，主项目上述 89 项最终通过。

### SQL 合成基准

使用同一机器、等价元数据副本及相同连接准备，比对原 SQL 与最终 SQL/索引。每组完整返回字段一致；10,000 条/100 目录各测 15 次，100,000 条/1,000 目录各测 9 次。以下为 P50/P95（毫秒），已预读，不能视为冷缓存或真实照片端到端指标。

| 规模与场景 | 原实现 P50/P95 | 最终实现 P50/P95 |
| --- | ---: | ---: |
| 1 万条，目录列表首屏 | 55.076 / 55.727 | 44.631 / 45.827 |
| 1 万条，同目录深页 | 6.468 / 6.886 | 4.234 / 5.016 |
| 10 万条，目录列表首屏 | 634.980 / 668.559 | 108.481 / 114.590 |
| 10 万条，目录列表深页 | 623.006 / 631.099 | 185.605 / 187.969 |
| 10 万条，同目录深页 | 67.156 / 67.744 | 13.952 / 14.226 |
| 10 万条，递归图片分页 | 95.605 / 96.891 | 99.203 / 101.899 |

10 万条状态数据库中新增索引约 8.37 MB（原库约 132.81 MB），补建耗时估算约 0.226 秒。一次 500 条写入分别测得 38.164 ms（无新增索引）与 40.809 ms（有索引），计时前后核对索引状态；单次写入数据只用于展示成本，不能据此做稳定吞吐结论。递归分页未确认收益，目录总数仍需计数扫描，未增加统计缓存。

复现命令：`python -B tools/benchmark_gallery_queries.py --scenario small` 与 `--scenario large`。完整本机验收资料位于 `D:\CodexTools\deepseek-worker\work\zvec-round1-20261007`，执行报告位于 `D:\CodexTools\deepseek-worker\work\runs`。
