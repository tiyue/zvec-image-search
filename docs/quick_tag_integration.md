# quick_tag 批量打标脚本集成说明

本文档面向需要把「按文件夹名批量打标」功能集成到其他软件的开发者。
介绍脚本的完整工作流程、HTTP API 协议、标签推导算法和文件格式。
其他软件既可以直接调用编译好的 `quick_tag.exe`，也可以按本文档重新实现 HTTP 协议。

> **源码位置说明**：`tools/quick_tag.c`、`tools/mjson.c`、`tools/mjson.h`、
> `tools/quick_tag_blacklist.json` 当前不在主分支工作树中（被提交 `edfad48`
> 移除），可从 git 历史恢复：
>
> ```bash
> git show edfad48^:tools/quick_tag.c > tools/quick_tag.c
> git show edfad48^:tools/mjson.c > tools/mjson.c
> git show edfad48^:tools/mjson.h > tools/mjson.h
> git show edfad48^:tools/quick_tag_blacklist.json > tools/quick_tag_blacklist.json
> ```

## 1. 功能概述

quick_tag 是一个 Windows 平台的 C 语言命令行工具，用于对 Zvec 图片库做
**按文件夹名的批量自动打标**：

1. 通过 API 获取图库根目录（image_root）和 library_id；
2. 递归扫描 image_root，找出所有含图片的文件夹；
3. 从文件夹名自动推导标签（清洗元数据、过滤黑名单、提取中文）；
4. 调用后端 `manual_tag_batch` 任务把标签批量加到该文件夹下的所有图片；
5. 用状态文件记录已处理文件夹，支持断点续跑。

典型执行结果（500+ 文件夹约 5 分钟）：

```
=== Done ===
  Tagged:   190 folders
  Untagged: 0 folders
  Errors:   0 folders
  Skipped:  341 folders (already processed)
  Time:     4m 50s
```

## 2. 命令行用法

```
quick_tag.exe --auto                       # 推荐：自动发现 gateway
quick_tag.exe --auto --mark-all            # 只把所有文件夹标记为已处理
quick_tag.exe --auto --clean               # 清理历史脏标签（[35P-417MB] 等）
quick_tag.exe --gateway "http://127.0.0.1:5635/TOKEN/"
quick_tag.exe --backend-url http://127.0.0.1:8765 --token TOKEN
```

| 参数 | 说明 |
| --- | --- |
| `--auto` | 从 `%LOCALAPPDATA%\zvec-image-search\gateway-url.txt` 读取 gateway URL（文件内容为 `http://host:port/TOKEN/`），无需手动传 token |
| `--gateway URL` | 手动指定 gateway 地址（WebView 地址栏复制的 URL） |
| `--backend-url URL` + `--token TOKEN` | 直连后端模式（跳过 gateway），token 走 `Authorization: Bearer` |
| `--mark-all` | 不打标，只把扫描到的所有文件夹写入状态文件（后续运行全部跳过） |
| `--clean` | 移除旧版本误加的元数据标签：`[...]`、`【...】` 内容以及 `_jpg` 这类扩展名后缀 |

编译（依赖 Windows + Winsock，随附 mjson.c/mjson.h 自研极简 JSON 库，无第三方依赖）：

```bash
# MinGW-w64 (UCRT)
gcc -O2 -DUNICODE -D_UNICODE -o quick_tag.exe quick_tag.c mjson.c -lws2_32
# MSVC
cl /O2 /DUNICODE /D_UNICODE quick_tag.c mjson.c ws2_32.lib
```

## 3. 两种 API 模式

所有 HTTP 通信都是普通 HTTP/1.1 + JSON，无 WebSocket、无特殊协议。

### 3.1 Gateway 模式（推荐）

Gateway 是 Zvec 桌面端启动的本地代理，token 编码在 URL 路径里，无需请求头鉴权。

- 基址：`http://host:port/TOKEN/`
- API 前缀：`http://host:port/TOKEN/api/`

| 操作 | 请求 |
| --- | --- |
| 获取图库列表 | `GET /api/bootstrap` |
| 查询文件夹 | `GET /api/libraries/{library_id}/folders?query={名称}&offset=0&limit=500` |
| 提交任务 | `POST /api/jobs`，body 为**扁平对象**：`{"task_type": "...", 参数...}` |
| 查询任务状态 | `GET /api/jobs/{job_id}` |

### 3.2 Backend 直连模式

- 基址：`http://host:port`，请求头 `Authorization: Bearer {TOKEN}`
- 只有一个入口：`POST /v1/jobs`，body 为 `{"command": "...", "params": {...}}`
- 查询任务状态：`GET /v1/jobs/{job_id}`
- 「获取图库列表」在此模式下也是一个任务：`command = "libraries"`，`params = {}`

### 3.3 任务轮询协议

提交任务后响应中返回 `job.id`，然后：

- 每隔 1 秒 `GET jobs/{id}` 轮询一次，最长 300 秒；
- `job.status` 为 `succeeded` / `partial` / `needs_attention` → 成功；
- `job.status` 为 `failed` / `cancelled` → 失败；
- 任务结果在 `job.result` 中。

## 4. 集成所需的完整调用流程

### 4.1 获取 image_root 与 library_id

Gateway 模式：

```
GET /api/bootstrap
```

响应中取 `libraries[0]`（脚本固定使用第一个图库）：

```json
{
  "libraries": [
    { "id": "lib-xxxx", "image_root": "D:\\图片数据库", "...": "..." }
  ]
}
```

Backend 模式：提交 `libraries` 任务，结果在 `job.result.libraries[0]`。

### 4.2 由相对路径获取 folder_key

打标 API 需要 `folder_key`（后端对文件夹的内部标识），不能直接用路径。
获取方式：用文件夹名做模糊查询，再精确匹配相对路径。

```
GET /api/libraries/{library_id}/folders?query={URL编码的文件夹名}&offset=0&limit=500
```

响应结构（gateway 模式可能在 `result.folders` 或顶层 `folders`，两者都要兼容）：

```json
{
  "result": {
    "folders": [
      { "relative_folder": "作者A/写真集B", "folder_key": "..." }
    ]
  }
}
```

遍历数组，找 `relative_folder` 与目标 POSIX 相对路径**完全相等**的项，取其
`folder_key`。找不到即视为该文件夹尚未被索引（脚本记为错误并跳过）。

### 4.3 执行打标：manual_tag_batch 任务

```
POST /api/jobs     （gateway 模式）
```

Body（gateway 扁平格式）：

```json
{
  "task_type": "manual_tag_batch",
  "library_id": "lib-xxxx",
  "selection": {
    "mode": "folder",
    "folder_key": "上一步拿到的 folder_key",
    "include_subfolders": false
  },
  "operation": "add",
  "tags": ["标签1", "标签2"]
}
```

Backend 直连格式为 `{"command": "manual_tag_batch", "params": { ...上述除 task_type 外的字段... }}`。

- `operation` 支持 `add`（追加）和 `remove`（移除）；`--clean` 模式就是用 `remove`；
- `include_subfolders`：常规打标为 `false`（每个含图文件夹都会被单独扫到并单独打标），`--clean` 模式为 `true`；
- 提交后按 3.3 轮询直到终态。

### 4.4 集成到其他软件的最小流程

```
1. 读 gateway-url.txt 或让用户提供 gateway URL
2. GET api/bootstrap → image_root, library_id
3. 本地扫描 image_root，得到「含图片文件夹」列表（见第 6 节）
4. 对每个未处理文件夹：
   a. 推导标签（第 5 节），无标签则跳过
   b. GET api/libraries/{id}/folders?query=... → 精确匹配得到 folder_key
   c. POST api/jobs 提交 manual_tag_batch，轮询至终态
   d. 无论成功失败，把相对路径追加到状态文件
```

## 5. 标签推导算法

核心规则：**标签来自文件夹名，而不是文件内容。**

### 5.1 文件夹名清洗（clean_folder_name）

按顺序执行：

1. 删除半角方括号块 `[...]`（如 `[35P-417MB]`）；
2. 删除全角方括号块 `【...】`（UTF-8：`E3 80 90` 至 `E3 80 91`）及孤立的 `】`；
3. 删除尾部的 `_扩展名` 后缀（如 `_jpg`、`_png`，要求下划线后 ≤4 个纯 ASCII 字母数字）；
4. 去除首尾的空格/`-`/`_`，合并连续空格。

### 5.2 分段与过滤

清洗结果按**空格**分词，逐段处理：

1. **黑名单前缀匹配**：若某段以黑名单关键词开头（不区分大小写，`_strnicmp`），丢弃该段；
2. 保留段本身作为一个标签，并做符号剥离（`strip_symbols`：只保留 CJK 统一汉字、平/片假名、ASCII 字母数字，其余符号全部删除）；
3. 若段内中英文混合，额外提取其中**纯中文连续片段**作为第二个标签（去重）。

### 5.3 父目录回退

以下两种情况会改用**上一级父文件夹名**重新推导（只回退一层）：

- 清洗后的文件夹名是纯 ASCII（无 CJK 字符），说明名字没有信息量（如日期、序号目录）；
- 所有分段都被黑名单过滤或清洗后为空。

父目录也推导不出标签时，文件夹记为 untagged（仍写入状态文件，下次跳过）。

示例：

| 文件夹名 | 推导标签 |
| --- | --- |
| `二佐Nisa - 犬之眷 碧蓝航线 爱宕 旗袍 [110P-743MB]_jpg` | `二佐Nisa`、`二佐`、`犬之眷`、`碧蓝航线`、`爱宕`、`旗袍` |
| `图包/2`（子目录纯 ASCII） | 回退父目录 `图包` → 命中黑名单 → untagged |

## 6. 文件系统扫描规则

- 从 image_root 开始递归扫描（Windows `FindFirstFileW`，全 UTF-8/宽字符处理）；
- 跳过：以 `.` 开头的目录、`$RECYCLE.BIN`、`System Volume Information`；
- 一个目录只要**直接包含**任意一张图片即视为「含图片文件夹」（不看子目录）；
- 图片扩展名：`jpg jpeg png gif bmp webp tiff tif svg ico heic heif avif raw cr2 nef arw dng psd`；
- 相对路径统一转为 POSIX 风格（`/` 分隔），作为状态文件与 API 查询的键。

## 7. 配置文件

### 7.1 黑名单 `quick_tag_blacklist.json`（与 exe 同目录）

```json
{
  "blacklist": [
    "自摄", "自拍", "自撮", "4K", "4k", "映画",
    "Vol", "vol", "VOL", "图包", "加冕", "月份", "舰长"
  ]
}
```

- 加载时机：程序启动时读取，修改后无需重新编译；
- 上限 256 条；匹配方式为**前缀匹配且不区分大小写**；
- 文件不存在、超过 1MB、解析失败或数组为空时，自动回退到内置默认列表（即上面 13 条）。

### 7.2 Gateway 自动发现文件

`%LOCALAPPDATA%\zvec-image-search\gateway-url.txt`，单行内容：

```
http://127.0.0.1:10911/AbCdEfGh.../
```

由 Zvec 桌面端启动时写入；`--auto` 模式解析出 host、port 和 token 路径段。

### 7.3 状态文件 `quick_tag_state.txt`（与 exe 同目录）

- 每行一个已处理的 POSIX 相对路径；
- **成功、失败、untagged 都会写入**——想重跑失败文件夹，需先手动从文件里删掉对应行；
- 启动时全量读入内存做跳过判断（线性查找，数千条无压力）。

### 7.4 日志 `quick_tag_log.txt`（与 exe 同目录，追加模式）

每次运行写一段：开始时间、图库信息、总数；随后每文件夹一行
（`[UNTAGGED]` / `[ERROR] 路径 (原因)`，成功的文件夹不记录）；最后写汇总与耗时。

## 8. 移植注意事项

1. **编码**：全部按 UTF-8 处理（HTTP body、JSON、状态文件）；Windows 文件系统调用用宽字符 API 再转 UTF-8。中文标签必须 UTF-8 传输。
2. **轮询超时**：单任务最长 5 分钟；大文件夹的 manual_tag_batch 可能需要几十秒。
3. **folder_key 必须实时查询**：不要缓存或自行构造，后端重建索引后可能变化。
4. **幂等性**：`operation: add` 重复执行无害（已存在的标签不会重复添加），所以状态文件即使丢失，重跑也只会浪费 API 调用，不会产生脏数据。
5. **黑名单是前缀匹配**：关键词 `V` 会过滤所有以 `V/v` 开头的段（如 `Vol.3`、`VIP`），添加短关键词时注意误伤。
6. **`--clean` 是修复工具**：用于清除早期 bug 版本写入的元数据标签（页数/体积/扩展名），集成时可按需实现或忽略。
7. **依赖**：C 版本只依赖 Winsock（`ws2_32`）+ 随附 mjson；若用其他语言实现协议，仅需标准 HTTP 客户端和 JSON 库。
8. **Python 参考实现**：git 历史中还有功能子集相同的 `tools/quick_tag.py`
   （支持 `--add`/`--remove`/`--replace` 单文件夹模式），可作为协议实现的参考：
   `git show edfad48^:tools/quick_tag.py`。
