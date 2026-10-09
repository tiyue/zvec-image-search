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

版本准备与本地预检查已完成，尚未发布。实际命令、日志、构建与下载校验报告保存于 `D:\CodexTools\deepseek-worker\work\zvec-release-20261010\`。完成后记录真实工作流、版本提交、校验与 Release 链接。

- 三次 DeepSeek 实施报告均 completed、scope_violations 为空、runtime_cleanup 为 removed；协调者已读报告、最后消息和实际差异，确认依赖锁仅改项目版本。
- 全仓 Ruff lint/format 通过；主 mypy 136 文件、冻结入口 2 文件、搜索质量工具 18 文件通过。预检查先复现6处类型错误与4文件格式阻断，最小修正涉及 `scripts/webview_preview_packaging.py`、`image_vector_service/backend_server.py`、`tests/test_lan_http_api.py`、`tests/test_webview_gateway.py`、`tests/test_webview_lan_access.py` 和 `tools/benchmark_gallery_queries.py`；四处格式修改 AST 相同，推荐只改类型声明，FRPC 检查只改局部变量名。
- 发布、打包和推荐68项回归初次有3个旧版本断言失败；同步对应仓库版本断言（包括 `tests/test_webview_preview_packaging.py`）后68项全部通过，不删除或弱化检查。
- Vite 重新构建成功，与已提交前端产物完全一致；0.6.0/0.6/0.6.0.0 版本与资源闭包已独立验证。
