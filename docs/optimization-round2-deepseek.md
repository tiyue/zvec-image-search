# YaoLens 第二轮：应用内大图与页面按需加载

日期：2026-10-07 至 2026-10-08。第一轮已由用户验收；本轮基线为 `339312e`。

## 1. 目标和分工

沿用此前三轮方案的第二轮：通用图片在应用内连续查看，非首屏功能页面首次进入时加载。主协调者准备任务与独立副本、审查实际差异、独立测试和视觉检查、组织返工及提交；DeepSeek 负责本范围的实施与定向自测。不得调用其他模型或执行器，不得提交、推送或发布。

第二轮不重做导航、图库工作区、图片后端调度、数据库、安全模块、ARW 专用选片、Android、推荐算法、搜索算法、模型设置或依赖。第一轮密度、可变容量、稳定顺序和异步分页保护必须保持。不得修改无关部分，尤其不得修改原有 `.qoder/better-harness/` 或真实图库数据。

DeepSeek 全局配置已按用户要求从 `low` 改为 `xhigh`，模型仍为 `deepseek-flash`。官方接口把 `xhigh` 兼容映射为原生 `high`，原生最高为 `max`；本次不改成 `max`。真实只读公共入口冒烟已完成，不能仅凭模型自述判断强度效果。参考：[DeepSeek Responses API](https://api-docs.deepseek.com/api/create-response/)。

## 2. 工作环境与精确范围

- 原工作区：`D:\code\zvec`，仅协调者整合和验证。
- 独立实施副本：`D:\CodexTools\deepseek-worker\work\projects\zvec-round2-20261007`，基线与原项目相同。
- 入口：`D:\CodexTools\deepseek-worker\worker.ps1`；PowerShell 7、`Mode=workspace-write`，仅下列 `AllowedFiles`。
- 前端现有依赖通过本副本 `frontend/node_modules` 联接复用，禁止升级、安装或修改依赖。`npm.cmd test -- ...`、`npm.cmd run typecheck`；生产构建由协调者执行。
- Python 如需使用：`D:\code\zvec\.venv\Scripts\python.exe -B`，禁止调用真实图库、付费模型或生成仓库缓存。
- 必须阅读项目 `AGENTS.md`、`docs/spec.md`、`docs/development-workflow-guide.md`。使用最少必要代码，不顺手清理或重排格式。

DeepSeek 允许修改的相对文件：

1. `frontend/src/App.vue`
2. `frontend/src/components/ImageViewer.vue`（新增，共享查看器）
3. `frontend/src/components/__tests__/ImageViewer.spec.ts`（新增）
4. `frontend/src/composables/useImageViewer.ts`（新增，确有共享状态或几何逻辑时使用）
5. `frontend/src/composables/__tests__/useImageViewer.spec.ts`（新增）
6. `frontend/src/components/AsyncPageState.vue`（新增，轻量加载/错误与重试状态）
7. `frontend/src/components/__tests__/AsyncPageState.spec.ts`（新增）
8. `frontend/src/components/GalleryContextMenu.vue`（仅明确系统打开文案）
9. `frontend/src/components/__tests__/GalleryContextMenu.spec.ts`
10. `frontend/src/__tests__/App.spec.ts`
11. `frontend/src/__tests__/App.search-submit.spec.ts`
12. `frontend/src/__tests__/App.search-feedback.spec.ts`
13. `frontend/src/__tests__/App.viewer.spec.ts`（新增）
14. `frontend/src/__tests__/App.lazy-pages.spec.ts`（新增）
15. `frontend/src/features/organize/OrganizePage.vue`
16. `frontend/src/features/organize/__tests__/OrganizePage.spec.ts`
17. `frontend/src/features/recommendations/RecommendationPage.vue`
18. `frontend/src/features/recommendations/__tests__/RecommendationPage.spec.ts`
19. `docs/spec.md`（只更新本轮长期产品行为）
20. `frontend/vite.config.ts`（实际构建复现 WebView 拒绝入口与异步页面间的循环引用，仅调整必要共享模块分包，不能改变资源校验、依赖或其他构建参数）
21. `frontend/src/composables/useSearch.ts`（浏览器已复现关闭后迟到分页覆盖选择、打开前在途容量响应使查看器消失，仅添加必要的分页取消及已应用容量恢复接口，不改搜索算法或既有分页语义）
22. `frontend/src/composables/__tests__/useSearch.spec.ts`（上述取消、迟到响应和容量恢复回归）
23. `frontend/src/features/organize/useOrganize.ts`（同类目录分页的必要取消及已应用容量恢复接口）
24. `frontend/src/features/organize/__tests__/useOrganize.spec.ts`（同类目录回归）
25. `frontend/src/composables/useLazyPage.ts`（只有真实模块重试需要共享逻辑时新增；初次动态导入不变、按需加载不变，不增加依赖或自动重试）
26. `frontend/src/composables/__tests__/useLazyPage.spec.ts`（如新增上述文件，覆盖真实成功、失败、手动恢复及无自动请求）

新增文件仅在确有需要时创建，不为名单而建空文件。原 `ImagePreview.vue` 和推荐详情继续作为标签/元数据详情；新查看器使用已有图片 URL，后端生产代码不在本轮白名单。`useSearch.ts`、`useOrganize.ts` 的扩展仅限独立浏览器验收已证明的分页取消接口缺口，不重写第一轮行为。若存在其他确切接口缺口，先报告证据和所需最小文件范围，不绕过限制。不得修改无关部分。

## 3. 应用内大图查看器

### 用户交互

- 搜索、目录图片保持单击选择；双击打开应用内大图。空格在相应图片列表焦点上下文打开当前主选中项。推荐保留目前选择/右键反馈行为，并提供双击和聚焦卡片空格进入同一查看器。
- 查看器是覆盖层，来源页面及滚动容器保持挂载；Esc 和关闭按钮退出。关闭后焦点回到来源图片或有效来源入口；相同页保持原滚动，跨页切图后原列表显示当前图片，不能跳回搜索首页或旧页。
- 左右键和小型上一张/下一张按钮连续切图。搜索和目录可跨分页边界，复用现有 `search.goToPage(): Promise<boolean>` 和 `organize.loadFolderImages(): Promise<boolean>`；搜索不得重新提交语义请求，目录不得读取整个文件夹。推荐只在当前已展示批次内切换，不能隐式创建新批次或增加 shown 曝光。
- 首尾明确禁用越界切换；页读取失败保留当前图片并可重试，不能把旧图片名称配到新图片上。连续按键、关闭、页面切换、数据源变化和迟到响应需要明确所有权，避免失效请求重新打开查看器或覆盖新选中项。
- 滚轮以指针为中心缩放、拖动平移、双击切换适合窗口/100%，并提供有标签的适合窗口/100%按钮。放大可查看细节，平移不能把图片彻底移出可见区域；窗口变化重新约束几何，切图重置适合窗口。使用真实图片自然尺寸，不能把缩略图尺寸当原图 100%。
- 当前图先用缩略图占位，再加载已有高分辨率 URL；迟到的上一张图不得覆盖新图，失败有可理解提示。仅为当前图前后各一张已知图片预加载；不为预取跨页、不复制完整图片数据、不建立无限缓存；关闭/切源清除持有的预加载引用。
- 信息侧栏可收起，至少显示文件名、已有尺寸、相对路径及标签。沿用产品主题 token，light/dark/system、390px 窄窗口合理布局，不遮住核心按钮、不产生横向溢出；适合窗口时完整图片可见。
- 系统打开继续通过原生操作；搜索右键“系统打开”保持原生，并在查看器提供明确系统打开入口。目录发出已有 `openImage` 原生事件；推荐复用 `runNativeAction` 与记录逻辑。查看器不能把纯切图错误记成一次系统打开，也不能重复记录推荐 shown。
- 输入框、textarea、select、contenteditable 获得焦点时不抢走空格/左右键；覆盖层打开时，主应用快捷键不能切换后台页面或修改后台选择。需要合理的对话框语义、初始焦点、焦点约束、关闭后恢复以及事件监听清理。

### 当前代码事实

搜索双击目前调用 `openSearchImage` → 原生系统打开，右键详情使用独立 `ImagePreview`。`useSearch` 已有分页、主选中项和大图启用状态，保持其第一轮行为。目录图片提供 `nativeImageId`、`imageUrl`、`thumbnailUrl`、尺寸和有效标签；推荐有 `previewUrl`、缩略图、批次和动作记账。不需要新增后端接口。

共享组件负责显示、几何和通用输入行为；来源页面负责数据、分页及既有业务动作。可以使用一个适度的共享 composable，不引入通用状态机框架、图片调度框架或新依赖。

## 4. 非首屏页面按需加载

- `App.vue` 目前静态导入任务、整理、推荐、ARW、图片编辑和设置。采用 Vue `defineAsyncComponent` 与直接 `.vue` 动态导入，避免经聚合入口把所有页面又拉回首包；类型用 `import type`。搜索首页维持首屏同步加载。
- 继续复用 `visitedPages` 的首次挂载及 `v-show` 保留机制；隐藏已访问页面不能卸载并丢失任务、参数或选择，隐藏页面仍按现有 visible 契约暂停前台逻辑。批量标签与相似分组复用同一个 OrganizePage。
- 首次进入有轻量加载状态；异步加载失败提供明确错误及按钮重试，不能永久空白，也不能自动循环请求。失败后切换到其他页面应可正常使用。
- 保留原有导航、Alt 快捷键、图片编辑拖入接线、图库设置保存事件、ARW 事件及原生桥；不把已有全局业务逻辑整体搬动。
- 用生产构建验证生成独立页面 chunk，入口不再包含未访问的大型功能；记录入口原始/gzip 字节、首页请求模块数和首页可操作测量口径。首次进入各页验证网络成功和页面内容，已访问页重新进入不重复加载模块。
- 测试不能只靠 stub 掩盖异步行为，也不能用“减少测试/删断言”解决挂载等待；需要调整等待并增加真实 async loader 成功、失败、重试和状态保留测试。

## 5. 检查与完成标准

DeepSeek：组件/几何和三个页面定向 Vitest、App 集成回归、TypeScript 检查、至少一次代码级冒烟。所有测试结果必须是真实运行结果；Windows 子进程或临时文件限制若阻断检查，报告具体错误，不修改依赖或工具代码来规避。

协调者独立：完整前端测试、类型检查、生产 build（更新嵌入资源）、静态资源验证和相关网关测试；构建完成后再运行依赖产物的测试，禁止并行清空产物。使用隔离真实页面后端和浏览器检查搜索/目录跨页、当前批次推荐、快速切图/关闭/错误、缩放平移、输入焦点、返回滚动、light/dark、1366×768/1920×1080/760×700/390×844。人工查看截图。

性能比较报告必须区分首屏同步依赖字节、浏览器页面时序与完整原生 WebView/后端冷启动。蓝屏排查后按用户要求只使用 Codex 自带浏览器，不再启动系统 Edge；测试与构建串行，Vitest 最多两个 worker。未测量实际 WebView 冷启动就不能承诺启动速度达标；不以最优暖缓存成绩代替多次分布。对本轮 xhigh 与第一轮 low 的缺陷数量只能如实记录任务难度差异，不能凭两次不同任务宣称因果效果。

检查 `report.json`、`last-message.md`、Git diff、范围违规、`runtime_cleanup`；发现实际问题用明确复现和白名单返工。验收通过后才整合，保留用户已有变更、同步 Spec 及索引，按项目会话检查规则提交，不自动推送。不得交付 demo 或未验证占位结果。

## 6. 实际实施与验收

### 最终实现与返工

搜索、目录与推荐接入共享图片查看器；提供连续切图、适合窗口/真实原图 100%、指针中心缩放、受限平移、信息栏、焦点约束与恢复。搜索和目录跨页只读取所需页，推荐只在当前批次内切换。任务、整理、推荐、ARW、图片编辑与设置六个页面按需加载，已访问页面继续挂载；失败后由用户手动重试实际模块或样式表请求。

独立验收曾发现并交给 DeepSeek 定向返工：入口与异步页的构建清单循环引用、控件指针被画布捕获、跨页迟到响应覆盖返回选择、打开前容量请求替换查看器窗口、隐藏控件进入焦点环、失败模块同 URL 无法恢复、失败 CSS 未重新加载，以及重试样式表未清理另一事件监听器。最后一轮同时补齐既有反馈测试的图片 URL 字段，保留原反馈、原生打开与容错断言，不删除测试或放松图片可用性判断。

已逐一阅读报告、最终说明和实际差异。以下五次完成运行均为 `completed`、`scope_violations=[]`、`runtime_cleanup=removed`：

- `2026-10-07T14-02-04-500Z-57456`
- `2026-10-07T14-26-28-877Z-13916`
- `2026-10-07T14-49-27-558Z-68008`
- `2026-10-07T15-28-25-518Z-28280`
- `2026-10-07T16-29-05-033Z-25628`

运行记录保留在 `D:\CodexTools\deepseek-worker\work\runs`。最后一次 DeepSeek Vitest 启动因 `spawn EPERM` 被其运行限制阻断，未规避限制；其类型检查及真实 TS 转译 + jsdom 监听器冒烟 15/15 通过。协调者随后独立执行完整前端检查。xhigh 本轮仍需要多次返工，不能据此判断它比第一轮 low 更好或更差，两轮任务难度和范围不同。

### 协调者独立验证

- 完整前端 Vitest：50 个文件、416 项全部通过，`--maxWorkers=2`，36.63 秒。
- `npm.cmd run typecheck` 与生产构建通过；未升级依赖。
- 最终生产构建通过既有 `validate_frontend_build`：19 个清单资源；真实 `GatewayServer` 返回 18 个可服务资源且内容逐字节匹配，JS 查询参数后缀正常。生产资源校验代码未修改。
- Codex 自带浏览器：39 项检查全部通过，包括搜索和目录跨页、返回当前图片及焦点、推荐选择与批次边界、真实自然尺寸 100%、缩放和平移、覆盖层快捷键与焦点环、页面状态保留、首页和异步页加载，以及 JS/CSS 失败后的实际请求恢复。单独执行推荐查看器打开/切图/关闭周期，新增业务请求为 0；重新进入推荐页仍保留其既有刷新行为。
- 对 1366×768、1920×1080、760×700、390×844 各保存 light/dark 截图，共 8 张，逐一人工检查；适合窗口时图片完整、核心操作可达、无横向溢出。深浅模式检查后恢复跟随系统及默认窗口尺寸。
- 隔离测试服务使用编号测试图与契约响应，不接触真实图库、模型或原生系统操作。期间设置学习状态及 ARW 项目列表的测试响应缺少字段，已补齐测试服务契约；未因此改动生产模块。补齐后的端口无新增浏览器错误。

原始证据：`D:\CodexTools\deepseek-worker\work\zvec-round2-20261007` 下的 `vitest-final.log`、`typecheck-final.log`、`build-final.log`、`bundle-comparison.json` 与 `visual\iab-final\checks.json`、请求日志、8 张截图。

最终已把 25 个实际变更的白名单源码/测试/Spec 文件逐字节整合到 `D:\code\zvec`（其中原有 `App.search-submit.spec.ts` 无需修改），哈希记录见 `integration-files.json`。主工作区 `npm.cmd run build`（包含类型检查）通过，生成的全部 19 个文件与浏览器验收构建逐字节一致；再次通过生产静态资源验证和网关服务检查。随后串行运行 `python -B -m unittest tests.test_webview_gateway tests.test_webview_preview_packaging`，57 项全部通过、6.438 秒。主工作区结果见 `main-build-final.log`、`main-python-final.log`、`main-gateway-assets-final.json`。没有在构建清空资源时并行测试，没有重启用户正在使用的源码版。

### 首屏数据和测量边界

以同一 gzip 算法统计生产清单入口及全部同步 JS 依赖，排除未访问的异步页，避免只比较入口而忽略 shared 模块：

| 指标 | 第一轮基线 | 第二轮 | 变化 |
| --- | ---: | ---: | ---: |
| 首屏同步 JS 字节 | 455,823 | 178,463 | 减少 60.85% |
| 同步 JS gzip 字节 | 144,281 | 64,528 | 减少 55.28% |
| 首屏 JS 模块请求 | 1 | 2（入口与 shared） | 功能页面不在首屏加载 |
| 独立异步功能页 | 0 | 6 | 首次进入加载 |

最终隔离首页在自带浏览器中禁用 HTTP 缓存，串行重新加载 7 次：DOMContentLoaded 中位数 41.5ms、范围 38.4–45.4ms；load 中位数 47.9ms、范围 44.3–54.1ms。搜索输入可见的观测上界中位数 215ms、范围 171–222ms，包含工具调用与观察延迟。这是已运行浏览器与隔离后端的页面时序，没有对应基线时序比较，不能解释为原生 WebView、真实图库/模型后端的完整冷启动耗时或速度增益。

### 保留项与整合约束

- 蓝屏关联到系统 Edge 的既有分析见 `D:\CodexTools\diagnostics\zvec-bsod-20261007\蓝屏排查报告.md`；当前工作流程改用自带浏览器，不宣称已修复蓝屏根因。
- 崩溃中断运行 `2026-10-07T15-18-39-303Z-46896` 没有完成报告；其旧临时状态清理被自动审批拒绝，理由仅为 `blocked by policy`，已保留 `interruption-note.md`，没有换路径或手段规避。
- 最后一次 DeepSeek 的 `%TEMP%\zvec-smoke` 清理被其沙箱阻止，文件在仓库外保留；它的 codex-state 清理已完成，二者不是同一目录。
- 本次停止并重启隔离测试服务的命令同样被自动审批以 `blocked by policy` 拒绝。原测试服务 PID 19616（49374）保留，后续使用独立测试服务 PID 15496（49375）；不绕过停止操作，不结束用户的 YaoLens/后端进程。
- 主工作区只整合上述白名单中实际发生变更的源码/测试、`docs/spec.md`、本报告、`AGENTS.md` 索引与生产构建派生的 `zvec_webview/frontend_dist/`。原有 `.qoder/better-harness/` 保留且不暂存；不自动推送或发布。
