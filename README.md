# Zvec 图片检索

Zvec 是本地图片库检索与整理工具。它使用 Zvec 保存本地索引，调用阿里云百炼模型生成向量和待审核标签，支持文字搜图、以图搜图、图文联合搜索、模糊标签搜索和跨图库查询。

Windows 桌面版已改为纯 Python 实现并冻结为独立程序。安装后不需要 PowerShell、.NET、Docker、WSL 或单独安装 Python。

## 下载与运行

当前桌面候选只提供 Windows x64：

- 安装包：`Zvec-Desktop-Python-Preview-<version>-win-x64-unsigned-setup.exe`
- 便携包：`Zvec-Desktop-Python-Preview-<version>-win-x64-portable.zip`

安装包按当前用户安装，不要求管理员权限。运行安装包，完成后从开始菜单启动 Zvec。

便携包需要先完整解压，再运行 `Zvec.Desktop.exe`。不要直接在 ZIP 内启动，也不要单独移动或删除同目录的 `zvec-backend.exe`、`zvec.exe` 和 `_internal`。

当前候选尚未进行 Authenticode 数字签名。Windows SmartScreen 可能显示“Windows 已保护你的电脑”。只有在下载来源和 SHA-256 校验值可信时，才选择“更多信息”→“仍要运行”。

Windows ARM64 暂无原生版本；可在支持 x64 仿真的设备上试用本候选。

## 首次使用

1. 打开“设置”→“图库与路径”。
2. 填写图库名称并选择图片文件夹。Workspace 和结果目录可使用默认值。
3. 打开“API Key”，保存阿里云百炼 DashScope API Key。
4. 打开“阿里云模型”，确认所用模型。
5. 进入“图库任务”，执行“建立索引”或“建立索引并标注”。
6. 索引完成后进入“图片搜索”。

API Key 保存在当前用户的 Windows 凭据管理器中，不写入 `config.json`、`models.json` 或日志。建立索引、语义搜索和智能标注需要联网，并可能产生阿里云 API 费用。

建议首次先处理 10～50 张图片，确认目录、搜索效果和费用，再扩大批次。

## 四个页面

| 页面 | 主要功能 |
|---|---|
| 图片搜索 | 文字、图片、图文联合、模糊标签和跨图库搜索；显示命中的标签与图库来源 |
| 图库任务 | 建立索引、同步、索引并标注、统计、任务进度、取消和错误图片目录 |
| 智能整理 | 筛选本次新增图片；审核模型建议；逐项确认身份；批量接受低风险标签；撤销最近批量操作；维护别名词典 |
| 设置 | 首次建库、图库管理、模型选择、JSON 配置和 API Key |

智能整理支持按角色、作品、动作、神态和审核状态筛选。真人身份、Cosplayer 名称、角色名和作品名优先采用人工标签、文件夹名或其他显式证据；身份标签不会随低风险标签批量写入。

## 画廊、预览与全屏

- 搜索结果每页显示 15 张。
- 正常窗口自动铺成 5×3，并使用整个画廊区域。
- 窗口较小时保留可读的卡片尺寸，自动改为纵向滚动布局。
- 右侧预览默认完整显示图片，不裁掉边缘。
- 双击结果或点击“全屏查看”进入全屏。
- 全屏默认铺满屏幕；按空格在“完整显示”和“铺满屏幕”之间切换。
- 全屏中按左右方向键切图，按 `Esc` 退出。

## 后台任务与错误隔离

界面、图片解码、后端轮询和模型请求分开运行。长时间索引或标注不会占用界面线程；任务执行时仍可查看结果、任务状态和日志。

程序采用有界并发：

- 文件扫描、校验和 SHA-256：默认并发 4。
- `qwen3-vl-embedding`：默认并发 2。
- 智能标注：默认并发 2。
- 不同图库可并行处理；同一图库保持单写入者，避免损坏 Zvec 和 SQLite 状态。

模型调用默认控制在每个模型 48 RPM、80,000 TPM 的安全水位，硬上限为 60 RPM、100,000 TPM。遇到 429、超时或临时网络故障时会受控退避，不会无限增加线程。

单张图片失败不会终止整批任务。最终失败的图片按 SHA-256 去重复制到：

```text
<结果目录>\failed-images\blobs
```

每次任务的错误清单写入：

```text
<结果目录>\failed-images\jobs\<任务ID>.jsonl
```

鉴权、磁盘、SQLite 或 Collection 等系统性错误会暂停受影响的任务，并保留已经完成的结果；软件本身不会因此直接退出。

## 模型配置

默认模型均来自阿里云百炼 DashScope：

| 角色 | 默认模型 | 用途 |
|---|---|---|
| `embedding` | `qwen3-vl-embedding` | 图片索引、语义搜索和描述向量 |
| `auto_tag_primary` | `qwen3-vl-flash` | 常规智能标注 |
| `auto_tag_escalation` | `qwen3-vl-plus` | 疑难图片复核，可在设置中调整 |

模型可在“设置”页面修改，也可编辑：

```text
%LOCALAPPDATA%\zvec-image-search\models.json
```

参考格式见 [model-catalog.default.json](./model-catalog.default.json)。只接受阿里云 DashScope 兼容模型和受支持的协议。更换标注模型不需要重建索引；更换向量模型后必须新建或重建 Collection，不能混用不同模型生成的向量。

## 托盘与单实例

点击窗口右上角关闭按钮只会隐藏到系统托盘，后台任务继续运行。单击托盘图标或再次启动 Zvec 会恢复原窗口，不会创建第二个实例。

只有页面顶部“退出”或托盘菜单“退出 Zvec”会完整结束程序。安装、升级或卸载前，应先等待任务结束，再从上述入口退出。

## 数据与旧版兼容

用户配置默认位于：

```text
%LOCALAPPDATA%\zvec-image-search
```

纯 Python 桌面版继续使用原有 `config.json`、`models.json`、Workspace、搜索结果和 Windows 凭据。卸载程序不会删除这些用户数据，也不会删除原图。

原生 schema v3 配置可直接复用。旧 bind Workspace 可原地迁移并保留已有向量；旧 Docker named volume 需要在仍能访问原 volume 的环境中做一次导出。迁移和 Collection schema 升级不会重新调用模型。详见 [从 Docker 迁移](./MIGRATION_FROM_DOCKER.md)。

迁移前先备份，并先执行预演：

```text
zvec.exe migrate-docker-workspace --dry-run --destination "D:\ZvecData\workspace"
zvec.exe migrate-schema --dry-run
```

确认报告后再去掉 `--dry-run`。迁移报告中的 `api_requests` 应为 `0`。

## 随包 CLI

安装包和便携包都包含 `zvec.exe`。它与桌面端使用同一份配置，可用于诊断、自动化和迁移：

```text
zvec.exe help
zvec.exe doctor
zvec.exe index
zvec.exe sync
zvec.exe search "海边日落" --tk 15
zvec.exe stats
```

CLI 不是桌面端的必需操作路径。普通使用只需要图形界面。

## 从源码开发

源码开发需要 CPython 3.10+；Windows x64 安装包固定使用 CPython 3.12 构建。

```text
python -m venv .venv
.venv\Scripts\python.exe -m pip install --constraint requirements-lock.txt --editable . --requirement requirements-dev.txt
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m mypy image_vector_service zvec_desktop image_service.py zvec_launcher.py zvec_logging.py scripts/build_python_preview.py scripts/python_preview_packaging.py
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m zvec_desktop.app
```

纯 Python 候选构建：

```text
python -m pip install --requirement requirements-packaging.txt
python scripts/build_python_preview.py --dry-run
python scripts/build_python_preview.py --output-root dist\python-preview\0.4.0-win-x64 --makensis "C:\Program Files (x86)\NSIS\makensis.exe"
```

构建脚本生成安装包、便携 ZIP 和带 SHA-256 的 payload 清单。完整发布边界见 [发布说明](./RELEASE.md)。

最佳实践：Workspace 和搜索结果目录应放在原图目录之外，并单独备份。
