# 下载链路审查 + 快速跳转场景全链路稳定性分析

> 审查日期: 2026-07-07
> 项目: 禁漫下载搜索插件

---

## 一、下载链路逐层审查

### 1.1 整体架构

```
用户点击"下载" → POST /api/jobs (15s 超时) → job_manager 入队
  → schedule_next() → 后台线程 → download_album_job()
    → ① get_album_detail (HTTP 获取元数据) — ❌ 无 timeout 包装
    → ② check_photo × N (HTTP 逐章检查页数) — ❌ 无 timeout 包装
    → ③ 首趟: ThreadPoolExecutor(photo_threads) → _download_chapter
      → 每张图 get_client(shared=False) → _download_image_single_attempt(120s)
    → ④ 末趟重试: 串行 _download_image_single_attempt(600s)
    → ⑤ organize + pack + metadata + finalize
```

### 1.2 前端"下载"按钮

| 位置 | 函数 | 超时 | 风险 |
|------|------|------|------|
| search.js:280 | `quickDownload()` → `fetchWithTimeout(15000)` | ✅ 15s | 无 — 只 POST 入队，不阻塞 |
| detail.js:212 | `createDownloadJob()` → `fetchWithTimeout(15000)` | ✅ 15s | 同上 |

**结论**: 前端下载按钮安全。后端入队操作是轻量的 DB 写入 + 队列调度，15s 足够。

### 1.3 后端下载线程 — 关键路径

#### 第一阶段: 元数据获取 (jm_service.py:619-629)

```python
client, _ = get_client(shared=False)
album = client.get_album_detail(album_id)           # ⚠️ NO timeout wrapper
for photo in album:
    if str(photo.photo_id) in photo_ids or not photo_ids:
        client.check_photo(photo)                    # ⚠️ NO timeout wrapper
```

对比前台搜索/详情函数:
- `search_albums()` → `pool.submit(client.search...)` + `fut.result(timeout=25)` ✅
- `get_album_detail()` → `pool.submit(client.get_album_detail...)` + `fut.result(timeout=20)` ✅

**下载** 的 `get_album_detail` 和 `check_photo` **没有任何显式超时包装**。虽然 jmcomic client 的 `postman.meta_data.timeout=30` 设了单次 HTTP 超时，但 `get_album_detail()` 内部可能发起多个 HTTP 请求，如果某步 hang 住但没触达 HTTP 超时（如 DNS 解析 hang 在 curl 内部），下载线程会永久阻塞。

#### 第二阶段: 图片下载 (jm_service.py:542-559, 564-587)

```python
# 每张图都创建独立 client
img_client, _ = get_client(shared=False)  # 新 client + 新连接
try:
    client.download_image(img_url, img_save_path, scramble_id, decode_image=True)
finally:
    close_client(img_client)
```

- ✅ 每张图独立 client，无 HTTP 非线程安全问题
- ✅ 首趟 `_FIRST_PASS_TIMEOUT=120s` 超时
- ✅ 末趟 `_RETRY_TIMEOUT=600s` 超时
- ⚠️ 默认 `image_threads=20` → 一个章节内 20 张图并发下载 → 20 个独立 TCP 连接

#### 1.4 `image_threads=20` 默认值风险

`settings.py:39` 默认值 `"image_threads": "20"`。对于有 30+ 页的章节，意味着同时建立 20+ 个 HTTPS 连接到 jmcomic CDN。

影响:
- 20 次独立的 TLS 握手（即便复用 curl_cffi session，但每个 `get_client(shared=False)` 创建全新的 client）
- 本地临时端口消耗（Windows 默认 ephemeral port 范围有限）
- 目标 CDN 可能触发速率限制（HTTP 429/Connection RST）
- 实际验证结论: 在慢速网络下，降低到 3-5 有明显改善

---

## 二、快速跳转场景分析

### 2.1 每次搜索页加载 → 发几个 fetch？

| fetch | 端点 | 超时 | 说明 |
|-------|------|------|------|
| F1 | `GET /api/search?q=...` | 30s | 主搜索请求 |
| F2 | `POST /api/wishlist/check` | 15s | 在 F1 成功后触发，批量查收藏状态 |
| F3 | `GET /api/search-history` | 15s | 独立加载搜索历史 |

**每次搜索页: 2-3 个 fetch**（F2 等待 F1 完成后才发）

### 2.2 每次详情页加载 → 发几个 fetch？

| fetch | 端点 | 超时 | 说明 |
|-------|------|------|------|
| F4 | `GET /api/album/<id>` | 30s | 主专辑详情，包含所有章节 |
| F5 | `GET /api/library/<id>/tags` | 15s | F4 成功后触发 |
| F6 | `POST /api/library/<id>/tags/sync` | 15s | F4 成功后异步触发 |
| F7 | `GET /api/wishlist/<id>` | 15s | 与 F4 并行独立触发 |

**每次详情页: 最多 4 个 fetch**（F5/F6/F7 在 F4 成功后并行发）

### 2.3 bfcache 回来后的行为

#### 搜索页 bfcache (search.js:495-514)
- ✅ 有 `pageshow` 监听，`event.persisted` 时重新获取 DOM 引用 + 重发 `fetchResults()`
- 这意味着从详情页按"返回"到搜索页 → **重新发送 F1/F2/F3**
- 好处: 搜索页恢复到最新状态（包括刷新 wishlist 状态）
- 代价: 多一次 HTTP API 调用；之前的 DOM 被 spinner 替换再重建

#### 详情页 bfcache
- ❌ 无 `pageshow` 处理
- IIFE 只会在首次页面加载时执行一次 (bfcache 保留 JS 状态)
- **bfcache 回来的详情页不会再发请求** — 这实际上是正确的行为

### 2.4 快速标签跳转的请求瀑布

```
时间线 →
┌─────────────┬─────────────┬─────────────┬─────────────┬─────────────┐
│ 搜索页       │ 详情页       │ bfcache 搜索  │ 详情页       │ bfcache 搜索  │
│             │             │             │             │             │
│ F1 ─────30s─│             │             │             │             │
│ F3 ───15s───│             │             │             │             │
│             │ F4 ────30s──│             │             │             │
│             │ F7 ──15s───│             │             │             │
│             │ F5/F6 ──15s│             │             │             │
│             │             │ F1 ────30s──│             │             │
│             │             │ F3 ──15s───│             │             │
│             │             │             │ F4 ────30s──│             │
│             │             │             │ F7 ──15s───│             │
│             │             │             │             │ F1 ────30s──│
│             │             │             │             │ F3 ──15s───│
└─────────────┴─────────────┴─────────────┴─────────────┴─────────────┘
```

用户快速操作时，同一时刻可能有 **4-6 个并发 outbound HTTP 请求** 到 jmcomic API。全部通过全局共享的 `get_client(shared=True)` 客户端发出。如果 curl_cffi_session 内部连接池大小为 N（通常 6-10），超过的请求会在客户端等待空闲连接。

### 2.5 快速跳转不会卡死的保证

| 层面 | 当前状态 | 判断 |
|------|---------|------|
| 浏览器连接池 | ✅ `fetchWithTimeout`, Chrome 同域名 6 连接限制 | 安全 |
| Flask/Waitress 线程 | ✅ 128 线程，普通操作微秒级 | 安全 |
| curl_cffi 连接池 | ⚠️ 默认池大小有限，并发可能排队 | **潜在瓶颈** |
| 全局 client 锁 | ✅ `_option_lock` 仅初始化时获取 | 安全 |
| DB 操作 | ✅ SQLite WAL 模式 + 轻量查询 | 安全 |

---

## 三、终极问题: 最薄弱的环节是什么？

### 🥇 最薄弱环节: `download_album_job()` 中 `get_album_detail` / `check_photo` 缺少超时兜底

**文件**: `core/jm_service.py:619-629`

```python
album = client.get_album_detail(album_id)        # ← 无 timeout 包装
check_photo(photo)                                 # ← 无 timeout 包装
```

对比前台的路由函数（`search_albums` 有 `fut.result(timeout=25)`, `get_album_detail` 有 `fut.result(timeout=20)`），下载线程的元数据获取阶段**没有任何显式超时机制**。虽然 `curl_cffi_session` 配置了 `timeout=30`，但 jmcomic 的 `get_album_detail()` 内部会发起多个 HTTP 调用（获取 album 元数据 + 各个 photo 信息），如果其中某一步 hang 在 curl 内部（DNS、TCP 连接等层面），`postman.meta_data.timeout` 只保护单个 HTTP 请求的整体耗时，不保护多步组合的耗时。

**场景**: 假设 18comic CDN 响应变慢（每步 HTTP 需 15 秒），`get_album_detail()` 内部发起 3 个请求共 45 秒 + `check_photo` × 10 章节各 ~10 秒 = 总计可能 **2 分钟以上**。此时下载线程一直阻塞，又因为 `max_running_jobs=1`（默认），后续下载任务全部排队。

### 🥈 第二薄弱: 关闭 `FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN` 导致的域名僵化

**文件**: `app.py:180`

```python
JmModuleConfig.FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN = False
```

这是个**双刃剑抉择**:
- ✅ 避免了启动时自动请求域名更新导致的 hang
- ❌ 如果配置的 5 个 CDN 域名全部过期或失效，整个应用**完全不可用**，且没有自动恢复机制
- ❌ 需要用户手动重启或更新域名列表才能恢复

考虑到 jmcomic 的 CDN 域名经常变动，这是最隐蔽的单点故障。

### 🥉 第三薄弱: `image_threads=20` 默认值过于激进

**文件**: `core/settings.py:39`

20 个并发 `get_client(shared=False)` → 20 个独立 HTTP session → 20 组 TLS 握手 + 20 个 TCP 连接。在按需 CDN 场景下:
- 很多请求会竞争同一域名的连接
- 端侧可能发送 TCP RST 或限流
- Windows ephemeral port 可能耗尽

建议将默认值降到 3-5。

---

## 四、要让「快速标签跳转」永不卡死, 还需要改什么？

### P0 — 必须改（防止下载线程永久 hang）

1. **给 `download_album_job` 的元数据获取加 timeout 包装**

```python
# 在 jm_service.py 中，参考 search_albums 的模式
pool = ThreadPoolExecutor(max_workers=1)
fut = pool.submit(client.get_album_detail, album_id)
try:
    album = fut.result(timeout=60)  # 60s 超时
except TimeoutError:
    log.error(f"获取 album 详情超时 album_id={album_id}")
    # 标记任务失败, return
```

同样给 `client.check_photo(photo)` 加超时保护。

### P1 — 必须改（恢复域名容灾能力）

2. **引入域名可用性探活或至少 Auto-Update 降级方案**

不要完全关闭 `FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN`。改为:
- 启动时不阻塞等待域名更新（用线程异步更新）
- 或者定期（每 24h）在后台静默刷新域名列表
- 或者提供一个手动"刷新域名"按钮

### P2 — 建议改（提升并发体验）

3. **`image_threads` 默认值从 20 降至 3-5**

4. **前端 search.js bfcache 恢复时优化: 不用 spinner 替换老结果**

当前 `pageshow` 中调用 `fetchResults()` 会用 spinner 替换已有结果 HTML，如果网络慢会有空白闪烁。可以改为:
- 先保留旧结果
- 后台 fetch，成功后只替换结果部分
- 或使用 `if-none-match` / ETag 减少带宽

5. **详情页 tag 同步改为仅首次加载时执行**

每次打开详情页都触发 `POST /api/library/<id>/tags/sync`（15s 超时），如果用户快速开关同一个专辑，重复的 sync 是浪费。加个 `_synced_albums` Set 去重。

---

## 五、结论汇总

| 环节 | 稳定性评分 | 说明 |
|------|-----------|------|
| 前端搜索页 fetch | ⭐⭐⭐⭐⭐ | 30s 超时 + 友好错误提示 |
| 前端详情页 fetch | ⭐⭐⭐⭐⭐ | 30s 超时 + 静默降级 |
| 前端下载按钮 | ⭐⭐⭐⭐⭐ | 15s 超时，只入队不阻塞 |
| 后端搜索 API | ⭐⭐⭐⭐ | 25s 超时，但阻塞 Waitress 线程 |
| 后端详情 API | ⭐⭐⭐⭐ | 20s 超时 + LRU 缓存 |
| **下载线程元数据获取** | ⭐⭐ | **缺少超时兜底 — 可导致永久阻塞** |
| 下载图片首趟 | ⭐⭐⭐⭐ | 120s 超时 + 独立 client |
| 下载图片重试趟 | ⭐⭐⭐⭐ | 600s 超时 |
| 域名容灾 | ⭐ | **AUTO_UPDATE 关闭后完全依赖硬编码域名** |
| bfcache 快速跳转 | ⭐⭐⭐⭐ | 有 pageshow 处理，但重复搜索请求 |

**最薄弱环节**: 🏆 `download_album_job()` 中 `get_album_detail` 和 `check_photo` 缺少超时兜底（P0），叠加 `FLAG_AUTO_UPDATE_DOMAIN = False` 导致的域名僵化（P1），是当前系统最大的稳定性风险。

要实现在快速标签跳转场景下**永不卡死**，需要:
1. **P0**: 下载线程元数据获取加 timeout（参考 `search_albums` 模式）
2. **P1**: 域名更新机制从"完全关闭"改为"后台异步 + 定期刷新"
3. **P2**: `image_threads` 默认值调低; bfcache 不闪 spinner
