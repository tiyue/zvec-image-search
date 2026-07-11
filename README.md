# 本地图片向量检索服务

本项目使用阿里云百炼 `qwen3-vl-embedding` 生成 1024 维图片/文本独立向量，
使用 Zvec 在本地完成 COSINE 距离检索。程序不启动 HTTP 服务，所有功能通过命令行调用。

安装依赖：

```powershell
python -m pip install -r .\requirements.txt
```

索引或搜索前设置 API Key：

```powershell
$env:DASHSCOPE_API_KEY = "你的百炼API-Key"
```

递归索引整个图片文件夹：

```powershell
python .\image_service.py index "D:\Pictures"
```

同步文件夹并删除已经不存在的图片记录：

```powershell
python .\image_service.py sync "D:\Pictures"
```

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
