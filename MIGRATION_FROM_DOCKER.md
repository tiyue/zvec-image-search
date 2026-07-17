# 从 Docker 迁移到原生 Python

默认运行路径已经改为宿主机原生 Python。迁移目标是复用已有 Collection、SQLite 状态和图片向量，不重新调用模型。只有旧配置使用 Docker named volume 时，导出该 volume 的一次性操作仍需要能访问原 volume 的 Docker Engine。

## 迁移前准备

1. 停止 Zvec Desktop、旧常驻后端和正在运行的索引任务。
2. 备份当前配置目录、bind Workspace 或 named volume。
3. 确认图片根目录仍可在宿主机访问。
4. 安装 Python 3.10 或更高版本，并验证原生命令：

```powershell
zvec help
zvec doctor
```

默认配置目录是 `%LOCALAPPDATA%\zvec-image-search`。自定义目录使用 `ZVEC_CONFIG_HOME`；旧 `ZVEC_DOCKER_CONFIG_HOME` 只作为兼容别名读取。

## 配置字段变化

旧 schema v2：

```json
{
  "image_name": "zvec-image-search:local",
  "libraries": [
    {
      "image_root": "D:/Pictures",
      "workspace_type": "bind",
      "workspace_source": "D:/ZvecData/workspace"
    }
  ]
}
```

新 schema v3：

```json
{
  "schema_version": 3,
  "results_directory": "D:/ZvecData/results",
  "libraries": [
    {
      "image_root": "D:/Pictures",
      "workspace_directory": "D:/ZvecData/workspace",
      "enabled": true
    }
  ]
}
```

`image_name`、`workspace_type` 和 `workspace_source` 不再用于日常运行。非预演迁移会在第一次导出、schema migration 或 rebind 之前保存 `config.v2.docker.backup.json`，并创建包含配置、Collection 元数据、SQLite 一致性快照及小型状态文件的 `migration-backup-manifest.json`；即使后续校验失败，恢复点也仍然存在。写入新配置采用临时文件和原子替换。

这里的配置 schema v3 与 Collection schema v4 是两套独立版本：前者描述图库目录，后者描述 Zvec 文档字段和向量。

默认 metadata 备份不会递归统计或复制大型 `image_collection`，因此预演不会因为图库很大而扫描全部向量文件。若确实需要完整离线副本，显式使用：

```powershell
zvec migrate-docker-workspace `
  --backup-directory "D:\ZvecBackups\full-before-migration" `
  --full-backup `
  --library "默认图库"
```

独立备份入口是 `zvec workspace-backup`；桌面端的备份/迁移服务也调用这一稳定 JSON 接口。`--dry-run` 只返回目录权限、可用空间、预计大小、阻断项和警告，不创建目录或修改 Workspace。

## Collection schema v4

当前 Collection schema 为 v4，新增：

```text
metadata_text
metadata_text_hash
metadata_embedding
```

v1、v2、v3 可直接升级。先预演，再执行：

```powershell
zvec migrate-schema --dry-run --library "默认图库"
zvec migrate-schema --library "默认图库"
```

升级会保留已有图片向量，建立旧 Collection 备份，校验文档数，并为描述字段写入空值和零向量占位。整个迁移不请求模型，报告中的 `api_requests` 必须为 `0`。

描述向量不是迁移的一部分，也不会在程序启动时自动生成。迁移完成后，用户可在桌面端“智能整理”点击“生成描述向量”，或显式运行：

```powershell
zvec raw metadata-backfill --max-images 200 --library "默认图库"
```

回填只使用人工标签、文件夹标签、已接受标签和已接受短描述，不发送原图。它按内容哈希断点续跑，单图失败跳过，默认有限并发为 2，并与其他 embedding 请求共用 48 RPM、80,000 TPM 的安全水位。该步骤会调用阿里云 Embedding API，可能产生费用。

本轮不执行方案第三阶段的人工质量匹配、阈值校准或 `search-quality.json` 启用；它们不属于 Docker 迁移或 Collection v4 升级。

## bind Workspace：直接复用

bind Workspace 本来就是宿主机目录，不需要复制数据，也不需要 Docker：

```powershell
zvec migrate-docker-workspace --dry-run --library "默认图库"
zvec migrate-docker-workspace `
  --backup-directory "D:\ZvecBackups\bind-migration" `
  --library "默认图库"
zvec verify-native --library "默认图库"
```

也可以直接运行任意原生命令触发配置升级；显式使用迁移命令更容易先查看完整报告。迁移器会：

1. 把 `workspace_source` 转成 `workspace_directory`。
2. 运行必要的 Collection schema migration，当前目标为 v4。
3. 将旧容器路径 `/data/roots/main` 重绑到宿主机 `image_root`。
4. 核对 Collection 文档数与 SQLite `entries` 数量。
5. 使用已有文档向量执行搜索探针。
6. 报告 `api_requests: 0`。

## named volume：显式导出

named volume 不能由原生进程直接读取。先预演，再明确指定宿主机目标目录：

```powershell
zvec migrate-docker-workspace `
  --dry-run `
  --destination "D:\ZvecData\workspace" `
  --library "默认图库"

zvec migrate-docker-workspace `
  --destination "D:\ZvecData\workspace" `
  --backup-directory "D:\ZvecBackups\volume-migration" `
  --library "默认图库"
```

目标目录必须为空。桌面配置服务只报告 named volume 阻断，不会在启动时自动调用 Docker；只有用户显式执行上述迁移命令时才进行一次性导出。迁移器先复制到同磁盘临时目录，确认 Collection、元数据和 SQLite 状态完整后写入包含源 volume 名称的完成收据，最后一次性落到目标目录。只有收据完整且源 volume 完全匹配时才会复用已有导出；无收据的目录、半成品和其他 volume 的导出一律拒绝。导出流程以只读方式挂载源 volume，完成后删除临时容器，不删除源 volume。

如果当前电脑已经无法运行 Docker：

1. 在仍能访问该 named volume 的电脑上完成导出。
2. 将整个 Workspace 目录原样复制到新电脑，不要只复制 `image_collection` 子目录。
3. 在新电脑把旧配置的图库改为 bind 目录，或使用 `zvec init --workspace <导出目录>` 创建对应配置。
4. 执行 `zvec migrate-schema`、`zvec rebind-root` 和 `zvec verify-native`。

不要通过新建同名空 volume 或重新索引来“恢复”旧数据，这会丢失 SQLite 路径状态并产生不必要的模型费用。

## 验证标准

每个图库都应执行：

```powershell
zvec verify-native --library "默认图库"
zvec stats --library "默认图库"
zvec roots --library "默认图库"
```

成功报告至少满足：

```json
{
  "status": "verified",
  "collection_documents": 1234,
  "sqlite_entries": 1234,
  "search_probe": "passed",
  "api_requests": 0
}
```

- `collection_documents` 必须等于 `sqlite_entries`。
- 非空 Collection 的 `search_probe` 必须是 `passed`。
- `api_requests` 必须为 `0`，证明 schema migration、路径重绑和搜索探针复用了已有图片向量。
- 空 Collection 可以返回 `not_applicable_empty_collection`。

完成零 API 验证后，再进行一次正常文字、图片或图文查询验证外部 API 与结果导出链路。正常语义查询本身可能调用 Embedding API，不应与迁移的零 API 探针混淆。

描述向量回填也会调用 Embedding API，必须在零 API 迁移验证完成后由用户单独触发。

## 常见问题

### Collection 与 SQLite 数量不一致

停止迁移，不要覆盖备份。确认导出的是完整 Workspace，并检查是否在复制时遗漏隐藏文件、SQLite WAL/SHM 文件或元数据。先恢复原目录，再重新导出。

### 图片路径仍是 `/data/roots/main`

运行：

```powershell
zvec roots --library "默认图库"
zvec rebind-root <root-id> "D:\Pictures" --library "默认图库"
zvec verify-native --library "默认图库"
```

`rebind-root` 只更新稳定路径映射，不重新生成向量。

### 旧 volume 是否可以立即删除

不可以。至少保留到以下条件全部满足：

- 新配置和备份配置都已另行保存。
- 每个图库的数量、SQLite、根目录和搜索探针通过。
- WPF 与 CLI 都能打开历史结果和原图。
- Workspace 已进入你的常规备份流程。

## 回滚

迁移不会删除旧 bind 目录或 named volume。需要回滚时：

1. 停止桌面端和原生后端。
2. 保存新 schema v3 配置作为审计材料。
3. 打开备份目录的 `migration-backup-manifest.json`，先核对所需文件的 SHA-256。
4. metadata 备份只恢复配置、元数据和 SQLite；它必须与仍然完整的原 `image_collection` 配套，不能单独当作完整 Workspace。full 备份才包含可独立恢复的向量 Collection。
5. 恢复 `config.v2.docker.backup.json`；schema v1 对应恢复 `config.v1.docker.backup.json`。
6. 仅在旧 Docker 环境仍完整可用时启动旧版本。

迁移后新增的索引记录不会自动出现在旧备份中；不要在新旧版本之间并发写同一个 Workspace。

若只需回滚 Collection v4 升级，先停止桌面端和后端，再将当前 v4 Collection 移出 Workspace，并把同一次迁移生成的 `image_collection.v<旧版本>.backup_*` 与对应元数据备份成对恢复；从 v1 升级时还要恢复同批 SQLite 状态备份。不要混用不同批次或不同 schema 的文件。

最佳实践：完成迁移后仍保留一次不可变的旧 Workspace 快照，直到至少做过一次完整备份恢复演练。
