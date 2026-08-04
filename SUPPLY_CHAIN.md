# 发布供应链

YaoLens 0.3 使用机器版本 `0.3.0`，公开展示版本为 `0.3`，稳定标签为
`v0.3.0`。GitHub Release 只交付 Windows x64、Android，以及对应的校验和、
验证报告和发布策略元数据。Zvec-Desktop 和 Python wheel 不属于发布矩阵；
wheel 只在 CI 中用于安装兼容性验证。

## 公开附件

产品附件名称固定为：

```text
YaoLens-0.3-win-x64-portable.zip
YaoLens-0.3-win-x64-setup.exe
YaoLens-0.3-android.apk
```

发布装配还会提供以下验证材料：

- `WINDOWS-RELEASE-VERIFICATION.json`
- `WEBVIEW-SHA256SUMS.txt`
- `ANDROID-SHA256SUMS.txt`
- `RELEASE-POLICY.json`
- `RELEASE-ASSETS-SHA256SUMS.txt`

## Windows 构建

Windows 包由固定工具链构建：

- CPython 3.12
- PyInstaller 6.16.0
- pywebview 6.2.1 与固定 pythonnet 依赖闭包
- NSIS 3.12
- Vue 3 + Vite 编译后的静态资源

权威输入包括 `pyproject.toml`、固定 Python 依赖锁、PyInstaller 构建描述、
NSIS 安装脚本和已经通过测试的前端静态资源。构建产物必须包含
`YaoLens.exe`，并可包含内部组件 `zvec.exe`、`zvec-backend.exe`。包内不得
包含 Tkinter、pystray、旧 Zvec-Desktop 可执行文件、Node.js 源码或
`node_modules`。

验证步骤会检查 Vite 资源闭包、冻结清单、便携 ZIP 和 NSIS 安装器内容，
并生成 Windows 验证报告和 SHA-256 清单。

### 安装与常驻约束

- NSIS 安装版只为执行安装的当前用户注册登录任务。任务必须使用 `AtLogon`、
  `InteractiveToken`、`LeastPrivilege`、`IgnoreNew`、
  `ExecutionTimeLimit=PT0S` 和固定有限次数的失败重启；不得保存用户密码、
  注册为 SYSTEM 或提升到最高权限。
- 登录任务隐藏启动。关闭窗口仅隐藏界面，后台任务、Android LAN 服务和自动
  增量索引 watcher 继续运行；二次启动只唤醒已有窗口。
- 完全停止必须从“设置 → 应用 → 退出 YaoLens”发起；存在活动任务时必须拒绝
  退出。卸载或升级不得以普通关窗语义中断已接受任务。
- portable ZIP 不得注册、更新或删除登录计划任务。它只在用户手工启动后的
  当前登录会话内隐藏常驻，完全退出使用同一应用内入口。
- Windows Job Object 仅作为宿主异常死亡时清理 backend 孤儿进程的兜底。
  正常停止仍先执行 authenticated idle-only shutdown，不能借 Job Object
  绕过 active-job 拒绝。

## Android 构建

Android 使用 JDK 17、Android SDK Platform 34、Build Tools 34.0.0 和仓库内
Gradle Wrapper。CI 执行单元测试、lint、APK 装配和 SHA-256 生成：

```text
cd android
gradlew.bat --no-daemon testDebugUnitTest assembleDebug lintDebug
```

## 发布装配

`.github/workflows/release.yml` 只下载 Windows 与 Android 两类上游 artifact。
`scripts/assemble_python_release.py` 在复制前复核每个上游目录的 SHA-256，
拒绝重复文件名、篡改内容和不符合固定公开名称的产品附件。

发布策略元数据记录机器版本、公开版本、Git revision、精确标签状态、许可证
状态、搜索质量认证状态、运行目标和验证报告名称。它不记录或推断构建链未验证
的属性。

稳定发布必须满足以下条件：

- 版本为 `0.3.0`，标签为 `v0.3.0`，且标签精确指向工作流提交。
- GitHub Release 标题为 `YaoLens 0.3`。
- Release 不使用草稿或预发布参数。
- 三个产品附件名称与本文件列出的名称完全一致。
- 所有上游和最终附件均通过 SHA-256 复核。

发布工作流不得重新加入 Zvec-Desktop 或 wheel 附件。修改发布矩阵必须同步
更新 `AGENTS.md`、`docs/spec.md` 和 `RELEASE.md`。登录常驻只改变现有
Windows 安装包和便携包的运行语义，不新增 GitHub Release 附件。
