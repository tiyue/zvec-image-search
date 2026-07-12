# Zvec 图片检索 Docker

通过阿里云百炼大模型完成图片索引、同步以及文本、图片和图文联合检索。

完整参数、挂载方式、Compose、Docker Hub 发布和故障排查见 [Docker 使用说明](./DOCKER_USAGE.md)。

## 安装 Docker

从 [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/) 下载并安装 Docker Desktop。

## 检查 Docker

```powershell
# 检查 Docker 客户端与 Docker Engine 是否正常连接。
docker version

# 检查 Docker Compose v2 是否可用。
docker compose version
```

## 开启虚拟化

检查 `BIOS/UEFI` 是否已启用硬件虚拟化：AMD 为 `SVM Mode`，Intel 为 `Intel VT-x`。不同主板的设置入口不同，可搜索对应主板型号的 Windows 虚拟化开启方法。

## 安装 WSL

```powershell
# 从网络下载并更新到最新 WSL 版本。
wsl --update --web-download

# 显示当前 WSL 及内核版本，确认 WSL 2 可用。
wsl --version
```

## Docker Hub 镜像

镜像仓库：`gelang999/zvec-image-search`

可用标签：`0.3.0`、`0.3`、`latest`

支持平台：`linux/amd64`、`linux/arm64`

```powershell
# 拉取最新稳定版镜像到本机。
docker pull gelang999/zvec-image-search:latest

# 创建临时容器并显示镜像内的命令帮助；退出后自动删除容器。
docker run --rm gelang999/zvec-image-search:latest --help
```

## Windows 快速启动

索引和检索需要大模型 `DASHSCOPE_API_KEY`。执行 `zvec init` 时会要求隐藏输入并保存 Key；Key 仅写入当前用户的配置目录，不会写入 Docker 镜像。

```powershell
# 安装全局 zvec 启动命令，只需执行一次。
.\scripts\install-zvec-command.cmd

# 将图片目录绑定到 Docker，保存配置并构建本地镜像。
zvec init "D:\Pictures"

# 扫描图片目录，生成向量并建立索引。
zvec index
```

安装后若当前终端无法识别 `zvec`，请重新打开 PowerShell。

## 常用命令

```powershell
# 检查 Docker、镜像、目录权限和 API Key 配置。
zvec doctor

# 查看 Collection、图片根目录和查询缓存统计。
zvec stats

# 建立或更新索引，并为该图片根目录的 Document 设置标签。
zvec index 标签

# 预览同步将删除的失效记录，不实际修改 Collection。
zvec sync --dry-run

# 正式同步图片目录，并删除源目录中已经不存在的图片记录。
zvec sync

# 文本搜图，tk参数为返回的图片数，并只检索带标签的图片，标签为可选项
zvec search "海边日落" --tk 10 --tags 风景 日落

# 使用查询图片搜图，tk参数为返回的图片数，并只检索带标签的图片，标签为可选项
zvec search-image "D:\Queries\example.jpg" --tk 10 --tags 人物

# 联合使用图片、文本和标签搜图，tk参数为返回的图片数，并只检索带标签的图片，标签为可选项。
zvec search-mix "D:\Queries\example.jpg" "红色跑车" --tk 10 --tags 汽车 红色

# 使用资源管理器打开搜索结果目录。
zvec results
```

其中 `--tk` 指定最大结果数；多个 `--tags` 默认全部匹配，使用 `--tag-mode any` 可改为任一标签匹配。

## 构建与发布

```powershell
# 使用 Docker 缓存构建当前配置的本地镜像。
zvec build

# 禁用 Docker 构建缓存，从头构建本地镜像。
zvec build --clean

# 构建并发布 0.4.0、0.4 和 latest 多架构标签到 Docker Hub。
.\scripts\publish-docker.ps1 -Version 0.4.0
```
