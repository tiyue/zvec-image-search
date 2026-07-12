# Docker 使用说明

本文只说明 Docker 环境下的启动、命令、挂载、构建、发布和故障排查。示例路径均为通用示例，请按实际目录替换。

## 1. 环境要求

- Docker Desktop 或 Docker Engine
- Docker Compose v2
- Windows 使用 WSL 2 后端
- BIOS/UEFI 已启用硬件虚拟化：AMD 为 `SVM Mode`，Intel 为 `Intel VT-x`

检查 Docker：

```powershell
docker version
docker compose version
```

安装 `zvec` 命令后可执行完整检查：

```powershell
zvec doctor
```

如果 Windows 的 WSL 版本过旧，可在管理员 PowerShell 中执行：

```powershell
wsl --update --web-download
wsl --version
```

## 2. Docker Hub 镜像

镜像地址：`gelang999/zvec-image-search`

| 标签 | 用途 |
| --- | --- |
| `0.3.0` | 固定补丁版本 |
| `0.3` | 跟随 0.3 系列更新 |
| `latest` | 最新稳定版本 |

镜像支持 `linux/amd64` 和 `linux/arm64`。

```powershell
docker pull gelang999/zvec-image-search:latest
docker run --rm gelang999/zvec-image-search:latest --help
```

需要固定版本时，请使用完整版本标签：

```powershell
docker pull gelang999/zvec-image-search:0.3.0
```

## 3. 使用 zvec 启动器

### 3.1 安装命令

在项目目录执行一次：

```powershell
.\scripts\install-zvec-command.cmd
```

如果安装脚本提示重新打开终端，请关闭当前 PowerShell，打开新终端后再继续。

### 3.2 本地构建并初始化

下面的命令会保存配置、构建 Docker 镜像并初始化 workspace：

```powershell
zvec init "D:\Pictures"
zvec index
```

初始化时会以隐藏输入方式读取 `DASHSCOPE_API_KEY`。

### 3.3 使用 Docker Hub 镜像初始化

先拉取镜像，并为 workspace 与搜索结果创建可写目录：

```powershell
docker pull gelang999/zvec-image-search:latest
New-Item -ItemType Directory -Force "D:\ZvecData\workspace"
New-Item -ItemType Directory -Force "D:\ZvecData\results"

zvec init "D:\Pictures" `
  --workspace "D:\ZvecData\workspace" `
  --results "D:\ZvecData\results" `
  --image-name "gelang999/zvec-image-search:latest" `
  --no-build
```

`--workspace` 使用宿主机目录，适合直接运行预构建镜像。使用 Docker 命名卷时，建议通过本地构建初始化，或使用 Compose 的 `workspace-init` 服务设置权限。

### 3.4 init 参数

```text
zvec init <图片目录> [选项]
```

| 参数 | 说明 |
| --- | --- |
| `--workspace <目录>` | 使用宿主机目录保存 Collection、状态、缓存和日志 |
| `--workspace-volume <卷名>` | 使用指定 Docker volume 保存 workspace |
| `--results <目录>` | 指定搜索结果输出目录 |
| `--image-name <镜像>` | 指定要使用的 Docker 镜像及标签 |
| `--no-build` | 只保存配置，不构建镜像 |
| `--skip-key` | 初始化时不询问 API Key |

`--workspace` 与 `--workspace-volume` 不能同时使用。

完整示例：

```powershell
zvec init "D:\Pictures" `
  --workspace "D:\ZvecData\workspace" `
  --results "D:\ZvecData\results" `
  --image-name "gelang999/zvec-image-search:0.3.0" `
  --no-build
```

## 4. API Key

索引和检索需要 `DASHSCOPE_API_KEY`。推荐让 `zvec init` 通过隐藏输入保存 Key；Key 仅写入当前用户的配置目录，不会写入 Docker 镜像。

也可以只在当前 PowerShell 会话中设置：

```powershell
$env:DASHSCOPE_API_KEY = "你的百炼 API Key"
zvec index
```

如需使用自定义兼容端点：

```powershell
$env:DASHSCOPE_API_URL = "https://example.com/embeddings"
```

不要将包含真实 Key 的 `.env` 文件提交到版本库，也不要通过 Dockerfile 的 `ENV` 或 `ARG` 写入镜像。

## 5. 状态检查

```powershell
zvec doctor
zvec stats
zvec roots
```

| 命令 | 说明 |
| --- | --- |
| `zvec doctor` | 检查 Docker、镜像、目录挂载和 API Key 配置 |
| `zvec stats` | 查看 Collection 和查询缓存统计 |
| `zvec roots` | 查看已登记的图片根目录及 `root_id` |

## 6. 索引与同步

### 6.1 建立或更新索引

```powershell
zvec index
zvec index 人物 旅行
```

`index` 后面的字符串会作为标签数组写入该图片根目录的所有 Document。再次指定标签会替换原标签，但会复用已有向量，不会重新计算嵌入。

可用选项：

```powershell
zvec index --no-recursive
zvec index --verify-hash
zvec index --clear-tags
```

| 参数 | 说明 |
| --- | --- |
| `--no-recursive` | 只扫描图片目录第一层 |
| `--verify-hash` | 使用文件哈希重新确认文件是否变化 |
| `--clear-tags` | 清空该图片根目录下所有 Document 的标签 |

### 6.2 同步删除状态

同步可能删除索引中已不存在的图片记录。始终先执行预演：

```powershell
zvec sync --dry-run
zvec sync
```

可用选项：

```powershell
zvec sync --no-recursive --dry-run
zvec sync --verify-hash --dry-run
zvec sync --allow-scope-change --dry-run
```

| 参数 | 说明 |
| --- | --- |
| `--dry-run` | 只显示将发生的删除，不修改数据 |
| `--no-recursive` | 只同步图片目录第一层 |
| `--verify-hash` | 使用文件哈希重新确认变化 |
| `--allow-scope-change` | 允许改变该根目录原先记录的递归范围 |

## 7. 搜索

### 7.1 文本搜索

```powershell
zvec search "海边日落" --tk 10
zvec search "海边日落" --tk 10 --tags 风景 日落
```

### 7.2 图片搜索

```powershell
zvec search-image "D:\Queries\example.jpg" --tk 10 --tags 人物
```

### 7.3 图文联合搜索

```powershell
zvec search-mix "D:\Queries\car.jpg" "红色跑车" --tk 10 --tags 汽车 红色
```

可以调整图像与文本权重：

```powershell
zvec search-mix "D:\Queries\car.jpg" "红色跑车" `
  --image-weight 0.7 `
  --text-weight 0.3 `
  --tk 20 `
  --tags 汽车 红色
```

多个标签默认使用 `all` 模式，只有同时包含全部标签的 Document 才会参与向量检索。使用任一标签匹配：

```powershell
zvec search "旅行照片" --tk 20 --tags 海边 城市 --tag-mode any
```

常用搜索参数：

| 参数 | 说明 |
| --- | --- |
| `--tk <数量>` | 返回结果数量，默认 `10` |
| `--tags <标签...>` | 使用一个或多个标签过滤 Document |
| `--tag-mode all\|any` | 多标签全部匹配或任一匹配，默认 `all` |
| `--image-weight <权重>` | 图文搜索中的图片权重，默认 `0.5` |
| `--text-weight <权重>` | 图文搜索中的文本权重，默认 `0.5` |
| `--include-self` | 允许查询图片自身出现在结果中 |

打开搜索结果目录：

```powershell
zvec results
```

## 8. 清理与维护

预览并删除指定天数以前的搜索结果：

```powershell
zvec clean 7 --dry-run
zvec clean 7
```

清空查询向量缓存：

```powershell
zvec cache-clear
```

图片目录移动后，先获取 `root_id`，再重新绑定：

```powershell
zvec roots
zvec rebind-root "<root-id>" "E:\NewPictures"
```

预演和执行 Collection schema 迁移。迁移会复用已有向量，不调用嵌入 API：

```powershell
zvec migrate-schema --dry-run
zvec migrate-schema
```

## 9. 镜像构建

使用启动器构建：

```powershell
zvec build
zvec build --clean
```

`--clean` 会禁用 Docker 构建缓存。

直接使用 Docker 构建：

```powershell
docker build --provenance=false -t zvec-image-search:local .
docker run --rm zvec-image-search:local --help
```

完全禁用缓存：

```powershell
docker build --no-cache --provenance=false -t zvec-image-search:local .
```

## 10. 容器挂载

| 容器路径 | 权限 | 用途 |
| --- | --- | --- |
| `/data/roots/main` | 只读 | 被索引的图片目录 |
| `/data/query` | 只读 | 外部查询图片目录 |
| `/data/workspace` | 读写 | Collection、状态、缓存和日志 |
| `/data/results` | 读写 | 搜索结果输出目录 |

图片目录和查询目录应保持只读；只有 workspace 和结果目录需要写权限。不要把 workspace 放在 SMB/NFS 等不适合数据库文件锁的网络文件系统中。

## 11. Docker Compose

从模板创建 Compose 环境文件：

```powershell
Copy-Item .env.example .env
```

`.env` 使用通用配置，例如：

```dotenv
ZVEC_IMAGES_DIR=D:/Pictures
ZVEC_RESULTS_DIR=./docker-data/search_results
ZVEC_WORKSPACE_VOLUME=zvec-image-workspace
DASHSCOPE_API_KEY=your-api-key
```

Windows 路径建议使用正斜杠。运行前确保图片目录和结果目录存在：

```powershell
New-Item -ItemType Directory -Force ".\docker-data\search_results"
docker compose build
docker compose run --rm workspace-init
```

执行常用命令：

```powershell
docker compose run --rm app index /data/roots/main
docker compose run --rm app sync /data/roots/main --dry-run
docker compose run --rm app search --text "海边日落" --tk 10 --tags 风景 日落
docker compose run --rm app stats
docker compose run --rm app roots
docker compose run --rm app cache-clear
docker compose run --rm app clean-results --days 7 --dry-run
```

搜索外部查询图片时，额外挂载查询目录：

```powershell
docker compose run --rm `
  --volume "D:/Queries:/data/query:ro" `
  app search --image /data/query/example.jpg --tk 10 --tags 人物
```

## 12. 直接使用 docker run

以下示例使用宿主机目录保存 workspace 和搜索结果：

```powershell
$env:DASHSCOPE_API_KEY = "你的百炼 API Key"
$Image = "gelang999/zvec-image-search:latest"

New-Item -ItemType Directory -Force "D:\ZvecData\workspace"
New-Item -ItemType Directory -Force "D:\ZvecData\results"

$DockerArgs = @(
  "--rm"
  "--init"
  "--read-only"
  "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m"
  "--cap-drop", "ALL"
  "--security-opt", "no-new-privileges=true"
  "--mount", "type=bind,source=D:\Pictures,target=/data/roots/main,readonly"
  "--mount", "type=bind,source=D:\ZvecData\workspace,target=/data/workspace"
  "--mount", "type=bind,source=D:\ZvecData\results,target=/data/results"
  "--env", "DASHSCOPE_API_KEY"
)
```

建立索引、同步和文本搜索：

```powershell
docker run @DockerArgs $Image index /data/roots/main
docker run @DockerArgs $Image sync /data/roots/main --dry-run
docker run @DockerArgs $Image search --text "海边日落" --tk 10 --tags 风景 日落
docker run @DockerArgs $Image stats
```

搜索外部图片时加入 `/data/query` 挂载：

```powershell
docker run @DockerArgs `
  --mount "type=bind,source=D:\Queries,target=/data/query,readonly" `
  $Image search --image /data/query/example.jpg --tk 10 --tags 人物
```

Linux 使用宿主机目录作为 workspace 时，目录应允许容器用户 `10001:10001` 写入。

## 13. 发布到 Docker Hub

先登录 Docker Hub：

```powershell
docker login
docker buildx inspect --bootstrap
```

发布多架构镜像：

```powershell
.\scripts\publish-docker.ps1 `
  -Version 0.4.0 `
  -Repository "<dockerhub-user>/zvec-image-search"
```

脚本默认发布：

- `<dockerhub-user>/zvec-image-search:0.4.0`
- `<dockerhub-user>/zvec-image-search:0.4`
- `<dockerhub-user>/zvec-image-search:latest`
- 平台：`linux/amd64`、`linux/arm64`

其他发布参数：

```powershell
# 禁用构建缓存
.\scripts\publish-docker.ps1 -Version 0.4.0 -Clean

# 不更新 latest
.\scripts\publish-docker.ps1 -Version 0.4.0 -SkipLatest

# 只发布指定平台
.\scripts\publish-docker.ps1 `
  -Version 0.4.0 `
  -Platforms "linux/amd64"
```

检查远端多架构清单：

```powershell
docker buildx imagetools inspect "<dockerhub-user>/zvec-image-search:0.4.0"
```

## 14. 故障排查

### `No such image`

当前配置引用的镜像在本机不存在。选择拉取或构建：

```powershell
docker pull gelang999/zvec-image-search:latest
zvec build
```

如果拉取的是 Docker Hub 镜像，请重新初始化并明确指定镜像名：

```powershell
zvec init "D:\Pictures" `
  --workspace "D:\ZvecData\workspace" `
  --image-name "gelang999/zvec-image-search:latest" `
  --no-build `
  --skip-key
```

### Docker 未启动或虚拟化不可用

```powershell
zvec doctor
docker version
wsl --version
```

确认 Docker Desktop 已启动、WSL 2 可用，并在 BIOS/UEFI 中启用 SVM 或 VT-x。

### `zvec` 命令无法识别

重新打开 PowerShell；仍无法识别时重新安装：

```powershell
.\scripts\install-zvec-command.cmd
```

### 挂载目录不存在或不可写

先创建宿主机目录，并确认 Docker Desktop 已允许访问对应磁盘。workspace 与结果目录必须可写，图片和查询目录只需可读。

### API Key 未配置

重新执行 `zvec init` 输入 Key，或在当前终端设置：

```powershell
$env:DASHSCOPE_API_KEY = "你的百炼 API Key"
```

### 图片目录移动

不要重新生成全部向量，先查看根目录 ID，再重新绑定：

```powershell
zvec roots
zvec rebind-root "<root-id>" "E:\NewPictures"
```

## 15. 命令速查

| 命令 | 说明 |
| --- | --- |
| `zvec help` | 显示命令帮助 |
| `zvec init <目录>` | 初始化 Docker 配置和图片目录 |
| `zvec doctor` | 检查 Docker 运行条件 |
| `zvec build [--clean]` | 构建本地镜像 |
| `zvec index [标签...] [选项]` | 建立或更新图片索引，并可替换 Document 标签 |
| `zvec sync [选项]` | 同步图片目录并处理失效记录 |
| `zvec search <文本>` | 文本搜图 |
| `zvec search-image <图片>` | 图片搜图 |
| `zvec search-mix <图片> <文本>` | 图文联合搜图 |
| `zvec stats` | 查看统计信息 |
| `zvec roots` | 查看图片根目录 |
| `zvec rebind-root <root-id> [目录]` | 重新绑定图片根目录 |
| `zvec results` | 打开搜索结果目录 |
| `zvec clean [天数] [--dry-run]` | 清理旧搜索结果 |
| `zvec cache-clear` | 清空查询向量缓存 |
| `zvec migrate-schema [--dry-run]` | 无需重新计算向量地迁移 Collection schema |
| `zvec raw <参数>` | 直接传递原始容器 CLI 参数 |
