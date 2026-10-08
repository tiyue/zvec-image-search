# YaoLens 第三轮：独立图库与图片渲染优先级

日期：2026-10-08。第二轮提交 `52b6337` 为本轮基线。

## 1. 目标、现状与分工

用户已要求第三轮开发。沿用此前方案的两项：把目录浏览提升为独立「图库」工作区；先测量图片服务，再处理已证明的排队问题。主协调者准备原型、范围、独立副本和基线，审查实际差异、独立测试与视觉验收、组织返工、整合和提交；DeepSeek 使用现有 `deepseek-flash`、`xhigh` 配置实施明确范围。不调用其他模型或执行器，不自行提交、推送或发布。

目前「批量标签」页已经拥有目录、网格、大图查看器和批量选择，但右侧编辑栏常驻，导航把浏览放在标签工具下。新工作区复用现有模块和 API：左侧图库/目录，中间图片网格，右侧按需展开图片详情，复杂批量标签输入在二级对话框；相似分组保留明确工具入口。保留原内部页面 ID、Alt 快捷键、分页/密度、查看器、已访问页面状态及按需加载。

交互原型为仓库外的 `D:\CodexTools\deepseek-worker\work\zvec-round3-20261008\prototype\gallery.html`，自带浏览器展示编号示例图片，仅用于布局确认，不作为开发交付。用户已确认采用推荐布局：「批量标签」入口升级为「图库」，图库统一浏览和整理，批量标签和相似分组保留图库内的明确入口。原型只说明结构和交互关系，正式界面沿用现有产品 token 与风格。

## 2. 范围与实施顺序

- 原工作区：`D:\code\zvec`，仅协调者整合。
- 独立副本：`D:\CodexTools\deepseek-worker\work\projects\zvec-round3-20261008`，分支 `codex/gallery-workspace-priority`，基线 `52b6337`。按当前 D 盘存储约束创建，已有第二轮工作副本保留。
- 执行入口：`D:\CodexTools\deepseek-worker\worker.ps1`，PowerShell 7、`Mode=workspace-write`、精确 `AllowedFiles`。前端依赖联接复用主项目，Python 用主项目 `.venv\Scripts\python.exe -B`；不安装或升级依赖。
- UI 与调度两个实施任务串行，测试与构建串行，Vitest 最多两个 worker；图片解码仍最多两路。浏览器检查使用 Codex 自带浏览器，不启动系统 Edge，不做原生冷启动或硬件压力测试。
- 不修改真实图库、源图片、模型配置、数据库、搜索/推荐算法、安全模块、ARW 专用流程、Android、发布矩阵或执行器配置；保留原有 `.qoder/better-harness/`。不得修改无关部分，不覆盖用户或其他会话已有修改。

### UI 任务 AllowedFiles

1. `frontend/src/App.vue`
2. `frontend/src/styles.css`（仅 shell/导航和直接相关响应式规则）
3. `frontend/src/features/organize/OrganizePage.vue`
4. `frontend/src/features/organize/GalleryDetails.vue`（确有需要时新增，图片详情职责）
5. `frontend/src/features/organize/__tests__/GalleryDetails.spec.ts`（对应新增组件）
6. `frontend/src/features/organize/__tests__/OrganizePage.spec.ts`
7. `frontend/src/__tests__/App.spec.ts`
8. `frontend/src/__tests__/App.lazy-pages.spec.ts`
9. `frontend/src/__tests__/App.viewer.spec.ts`
10. `frontend/src/__tests__/App.search-feedback.spec.ts`（只有导航选择器确受影响时调整，不删除断言）
11. `docs/spec.md`（仅第三轮长期行为）

### 图片调度任务 AllowedFiles

1. `zvec_webview/image_registry.py`
2. `zvec_webview/image_scheduler.py`（确有需要时新增，有界优先级门）
3. `zvec_webview/server.py`（仅通用图片路由的可选 priority 接线与可理解的繁忙响应）
4. `tests/test_webview_image_registry.py`
5. `tests/test_webview_image_scheduler.py`（对应新增模块）
6. `tests/test_webview_gateway.py`（优先级接线与非法参数回归）
7. `frontend/src/components/ImageViewer.vue`（仅已知邻图预取优先级接线）
8. `frontend/src/components/__tests__/ImageViewer.spec.ts`
9. `docs/spec.md`（仅调度长期行为）

新增文件仅在职责确有需要时创建。发现白名单外确切缺口，先报告证据与最小所需范围，不绕过限制。协调者另维护本报告和 `AGENTS.md` 索引，并在验收后生成 `zvec_webview/frontend_dist/`；DeepSeek 不写构建产物。

## 3. 图库工作区验收契约

- 主导航「图库」替代原「批量标签」显示入口；内部 `batch/groups` 页面 ID 与 Alt+3/Alt+4 兼容。搜索、推荐、任务、ARW、编辑、设置仍可达，不重写它们的业务。相似分组由图库工具入口进入，提供返回图库；两个视图使用同一已挂载 OrganizePage，切换保留图库、文件夹、页码和选择，不新增第二套图库状态。可按浏览/工具做简单导航分组，密度合理，不增加导航框架。
- 目录/Root/文件夹搜索、刷新、图库切换、现有清理确认与从文件夹名生成标签保留既有含义。空 Root 与根目录直接图片入口不丢失；危险操作仍使用原来的预览与确认，不修改规则。
- 默认浏览布局不常驻编辑栏或详情栏。选中图片后出现相关操作栏；单击、Ctrl 多选、Shift 范围、全选当前页/整文件夹、反选/清空沿用原语义。密度/详情开关/窗口变化按现有锚点保留选择，不额外重新搜索或全目录读取。
- 详情按需打开，展示当前已加载选中图的文件名、尺寸、相对路径和已有标签；多选/整目录只展示已知条目的摘要，不为详情读取整个目录或假装知道未加载信息。关闭详情返回合适焦点。零选择或切换数据源时详情状态应一致。
- 批量标签由明确入口打开二级对话框，复用已有 `tagInput/draftTags`、添加/移除/替换、别名、统计、撤销、任务进度及作用范围；不复制业务逻辑到新 store。关闭、Esc 与点背景正常，焦点进入对话框并合理限制，返回入口。关闭/重新打开保留未提交草稿；切换文件夹/图库保持既有清理行为。提交回归以真实 API 测试断言，禁止删断言或把功能改成占位。
- 第二轮双击/空格大图、跨页、缩放、系统打开、焦点约束、迟到响应保护全部保持；浮动详情与标签对话框打开时，后台快捷键不改变页面或选择，输入框不被左右键/空格抢走。桌面详情为非模态侧栏，仍允许继续浏览选择；窄窗目录/详情作为可关闭二级面板。
- 使用现有 token 与语义 HTML，8pt 网格，合理标题/按钮，无蓝紫重点、纯装饰或卡片套卡片。1366×768、1920×1080、760×700、390×844 以及 light/dark/system 检查：目录、网格、详情和标签表单核心操作可达，无横向溢出；窄窗按抽屉或二级面板组织。弹出内容点外部能收起。

## 4. 排队测量与调度契约

基线脚本：`D:\CodexTools\deepseek-worker\work\zvec-round3-20261008\measure_image_queue.py`，结果 `queue-baseline.json`。只用 6 张 2048×1365 的合成 JPEG，在 D 盘运行真实注册、渲染与缓存；两路解码、不接触真实图库。冷缩略图中位数 18.47ms，内存命中 1.51ms，重启注册器后的磁盘命中 4.28ms；实际 `_render_image` 中位数 12.75ms。

另用事件保持两路已有渲染忙碌，先排入一个预取，再排当前 preview。已有信号量不区分优先级，渲染顺序为 `[0,1,2,3]`，当前图等待 295.39ms，预取释放前当前图无法开始。此 250ms 等待是人为控制的复现，证明排队归属而非真实图库速度；不得在交付中宣称 295ms 是用户实际耗时或新的真实加速比。

据此实现必要的优先级门，保持原有并发、缓存、文件一致性及单航班合并：

- 优先级依次为 current（当前大图）、visible（可见缩略图）、prefetch（已知邻图/页预取）、background（低优先级调用兼容）。不创建此前不存在的后台预生成服务。现有 `payload(id, variant)` 仍可调用，默认 preview=current、thumbnail=visible；网关通用图片路由接受可选 `priority`，不变更原 variant、ETag、尺寸或返回内容。
- 空闲名额优先给更高优先级，同级按到达顺序。优先级不抢占已在解码的图片；当前默认两路，不为结果更好而加并发。队列有固定内部上限，满时返回可理解的繁忙错误，不能无限堆积内存或线程任务；无需新用户配置项。
- 有可测试的公平性规则（例如最多连续 8 次高优先级后让最早等待者获得机会），防止后台永远饿死。所有名额在成功/异常时释放，关闭唤醒等待者，不遗留调度线程；优先使用 Condition/有界门，不再套线程池或庞大任务框架。
- 同一缓存键仍只渲染一次；当前图加入一个正在排队的同键预取时，能提升该排队任务的优先级，无论提升发生在磁盘检查前后都不能丢失。内存/磁盘缓存命中不等待解码名额，内容和缓存键不因 priority 分裂。
- 查看器至多预取当前前后各一张已知图片。仅为本应用通用 `api/image/{id}` 预取附带 prefetch；普通图片 URL、blob、外部 URL、ARW 路由保持原状。当前显示请求保持其默认 current，浏览器可给预取低 fetchPriority。快速切图/关闭沿用清理失效图片引用；已开始的 Python 解码不强行终止，不新增网络取消协议或断连探测。需要明确这一实际边界，不声称队列任务已取消。
- 新优先级输入补非法值测试；回归缓存命中、相同请求合并、文件改变/forget/close、队列满、异常释放、公平性、关停、并发上限和提升竞态。真实网关验证参数接线与返回语义；不修改权限、路径验证或安全头逻辑。

## 5. 验证、交付与回退

DeepSeek 各任务先读项目规则/Spec/开发指南，报告实际文件和检查；受限制失败时给出错误，不规避权限或启动别的执行器。UI 定向 Vitest、类型检查及代码冒烟；调度定向 unittest、非法输入与资源释放回归、Python Ruff 针对修改文件及查看器测试。

协调者独立审查 `report.json/last-message.md`、Git diff、范围违规和 runtime_cleanup；完整前端测试、类型检查与生产构建，构建完成后相关 Python/静态网关检查。浏览器用隔离合成图片/契约响应核对完整交互，并保存基线/新布局截图。调度用相同低负载脚本比较顺序、命中路径与两路上限；不使用旧系统 Edge 时序、不承诺原生冷启动或真实大图库收益。

两部分都真实验收通过才整合到主工作区，保留用户已有改动，更新 Spec/索引，检查同项目其他活动会话后按规则提交，不自动推送或发布。回退以本轮 commit 或基线 `52b6337` 对本轮文件恢复，不改变真实数据、配置或数据库格式。不得交付原型、demo、假想或未验证结果。

## 6. 实际运行与验收

- UI 初次运行：`2026-10-08T01-33-17-486Z-25588`，`deepseek-flash`、`xhigh`。执行器报告 `failed`，因 `ui-smoke/` 生成的 71 个白名单外临时文件；8 个正式源码/测试/Spec 文件在允许范围。`runtime_cleanup=removed`。执行器 Vitest 启动因 `spawn EPERM` 失败，其自建运行冒烟 42 项不代替正式验收。临时目录删除被执行器报告为策略拒绝，保留在独立副本作证据，不重试删除/移动、不整合或提交；后续任务明确禁止生成白名单外临时文件。
- 协调者初次独立验收：6 个前端测试文件、67 项通过；类型检查与正式构建通过。IAB 仍复现零选择标签对话框焦点停在后台、Alt+1 外泄切页；390px 目录被压至 12px；窄窗浮动详情没有点外部关闭。初版没有验收通过或整合。
- UI 返工运行：`2026-10-08T01-54-47-931Z-21544`，完成 4 个白名单源码文件的部分修正，无新增范围违规。执行器在已知 Vitest 限制后尝试以内存改写 Node 调用替代检查环境，协调者停止该进程，不接受其检查为验收；报告 `failed`，`runtime_cleanup=removed`，最后消息只有实施过程说明，未产生完成总结。
- 协调者完成必要收尾：测试组件自动卸载释放覆盖层，保留容量/跨页原断言；补空选择焦点、折叠 summary/Tab、草稿/输入键、目录抽屉、浮动详情释放、详情→查看器→详情焦点与完成任务进度/撤销回归；相关操作栏按选择显示，新增 UI 复用 token 与间距。主源码仍未整合。
- 图库阶段 IAB：16 项检查通过，包含空对话框焦点/Alt/Tab/Esc、草稿重开、分组返回选择与导航状态、390px 目录抽屉（320px 可用宽度）、浮动详情/大图返回与外点击；1366×768、390×844、760×700、1920×1080，深色输入实际为浅字深底。合成图片与隔离 API 契约响应，不代表真实图库或原生冷启动验收。最终构建将包含后续调度接线后再复核。

- 调度实施运行：`2026-10-08T02-14-40-043Z-5996`，9 文件白名单，`deepseek-flash/xhigh`。模型退出码 0，但执行器报告 `failed`；`runtime_cleanup=removed`。报告 12 个范围变动中，两个 OrganizePage 文件来自协调者同期的已复现焦点修正（执行器 reported_changed_files 不包含它们），另外 10 个为 Ruff 与 py_compile 生成的忽略缓存。缓存和受限空临时目录留在独立副本，不整合、不提交；既有 ui-smoke 也保留，不重试受拒绝的清理。此后返工和检查均串行，避免范围归属混淆。
- 执行器正式 unittest 受 Python 3.14 临时目录权限限制；Vitest 仍报 spawn EPERM，如实结束该项，没有再次使用 Node 调用改写。其 stdout 库方法冒烟与类型检查只是实施证据，不代替协调者正式测试。协调者查看 report.json、完整 last-message.md 和实际源码差异后继续验收。
- 协调者后端修正：正式测试先发现两处合成图片文件名前缀的预期笔误；保持 FIFO/提升顺序断言并修正文件名。新复现证明关闭时同图合并等待者会被活动解码或磁盘检查卡住、列表/字典 priority 抛 TypeError、文件变化重试会丢失已经提升的 priority；修复后关闭清空队列与航班并唤醒等待者，非法输入统一 ImageRegistryError，重试携带提升后的等级。补默认优先级与不同优先级复用同一内存 payload；原缓存、单航班、异常释放、并发、源变化、forget、磁盘发布等断言均保留。
- 协调者 UI 修正：将预取排除项按每组两张测试，保持全部 URL 排除断言及独立的最多两张断言；新增失败详情预览后从网格查看正常图的焦点回归。最终浏览器还复现提交按钮禁用后的焦点丢失，先补失败测试再修复到可见且可用的对话框控件；完成任务后 Esc 可直接关闭，进度与撤销保留。修正文案中的旧左右位置指示，使目录抽屉和标签对话框描述符合实际布局。
- 正式前端：51 个测试文件、435 项全部通过（maxWorkers=2），类型检查与生产构建通过。主工作区整合 16 个审查过的源码/测试/Spec 文件，保留用户 .qoder/better-harness；主构建与已视觉验收的独立副本 19 个产物逐字节一致。
- 正式 Python：主工作区 image_registry、image_scheduler、gateway、preview_packaging 共 97 项通过，其中调度相关三个模块 73 项；6 个改动 Python 文件 Ruff --no-cache 通过，3 个正式源模块 mypy 通过。本轮新增格式差异按 Ruff 修正并验证 AST 不变；gateway 测试第 696 行的既有格式提示保留，不扩改无关旧断言。真实静态网关验证 19 个产物完整性、18 个公开文件 HTTP 内容逐字节一致与 JS retry 查询后缀。未运行模型、真实大图库、原生冷启动或压力矩阵；已有 HTTPError 资源警告不影响测试通过。
- 最终 IAB：18 项通过，1366×768、1920×1080、760×700、390×844 的截图已人工检查；light/dark/system、零选择模态与可见 summary、Alt/Tab/Esc、目录抽屉、浮动详情、外部点击、大图返回、标签提交后进度/撤销与焦点、相似分组返回选择、密度锚点、空 Root/清除过期详情。隔离网络记录明确为当前 qa-2 的无 priority preview，以及 qa-1/qa-3 两张 priority=prefetch 邻图。这些响应是合成图片/API 契约；真实网关与渲染行为由上述 Python 测试另行验证，不代表原生窗口或真实数据验收。
- 受控复测：同一脚本、6 张合成 JPEG、两路解码，顺序从 `[0,1,2,3]` 改为 `[0,1,3,2]`；current 在释放低优先级 gate 前开始，max_active_decodes=2。候选冷缩略图中位数 18.36ms、内存命中 1.49ms、磁盘命中 3.94ms，与基线同量级；人为事件门下 current 等待约 33.49ms 对比基线 295.39ms，只用于确认调度，不宣称真实加速比。通用 registry 默认 2，production 的既有 4 不变，本轮没有提高并发。
- 预取边界：不同 priority query 是不同浏览器缓存地址，复用来自后端同缓存键/单航班；不保证少一次 HTTP 读取。清除浏览器 Image 引用不等于取消 Python 等待任务，不新增取消协议，也不终止已开始解码。

本轮证据位于 `D:\CodexTools\deepseek-worker\work\zvec-round3-20261008`：frontend-full-final.log、main-python-final.log、candidate-build-final.log、main-build-final.log、build-byte-comparison.json、main-gateway-assets-final.json、ui-final-checks.json、iab-owned-image-requests.json、queue-baseline.json、queue-candidate.json、integration-files.json，以及 gallery-desktop-final.png、gallery-mobile-final.png、gallery-1366-final.png、gallery-760-final.png。隔离验收标签操作不修改用户图库。临时 IAB 页与视口/主题覆盖已清理，自己的隔离服务在任务收尾关闭；以前被拒绝的进程/临时目录清理不重试。

提交前会话检查：同项目另一会话「提交当前分支，并建立新分支。」为 notLoaded，除当前协调会话外无活动会话；执行器已退出。本轮按项目规则本地提交，不自动推送或发布。源码版需重新打开后才加载本轮后端代码；已运行的旧窗口不作为本轮生效证据。
