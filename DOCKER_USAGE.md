# Docker 支持已弃用

Zvec 的默认运行、桌面端、持续集成和发布流程均已改为宿主机原生 Python，不依赖 Docker Desktop、Docker Engine、Docker Compose、WSL 或容器镜像。仓库不再提供 Dockerfile、Compose 配置、镜像发布脚本或 Docker 端到端测试。

新安装请直接按照 [README](./README.md) 配置原生 Python 环境。

## 旧数据迁移

- bind workspace 已经位于宿主机目录，可直接复用，不需要 Docker。
- Docker named volume 必须先一次性导出到宿主机目录。只有用户显式执行该迁移时，zvec 才可能调用本机 Docker CLI；日常索引、同步、搜索、桌面端、CI 和发布均不会调用 Docker。
- 迁移会复用已有 Collection、SQLite 状态和向量，不会为已有图片重新请求模型。完成后应确认迁移报告中的 api_requests 为 0。

完整的备份、预演、导出、schema migration、rebind-root、校验和回滚步骤见 [从 Docker 迁移到原生 Python](./MIGRATION_FROM_DOCKER.md)。

迁移命令入口：

    zvec migrate-docker-workspace --dry-run --library "图库名称"
    zvec migrate-docker-workspace --library "图库名称" --destination "D:\ZvecData\workspace"

如果原 Docker Engine 或 named volume 已不可访问，请不要初始化同名空 Workspace，也不要重新索引覆盖旧数据；请先从备份或仍能访问该 volume 的机器导出。
