# 远程访问、双模式与按需图片压缩实施

- 日期：2026-10-09；基线：`e3e55a2`，安卓 A/B 已完成。
- 状态：已完成实施与下述源码、真实 ECS 及模拟器范围的独立验收；提供 debug APK，尚未构建完整 Windows 发布包。
- C 阶段统一图库继续搁置。本轮不增加目录 API、数据库迁移、标签写入或图片编辑。
- 分工：协调者明确任务、接口、独立副本与精确文件范围；DeepSeek `deepseek-flash/xhigh` 实施；协调者读报告和实际差异，独立构建、测试、截图、纠正、提交。不得再次启动模型或执行器。

## 1. 已核对的输入与取舍

现用 `D:\frp_0.61.1_windows_amd64\frpc.toml`：FRPC 0.61.1，FRPS `39.105.48.52:7000`，一个 `zvec-api` TCP 代理，现本地地址 `192.168.10.136:38522`，公网入口 `http://39.105.48.52:38522`。ECS 配置由用户完成，本轮不修改服务器。实测带宽约 3 MB/s，不是 3 Mbps。

协调者下载官方同版 Windows amd64 ZIP，其 FRPC 与本机文件逐字节一致；来源与 SHA-256 记录在 `third_party/frp/windows-amd64/provenance.json`，保留许可证。只在 Windows 包内部附带 FRPC，不作为独立 Release；不改发布矩阵、版本号或发布行为。

现有 Android 已有首配令牌、本机保存、启动自动连接；`canRetainBearer` 仅保留同 origin 令牌，换公网地址会清除。手机推荐沿用响应中的内网绝对 URL，公网需要按当前入口生成媒体 URL。现有 thumbnail 为 640px，Windows preview 为 1920px；LAN 大图走 original，不能用现有 `preview_url` 声称已压缩。

第一版复用现有手机访问网关与当前有效 LAN 监听地址，FRPC 本地转发必须依据实际运行地址/端口，不硬编码旧 IP 或 127.0.0.1。远程开启时确保手机网关已启动；未配置有效 LAN 地址时返回可操作原因，不虚报联网。公网端口须与当前 HTTP 网关端口一致，符合现有 Host 契约与本机配置。仅支持已核对的单 TCP TOML 配置导入；不发明多代理、NSSM、系统服务、域名或新云端 API。

配置导入创建应用配置目录内独立副本，不覆盖原 FRPC 文件。FRP 凭据不进入仓库、文档、UI 状态、报告或 DeepSeek 任务；协调者本机真实联调使用原配置，执行器只使用合成配置测试。

## 2. 功能契约

### 2.1 Windows FRPC 托管

- 设置显示远程状态与操作入口；导入配置/修改复杂输入放二级模态，支持外部点击/Back 关闭，随系统深浅主题。
- 先启动手机网关，再隐藏 `shell=False` 启动随包 FRPC；单实例，工作线程处理输出/恢复，不阻塞 UI。首次登录失败保持有界重连，进程异常退出采用有上限退避；无紧密重启循环。
- 状态区分未配置、关闭、连接中、代理已注册、重连中、失败；进程存在不等于代理已注册。使用固定 0.61.1 实际日志事件核对注册/断开；只展示规范化原因，不返回原始日志/配置。
- 导入保存配置；开启时持久化，软件下次常驻启动恢复。关闭窗口隐藏不关闭隧道；完整退出关闭自己创建的子进程并等待结束，不能扫描/终止其他 FRPC。
- LAN 停止/地址配置变化与 remote 同步停止/重建，不留下指向旧网关的“已连接”。源配置、用户图片、ECS 保持。

### 2.2 两个入口、一份设备授权

App 显式 `LAN`/`REMOTE`，记住选择；两个入口关联同一已配对电脑。首次可以在任一入口配对，人工核对六位码并批准后保存既有 256-bit token。后续使用 token 的自动核对，不重复弹配对。

认证后的 `/api/v1/status` 新增可选 `connection_endpoints` 对象（`lan`/`remote` 为受支持的 HTTP IPv4 origin），以及 `media_profiles: ["remote-v1"]`。旧返回字段保留。电脑从自己的实际监听与导入配置提供入口，不信任请求 Host/发现数据生成别名。

Android 只在已认证状态返回且 `instance_id` 一致的配置中登记第二入口；不因为发现包宣称相同 ID 就复用 token。模式切换只选择已登记入口，不清 token，认证仍限制当前 origin。不同电脑或任意新地址保持原有重新配对行为；401 才进入凭据失效流程，超时/离线/FRPC 断开不清除授权。

切模式取消旧地址在途查询/上传/推荐预载并失效旧回调，保留查询草稿；传输正在进行时禁止切模式并给原因，不静默打断保存。旧搜索/推荐会话清空显示并提示重新查询，避免跨地址残留请求；不自动重复模型查询。上次模式恢复不强行转 UDP 发现来掩盖远程离线。

### 2.3 图片交付

| 情景 | 路径与交付 |
| --- | --- |
| LAN 网格/大图 | 原有 `/thumbnail`、`/original` |
| REMOTE 网格 | `/api/v1/media/{id}/remote-thumbnail`，长边最多 640px，80 KiB 目标 |
| REMOTE 默认大图 | `/api/v1/media/{id}/preview`，长边最多 1600px，500 KiB 目标 |
| 显式查看原图、保存、分享 | 原有 `/original`，完整原始字节与 Range/哈希不变 |

两个新增固定媒体路由 GET/HEAD、Bearer、原 session ownership/version/expiry、ETag/304/private cache；继续拒绝未知 query。不替换旧路由，不修改搜索输入图或嵌入、排序、推荐配额与 shown/export 语义。

新增 `ImageRegistry` 的 `remote_thumbnail`/`remote_preview` 固定 variant，复用原有共享 render=4 排队、single-flight、内存/有界磁盘缓存，cache key 必须含 variant、编码策略版本、文件版本。原 Windows thumbnail/preview 渲染不变；不能全图库预压缩、另起无界工作池或扩大既有缓存。

按真实编码字节有限尝试：照片 JPEG 或 WebP；透明图保 alpha；文字/平面图保清晰度；小图避免无收益重编码。从原始源生成，不反复压缩已生成的缩略图。质量下限后仍超预算再降尺寸，最多 6 次编码；目标并非任意图片的强保证，1 MiB 为预览交付上限，无法生成有效输出必须返回明确错误，不能偷偷退回大原图。EXIF 方向/色彩正确，原文件无修改。

手机预载与网格使用同一个当前 origin/profile thumbnail helper；推荐不能再直用电脑内网 URL。远程默认不预取原图；仅当前页发 preview，相邻页可复用缩略图。查看器明确“预览/原图”与按需“查看原图 · 大小”，详情元数据仍来自源图片。保存/分享不使用压缩 Bitmap；现有双击/旋转/返回/进度继续工作。旧桌面无 remote-v1 时远程预览显示升级原因及显式原图选项，不自动拉大文件。

## 3. 精确实施范围与批次

不得修改无关部分。每个任务再提供相对文件 `AllowedFiles`，不授权整个目录。候选复用 D 盘工作副本，主工作区保留 `.qoder/better-harness/`。

| 批次 | 文件位置范围 |
| --- | --- |
| R1 托管及桌面接线 | 新 `zvec_webview/{remote_settings,remote_access}.py`；`zvec_webview/{facade,runtime,server,native_bridge,lan_access}.py`；`release/webview_preview/zvec_webview_preview.spec`、`scripts/webview_preview_packaging.py`；对应 remote、app lifecycle、native bridge、包装测试 |
| R2 媒体与认证后入口契约 | `zvec_webview/{image_registry,lan_access}.py`、必要的新 `remote_image.py`；`zvec_lan/{models,http_server,service}.py`；对应 image_registry、remote image、LAN HTTP、adapter 测试 |
| R3 Windows UI | `frontend/src/features/settings/{SettingsPage,LanAccessSection}.vue`；新 `RemoteAccessSection.vue`、`remoteTypes.ts`、`remoteApi.ts`、`useRemoteAccess.ts` 与对应测试；只改必要接线，不重排其他设置 |
| R4 Android | `android/app/src/main/java/com/zvec/lanviewer/data/{local/ConnectionStore,network/LanApiClient,model/Models,repository/ZvecRepository}.kt`；`ui/{AppUiState,AppViewModel,ZvecApp}.kt` 与必要的连接/查看器业务文件；对应 JVM、原生测试 |
| 协调者 | 本文、`docs/android-lan-api-v1.md`、`docs/spec.md`、`AGENTS.md`、`SUPPLY_CHAIN.md`；`third_party/frp/windows-amd64/{frpc.exe,LICENSE,provenance.json}`；Python 3.10 兼容的 `pyproject.toml`、`requirements-lock.txt`；包装闭包对应的 `tests/test_webview_preview_{packaging,release_verification}.py`；验证记录/任务/截图只放 D 盘证据目录 |

Windows/Wear OS 无关 UI、数据库、C 图库、RAW、图片编辑、模型调用、搜索排序/top_k/100 项分页、推荐 15 张/首4/一批预热/事件幂等、HTTP=10/render=4、发布矩阵均不改变。

## 4. 验收与发布边界

1. 先复现换地址 token 丢失、公网推荐 URL 与原图传输；用真实调用链回归一次配对→两入口→切模式/重启→不再批准，非法/未登记地址不得复用授权，临时网络错误保留 token。
2. FRPC 单进程启动、日志注册状态、断开/退出恢复、停止仅操作自己 PID、配置非法输入、自动启动及 import 原文件字节不变。固定官方 FRPC `verify` 冒烟，实际 ECS 隧道仅由协调者启动；不改变 FRPS/使用用户图库作测试数据。
3. 合成真实照片、透明图、文字图/小图的尺寸、实际 bytes、alpha/方向、缓存/ETag、文件改变失效、expired capability，原文件 SHA-256 不变；GET/HEAD/304 和旧 original Range 回归。
4. Android 单元+原生：两模式、草稿/取消/旧回调、当前 preview/显式 original、媒体始终当前 origin、原图保存哈希；A/B 与推荐既有用例保留。Windows typecheck、相关 Vitest、构建与响应式深浅截图；Android 原生截图与正式 Activity 冒烟。
5. 构建、模拟器、图片测量串行；单 Gradle worker/JVM 1024 MiB，已有 API34 模拟器只读单实例 1536 MiB/1核/softwareGPU。不启动 GPU 模型、并行模拟器或大压力测试；小样本不能称真机 P95/全设备认证。
6. 所有执行器结束后读 `report.json`/`last-message.md`/实际 diff，检查 scope_violations 与 runtime_cleanup。先验收再合主工作区；无其他同项目活动 session 时提交，不自行 push/发布。

证据目录：`D:\CodexTools\deepseek-worker\work\zvec-remote-access-20261009\`。后续在本文记录实际结果、未覆盖项与可安装包，不把计划目标写成已通过。

## 5. 使用流程

1. Windows“设置 → 局域网访问”：选择当前电脑有效的内网地址、端口 `38522`，开启并保存手机网关。
2. “设置 → 远程访问”：导入现有 `frpc.toml`，开启远程访问。看到“已连接”表示 ECS 已接受代理注册；关闭窗口保持常驻，完整退出会停止应用自己启动的 FRPC。
3. Android 首次可在局域网发现电脑，也可选择“非局域网（远程）”填写 ECS 公网 IP 和 `38522`。核对六位码并在电脑“局域网访问”批准一次。
4. 后续启动自动核对已保存令牌。在“设备 → 连接方式”切换局域网/远程；已登记的同电脑入口无需再次批准。电脑配置变化后重新打开 App 读取最新入口。
5. 远程网格加载压缩缩略图，大图默认显示“预览”。需要完整细节时点“查看原图”；保存/分享始终交付原文件。切换连接保留查询草稿、清除旧结果，需要手工重查；正在保存/分享时先结束传输。

## 6. 独立验收记录

- DeepSeek R1/R2 的业务改动未超出范围，但执行器生成缓存造成报告失败（164/24 项）；协调者逐项核查并将缓存隔离到证据目录，不把缓存作为产品改动合入。两个报告均 `runtime_cleanup=removed`。R3/R4/R4b 实施报告 completed、scope_violations 为空且运行状态已清理；协调者独立检查和纠正，返工只读检查和修改代码。
- 后端及现有 LAN/图像/生命周期/打包回归 333 项通过；协调者复现并修正官方 FRPC 0.61.1 实际错误日志识别、缺失随包文件检查、LAB 色彩转换与 Python 3.10 TOML 后备导入。后续 remote 三模块测试 50 项通过；新模块 Ruff、mypy 通过。真实 tomli 2.2.1 后备路径 14 项通过（在当前 Python 3.14 强制关闭标准库 tomllib 测试，未替代原生 Python 3.10 运行认证）。
- 协调者使用现有 ECS、官方 FRPC 和生产网关/图片注册器，在隔离配置和合成图片上验证：一次批准后同令牌可经 LAN/ECS 使用；GET/HEAD/304、原图 Range/完整 SHA-256、隧道停止/恢复、网关重启后授权复用均通过。源 FRPC TOML 与合成原图字节未改变，测试拥有的 FRPC 已关闭。
- 一张合成 JPEG 原图 4,326,292 B，远程缩略图 67,286 B（640×427）、预览 351,563 B（1312×875）。这是小样本真实编码结果，不能推断所有照片、真机 P95 或宽带峰值。
- Windows 两类 TypeScript 检查、4 文件 40 项 Vitest、Vite 构建及资源闭包校验通过；内置浏览器实际生产页面/合成配置验证导入与切换状态。桌面和 390×844 窄屏、浅色/系统深色、导入模态已目检。协调者修正错误状态缺少关闭操作与样式覆盖问题。更新已跟踪 `zvec_webview/frontend_dist/` 构建资源，与本轮源码一致；包装相关 33 项回归通过。
- Android 最终 116 项 JVM 测试通过，lint 0 错误/6 项既有警告，debug APK 和原生测试 APK 均构建成功。原生测试首次编译有缺失引号，协调者修正后首轮 57 项通过。R4b 后完整 62 项回归有 61 项通过、1 项新测试夹具漏匹配带查询参数的上传 URL；修正为 encodedPath 匹配后，连接相关 9 项重跑全部通过，本轮 62 个不同用例全部完成验证。
- 原生验证覆盖同令牌双入口切换/进程恢复、未登记地址重新配对、临时断线保留凭据与显式切回 LAN、401 配对、旧状态和错误别名兼容、搜索及上传迟到回调、附件草稿保留/手工重传、传输中禁止切换、远程网格/当前页预览/显式原图、无 remote-v1 的升级提示，以及原有 A/B 和推荐流程。发现页、设备页、预览/原图深浅截图已目检；预览截图等待图片实际显示。正式 `MainActivity` 冷启动冒烟通过。模拟器为单实例/1536 MiB/1 核/software GPU，验收后已关闭。
- 可安装测试包：`D:\CodexTools\deepseek-worker\work\zvec-remote-access-20261009\apk\YaoLens-remote-access-debug.apk`；它是本次源码 debug 构建，不是 GitHub 正式发布。完整检查日志、执行器报告、截图和 APK 校验保存在同一证据目录，未写入用户 FRP 凭据。
- 尚未构建本轮完整 Windows 冻结/NSIS 包或发布。Android 真机、不同系统版本、真实图库规模、长时间后台重连与弱网性能尚未覆盖。C 图库仍暂停。
