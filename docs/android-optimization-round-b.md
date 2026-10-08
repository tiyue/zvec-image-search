# 安卓 B 阶段：自适应浏览与大图交互验收

- 日期：2026-10-08；实现基线为 A 阶段提交 `4a32239`。
- 按 [安卓优化方案](android-optimization-plan.md) 实施 B1–B4，功能、构建和原生验收已完成。生命周期收集及传输归属在 A 阶段已实现，B 保留并回归。
- DeepSeek 使用既有 `deepseek-flash/xhigh` 配置实施；协调者独立审查、复现、纠正、验收和提交。本阶段没有依赖升级、后端接口扩展或正式发布。

## 1. 结果与范围

生产修改限定在 `android/app/src/main/java/com/zvec/lanviewer/ui/`：

| 文件 | 实际行为 |
| --- | --- |
| `GridDensity.kt`、`search/SearchResults.kt` | 搜索与推荐共用紧凑/标准/大图三档本机密度；自适应列数、8dp 间距、固定图片区域、Fit 完整显示和一行文件名。加载/失败覆盖层不改变图片区域高度 |
| `AppUiState.kt`、`SearchSessionState.kt` | 结果标题使用提交时查询快照，编辑下一次草稿不会改写已完成结果的标题；不增加进程恢复字段 |
| `ZvecApp.kt`、`search/QueryImageControl.kt` | 搜索和二级设置可滚动并避让键盘；提交收起键盘；输入/附图入口 48dp，附图提供 48dp 预览、替换、移除和原有上传状态 |
| `ZvecApp.kt`、`viewer/ViewerImageGeometry.kt` | 大图顶部返回/位置、底部保存/分享/更多；单击显隐、长按同一菜单、详情按需展开。按真实图片尺寸计算 Fit、旋转和平移边界，未知尺寸保持 1× 居中 |
| `ZvecTheme.kt` | 完整灰阶 Material3 深浅主题，跟随系统；导航采用现有语义图标 |

缩略图请求使用稳定 URL 内存缓存键；大图优先直接复用已加载缩略图，未命中时使用同一路由作为占位，原图加载失败仍可重试/退出。没有独立原图预取任务；HorizontalPager 可组合当前与相邻页，受控测试确认原图请求不超过当前及相邻两页，不能表述为永远只请求一个 URL。保存/分享继续获取完整原始文件字节。

保存和分享放在可见工具栏，更多/长按菜单保留旋转、详情和推荐反馈；菜单支持外部点击和 Back 收起。传输期间保存/分享禁用，进度在工具栏上方，取消仍可达。

测试范围：新增 `android/app/src/androidTest/java/com/zvec/lanviewer/ui/BrowseUiTest.kt`、`android/app/src/test/java/com/zvec/lanviewer/ui/ViewerImageGeometryTest.kt`，修改 `RecommendationUiTest.kt` 和 `SearchSessionStateTest.kt`。另仅整理 A 阶段自己新增的两处 Python 测试断言换行，涉及 `tests/test_lan_http_api.py`、`tests/test_webview_lan_access.py`，没有行为变化。

文档范围为本文件、优化方案、A 阶段记录、`docs/spec.md`、`AGENTS.md`。不得修改无关部分；Windows、Wear OS、数据库、配对、搜索排序/top_k/100 项分页、推荐 15 张/首 4 张/shown/一批预热、HTTP=10/render=4、版本号和发布矩阵均保持，保留用户 `.qoder/better-harness/`。

## 2. DeepSeek 与独立纠正

执行任务与复核反馈分别保存在证据目录的 `task-b-ui.md`、`task-b-review-fixes.md`。运行目录为 `D:\CodexTools\deepseek-worker\work\runs\`：

- `2026-10-08T13-12-06-601Z-3632`：B 界面、密度、图片几何和原生测试。
- `2026-10-08T13-54-44-476Z-21544`：查询快照、旋转布局、真实 PNG HTTP 和软键盘回归。

两份 `report.json` 均为 `completed`、`scope_violations=[]`、`runtime_cleanup=removed`；协调者已读报告、`last-message.md` 和实际差异。执行器未自行运行构建或模拟器，以下结果来自协调者。

实际复现并纠正：缺少测试 import；宽图旋转时父约束把 640px 目标高度压到 320px；结果标题随未提交草稿改变；大字体占位文字换行裁切；Pager 相邻页使单一标签选择器失效；已加载缩略图进入查看器仍多发一次 HTTP；放大后双击无法复位；原图尺寸未知仍可放大占位图。保留实际网络、像素、手势和键盘断言，没有用删除断言代替修复。

删去本阶段自己新增的未使用列数计算及镜像测试，以真实 LazyGrid 布局检查列数。新查询标题快照只在内存中存在，SavedStateHandle 仍为 A 阶段六类小字段。

## 3. 实际验证

证据目录：`D:\CodexTools\deepseek-worker\work\zvec-android-a-20261008\`。候选和验证使用独立 D 盘 Git 工作副本；通过后合入 `D:\code\zvec`。

| 检查 | 实际结果与证据 |
| --- | --- |
| 最终构建与 JVM | `testDebugUnitTest`、`lintDebug`、`assembleDebug`、`assembleDebugAndroidTest` 成功；JVM 105 项，失败/错误 0；`b-final-build.log` |
| 最终原生 | Browse 14 + Search 15 + Recommendation 10 + ViewModel 8，共 47 项通过，111.287 秒；`b-final-native.log` |
| 列数与状态 | 实际 LazyGrid：320dp 标准 2 列、360dp 紧凑 3 列、600dp 标准 3 列、840dp 标准 5 列；密度切换保持图片锚点、本机持久化、不重新查询 |
| 大图与真实 HTTP | MockWebServer 提供真实 800×200 PNG：列表不取原图；500 后显式重试，阻塞期间真实渲染红色缩略图，释放后显示绿色原图；已加载缩略图不重复请求。另验证 90° 旋转后 160×640 Fit、1× 翻页、放大不翻页和双击复位 |
| 真实软键盘 | 使用设备实际 density、360×640dp 和 1.5 字体：输入聚焦后 IME 显示，输入更新状态，搜索按钮可点击并收起键盘；设置输入再次显示 IME，“完成”仍可滚动到达 |
| 原生视觉 | `b-final-ui-screenshots/` 的 10 张 B 截图已逐张检查：320/360/412 手机、600/840 窗口、深浅主题、1.3/1.5 字体、宽图旋转；另复查深浅传输面板。截图使用测试数据，真实图片 Fit 另由 PNG 几何/像素断言验证 |
| 正式入口冒烟 | 实际 `am start -W` 启动生产 `MainActivity`/Factory/ViewModel，`Status: ok`、进程存活、crash buffer 无异常；`main-activity-startup.png`、`b-main-activity-crash.log`。停留连接页，不称为真实图库端到端 |
| LAN 回归 | A 阶段实际运行 62 项 unittest 通过，`lan-contract-tests.log`；B 不改后端或 LAN 契约，只有两处测试断言排版 |

构建采用单 worker、串行、JVM 1024 MiB、Kotlin in-process；构建退出后再运行已有 `zvec_visual_api34`，API 34、单实例只读、1536 MiB、1 核、软件 GPU，无快照/音频。键盘设置仅作用于只读模拟器临时会话；验收后关闭本次实例。

Lint 为 0 errors / 6 warnings，含既有 `ObsoleteLintCustomCheck`，部分 Compose/Lifecycle 自定义规则被跳过；没有以升级依赖掩盖限制。两个 Python 测试文件 Ruff lint 通过；`test_lan_http_api.py` 的 format 检查仍提示既有第 166 行格式，不清理无关代码。`git diff --check` 通过。

## 4. 交付与未覆盖项

可安装测试包：证据目录 `apk/YaoLens-android-ab-debug.apk`；是源码构建的 debug APK，未改版本号、未作为 Release 发布。最终哈希记录在同目录 `YaoLens-android-ab-debug.sha256`。

A/B 功能验收完成，正式性能门禁仍未完成：没有真机首屏 P95、代表性图库流量组、大文件 SAF、长期滚动掉帧或 Windows/Android 公平性认证。A 的单张合成 JPEG 字节结果只能证明路由用途，不能代替这些指标。窗口截图也不能替代真机旋转/厂商键盘兼容性验收。

C 统一图库和目录浏览 API、原始字节缓存治理仍按原方案独立确认，不包含在本阶段完成范围。A/B 可分别按提交回退；不修改用户图片、数据库或已完成 Windows 优化。
