# download_album_job 流水线拆分方案

> 审查日期: 2026-07-04
> 审查对象: `core/jm_service.py` → `download_album_job()` (L496–L730, ~234 行)
> 相关模块: `job_manager.py`, `progress.py`, `packer.py`, `database.py`, `path_guard.py`

---

## 1. 当前结构全景

```
download_album_job(job_id, album_id, photo_ids)
  │
  ├─ ① 初始化 (L503–513)    — tracker, failed_pages[], pending_images[]
  ├─ ② 状态标记 + 元数据获取 (L516–535)
  │     ├── db/job → "running"
  │     ├── client.get_album_detail(album_id)
  │     ├── 按 photo_ids 过滤章节, 计算 total_pages
  │     └── client.check_photo(photo) × N
  ├─ ③ 输出目录准备 (L537–542)
  ├─ ④ 首趟并行下载 (L562–581)   ← 核心 IO 阶段
  │     └── ThreadPoolExecutor(max_workers=photo_threads)
  │           └── _download_chapter() → _download_chapter_image() → _download_image_single_attempt()
  ├─ ⑤ 第二趟串行重试 (L583–606) ← 条件执行
  │     └── 新 client, 对 pending_images 逐一 _download_image_single_attempt(…, pass_num=2)
  ├─ ⑥ 目录整理 (L611–617)      — organize_download() [已有独立函数]
  ├─ ⑦ CBZ 打包 (L619–639)      — CbzPacker.pack() [已有独立类]
  ├─ ⑧ 元数据写入 (L641–659)    — upsert_album_meta + batch_sync_auto_tags
  ├─ ⑨ 最终状态写入 (L661–695)  — transition_job_status + tracker push
  ├─ ⚠ 异常处理 (L697–728)      — 统一的 except 处理
  └─ ⚠ finally 清理 (L729–730)  — close_client(client)
```

### 当前核心痛点

| 问题 | 表现 |
|------|------|
| **单一函数职责过重** | 1 个函数同时负责：状态管理、HTTP 元数据获取、并行调度、重试逻辑、目录操作、打包、DB 写入 |
| **共享可变状态耦合** | `done_pages=[0]`、`pending_images=[]`、`failed_pages=[]` 通过可变列表引用在多个阶段间透传，破坏阶段隔离 |
| **阶段边界不清晰** | 暂停检查 (`_should_stop`) 散布在下载循环中，但不在打包/整理前检验；异常处理与正常流程交织 |
| **线程安全问题** | 模块级 `_download_lock` 与局部 `_lock` 职责重叠；`_download_chapter` 内多图线程共享同一 `client` 对象（隐患） |
| **缺乏可测试性** | 234 行的单体函数几乎无法单元测试；必须 mock HTTP、DB、文件系统、并发调度 |

---

## 2. 可独立抽取的逻辑单元

以下是按**内聚边界**识别的可抽取单元:

| 编号 | 阶段 | 当前行号 | 是否已有独立封装 | IO 类型 | 可否独立测试 |
|------|------|----------|----------------|---------|-------------|
| P1 | 元数据获取 + 章节过滤 | L516–535 | ❌ 内联 | HTTP + DB | ✅ (mock client + DB) |
| P2 | 输出目录创建 | L537–542 | ❌ 内联 | 文件系统 | ✅ |
| P3 | 首趟并行下载 | L562–581 | ❌ 内联 (已复用 `_download_chapter`) | HTTP + 文件系统 | ⚠ 需 mock 并发 |
| P4 | 第二趟重试 | L583–606 | ❌ 内联 | HTTP + 文件系统 | ✅ |
| P5 | 目录整理 | L611–617 | ✅ `organize_download()` | 文件系统 | ✅ 已有 |
| P6 | CBZ 打包 | L619–639 | ✅ `CbzPacker.pack()` | 文件系统 | ✅ 已有 |
| P7 | 元数据写入 | L641–659 | ❌ 内联 | DB | ✅ |
| P8 | 最终状态写入 | L661–695 | ❌ 内联 | DB + SSE | ✅ |
| P9 | 异常处理 + 清理 | L697–730 | ❌ 内联 | 混合 | N/A (框架代码) |

---

## 3. 各阶段接口设计 (推荐方案)

### 核心数据结构 — `DownloadContext`

```python
@dataclass
class DownloadContext:
    # ── 构造时注入（不可变）──
    job_id: str
    album_id: str
    photo_ids: list[str]

    # ── 流程中逐步填充 ──
    album: Any = None               # jmcomic album 对象
    album_dir: Optional[Path] = None
    output_path: Optional[str] = None
    total_pages: int = 0
    done_pages: int = 0             # 原子计数器（AtomicInteger 替代可变列表）
    pending_images: list[dict] = field(default_factory=list)  # 首趟未完成的图片
    failed_pages: list[str] = field(default_factory=list)
    event_sink: Optional[ProgressTracker] = None
    pause_event: Optional[threading.Event] = None
```

### P1 — `fetch_and_filter_meta(ctx) → bool`

```
输入:  ctx.job_id, ctx.album_id, ctx.photo_ids
输出: ctx.album, ctx.total_pages, photos_to_download (内部临时)
内部:
  1. db/job → "running", wishlist → "downloading"
  2. client.get_album_detail(album_id)
  3. 按 photo_ids 过滤 → photos_to_download, total_pages
  4. 创建 album_dir + output_path → ctx.album_dir, ctx.output_path
  5. db.update_job(total_pages, output_path)
返回: False 表示致命错误（应终止整个流水线）
```

### P2 — `first_pass_download(ctx, photos_to_download) → bool`

```
输入:  ctx.*, photos_to_download
输出: ctx.done_pages, ctx.pending_images, ctx.failed_pages
内部:
  1. 读取 settings(photo_threads)
  2. 用 ThreadPoolExecutor 调度各章节
  3. 每个 chapter 内按 settings(image_threads) 并行下载
  4. 所有 _should_stop 检查嵌入下载循环
返回: False = 被取消/删除
```

### P3 — `retry_pass(ctx) → bool`

```
输入:  ctx.pending_images
输出: ctx.done_pages, ctx.failed_pages (更新)
内部:
  1. 条件: pending_images 为空直接跳过
  2. 新建 client
  3. 串行重试每张 pending 图片
  4. 每张前调用 _should_stop
返回: False = 被取消
```

### P4 — `organize_dir(ctx) → bool`

```
输入:  ctx.output_path, ctx.album
输出: ctx.output_path (可能更新)
内部:
  1. 读取 settings(organize_mode)
  2. 条件: "none" 直接跳过
  3. 调用 organize_download()
  4. 更新 ctx.output_path + db
返回: 总是 True（整理失败不阻塞流水线）
```

### P5 — `pack_cbz(ctx) → bool`

```
输入:  ctx.output_path
输出: 无（写文件）
内部:
  1. 读取 settings(auto_pack, delete_originals)
  2. 条件: "false" 直接跳过
  3. CbzPacker.pack() + 可选 rmtree()
  4. 失败时仅 log.warning（不阻塞）
返回: 总是 True
```

### P6 — `write_metadata(ctx) → bool`

```
输入:  ctx.album_id, ctx.album
输出: 无（写 DB）
内部:
  1. upsert_album_meta(album_id, title, author, cover_url)
  2. batch_sync_auto_tags(album_id, tags)
  3. 失败时仅 log.warning（不阻塞）
返回: 总是 True
```

### P7 — `finalize_status(ctx) → None`

```
输入:  ctx.done_pages, ctx.failed_pages, ctx.total_pages
输出: 无（写 DB + SSE）
内部:
  1. 检查最终 cancel 状态（防止覆盖）
  2. failed_pages 非空 → status="failed"
  3. 全部成功 → status="completed"
  4. tracker push + close
  5. wishlist 同步
```

---

## 4. 拆分方案对比

### 方案 A: 顺序流水线 (推荐 ★)

```
download_album_job()
  ├─ ctx = DownloadContext(job_id, album_id, photo_ids)
  ├─ if not fetch_and_filter_meta(ctx): return
  ├─ if not first_pass_download(ctx): return
  ├─ if not retry_pass(ctx): return
  ├─ organize_dir(ctx)          // 非阻塞
  ├─ pack_cbz(ctx)              // 非阻塞
  ├─ write_metadata(ctx)        // 非阻塞
  └─ finalize_status(ctx)
```

| 维度 | 评分 |
|------|------|
| 侵入性 | 低 — 保留现有函数签名，逐阶段提取 |
| 可测性 | 高 — 每阶段可独立 mock 测试 |
| 并发安全 | 中 — 用 `ctx.done_pages` (AtomicInteger) 替换 [0] 列表，消除锁混乱 |
| 向后兼容 | 高 — 外部接口不变 |
| 代码量影响 | -50 行（去重） |

### 方案 B: 策略模式 + Pipeline Builder

```python
pipeline = (
    PipelineBuilder()
    .add_stage("meta", FetchMetaStage())
    .add_stage("download", DownloadStage(use_pass=True))
    .add_stage("retry", RetryStage(max_timeout=600))
    .add_stage("optional", OptionalStage("organize", OrganizeStage()))
    .add_stage("optional", OptionalStage("pack", PackStage()))
    .add_stage("meta_writer", MetaWriterStage())
    .add_stage("finalizer", FinalizerStage())
    .build()
)
pipeline.execute(ctx)
```

| 维度 | 评分 |
|------|------|
| 侵入性 | 高 — 需定义 Stage 接口 + builder |
| 可扩展性 | 最高 — 可动态组合/替换阶段 |
| 可测性 | 最高 |
| 复杂度 | 中 — 额外抽象层 |
| 适用场景 | 未来需要可插拔下载后处理（AI 推荐标签、自定义 hook） |

### 方案 C: 事件驱动 + Worker 管道

```
PhotoFetchWorker → ChapterFilterWorker → DirPrepWorker
    → DownloadWorkerPool(chapter_workers)
    → RetryWorker → OrganizeWorker → PackWorker
    → MetaWriteWorker → FinalizeWorker
```

| 维度 | 评分 |
|------|------|
| 侵入性 | 最高 — 需重写整个调度 |
| 灵活性 | 最高 — 可动态伸缩 Worker |
| 可调试性 | 低 — 异步管道追踪困难 |
| 适用场景 | 当前架构不需要，过度设计 |

---

## 5. 推荐方案: 方案 A — 顺序流水线

### 理由

1. **渐进式重构** — 不影响当前功能，逐个阶段提取可随时合并
2. **风险最低** — 不改动 `ThreadPoolExecutor` 调度模型，只移动代码边界
3. **测试价值最高** — 当前主要痛点（无法单测）通过函数提取即可解决
4. **团队接受度** — 不需要新框架、新抽象概念

### 重构步骤建议

```
Step 1: 提取 P7 (finalize_status) 和 P6 (write_metadata)
           → 这两个纯 DB 操作，风险最低，快速见效
Step 2: 提取 P1 (fetch_and_filter_meta)
           → 将 client.get_album_detail 移出主函数
Step 3: 引入 DownloadContext + AtomicInteger 替换 done_pages[0]
           → 消除模块级 _download_lock 与局部 _lock 的冲突
Step 4: 提取 P4 (organize_dir) + P5 (pack_cbz) → 条件阶段
Step 5: 简化 download_album_job 为主控骨架
```

---

## 6. 数据依赖图 (DAG)

```
                      ┌─────────────────┐
                      │ job_id, album_id,│
                      │   photo_ids     │
                      └────────┬────────┘
                               │
                               ▼
                     ┌─────────────────┐
                     │  P1: FetchMeta  │──────── album
                     │  + Filter       │──────── total_pages
                     │  + DirPrep      │──────── output_path
                     └────────┬────────┘
                              │
                    ┌─────────▼──────────┐
                    │                    │
                    ▼                    ▼
         ┌──────────────────┐    ┌──────────────────┐
         │ P2: FirstPass    │    │  P2 内部:         │
         │ (章节池)          │    │_download_chapter  │
         │ done=, pending=, │    │ (图片池)           │
         │ failed=          │    │ done_pages (共享)   │
         └────────┬─────────┘    └──────────────────┘
                  │
          pending_images 非空?
                  │
         ┌────────▼─────────┐
         │ P3: RetryPass    │── done_pages, failed_pages
         └────────┬─────────┘
                  │
         ┌────────▼─────────┐
         │ P4: Organize(?)  │── output_path (可能更新)
         └────────┬─────────┘
                  │
         ┌────────▼─────────┐
         │ P5: PackCbz(?)   │
         └────────┬─────────┘
                  │
         ┌────────▼─────────┐
         │ P6: WriteMeta    │
         └────────┬─────────┘
                  │
         ┌────────▼─────────┐
         │ P7: Finalize     │── completed/failed
         └──────────────────┘
```

### 关键依赖关系

| 阶段 | 前置依赖 | 备注 |
|------|---------|------|
| P1 | 无 (入口) | 参数由调用方注入 |
| P2 | P1 (album, total_pages, output_path) | 强依赖 |
| P3 | P2 (pending_images, done_pages) | 条件依赖 |
| P4 | P3 (output_path, album) | output_path 可能被 P1 更新 |
| P5 | P4 (output_path 终值) | 条件执行 |
| P6 | P1 (album_id, album) | 与 P4/P5 无依赖，可提前 |
| P7 | P2/P3 (done_pages, failed_pages) | 必须最后执行 |

---

## 7. 线程安全影响分析

### 当前风险

1. **`_download_lock` (模块级) × `_lock` (局部) 职责重叠**
   - `_download_lock` 保护 `done_pages`/`failed_pages`/`pending_images`
   - `_lock` 保护 pause 期间的 DB 查询
   - 两个锁无层次关系 → 潜在死锁 (虽当前未触发)

2. **Client 对象在线程间共享**
   - `_download_chapter` 中创建 1 个 `client`，供该章节内多图线程共享
   - `requests.Session` 不是完全线程安全的(连接复用时可能 race)

### 方案 A 的改进

| 改动 | 效果 |
|------|------|
| `DownloadContext.done_pages: AtomicInteger` | 消除 `_download_lock` 在 `done_pages` 上的需求 |
| `DownloadContext._list_lock` 仅保护 `pending_images` / `failed_pages` | 缩小锁范围至 `append` 操作 |
| 每章节独立 `client` (已有) | 无需改动，已验证 |
| P3 `retry_pass` 使用独立 `client` (已有) | 无需改动 |
| P1 不再持锁 | 元数据获取是串行的 |

### 消除的竞态条件

| 场景 | 当前 | 方案 A 后 |
|------|------|----------|
| 两个 chapter 线程同时 `done_pages[0] += 1` | 用 `_download_lock` 保护 | `AtomicInteger.increment()` 无锁 |
| P2 结束后 P3 读取 `pending_images` | 隐式 happens-before (pool shutdown) | 同左 (不变) |
| P7 检查 cancel 时 P2 仍在跑 | 用 `final_job = db.get_job(job_id)` 检查 | 同左 (不变) |
| 异常 + cancel 竞争 | except 块内再次检查 `get_job()` | 同左 (不变) |

---

## 8. 风险点

| # | 风险 | 等级 | 缓解措施 |
|---|------|------|---------|
| 1 | `AtomicInteger` 替换 `[0]` 列表时遗漏共享引用 | 低 | 加类型注解 + 编译/运行时断言 |
| 2 | `_download_lock` 和 `_lock` 消除后遗留未发现的锁依赖 | 低 | 提取后运行现有下载场景回归 |
| 3 | `DownloadContext` 被多个线程同时写入不同字段 | 中 | 按阶段隔离写入上下文: P2 写 `done_pages`，P3 写 `pending_images`，P7 只读 |
| 4 | P6（WriteMeta）前置到 P2 后可能写入尚未完成的元数据 | 无 | P6 不依赖下载结果，可安全提前 |
| 5 | 整理/打包阶段未检查 cancel 状态 | 低 | P4/P5 是幂等的副作用操作，cancel 跳过后最终状态写入会自动处理 |
| 6 | 多阶段间的 `_should_stop` 检查可能重复 | 低 | 统一为 `ctx.should_stop()` → ctx 内持有 pause_event 引用 |

---

## 9. 总结

| 对比项 | 现状 | 方案 A (推荐) |
|--------|------|-------------|
| 函数长度 | ~234 行单体 | ~40 行主控骨架 |
| 可独立测试的阶段 | 0 | 7 (P1–P7 均可) |
| 锁的数量 | 2 个 (`_download_lock` + `_lock`) | 1 个 (仅保护 pending/failed 列表) |
| 共享可变状态 | `[0]` 列表 + 3 个外部列表 | `DownloadContext` + `AtomicInteger` |
| 阶段间耦合 | 隐式 (通过闭包变量) | 显式 (通过 Context 字段) |
| 新增抽象层级 | 0 | 1 (`DownloadContext` dataclass) |
| 风险 | 中 | 低 (增量提取, 每步可回滚) |

**推荐路径**: 方案 A 顺序流水线，Step 1→Step 5 逐阶段提取，每步提取后 CI 通过再向下进行。
