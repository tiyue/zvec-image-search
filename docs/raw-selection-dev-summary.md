# Zvec Sony A7M4 ARW 选片模块 — 开发进度交接文档

> 生成时间：2026-08-03 21:30
> 分支：`codex/collection`（本地领先 origin 9 个提交，未 push）
> 目的：供其他大模型/开发者无缝接续开发，无需回看本会话

---

## 1. 项目与需求文档位置

| 文件 | 说明 |
|------|------|
| `docs/raw-image-support-requirements.md` | **权威需求文档**（15 节正文 + 第 16 节开发进度/文件范围/已知限制） |
| `docs/spec.md` | 项目主规格（已同步 ARW 选片模块章节） |
| `AGENTS.md` | 项目规则（Spec 索引、发布矩阵） |

**需求核心**：在 Zvec-Webview Windows 源码版新增独立「ARW 选片」模块——项目化导入 Sony A7M4 ARW + JPG/PNG、快速浏览、双图对比、星级/色标筛选、统一导出、批量永久删除。不参与现有图库索引/搜索/推荐/模型调用；不改 Android、LAN、发布矩阵。

## 2. 已提交的工作（按 commit 顺序）

| Commit | 内容 |
|--------|------|
| `57f4210` | 需求文档基线 |
| `61b6b88` | **Phase 1-5**：后端 DB/导入/解码/服务编排 + WebView 路由 + 前端项目页/工作区 + 导出/删除 |
| `34da242` | **Phase 6-7**：磁盘缓存、胶片栏虚拟化、双图对比、永久删除确认模态、Sony 创意外观、rawpy 修复与锁定、`do_PATCH` 修复 |
| `0f6960c` | 后端测试套件（18 个测试）+ 永久删除引用清理 bug 修复 |
| `6f2833a` | **§8 调度器**：有界线程池 + single-flight + generation token（死锁修复） |

## 3. 当前执行到哪一步

- **已完成**：需求 §1–§12 的全部功能阶段、§8.2/8.3（有界并发 + single-flight）、§8.4 磁盘缓存、§11 删除安全、§14.1/14.2 后端测试。
- **浏览器端到端验证通过**：项目页创建/导入/进入工作区、384 张真实 A7M4 ARW 缩略图/预览渲染、创意外观 11 预置选择器（JPG 禁用）、虚拟化胶片栏。
- **尚未完成**：
  1. §8.1 前端侧「任务优先级调度 + 视口预取」的**前端集成**（后端 single-flight 已就绪，前端胶片栏为按需加载）
  2. §8.2 **并发矩阵基准**（8/12/16/24 等档位实测吞吐/内存，选择生产档位并记录）
  3. §13 **正式性能门禁**（20 组冷缓存首 24 张 P50/P95 ≤1s、前 100 张 ≤3s、1000 张 ≤20s、已缓存重启 ≤200ms 等）
  4. §14.3–14.6 测试（创意外观校准、UI/vitest、导出/删除边界）
  5. **创意外观需真实参考图校准**：当前 ST/PT/NT/VV/VV2/FL/IN/SH/BW/SE 为**初始参数近似**（`creative_look.py` 中 `_LOOK_PARAMS`），诚实标注"非 Sony 像素级复刻"；有相机参考 JPEG 后用 `calibration_note` 流程记录色差

## 4. 文件结构

```
image_vector_service/raw_selection/     # 后端（独立 SQLite，不触图库状态库）
  db.py            # 项目/资产/成员/评级/工作区状态/两阶段操作日志
  importer.py      # 递归扫描（跳过 reparse/symlink/._ 资源文件），批量事务，幂等
  decoder.py       # 三级渐进解码（内嵌JPEG缩略图/内嵌预览/完整解码），rawpy 可选
  creative_look.py # A7M4 创意外观（as_shot=内嵌JPEG；其他=完整解码+校准变换）
  cache.py         # 派生缓存（原子写入，key=path+size+mtime_ns+解码版本+外观版本）
  scheduler.py     # 有界线程池（ARW内嵌12/JPG12/PNG6/ARW完整3）+ single-flight + generation
  service.py       # 高层编排（项目CRUD/导入/评级/筛选/导出/永久删除/图像生成）
frontend/src/features/raw-selection/    # 前端
  RawSelectionPage.vue      # 项目列表（新建/重命名/删除/清缓存/导入文件夹）
  RawSelectionWorkspace.vue # 工作区（中央预览+虚拟化胶片栏+星级色标+筛选排序+外观+对比+删除模态）
  api.ts / types.ts / index.ts
zvec_webview/server.py       # 集成点：api/raw-selection/* 路由（含 do_PATCH 修复）
tests/test_raw_selection.py # 22 个测试（unittest 风格）
requirements.txt / requirements-lock.txt  # 已锁定 rawpy==0.27.0
```

**数据位置**：`%LOCALAPPDATA%/zvec-image-search/raw-selection/`（projects.sqlite3 + cache/）。

## 5. 验证状态

- 后端：`python -m unittest tests.test_raw_selection` → **22 个测试全过**（项目CRUD/导入幂等与跳过/评级/筛选/两阶段删除/缓存回环与失效/外观BW单色/解码/调度器4项）
- 前端：`vue-tsc --noEmit` 通过；`vite build` 通过（产物已入 `zvec_webview/frontend_dist/`）
- Python lint：`ruff check image_vector_service/raw_selection/ tests/test_raw_selection.py zvec_webview/server.py` 全过
- 真实 ARW（D:/105MSDCF，384 张）：缩略图 546ms / 内嵌预览 305ms / 完整解码 1498ms；磁盘缓存命中 0.5ms；VV/BW/SE 外观完整解码+变换 3s 级
- 浏览器端到端：已截图确认（build/verify-*.png）

## 6. 环境与已知坑（重要）

1. **主 venv 是 Python 3.14**（`.venv`），rawpy 0.27.0 才有 cp314 wheel；`requirements-lock.txt` 的 numpy 锁（2.3.5, py>=3.11）与 3.14 实际装到的 2.5.1 不一致——打包走 `.venv-build`（Python 3.12）时应一致。CI 构建环境需确认。
2. **启动源码版**：`.venv/Scripts/zvec-webview-preview.exe`；首次启动若报 `No module named 'webview'` 需 `uv pip install --python .venv/Scripts/python.exe pywebview`。gateway URL 写在 `%LOCALAPPDATA%/zvec-image-search/gateway-url.txt`（每次启动 token 变化）。
3. **浏览器验证**：文件夹选择依赖原生对话框，浏览器自动化无法操作 → 用 curl 直接调 API（`POST api/raw-selection/projects/{id}/import-folder`）再刷新页面。
4. **杀进程**：沙箱内 `taskkill` 不可用（报 CreateProcessAsUserW 267），用 `build/kill_zvec.py`（ctypes TerminateProcess）或 build/kill_webview2.py；该目录已被 .gitignore。
5. **single-flight 死锁教训**：`add_done_callback` 不能在持有锁时调用（future 同步完成时回调立即执行导致重入死锁）；已在 scheduler.py 修复并留注释。
6. **项目卡片"导入图片"入口**：§3.2 要求空项目页提供"导入图片"+"导入文件夹"两个入口，当前只有"导入文件夹"（文件级导入 API 已存在 `import_files`，前端入口未做）——接续时按需补。
7. **永久删除**：只删除 DB 中登记的精确文件路径，二次校验安全；`already_missing` 自动清理引用；两阶段日志 `operation_log` 支持崩溃恢复（`recover_pending_deletes` 启动时调用）。

## 7. 接续建议（按优先级）

1. §8.1 前端视口预取/优先级集成（可复用后端 single-flight，胶片栏预取可见区缩略图）
2. §13 性能门禁基准脚本（用 D:/105MSDCF 384 张，冷缓存 20 组 P50/P95）
3. §14.3–14.6 补充测试（含 vitest 前端测试，注意 `@vue/test-utils` 必须 2.2.7）
4. 创意外观真实参考图校准（需要 Sony 官方软件或相机输出的参考 JPEG，客观 ΔE + 人工检查，结果写回 `docs/raw-image-support-requirements.md` 第 16 节）
