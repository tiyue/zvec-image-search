# YaoLens 0.6 打包与发布记录

日期：2026-10-10。用户授权：总结新功能并打包发布到 GitHub。功能基线为 `d8ea0d0`，旧版本 `v0.5.0` 保留。

## 范围、分工与成功标准

- 新版机器版本 `0.6.0`、公开版本 `0.6`、新标签 `v0.6.0`，手机 versionCode 为 `600000`。
- DeepSeek `deepseek-flash/xhigh` 只调整 `pyproject.toml`、`frontend/package.json`、`frontend/package-lock.json`、`android/app/build.gradle.kts`、`release/webview_preview/yaolens_version_info.txt` 和 `tests/test_release_pipeline.py` 的必要版本契约；协调者审查差异并独立验证。
- 协调者更新 `README.md`、`RELEASE.md`、`SUPPLY_CHAIN.md`、`docs/spec.md`、本文和 `AGENTS.md` 索引，负责提交、标签、构建、附件复核和发布。出现真实 CI 阻断时仅修正阻断文件并记录，不夹带其他功能。
- 保留 `.qoder/better-harness/`、用户图片、配置、数据库、模型、搜索/推荐策略和发布矩阵。不得修改无关部分。
- 构建使用现有 GitHub Actions 和锁定工具链，不在用户电脑运行重型打包或并行模拟器。
- 成功标准：新标签精确指向发布提交；所有必需门禁通过；Windows 安装器/便携包和手机 APK 可下载，公开附件与校验和一致，版本和来源正确；Release 新功能说明完整。

## 发布边界

产品附件仅 `YaoLens-0.6-win-x64-setup.exe`、`YaoLens-0.6-win-x64-portable.zip`、`YaoLens-0.6-android.apk`，另附既有校验和、Windows 验证报告、发布策略元数据。不发布 Zvec-Desktop、wheel 或独立 Wear APK。

APK 沿用既有 debug 构建/签名，不生成或承诺商店正式签名。旧安装与新包是否可覆盖以签名一致为准。正式搜索质量报告未提供，沿用原 Release 的显式未认证选项并如实记录 `search_quality_certified=false`；其他发布门禁不跳过。

## 执行状态

正式发布与最终附件复核已完成。实际命令、日志、构建与下载校验报告保存于 `D:\CodexTools\deepseek-worker\work\zvec-release-20261010\`。发布标签固定于 `294bd123941578ac790472f6217afd504c626fa1`，后续发布记录提交不移动已发布标签。

- 五次 DeepSeek 实施报告均 completed、scope_violations 为空、runtime_cleanup 为 removed；协调者已读报告、最后消息和实际差异，确认依赖锁仅改项目版本。
- 全仓 Ruff lint/format 通过；主 mypy 136 文件、冻结入口 2 文件、搜索质量工具 18 文件通过。预检查先复现6处类型错误与4文件格式阻断，最小修正涉及 `scripts/webview_preview_packaging.py`、`image_vector_service/backend_server.py`、`tests/test_lan_http_api.py`、`tests/test_webview_gateway.py`、`tests/test_webview_lan_access.py` 和 `tools/benchmark_gallery_queries.py`；四处格式修改 AST 相同，推荐只改类型声明，FRPC 检查只改局部变量名。
- 发布、打包和推荐68项回归初次有3个旧版本断言失败；同步对应仓库版本断言（包括 `tests/test_webview_preview_packaging.py`）后68项全部通过，不删除或弱化检查。
- Vite 重新构建成功，与已提交前端产物完全一致；0.6.0/0.6/0.6.0.0 版本与资源闭包已独立验证。
- 首轮云端工作流 `37958821714` 在Linux全量1697项回归出现4个旧测试替身错误，发布被门禁阻止；安卓、前端、Python 3.10/3.14兼容及Windows冻结包/安装器门禁已经通过。协调者在本地9项聚类集成用例复现同样4错，限定 `tests/test_clustering_task_integration.py` 补真实内存感知哈希写回、初始化四个文档标签后9项通过，保留所有断言。第四份执行器报告 completed、scope为空、runtime_cleanup=removed。修复后重新执行全部发布门禁；仅更新本任务新建且尚无Release的0.6标签，旧0.5保持。
- 第二轮工作流 `37959951170` 的 Linux 全量1697项、前端459项、安卓、四组Python原生兼容、Windows冻结打包和10万图库30项门禁通过。Windows全量唯一失败为目录搜索测试依赖随机临时目录名：临时路径含下划线时，完整根路径搜索正确命中8条，与测试预设0条冲突。协调者用含下划线的父目录复现；第五次执行器仅调整 `tests/test_library_browser.py` 内此用例的根路径元数据为确定绝对路径，不改生产查询或任何断言。报告 completed、scope为空、runtime_cleanup=removed；本地18项图库测试及含/不含下划线父目录下各6项均通过，Ruff通过。
- 同提交CI便携包已下载并核对校验和，在本机运行冻结自检通过，SQLite WAL及全部依赖加载正常；三个入口的文件/产品版本均为0.6.0，内置FRPC版本为0.61.1。未启动模型、网关或额外窗口。最终公开附件已按下面记录重新下载校验。

## 发布结果与最终验收

- [YaoLens 0.6 正式 Release](https://github.com/tiyue/zvec-image-search/releases/tag/v0.6.0)，发布于北京时间2026-10-10 01:09:34，非草稿、非预发布。中文新功能、远程首次配置、升级步骤及验收结果已更新到Release说明。
- [最终发布工作流37961970529](https://github.com/tiyue/zvec-image-search/actions/runs/37961970529)全部15项工作通过，精确源码提交为 `294bd123941578ac790472f6217afd504c626fa1`。
- Windows/Linux各1697项完整回归通过，分别有4/5项已有跳过；两端后续18项原生CLI回归通过。前端53文件459项通过，Android单元测试、lint、APK构建通过，Python3.10/3.14的Windows/Linux原生兼容检查通过。
- Windows10万图库门禁11组30项全部通过、无跳过，耗时419.562秒；本次为既有`pr`规模门禁，没有运行可选百万规模，不将其描述为真实图库端到端P95认证。
- Windows最终发布包通过冻结依赖自检、载荷清单、便携ZIP和NSIS安装器内容检查。三个Windows入口的文件/产品版本均0.6.0、数值资源版本0.6.0.0。协调者对从Release下载的最终便携包再次运行本机冻结自检，SQLite WAL和依赖加载通过；未在用户电脑安装NSIS安装器或运行模型。
- 所有8份公开附件已下载到证据目录`assets/`，与GitHub记录的文件名、大小及SHA-256一致；总清单7项、Windows清单3项、Android清单1项全部匹配。内置FRPC0.61.1的SHA-256与固定官方来源一致，许可证和来源记录存在，未夹带FRPC用户配置或前端源码/依赖目录。
- APK实际包名`com.zvec.lanviewer`，版本0.6.0、versionCode600000、最低API26（Android8.0）。签名验证通过，最终下载APK与发布构建产物哈希一致。本次证书SHA-256为`48de11440b8565fa7f653a08a41b805ac1a94a4f87cdf7042f2d1f6691dc5db5`，与GitHub0.5证书`75d40eb09a842f73c6624ea0dbf50e643e6a5d6148b98549b7796d0cc4f927ad`不同；不能直接覆盖该旧APK。应先记录/备份手机连接设置，卸载旧客户端后安装新版并首次配对，电脑图库不受手机卸载影响。沿用现有debug签名流程，没有新增签名密钥。
- `RELEASE-POLICY.json`的版本、精确标签、源码与公开产品名匹配，`publication_ready=true`、`search_quality_certified=false`。仅发布Windows Webview与Android及既有元数据，没有增加Desktop、wheel或Wear发布附件；旧0.5Release保留，安卓C阶段继续暂停。

主要最终证据：`workflow-status.json`、`ci-windows-third.log`、`ci-linux-third.log`、`ci-frontend-third.log`、`ci-large-library-third.log`、`github-release.json`、`download-verification.json`、`final-frozen-self-test.json`、`final-windows-local-verification.json`、`final-apk-verification.json`及`release-notes-final.md`，均位于上述D盘证据目录。跨真机、长时间后台重连与弱网P95的限制沿用功能验收记录，不以打包成功替代这些后续实测。
