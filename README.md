# 本地图片向量检索服务

本项目使用阿里云百炼 `qwen3-vl-embedding` 生成 1024 维图片/文本独立向量，
使用 Zvec 在本地完成 COSINE 距离检索。程序不启动 HTTP 服务，所有功能通过命令行调用。

> 隐私提示：图片来自本地、向量和检索数据保存在本地，但向量计算会将图片内容发送到阿里云百炼。

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

查看 Collection 状态：

```powershell
python .\image_service.py stats
```

## 工作目录与日志

默认把当前命令行目录作为运行工作区，也可以显式指定：

```powershell
python .\image_service.py --workspace "D:\ImageSearch" index "D:\Pictures"
```

或者设置环境变量 `ZVEC_IMAGE_WORKSPACE`。Collection、SQLite 状态和搜索结果保存在工作区；
所有 Zvec 日志统一保存在工作区的 `logs` 文件夹，单文件上限 1 GB，保留 7 天。

## 支持的图片格式

JPEG、PNG、WEBP、BMP、TIFF、ICO、DIB、ICNS 和 SGI。非图片文件会跳过，损坏图片会记录失败但不会中断整个任务。
