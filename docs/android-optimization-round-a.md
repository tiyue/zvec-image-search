# 安卓 A 阶段：图片交付与状态验收

- 日期：2026-10-08；实现基线 `6a458e6`。
- 用户授权按 [安卓优化方案](android-optimization-plan.md) 开发，使用已有模拟器、单实例低资源运行；本阶段已完成代码、构建和独立原生验收，B 阶段继续实施。
- DeepSeek 负责限定范围实现；协调者负责实际差异、编译、测试、截图、纠正和提交。未推送或发布，未改 Windows、Wear OS、数据库、推荐算法和发布矩阵。

## 1. 实现范围

源码只涉及 `android/app/build.gradle.kts`，以及 `android/app/src/main/java/com/zvec/lanviewer/` 下：

- `data/network/LanApiClient.kt`：缩略图 URL；原图 URL 保持。
- `data/repository/{ZvecRepository,OriginalFileCopy}.kt`：原始字节流式复制，由 Repository 负责 IO 调度与取消检查。
- `ui/{AppUiState,AppViewModel,SearchSessionState,SearchNavigation,ZvecApp}.kt`：上传/查询/传输归属、分页重试、草稿恢复、一次性导航、固定保存目标和可见消息。
- `ui/search/{SearchResults,QueryImageControl}.kt`：搜索缩略图及实际状态页面接线。

新增或修改对应 JVM/原生测试；Python 仅增补 `tests/test_webview_lan_access.py` 和 `tests/test_lan_http_api.py` 的搜索来源缩略图契约测试，没有改后端生产实现。依赖只新增同系列 `lifecycle-runtime-compose:2.8.3` 和原生测试的既有版本 `mockwebserver:4.12.0`。

文档范围为本文件、优化方案、`docs/spec.md` 与 `AGENTS.md` 索引。不得修改无关部分，保留用户 `.qoder/better-harness/`。

## 2. DeepSeek 分工与协调者纠正

公共运行目录：`D:\CodexTools\deepseek-worker\work\runs\`。五份 `report.json` 均为 `completed`、`scope_violations=[]`、`runtime_cleanup=removed`；均独立阅读 `last-message.md` 和实际差异。

| 任务 | 运行 ID |
| --- | --- |
| 数据层与 IO | `2026-10-08T12-06-57-779Z-1524` |
| 状态与恢复 | `2026-10-08T12-10-28-241Z-5356` |
| UI 与原生界面测试 | `2026-10-08T12-23-29-145Z-15396` |
| 搜索缩略图契约 | `2026-10-08T12-38-41-973Z-8088` |
| 真实 ViewModel 调用链原生测试 | `2026-10-08T12-45-02-462Z-22964` |

执行器只做静态核对，不能代替编译/设备验收。协调者发现并修正：文件复制调用的 trailing lambda 编译错误；协程调试线程名导致的脆弱断言；上传 URI 类型读取的异常归属；草稿附图恢复提示；同页重复提交；保存选择器不能伪造丢失的推荐元数据；导航重复消费旧完成信号；搜索页首屏错误缺少可见重试；浅色主题大图进度条黑底对比度不足。

原生复测还复现了图片仍加载时使用图片描述点击的测试失败，改用始终可见的文件名节点。新增调用链测试错误使用 `SharedFlow.subscriptionCount` 导致编译失败，改成真实 `onSubscription` 回调及有界等待；最终编译与全部用例通过，没有只删除失败断言。

## 3. 实际验证

证据目录：`D:\CodexTools\deepseek-worker\work\zvec-android-a-20261008\`；候选与验收分别在独立 D 盘 Git 工作副本运行，用户主目录源码在通过后才合入。

| 检查 | 实际结果与证据 |
| --- | --- |
| 基线构建与推荐原生回归 | 现有推荐 10 项通过，`baseline-build.log`、`baseline-ui.log` |
| 最终 JVM | 91 项，失败/错误均 0；`a-final-revised-build.log` 与验证副本 JUnit XML |
| 最终原生 | 搜索 15 + 推荐 10 + ViewModel 调用链 8，共 33 项通过，69.605 秒；`a-final-native.log` |
| LAN 契约 | `python -m unittest tests.test_webview_lan_access tests.test_lan_http_api`，62 项通过；`lan-contract-tests.log` |
| 构建/静态检查 | `testDebugUnitTest`、`lintDebug`、`assembleDebug`、`assembleDebugAndroidTest` 成功；单 worker、串行、JVM 1024 MiB，Kotlin in-process；`git diff --check` 通过 |
| 原生视觉 | `a-ui-screenshots/` 的 320dp 深浅主题：零结果、上传失败、下一页失败、查看器传输八张截图；已逐张人工检查，修正后复查浅色进度条 |
| 正式 Activity | `am start` 实际启动 `MainActivity`，创建生产 Factory/ViewModel，无 crash buffer 异常；截图 `main-activity-startup.png`。只验证启动入口，不称为真实图库连接/真机端到端 |

真实调用链测试使用模拟器本机 MockWebServer、生产 LAN 客户端/Repository/ViewModel、实际 `file://` 查询图及目标流。验证首页 100 项、续页失败与重试、重复页门禁、取消/迟到 A 响应、失败附图不降级、固定目标且关闭大图继续保存、文件哈希、重复保存/分享不打断、取消不计成功、六类小草稿恢复且不自动查询。偏好、缓存和文件在测试专属上下文隔离；不读取用户图库。

模拟器复用 `zvec_visual_api34`，API 34，单实例只读、1536 MiB、1 核、软件 GPU、无快照、无音频；构建时停止模拟器，原生验收在构建退出后启动。资源抽查构建期间可用物理内存约 15.7 GiB。本阶段未进行压力测试。

Lint 无错误，但现有 AGP/Compose 自定义检查出现 `ObsoleteLintCustomCheck` 警告，部分规则未执行；没有夹带依赖升级。Python 现有环境没有 pytest，实际使用 unittest 运行同一测试模块；不把未运行的 pytest 写成通过。

## 4. 有控流量检查及限制

`measure-thumbnail-bytes.py` 使用真实 LAN Server、PreviewLanAdapter 和搜索媒体绑定，对单张合成 2400×1600 JPEG 分别请求 thumbnail/original，HTTP=10、render=4 保持。

- 原图 3,336,288 bytes；640×427 缩略图 69,067 bytes，减少 97.930%。
- 原图 SHA-256：`ad111a4a411d7561f8ed811254e42a27f3966f03752bb71c68d40316872e8e9e`。
- 完整结果：`thumbnail-byte-measurement.json`。

这只是单个合成样本，证明正式路由和交付字节用途；没有完成普通图库代表样本、真机首屏 P95、掉帧、两端公平性或大文件 SAF 保存测试。不能宣称已达到方案的正式性能门禁或所有设备表现。

## 5. 回退与下一阶段

A 可按独立提交回退客户端和对应测试；不回退用户图片、数据库或 Windows 优化。B 接着实施密度、完整主题、键盘/附图、大图工具栏和 Fit/旋转边界；保留 A 归属、目标、分页及推荐回归。C 需先确认原型和目录 API，独立于 A/B。
