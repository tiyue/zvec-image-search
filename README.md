# 本地图片向量检索服务

本项目使用阿里云百炼 `qwen3-vl-embedding` 生成 1024 维图片/文本独立向量，
使用 Zvec 在本地完成 COSINE 距离检索。程序不启动 HTTP 服务，所有功能通过命令行调用。

> 隐私提示：图片来自本地、向量和检索数据保存在本地，但向量计算会将图片内容发送到阿里云百炼。

## Docker 快速使用

Docker Desktop 4.81 需要支持 `wsl --version` 的 Microsoft Store 版 WSL。若
`zvec doctor` 提示检测到旧版 Inbox WSL，请先在管理员 PowerShell 中执行：

```powershell
wsl --update --web-download
wsl --version
```

Docker 还要求固件虚拟化已开启。AMD 主机请在 BIOS/UEFI 中启用 `SVM Mode`，
Intel 主机启用 `Intel VT-x`。`zvec doctor` 会区分 WSL 版本问题和固件虚拟化问题。

Windows 用户只需安装一次全局 `zvec` 启动命令：

```powershell
.\scripts\install-zvec-command.cmd
zvec init "D:\Pictures"
```

`zvec init` 会检查 Docker Desktop、构建镜像，并以隐藏输入方式询问
`DASHSCOPE_API_KEY`。配置和 Key 保存在当前用户的
`%LOCALAPPDATA%\zvec-image-search`，不会写入镜像或 Git 仓库。

如果安装脚本提示需要打开新终端，请重新打开 PowerShell 后再执行 `zvec init`。
初始化完成后，日常不需要再输入 Docker 命令：

```powershell
zvec index
zvec sync
zvec search "海边日落" --top-k 10
zvec search-image "D:\Queries\example.jpg"
zvec search-mix "D:\Queries\car.jpg" "红色跑车"
zvec stats
zvec roots
zvec results
zvec clean 7
zvec doctor
```

更新代码后直接重建镜像；需要排除全部构建缓存时使用 `--clean`：

```powershell
zvec build
zvec build --clean
```

两种构建方式都会在完成后自动检查容器 UID 和 CLI 入口。

默认情况下，每个图片库使用独立的 Docker workspace volume，搜索结果保存在
Windows 用户目录中。需要把 workspace 或结果放到指定位置时：

```powershell
zvec init "D:\Pictures" `
  --workspace "D:\ImageSearchDocker" `
  --results "D:\ImageSearchResults"
```

启动器始终把已配置的图片库挂载到容器内的 `/data/roots/main`，因此宿主机盘符
变化不会改变 Collection 中的逻辑图片路径。图片库搬家后可重新初始化配置，并用：

```powershell
zvec roots
zvec rebind-root "roots 命令显示的 root_id" "E:\NewPictures"
```

高级用户也可以直接使用 Compose。先从 `.env.example` 创建 `.env`，填写图片目录、
结果目录和 API Key，并先创建结果目录，然后执行：

```powershell
New-Item -ItemType Directory -Force ".\docker-data\search_results"
docker compose build
docker compose run --rm workspace-init
docker compose run --rm app index /data/roots/main
docker compose run --rm app search --text "海边日落" --top-k 10
docker compose run --rm app stats
```

镜像使用 `python:3.12-slim-bookworm`，最终进程以固定的非 root 用户运行。图片目录
只读挂载；Collection、SQLite 状态和日志保存在 workspace；搜索结果通过独立目录
写回宿主机。不要把 workspace 放在 SMB/NFS 等网络文件系统上。

Dockerfile 默认从 AWS ECR Public 获取 Docker Library 的官方 Python 镜像，避免部分网络
环境无法访问 Docker Hub。需要改回其他镜像源时可传入：

```powershell
docker build --build-arg PYTHON_IMAGE=python:3.12-slim-bookworm `
  -t zvec-image-search:local .
```

已有 Windows 原生 workspace 建议先复制一份再交给 Docker 使用。首次进入容器后通过
`zvec rebind-root` 把根目录绑定到 `/data/roots/main`，不需要重新生成向量。

安装依赖：

```powershell
python -m pip install -r .\requirements.txt
```

也可以安装为本地命令：

```powershell
python -m pip install -e .
zvec-image-search --help
```

索引或搜索前设置 API Key：

```powershell
$env:DASHSCOPE_API_KEY = "你的百炼API-Key"
```

递归索引整个图片文件夹：

```powershell
python .\image_service.py index "D:\Pictures"
```

默认使用文件大小和修改时间快速判断未变化文件。需要强制重新校验 SHA-256 时：

```powershell
python .\image_service.py index "D:\Pictures" --verify-hash
```

同步文件夹并删除已经不存在的图片记录：

```powershell
python .\image_service.py sync "D:\Pictures"
```

先查看将删除多少条记录而不真正删除：

```powershell
python .\image_service.py sync "D:\Pictures" --dry-run
```

如果目录扫描不完整，`sync` 会自动跳过删除，避免把暂时不可访问的图片误判为已删除。

使用文本、图片或图文联合搜图：

```powershell
python .\image_service.py search --text "sunset by the sea" --top-k 10
python .\image_service.py search --image "D:\Queries\example.jpg" --top-k 10
python .\image_service.py search --image "D:\Queries\car.jpg" --text "red sports car" --top-k 10
```

每次搜索都会在 `search_results` 下创建全新目录，将排序后的图片复制进去，
并生成 `results.json`。Zvec 返回的是 COSINE 距离，`distance` 越小表示越相似；
图文联合检索的 `fused_score` 是 RRF 分数，越大越好。

搜索结果目录使用北京时间命名：

- 文字搜图：`搜索提示词_20260712_083015_123`
- 图片搜图：`图片搜索_20260712_083015_123`
- 图文搜图：`搜索提示词_图文搜索_20260712_083015_123`

提示词中的 Windows 非法字符会自动替换，过长提示词会截断；同名时追加数字序号。

查询向量会自动复用：已入库图片直接读取 Zvec 中的向量，相同的文本或未入库图片查询会读取本地 SQLite 缓存，避免重复消耗百炼额度。`results.json` 的 `embedding_sources` 会标明每个向量来自 `index`、`cache` 还是 `api`。

查看 Collection 状态：

```powershell
python .\image_service.py stats
```

`stats` 会同时显示查询向量缓存的条目数、占用字节数和累计命中数。需要清空缓存时：

```powershell
python .\image_service.py cache-clear
```

预览或删除 7 天前的搜索结果目录：

```powershell
python .\image_service.py clean-results --days 7 --dry-run
python .\image_service.py clean-results --days 7
```

## 可移动图片根目录

Collection 使用 `root_id + relative_path` 保存图片逻辑位置，不在每条 Zvec 记录中保存绝对路径。SQLite 只为每个图片根目录保存一次当前本地路径。

查看已经注册的图片根目录：

```powershell
python .\image_service.py roots
```

图片文件夹移动或盘符变化后，更新根目录绑定即可继续使用原有向量，不会调用百炼：

```powershell
python .\image_service.py rebind-root "roots 命令显示的 root_id" "E:\NewPictures"
```

从 0.2.x 的绝对路径 Collection 升级时，先预演再迁移：

```powershell
python .\image_service.py migrate-path-schema --dry-run
python .\image_service.py migrate-path-schema
```

迁移会直接复制现有向量，不调用百炼。只有新 Collection、SQLite 状态和文档数量全部校验成功后才会切换；原 V1 Collection、状态库和元数据会保留为带时间戳的备份。

## 工作目录与日志

默认把当前命令行目录作为运行工作区，也可以显式指定：

```powershell
python .\image_service.py --workspace "D:\ImageSearch" index "D:\Pictures"
```

或者设置环境变量 `ZVEC_IMAGE_WORKSPACE`。Collection、SQLite 状态和搜索结果保存在工作区；
所有 Zvec 和应用日志统一保存在工作区的 `logs` 文件夹，单文件上限 1 GB，保留 7 天。应用日志只记录操作类型、数量和状态，不记录搜索文本、图片路径或 API Key。

## 支持的图片格式

JPEG、PNG、WEBP、BMP、TIFF、ICO、DIB、ICNS 和 SGI。非图片文件会跳过，损坏图片会记录失败但不会中断整个任务。
