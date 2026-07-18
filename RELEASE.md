# Zvec Desktop 0.4.0 Python 候选版

本版将 Windows 桌面端和常驻后端统一为纯 Python 实现。发布包已冻结运行环境，用户无需安装 PowerShell、.NET、Docker、WSL 或 Python。

当前状态：Windows x64、未签名候选。它用于功能验证，不应冒充已签名稳定版。

## 本版内容

### 纯 Python 桌面链路

- `Zvec.Desktop.exe`：桌面界面。
- `zvec-backend.exe`：本机常驻后端。
- `zvec.exe`：诊断、迁移和自动化 CLI。
- 三个入口共享冻结后的 CPython、Tcl/Tk、Pillow 和 Zvec 依赖。
- 正常运行不调用 `.ps1`，不加载 WPF/.NET，也不连接 Docker Engine。

### 四页界面

- “图片搜索”：文字、图片、图文联合、模糊标签和跨图库查询。
- “图库任务”：索引、同步、索引并标注、统计、任务中心和错误图片目录。
- “智能整理”：筛选、差异对比、来源显示、身份逐项确认、低风险批量接受、撤销和别名词典。
- “设置”：首次建库、图库、阿里云模型、JSON 配置和 API Key。

### 图片显示

- 搜索结果固定每页 15 张。
- 正常窗口采用 5×3 画廊并填满可用区域。
- 小窗口自动减少列数并启用纵向滚动。
- 预览默认完整显示，不裁切原图。
- 全屏默认铺满屏幕；空格切换完整/铺满，方向键切图，`Esc` 退出。

### 并发与容错

- 界面线程不执行图片解码、任务轮询或长时间模型请求。
- 不同图库可并行；同一图库保持单写入者。
- 文件扫描默认并发 4，Embedding 和智能标注默认各并发 2。
- 每个模型按 48 RPM、80,000 TPM 的安全水位运行，硬上限为 60 RPM、100,000 TPM。
- 单图错误跳过并继续整批任务；最终失败图片复制到 `failed-images/blobs`，任务清单写入 `failed-images/jobs`。
- 系统性错误只暂停受影响任务，不直接关闭软件。

### 模型与凭据

- 默认向量模型：`qwen3-vl-embedding`。
- 默认标注模型：`qwen3-vl-flash`。
- 疑难复核默认可使用 `qwen3-vl-plus`。
- 模型可从前端或 `%LOCALAPPDATA%\zvec-image-search\models.json` 修改。
- API Key 保存于 Windows 凭据管理器，不进入 JSON 和日志。

### 桌面会话

- 关闭窗口只隐藏到托盘，后台任务继续运行。
- 再次启动程序会激活已有窗口，不创建第二实例。
- 页面或托盘中的“退出”执行安全关闭；有活动任务时不会强杀后端。

## 发布产物

本候选生成两个面向用户的文件：

```text
Zvec-Desktop-Python-Preview-0.4.0-win-x64-unsigned-setup.exe
Zvec-Desktop-Python-Preview-0.4.0-win-x64-portable.zip
```

解包后的核心入口：

```text
Zvec.Desktop.exe
zvec-backend.exe
zvec.exe
python-preview-manifest.json
```

`python-preview-manifest.json` 记录 payload 文件、大小和 SHA-256。发布时还应为安装包与 ZIP 单独提供 SHA-256，不得覆盖已经分发的同版本文件。

## 安装与便携版

安装版：

1. 结束旧版任务。
2. 从页面或托盘明确退出 Zvec。
3. 运行 `*-unsigned-setup.exe`。
4. 从开始菜单启动。

便携版：

1. 将 ZIP 完整解压到普通可写目录。
2. 运行 `Zvec.Desktop.exe`。
3. 保持整个解压目录结构不变。

安装器只写入当前用户目录，不要求管理员权限。升级或卸载时，如果桌面程序、后端或 CLI 仍在运行，安装器会停止操作，不会强制结束任务。

## SmartScreen 提示

本候选没有 Authenticode 签名，Windows 可能拦截首次运行。只有同时满足以下条件时，才选择“更多信息”→“仍要运行”：

- 文件来自可信的项目发布页或交付目录。
- 文件名与本说明一致。
- SHA-256 与发布方提供的校验值一致。

未签名不等于文件损坏，但无法建立发行者身份。正式公开版应先完成代码签名和可信时间戳。

## 首次使用

1. 在“设置”创建首个图库。
2. 保存 DashScope API Key。
3. 确认 `qwen3-vl-embedding` 和标注模型。
4. 在“图库任务”建立索引。
5. 在“图片搜索”验证文字、图片和标签查询。
6. 如启用智能标注，到“智能整理”审核建议。

模型调用会把对应文字或图片发送到阿里云百炼，并可能产生费用。首次建议用 10～50 张图片完成验证。

## 升级与兼容

- 继续使用 `%LOCALAPPDATA%\zvec-image-search` 下的用户配置。
- 保留原有 Workspace、Collection、SQLite 状态、结果目录、API Key 和原图。
- 原生 schema v3 配置可直接复用。
- Collection schema 升级保留已有图片向量，迁移过程不调用模型。
- 旧 bind Workspace 可迁移后直接使用。
- 旧 Docker named volume 只在一次性导出时需要原 Docker Engine；日常运行不需要 Docker。
- 旧 WPF/PowerShell 客户端仅作为迁移来源，不再是默认运行路径。

迁移前执行预演并备份。完整步骤见 [从 Docker 迁移](./MIGRATION_FROM_DOCKER.md)。

## 已知限制

- 当前产物未签名，SmartScreen 可能提示风险。
- 当前只发布 Windows x64；Windows ARM64 依赖系统的 x64 仿真。
- 尚无自动更新。
- 阿里云模型需要网络、有效 API Key 和可用额度。
- 更换向量模型后必须新建或重建 Collection；更换标注模型不需要重建索引。
- 本轮不启用人工搜索质量匹配和阈值校准；诊断分数不能解释为概率。
- 稳定公开版仍需完成 Authenticode、时间戳和干净机器安装验证。

## 开发验证

主线验证以 Python 为准：

```text
python -m venv .venv
.venv\Scripts\python.exe -m pip install --constraint requirements-lock.txt --editable . --requirement requirements-dev.txt
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m mypy image_vector_service zvec_desktop image_service.py zvec_launcher.py zvec_logging.py scripts/build_python_preview.py scripts/python_preview_packaging.py
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Windows GUI 冒烟至少覆盖：

- 首次建库与凭据保存。
- 后端启动、健康检查和安全关闭。
- 单图库与跨图库搜索。
- 15 张画廊、小窗口滚动、完整预览和全屏模式切换。
- 索引、同步、索引并标注、取消和错误隔离。
- 智能整理、批量撤销和别名维护。
- 托盘隐藏、恢复和单实例激活。

## 构建候选

构建机要求 Windows x64、CPython 3.12、PyInstaller 6.16.0 和 NSIS 3.12。构建脚本不会自动下载依赖。

```text
python -m pip install --requirement requirements-packaging.txt
python scripts/build_python_preview.py --dry-run
python scripts/build_python_preview.py --output-root dist\python-preview\0.4.0-win-x64 --makensis "C:\Program Files (x86)\NSIS\makensis.exe"
```

验证已有 payload：

```text
python scripts/build_python_preview.py --verify-only dist\python-preview\0.4.0-win-x64\Zvec-Python-Preview
```

通过验证只说明构建结构完整，不代表产物已经签名或达到稳定发布门槛。

最佳实践：每次修复都提高版本号并重新生成校验值，不覆盖已经交付的二进制文件。
