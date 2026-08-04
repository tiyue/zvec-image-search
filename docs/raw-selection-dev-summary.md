# Zvec Sony A7M4 ARW 选片模块 — 开发进度交接文档

> 生成时间：2026-08-03 21:30
> 最近检查点：2026-08-04（最终构建、回归与视觉复验完成）
> 分支：`codex/collection`（本地领先 origin 9 个提交，未 push）
> 目的：供其他大模型/开发者无缝接续开发，无需回看本会话

> **事实优先级：本节之后的“0. 当前最终事实校正”高于第 2～8 节中的历史检查点；历史段落保留用于追溯，不得把其中已撤回的近似创意外观或旧性能数据当成当前结论。**

---

## 0. 当前最终事实校正（2026-08-04）

- 当前任务是 **ARW 选片模块**，不是图片推荐页面；修改范围严格限于 RAW 后端/前端、WebView 直接接入、相关测试、RAW 基准、三份规格文档和验证后机械生成的前端产物。Android、LAN、普通图库、搜索、推荐、模型调用和发布矩阵均未纳入本轮范围，仍然不得改动。
- 导入只接受 Sony `ILCE-7M4` ARW；轻量 TIFF 身份读取在进入 rawpy 前拒绝其他相机、损坏文件和不支持输入。直接 TIFF IFD/SubIFD JPEG 定位用于快速缩略图/预览，无法覆盖的合法布局才回退 rawpy。
- 文件夹导入会渐进登记并继续后台缩略图；导入与导出均有任务 ID、进度、取消、错误和操作日志。源文件变化会自动失效旧缓存、重新验证相机型号并刷新派生结果。
- 前端项目卡显示最多四张封面拼图；空项目工作区只展示返回与“导入图片 / 导入文件夹”两个主入口；预览在同一图像层按缩略图 → 内嵌预览 → 最佳预览原位替换，双图两侧独立推进并保留视图变换。
- A7M4 ARW 内嵌 JPEG 没有自身 EXIF 方向时读取 TIFF 容器方向；元数据、缩略图、内嵌预览和最佳预览现在一致。`embedded` 按实际显示尺寸快速缩放，`best` 仍保留 4672×7008 完整预览；同一成员因容器 resize 重载时不会从内嵌预览倒退到缩略图。
- 创意外观安全结论已改变：仅 `拍摄时（As Shot）`可用。旧 ST/PT/NT/VV/VV2/FL/IN/SH/BW/SE 近似滤镜没有可信 A7M4 逐预置参考输出校准，不符合 §7.4，已经从生产路径移除；历史数据库值会重置为 `as_shot`。取得合法、可复现、逐项校准的真实渲染路径前，这是明确阻断，不是待美化功能。
- 最终验证证据：Python RAW/WebView 相关测试 **120 passed + 63 subtests**；完整前端 **41 files / 303 tests passed**；TypeScript typecheck、production build、生产 RAW Python Ruff 和基准自检均通过。项目页、空状态、工作区、双图对比、窄窗及 light/dark 已通过真实浏览器或 Windows WebView 视觉检查。
- 最新非正式只读基准在目标 Python 3.12.13 / NumPy 2.3.5 锁定闭包运行，输入为 `D:/105MSDCF` 的 384 项（192 ARW + 192 JPG，无 PNG）：冷首 24 张 P95 **525.142ms**、冷首 100 张 **2067.703ms**、冷全 384 张 **7230.719ms**、冷内嵌预览 **98.971ms**、重启热首 24 张 **43.538ms**、热预览 **2.982ms**、重启预览 **2.827ms**、缓存双图 **3.764ms**。报告为 `%TEMP%/raw-selection-py312-final.json`，SHA-256 `D6CCF275134F1217DA09F81CA897ABE62B0510AFAEC8534F991B02C6D2E6B30E`。当前可测硬门禁通过，但缺少 ≥300 ARW、1000 项、真实 PNG 和完整 RAW 模式清单，因此 `formal_run.status=not_formal`，不能宣称正式验收。
- 日常开发 `.venv` 是 Python 3.14.5 / NumPy 2.5.1；发布目标闭包是 Python 3.12 / NumPy 2.3.5。一次性 Python 3.12.13 环境已从 `requirements-lock.txt` 安装并验证 Pillow 12.3.0、rawpy 0.27.0、zvec 0.5.1 与 NumPy 2.3.5 全部一致，报告 `matches_target_closure=true`；临时环境已移入回收站。正式打包仍必须使用同一闭包并保留 rawpy/LibRaw 许可证。

## 1. 项目与需求文档位置

| 文件 | 说明 |
|------|------|
| `docs/raw-image-support-requirements.md` | **权威需求文档**（15 节正文 + 第 16 节开发进度/文件范围/已知限制） |
| `docs/spec.md` | 项目主规格（已同步 ARW 选片模块章节） |
| `AGENTS.md` | 项目规则（Spec 索引、发布矩阵） |

**需求核心**：在 Zvec-Webview Windows 源码版新增独立「ARW 选片」模块——项目化导入 Sony A7M4 ARW + JPG/PNG、快速浏览、双图对比、星级/色标筛选、统一导出、批量永久删除。不参与现有图库索引/搜索/推荐/模型调用；不改 Android、LAN、发布矩阵。

## 2. 已提交的工作（按 commit 顺序）

| Commit | 内容 |
|--------|------|
| `57f4210` | 需求文档基线 |
| `61b6b88` | **Phase 1-5**：后端 DB/导入/解码/服务编排 + WebView 路由 + 前端项目页/工作区 + 导出/删除 |
| `34da242` | **Phase 6-7**：磁盘缓存、胶片栏虚拟化、双图对比、永久删除确认模态、Sony 创意外观、rawpy 修复与锁定、`do_PATCH` 修复 |
| `0f6960c` | 后端测试套件（18 个测试）+ 永久删除引用清理 bug 修复 |
| `6f2833a` | **§8 调度器**：有界线程池 + single-flight + generation token（死锁修复） |

## 3. 当前执行到哪一步

- **已完成**：需求 §1–§12 的全部功能阶段、§8.2/8.3（有界并发 + single-flight）、§8.4 磁盘缓存、§11 删除安全、§14.1/14.2 后端测试。
- **浏览器端到端验证通过**：项目页创建/导入/进入工作区、384 张真实 A7M4 ARW 缩略图/预览渲染、创意外观 11 预置选择器（JPG 禁用）、虚拟化胶片栏。
- **尚未完成**：
  1. §8.1 前端侧「任务优先级调度 + 视口预取」的**前端集成**（后端 single-flight 已就绪，前端胶片栏为按需加载）
  2. §8.2 **并发矩阵基准**（8/12/16/24 等档位实测吞吐/内存，选择生产档位并记录）
  3. §13 **正式性能门禁**（20 组冷缓存首 24 张 P50/P95 ≤1s、前 100 张 ≤3s、1000 张 ≤20s、已缓存重启 ≤200ms 等）
  4. §14.3–14.6 测试（创意外观校准、UI/vitest、导出/删除边界）
  5. **创意外观需真实参考图校准**：当前 ST/PT/NT/VV/VV2/FL/IN/SH/BW/SE 为**初始参数近似**（`creative_look.py` 中 `_LOOK_PARAMS`），诚实标注"非 Sony 像素级复刻"；有相机参考 JPEG 后用 `calibration_note` 流程记录色差

## 4. 文件结构

```
image_vector_service/raw_selection/     # 后端（独立 SQLite，不触图库状态库）
  db.py            # 项目/资产/成员/评级/工作区状态/两阶段操作日志
  importer.py      # 递归扫描（跳过 reparse/symlink/._ 资源文件），批量事务，幂等
  decoder.py       # 三级渐进解码（内嵌JPEG缩略图/内嵌预览/完整解码），rawpy 可选
  creative_look.py # A7M4 创意外观（as_shot=内嵌JPEG；其他=完整解码+校准变换）
  cache.py         # 派生缓存（原子写入，key=path+size+mtime_ns+解码版本+外观版本）
  scheduler.py     # 有界线程池（ARW内嵌12/JPG12/PNG6/ARW完整3）+ single-flight + generation
  service.py       # 高层编排（项目CRUD/导入/评级/筛选/导出/永久删除/图像生成）
frontend/src/features/raw-selection/    # 前端
  RawSelectionPage.vue      # 项目列表（新建/重命名/删除/清缓存/导入文件夹）
  RawSelectionWorkspace.vue # 工作区（中央预览+虚拟化胶片栏+星级色标+筛选排序+外观+对比+删除模态）
  api.ts / types.ts / index.ts
zvec_webview/server.py       # 集成点：api/raw-selection/* 路由（含 do_PATCH 修复）
tests/test_raw_selection.py # 22 个测试（unittest 风格）
requirements.txt / requirements-lock.txt  # 已锁定 rawpy==0.27.0
```

**数据位置**：`%LOCALAPPDATA%/zvec-image-search/raw-selection/`（projects.sqlite3 + cache/）。

## 5. 验证状态

- 后端：`python -m unittest tests.test_raw_selection` → **22 个测试全过**（项目CRUD/导入幂等与跳过/评级/筛选/两阶段删除/缓存回环与失效/外观BW单色/解码/调度器4项）
- 前端：`vue-tsc --noEmit` 通过；`vite build` 通过（产物已入 `zvec_webview/frontend_dist/`）
- Python lint：`ruff check image_vector_service/raw_selection/ tests/test_raw_selection.py zvec_webview/server.py` 全过
- 真实 ARW（D:/105MSDCF，384 张）：缩略图 546ms / 内嵌预览 305ms / 完整解码 1498ms；磁盘缓存命中 0.5ms；VV/BW/SE 外观完整解码+变换 3s 级
- 浏览器端到端：已截图确认（build/verify-*.png）

## 6. 环境与已知坑（重要）

1. **主 venv 是 Python 3.14**（`.venv`），rawpy 0.27.0 才有 cp314 wheel；`requirements-lock.txt` 的 numpy 锁（2.3.5, py>=3.11）与 3.14 实际装到的 2.5.1 不一致——打包走 `.venv-build`（Python 3.12）时应一致。CI 构建环境需确认。
2. **启动源码版**：`.venv/Scripts/zvec-webview-preview.exe`；首次启动若报 `No module named 'webview'` 需 `uv pip install --python .venv/Scripts/python.exe pywebview`。gateway URL 写在 `%LOCALAPPDATA%/zvec-image-search/gateway-url.txt`（每次启动 token 变化）。
3. **浏览器验证**：文件夹选择依赖原生对话框，浏览器自动化无法操作 → 用 curl 直接调 API（`POST api/raw-selection/projects/{id}/import-folder`）再刷新页面。
4. **杀进程**：沙箱内 `taskkill` 不可用（报 CreateProcessAsUserW 267），用 `build/kill_zvec.py`（ctypes TerminateProcess）或 build/kill_webview2.py；该目录已被 .gitignore。
5. **single-flight 死锁教训**：`add_done_callback` 不能在持有锁时调用（future 同步完成时回调立即执行导致重入死锁）；已在 scheduler.py 修复并留注释。
6. **项目卡片"导入图片"入口**：§3.2 要求空项目页提供"导入图片"+"导入文件夹"两个入口，当前只有"导入文件夹"（文件级导入 API 已存在 `import_files`，前端入口未做）——接续时按需补。
7. **永久删除**：只删除 DB 中登记的精确文件路径，二次校验安全；`already_missing` 自动清理引用；两阶段日志 `operation_log` 支持崩溃恢复（`recover_pending_deletes` 启动时调用）。

## 7. 接续建议（按优先级）

1. §8.1 前端视口预取/优先级集成（可复用后端 single-flight，胶片栏预取可见区缩略图）
2. §13 性能门禁基准脚本（用 D:/105MSDCF 384 张，冷缓存 20 组 P50/P95）
3. §14.3–14.6 补充测试（含 vitest 前端测试，注意 `@vue/test-utils` 必须 2.2.7）
4. 创意外观真实参考图校准（需要 Sony 官方软件或相机输出的参考 JPEG，客观 ΔE + 人工检查，结果写回 `docs/raw-image-support-requirements.md` 第 16 节）

## 8. 当前实施检查点（2026-08-04 02:12）

### 8.1 仓库与范围

- 当前分支/HEAD：`codex/collection` / `dfe2d75`；工作树包含本轮 RAW 模块在途修改，未提交、未 push，禁止 reset/clean 或覆盖共享改动。
- 允许范围仅为 RAW 前端模块、RAW 后端包、直接 App/原生桥接/WebView 路由与测试、单一 RAW 基准工具、三份 RAW/Spec 文档，以及通过验证后机械生成的 `frontend_dist`。
- Android、LAN、现有图库/搜索/推荐事实、模型调用和发布矩阵均不得修改。
- 文档由主代理独占写入；子代理只回传事实，避免共享文档冲突。

### 8.2 已完成并有实际证据的步骤

- 原生多文件选择已接通：一次多选 ARW/JPG/JPEG/PNG，绝对文件路径校验、顺序保留、去重、取消返回空成功；NativeBridge/WebView 定向 `unittest` 共 40 项通过。
- RAW-only 全高 shell、底部工作区、筛选/排序、单/双图、胶片虚拟化及前端预取代码已落入工作树；前端全套 Vitest 41 文件 / 294 项通过，TypeScript typecheck 通过。
- 真实最新构建已复现旧预览缺陷，而不是旧基线误判：在 1280×720 视口中，预览 stage 为约 1020×572.7，图片盒为 1020×680（自然尺寸 7028×4688），图片底部被 stage 裁掉约 107.3px。底部控制栏本身位于独立行，根因是图片 replaced-element 盒尺寸与 fit 语义错误。
- 第一轮返修已改为显式 natural-size 图像层、按容器与自然尺寸计算 fit、100%=1，并加入 ResizeObserver 与单/双图回归；该轮 25 项 RAW Vitest、294 项全前端 Vitest及 typecheck 均通过。
- 2026-08-04 02:10 完成一次中间 production build；它仅证明可构建，尚未包含随后追加的安全留白要求，也尚未在重启后的 WebView 通过视觉验收，不能视为完成。

### 8.3 当前在途

- 前端代理正在追加稳定安全留白：宽窗口每侧 24px，窄窗口 12/16px；fit 使用扣除安全区后的 available width/height，单图和对比两侧均居中且不铺满。
- 后端代理正在复核 EXIF 最终方向在元数据、缩略图和预览路径的一致性，以及筛选不改变相对顺序、同键以 `import_order` 稳定 tie-break、unknown 元数据确定分组的测试。
- RAW 同输入隔离性能矩阵由单一基准代理运行；旧基线 full 并发档 2/3/4 已完成，full=6 正在运行，随后运行 looks。不得另起重复完整矩阵争抢 CPU/I/O。
- 当前最终留白构建的源码版主窗口进程 PID 18508 正在运行；它由前台捕获命令单元启动，网关与后端子进程正常。安全停止必须使用应用的 `--exit-running-instance` 单实例退出信号，先确认无活动任务，不得强杀。

### 8.4 未验证、限制与下一步

- **部分完成**：安全留白后的浏览器真实 DOM 尺寸、用户所指全身图底边、真实 WebView 横/竖图和筛选展开已验证；方图、窄窗、对比两侧和 dark 截图仍未验证。
- **未验证**：最终完整 Python/前端测试矩阵、最终 production build、真实多文件/文件夹导入、导出状态、移出与仅临时副本永久删除冒烟。
- **在途**：冷缓存/热重启/预览/双图/创意外观正式性能门禁；所有最终数值必须注明数据集、冷热口径、输入摘要与证据 JSON，不能使用缓存重放冒充冷路径。
- 创意外观事实保持诚实：`as_shot` 使用内嵌 JPEG，其余预置为版本化初始近似；当前没有可信相机参考 JPEG，未完成真实样片校准，不要求 Sony 官方 SDK，不能声称原厂或像素级复刻。
- 下一条操作：前端安全留白代码完成后重新运行全前端测试/build；通过单实例信号安全重启源码版；在真实 WebView 记录 stage/img/control/filmstrip rect，并保存全身图与横图、宽/窄、light/dark 证据。只有四边均在安全区、底栏不覆盖后，才可标记首个 UI 里程碑完成。

### 8.5 UI 首个里程碑完成（2026-08-04 03:47）

- 最终安全留白 production build 已成功，旧源码版通过应用自身单实例退出信号正常关闭，随后启动最新源码版；没有强杀活动任务。
- 在与源码版相同网关和 1280×720 视口的真实浏览器 DOM 中，用户所指全身竖图的 stage 为约 1020×572.7px，方向归一后的自然尺寸为 4672×7008，fit 后图片为 350×525px；四边距约为左/右 335px、上 23.9px、下 23.8px，符合宽窗 24px 安全区的像素取整误差。
- 同一 DOM 中底部控制栏从 y≈572.7 开始、高≈47.3px；胶片栏从 y=620 开始、高=100px。两者均为独立 grid area，未覆盖预览。
- 最新 Windows WebView 已用真实只读项目复测：用户截图对应的全身图脚部和图片底边完整可见，图片居中且不铺满；筛选栏展开后仍完整 fit；另有真实横图和另一张竖图通过。截图证据位于 `%TEMP%/raw-selection-final-evidence/`，关键文件为 `raw-webview-user-target-fit-light.png`、`raw-webview-user-target-index262-filter-open-light.png`、`raw-webview-before-navigation.png`。
- 该里程碑只代表宽窗口 light 下的预览布局/fit 缺陷已关闭；方图、窄窗、dark、对比双侧、菜单收起与最终完整回归仍按“未验证”处理。
- 下一条操作更新为：完成窄窗/dark/对比和菜单视觉交互证据；接收同输入性能矩阵；跑最终 Python/前端/lint/type/build；更新正式需求与 Spec，再做完整 diff 审查。

### 8.6 性能矩阵检查点（2026-08-04 03:49）

- 修正后的最终同一工具旧基线全套已完成并原子落盘：`%TEMP%/raw-selection-dfe2d75-final-tool-all.json`，SHA-256 `62FB341A46A9457328BCFF77144178F632E3561EA3EE219094D5B9BD687791F3`。
- 旧基线覆盖冷首 24/100/全 384 项各 20 组、hot24、preview、ARW embed 8/12/16/24、JPG 8/12/16、ARW full 2/3/4/6，以及 10 种 look 各 20 组；错误数 0、`failed_scenarios` 为空、源文件前后不变、临时目录清理成功。
- 当前代码同工具已完成冷 24/100/全项目、hot、preview、embed 全档和 JPG 全档；正在 ARW full=2，之后剩 full=3/4/6 与 looks。按旧基线对应阶段实耗，预计还需 30–38 分钟。
- 当前结果文件尚未落盘是预期行为：工具只在全套完成后原子写入 `%TEMP%/raw-selection-current-final-tool-all.json`。不得中断或另起重复矩阵；若平台中断，先确认该进程和场景目录，再决定等待或从工具检查点恢复。
- 数据集仍只有 384 项且没有 PNG，不能冒充需求中的正式 1000 项/300 ARW/PNG 覆盖；最终门禁必须将这些标为数据覆盖限制。

### 8.7 性能矩阵完成（2026-08-04 04:00）

- 当前工作树同工具全套已完成并原子落盘：`%TEMP%/raw-selection-current-final-tool-all.json`，SHA-256 `5588E206B1779D387CC16457EACA90C4433E61AC59B5A1EBD632DDBD3A1008F8`；生产候选文件合成指纹为 `dcfae2a1c0766ec6aaebafe4c94e9e200d7cb29b9d1729d3ad1b1820d577eb4b`。基准工具 SHA-256 为 `399fe6f597d03be3c10781d304da38173defc0ae468c1a9ad711bf99a15a32c0`。
- 两次运行均使用同一只读 384 项/约 16.97GB 输入（192 ARW + 192 JPG），输入摘要 `1486efb56b5194cf31c471652ee5331659d088448702c2d80ecb1f8ffdd56692`；20 组冷/热门禁、矩阵每档 3 轮、10 种 creative look 各 20 组。两次均 `source_unchanged=true`、`failed_scenarios=[]`、错误 0、活动 future/临时目录残留 0。

| P95 场景 | `dfe2d75` | 当前工作树 | 变化 | 门禁 |
|---|---:|---:|---:|---|
| 冷首 24 | 1418.972ms | 1382.813ms | -2.55% | **未达** ≤1000ms |
| 冷首 100 | 5178.778ms | 4889.705ms | -5.58% | **未达** ≤3000ms |
| 冷全 384 | 19306.427ms | 17440.969ms | -9.66% | 仅 384 项，不能代替 1000 项正式门禁 |
| 导入登记 24 | 22.441ms | 18.962ms | -15.50% | 通过当前口径 |
| 导入登记 100 | 99.108ms | 69.279ms | -30.10% | 通过当前口径 |
| 导入登记 384 | 384.029ms | 257.855ms | -32.86% | 通过当前口径 |
| 重启热首 24 | 10.919ms | 38.990ms | +257.08% | 绝对值仍通过 <200ms |
| 冷 embedded preview | 396.433ms | 394.886ms | -0.39% | **未达** ≤300ms |
| 热 preview | 4.033ms | 4.755ms | +17.90% | 通过 <100ms |
| 重启 preview | 3.353ms | 4.707ms | +40.38% | 通过 <100ms |
| 缓存双图 | 4.235ms | 7.364ms | +73.88% | 通过 <200ms |

- 当前 ARW thumbnail 矩阵（P95 / 吞吐 / CPU 平均 / RSS 峰值）：8=`16676.579ms / 12.121/s / 694.128% / 2081.031MiB`；12=`13803.897 / 14.169/s / 958.155% / 2971.980MiB`；16=`13053.353 / 15.067/s / 1124.981% / 3809.363MiB`；24=`12134.499 / 16.143/s / 1217.015% / 4058.734MiB`。24 相比 16 吞吐只增 7.14%，RSS 约 4.06GiB且取消收尾增至约 1.94s；没有 UI 争用证据，不提高生产值。
- 当前 JPG thumbnail 矩阵：8=`7227.390ms / 28.501/s / 698.595% / 154.352MiB`；12=`5405.652 / 37.443/s / 987.074% / 188.602MiB`；16=`4800.109 / 42.153/s / 1159.824% / 213.836MiB`；错误 0。PNG 4/6/8 均因没有真实 PNG 标记 `not_formal`。
- 当前 ARW full 矩阵：2=`176259.526ms / 1.144/s / 954.455% / 1134.141MiB`；3=`131125.817 / 1.490/s / 1164.456% / 1639.410MiB`；4=`116062.794 / 1.678/s / 1274.972% / 2038.082MiB`；6=`105419.241 / 1.832/s / 1359.412% / 2712.316MiB`。6 相比 4 吞吐只增 9.18%，额外约 674MiB RSS且取消收尾增加约 780ms；没有 UI 公平性证据，不提高生产值。
- 正式性限制必须保留：只有 192 个真实 ARW（少于 300）、没有真实 PNG、项目只有 384 项（少于 1000）、没有 A7M4 RAW 模式覆盖清单；Windows 文件系统缓存未强制清空；基准不含 HTTP/DOM/paint/native picker；CPU 100%=一个逻辑核，I/O 是 Windows 进程计数而非物理盘吞吐。
- creative look 矩阵错误 0，但只测独立 decode/transform/encode/cache，不测 production service 的 LRU base reuse；也没有可信逐预置相机参考 JPEG，不能声称校准完成。

### 8.8 工具中断检查点（2026-08-04 04:08）

- 最后成功步骤：宽窗口 light、真实 WebView 980×680、浏览器 720×680、双图两侧 fit、安全留白、100% 与 fit 差异、对比 Esc 恢复，以及菜单外部点击/Escape/resize 收起均已取得实际证据。
- light 源码版已通过应用自身 `--exit-running-instance` 信号正常退出，返回码 0，原主窗口 PID 18508 已确认不存在；没有活动任务被强杀。
- 启动 light 实例的前台 shell 监督单元在应用退出后仍到达自身 124 秒超时并返回工具错误；这是监督命令超时，不是应用崩溃，工作树、最终基准 JSON和截图均未丢失。
- 当前没有运行中的源码版，也没有运行中的 RAW 基准进程。共享工作树中的 RAW 前后端、测试、基准、文档和机械 build 改动不得覆盖或 reset。
- 安全恢复的下一条操作：以仅当前进程的 WebView2 暗色参数启动源码版，完成 dark 宽/窄截图；随后再次通过单实例信号退出 dark 实例，并用正常环境启动最终源码版供用户检查。

### 8.9 最终收尾检查点（2026-08-04 09:18）

- 本节取代 8.6～8.8 中“仍在执行 / 未达 / 未验证”的临时状态；保留旧段落只用于追溯。
- 视觉复验发现并修复两项真实问题：同一图片在容器 resize 时可能从内嵌预览倒退到缩略图；ARW 内嵌 JPEG 没有自身 EXIF 方向时曾横置显示。前端现在保持较高阶段，后端使用 A7M4 TIFF 容器方向，并以 `decode-v3` 和 ARW 元数据版本使历史结果重新生成。
- 真实 `D:/105MSDCF/PHS08256.ARW` 的容器方向为 8；元数据、内嵌预览与最佳预览均为竖图。1600×1000 显示区域的快速内嵌结果为 833×1250，三次直接解码约 78～98ms；最佳预览仍为 4672×7008。
- 最终非正式基准报告为 `%TEMP%/raw-selection-py312-final.json`，SHA-256 `D6CCF275134F1217DA09F81CA897ABE62B0510AFAEC8534F991B02C6D2E6B30E`；冷内嵌预览由历史完整图误测路径修正为 98.971ms，300ms 门禁通过。报告退出码 0、`failed_scenarios=[]`、`source_unchanged=true`、`matches_target_closure=true`，仅因样本覆盖不足仍为 `not_formal`。
- 最终回归：Python `120 passed, 63 subtests passed`；前端 `41 files, 303 tests passed`；Ruff format/check、TypeScript typecheck、production build 与基准自检通过。最终构建产物为 `index-BIChQNRN.js` 和 `index-Bi_V06CG.css`。
- 最终视觉证据位于 `%TEMP%/raw-selection-final-evidence/`：`raw-webview-arw-light-final.png`、`raw-webview-arw-dark-final.png`、`raw-webview-workspace-dark-final.png`。另在 1280×720 与 720×680 的真实浏览器 DOM 中检查项目封面、空状态、窄工作区、对比模式、禁用外观选择器和菜单收起行为。

### 8.10 ARW 预览闪烁缺陷修复（2026-08-04 10:04）

- 真实 production 页面复现到同一 ARW 在 10 秒内发生 54 次状态切换：`embedded` 1074×716 与 `best` 7008×4672 互相覆盖，显示框在约 121×80 与 788×525 间跳变。
- 根因是图片 `load` 回调每次都写入数值相同的新尺寸对象，误触发渐进预览 watcher；新一轮流程又无条件把已显示的 `best` 降回 `embedded`，形成自激循环。
- 修复后容器尺寸只在宽高数值实际变化时更新；同成员 `best` 不再回退；替换 `src` 前使用预加载图片自然尺寸预先计算 fit，并移除跨自然分辨率的 transform 插值。
- 高频真实页面复验以 20ms 采样切换另一张竖幅 ARW，只出现 `embedded` → `best` 两个单向状态，两者显示框始终为 350×525；随后持续采样不再出现任何回退。
- 前端完整回归更新为 41 文件 / 304 项；最终构建产物更新为 `index-C481BcIg.js` 和 `index-8j2k_u6s.css`。
