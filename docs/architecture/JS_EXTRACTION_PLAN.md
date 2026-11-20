# 模板内联 JS 提取方案 — 审计报告

## 1. 现状概览

### 1.1 模板与内联 JS 规模

| 模板 | 内联 JS 行数 | 权重 | 特征 |
|------|-------------|------|------|
| `library.html` | ~520 行 (L78-L603) | ⭐⭐⭐⭐⭐ 最大 | 资源库 CRUD + 标签云 + 分页 + 自动刷新 |
| `detail.html` | ~450 行 (L27-L476) | ⭐⭐⭐⭐⭐ | 专辑渲染 + 标签管理 + 收藏 + 下载任务 |
| `wishlist.html` | ~320 行 (L103-L424) | ⭐⭐⭐⭐ | 收藏 CRUD + 批量操作 + 导入导出 + 自动刷新 |
| `preview.html` | ~267 行 (L150-L418) | ⭐⭐⭐⭐ | 本地阅读器 + 分页缩略图 + 键盘导航 |
| `index.html` | ~0 行 | - | 已使用独立 .js 文件，无需提取 |

**总计：~1,557 行内联 JS** 需要提取。

### 1.2 现有独立 .js 文件（6 个）

| 文件 | 行数 | 作用域 | 职责 |
|------|------|--------|------|
| `utils.js` | 79 行 | 全局 | `escapeHtml`, `escapeHtmlAttr`, `apiFetch`, `confirmAction`, `openFolder`, `encodeJobId` |
| `sse-client.js` | 182 行 | 首页+下载 | `connectSSE`, `connectAllSSE`, `disconnectAllSSE`, `setSSECallbacks` |
| `home.js` | 17 行 | 首页 | 首页 SSE 连接初始化 |
| `search.js` | 490 行 | 搜索页 | 搜索结果渲染、分页、收藏、搜索历史 |
| `settings.js` | 488 行 | 设置页 | 设置表单加载/保存、验证、导入/导出 |
| `downloads.js` | 353 行 | 下载管理页 | 任务 CRUD、渲染、SSE 回调、批量操作 |

### 1.3 当前模板加载关系

```
base.html (inline: showToast + escapeHtml)
├── index.html → [utils.js + sse-client.js + home.js]
├── search.html → [search.js]                          ★ 缺 utils.js
├── settings.html → [settings.js]                      ★ 缺 utils.js
├── downloads.html → [utils.js + sse-client.js + downloads.js]
├── library.html → [inline ~520行]                     ★ 全部内联
├── detail.html → [inline ~450行]                      ★ 全部内联
├── wishlist.html → [inline ~320行]                    ★ 全部内联
└── preview.html → [inline ~267行]                     ★ 全部内联
```

---

## 2. 重复函数分析

### 2.1 `escapeHtml()` — 重复出现 5 处

| 位置 | 行号 | 是否可删除 |
|------|------|-----------|
| `base.html` inline | L101-L106 | ❌ 保留（被 `showToast` 依赖，且最先加载提供兜底） |
| `utils.js` | L13-L18 | ✅ 全局统一版，覆盖 base.html 版本 |
| `library.html` inline | L569-L574 | ✅ 删除，改用 `window.escapeHtml` |
| `detail.html` inline | L310-L315 | ✅ 删除，改用 `window.escapeHtml` |
| `wishlist.html` inline | L387-L392 | ✅ 删除，改用 `window.escapeHtml` |
| `preview.html` inline | L223-L228 | ✅ 删除，改用 `window.escapeHtml` |
| `search.js` | L478-L483 | ✅ 删除（IIFE 内局部函数），改用 `window.escapeHtml` |

### 2.2 `escapeHtmlAttr()` — 重复出现 4 处

| 位置 | 行号 | 是否可删除 |
|------|------|-----------|
| `utils.js` | L26-L34 | ✅ 全局统一版 |
| `library.html` inline | L576-L579 | ✅ 删除，改用 `window.escapeHtmlAttr` |
| `detail.html` inline | L317-L320 | ✅ 删除，改用 `window.escapeHtmlAttr` |
| `preview.html` inline | (无) | ✅ 已在提取文件中改用全局版 |
| `search.js` | L485-L488 | ✅ 删除，改用 `window.escapeHtmlAttr` |

### 2.3 `getStatusBadge()` / `statusMap` — 重复 3 处

| 位置 | 详情 |
|------|------|
| `library.html` inline | `getStatusBadge(status)` 自定义函数 L220-L230 |
| `wishlist.html` inline | `statusMap` 对象 L139-L145 |
| `search.js` | 没有直接定义，用模板渲染 |

⛔ 这三个模板使用了不同的状态映射方式，不能直接合并。留待 **第二阶段** 统一。

---

## 3. HTML `onclick` → 函数映射分析

### 3.1 `library.html` onclick 引用

| HTML onclick | 函数定义位置（当前） | 提取后 | 需暴露到 window |
|-------------|-------------------|--------|---------------|
| `reSyncAll()` | library.html L435 | library.js | ✅ |
| `refreshLibrary()` | library.html L581 | library.js | ✅ |
| `toggleTagCloud()` | library.html L542 | library.js | ✅ |
| `debouncedSearch()` | library.html L526 | library.js | ✅ |
| `loadLibrary(1)` | library.html L110 | library.js | ✅ |
| `clearFilters()` | library.html L325 | library.js | ✅ |
| `toggleTag('...')` | library.html L298 | library.js | ✅ |
| `removeCardTag('...')` | library.html L409 | library.js | ✅ |
| `toggleLibraryWishlist('...')` | library.html L464 | library.js | ✅ |

### 3.2 `wishlist.html` onclick 引用

| HTML onclick | 函数定义位置（当前） | 提取后 | 需暴露到 window |
|-------------|-------------------|--------|---------------|
| `exportWishlist()` | wishlist.html L359 | wishlist.js | ✅ |
| `importWishlistFile(this)` | wishlist.html L364 | wishlist.js | ✅ |
| `showImportModal()` | wishlist.html L307 | wishlist.js | ✅ |
| `refreshList()` | wishlist.html L394 | wishlist.js | ✅ |
| `searchWishlist()` | wishlist.html L350 | wishlist.js | ✅ |
| `toggleSelectAll()` | wishlist.html L235 | wishlist.js | ✅ |
| `batchDownload()` | wishlist.html L264 | wishlist.js | ✅ |
| `batchRemove()` | wishlist.html L285 | wishlist.js | ✅ |
| `downloadSingle('...')` | wishlist.html L199 | wishlist.js | ✅ |
| `removeSingle('...')` | wishlist.html L218 | wishlist.js | ✅ |
| `doImport()` | wishlist.html L313 | wishlist.js | ✅ |

### 3.3 `preview.html` onclick 引用

| HTML onclick | 函数定义位置（当前） | 提取后 | 需暴露到 window |
|-------------|-------------------|--------|---------------|
| `goPage(n)` | preview.html L268 | preview.js | ✅ |
| `jumpToPage()` | preview.html L353 | preview.js | ✅ |
| `onImageError()` | preview.html L361 | preview.js | ✅ |

### 3.4 `detail.html` — 无 onclick，全部使用 addEventListener

detail.html 没有 HTML onclick 属性，完全通过 `addEventListener` 绑定事件。提取只需要修改内部引用路径。

---

## 4. 模板 `{{ }}` 变量 → JS 注入分析

| 模板 | Jinja2 变量 | JS 中用法 | 提取方案 |
|------|------------|----------|---------|
| `detail.html` L31 | `{{ album_id|tojson }}` | `var albumId = ...` | 提取后的 .js 文件需从模板接收变量（方案见后） |
| `preview.html` L163 | `{{ album_id|tojson }}` | `var albumId = ...` | 同上 |
| `wishlist.html` | 无直接 JS 注入 | - | 无特殊处理 |
| `library.html` | 无直接 JS 注入 | - | 无特殊处理 |
| `search.html` | `{{ keyword or '' }}` | HTML value 属性（无需处理） | 无需处理 |

---

## 5. 提取方案

### 5.1 新建 4 个 .js 文件

#### 📄 `static/js/library.js` (~520 行)

从 library.html L78-L603 提取，IIFE 包装。

**包含函数（分组）：**

```
┌─ 全局状态 ──────────────────────────────
│  currentPage, pageSize, selectedTags, currentQuery,
│  currentStatus, currentSort, totalItems, allTags
│  _fetchController, _searchTimer, _refreshTimer
│
├─ 初始化 ────────────────────────────────
│  initLibrary()                          L97-L104
│
├─ 1. 资源库列表 ─────────────────────────
│  loadLibrary(page, options)             L110-L162
│  renderCards(items)                     L164-L218
│  getStatusBadge(status)                 L220-L230
│  renderPagination(total, page)          L232-L252
│
├─ 2. 标签云 ─────────────────────────────
│  loadTagCloud()                         L258-L267
│  renderTagCloud()                       L269-L296
│  toggleTag(tag)                         L298-L308
│  updateTagFilterHint()                  L310-L317
│  updateClearFiltersBtn()                L319-L323
│  clearFilters()                         L325-L336
│  toggleTagCloud()                       L542-L548
│
├─ 3. 统计信息 ───────────────────────────
│  loadStats()                            L342-L353
│
├─ 4. 标签管理 ───────────────────────────
│  addTag(albumId, tag)                   L359-L383
│  removeTag(albumId, tag)                L385-L406
│  window.removeCardTag(albumId, tag, e)  L409-L413
│  syncTags(albumId)                      L415-L433
│  reSyncAll()                            L435-L458
│
├─ 5. 收藏切换 ───────────────────────────
│  window.toggleLibraryWishlist(albumId, btn)  L464-L520
│
├─ 6. 搜索防抖 ───────────────────────────
│  debouncedSearch()                      L526-L532
│
├─ 7. 自动刷新 ───────────────────────────
│  startAutoRefresh()                     L556-L563
│
├─ 8. 工具/入口 ──────────────────────────
│  refreshLibrary()                       L581-L584
│  window.xxx 暴露 (L586-L595)            L586-L595
│  beforeunload 清理                      L597-L600
└──────────────────────────────────────────
```

**删除内联的 2 个函数**（已由 utils.js 提供）：
- `escapeHtml()` L569-L574 — 删除
- `escapeHtmlAttr()` L576-L579 — 删除

---

#### 📄 `static/js/detail.js` (~450 行)

从 detail.html L27-L476 提取，IIFE 包装。

**包含函数：**

```
┌─ 变量 ──────────────────────────────────
│  albumId ← 从模板注入                    L31
│  container, spinner, errorDiv            L32-L34
│
├─ 主流程 ────────────────────────────────
│  fetch /api/album/ + 15s 超时            L37-L71
│  loadAlbumTags(albumId)                  L49, L327-L344
│  异步标签同步                             L51-L61
│
├─ renderAlbum(album)                      L73-L175
│
├─ bindEvents(album)                       L177-L217
│  全选/章节下载事件绑定
│
├─ createDownloadJob(albumId, photoIds)    L219-L234
│
├─ 收藏检查/切换 (委托监听)                L238-L308
│
├─ 标签管理 ──────────────────────────────
│  loadAlbumTags(albumId)                  L327-L344
│  renderTagManager(albumId, tags)         L346-L380
│  addCustomTag(albumId)                   L383-L420
│  removeAlbumTag(albumId, tag)            L423-L442
│  syncAlbumTags(albumId)                  L445-L467
│
└─ window 暴露                             L470-L473
```

**删除内联函数：**
- `escapeHtml()` L310-L315 — 删除
- `escapeHtmlAttr()` L317-L320 — 删除

---

#### 📄 `static/js/wishlist.js` (~320 行)

从 wishlist.html L103-L424 提取（全局作用域，不用 IIFE 包装以保持简洁）。

**包含函数：**

```
┌─ 全局变量 ──────────────────────────────
│  currentPage, pageSize, allItems         L104-L106
│  currentQuery, currentSort               L107-L108
│
├─ 1. 加载收藏列表 ───────────────────────
│  loadWishlist(page)                      L111-L179
│  renderPagination(total, page)           L181-L196
│
├─ 2. 单个操作 ───────────────────────────
│  downloadSingle(albumId)                 L199-L216
│  removeSingle(albumId)                   L218-L232
│
├─ 3. 批量操作 ───────────────────────────
│  toggleSelectAll()                       L235-L241
│  getSelectedIds()                        L244-L251
│  updateBatchActions()                    L253-L262
│  batchDownload()                         L264-L283
│  batchRemove()                           L285-L304
│
├─ 4. 批量导入 ───────────────────────────
│  showImportModal()                       L307-L311
│  doImport()                              L313-L347
│
├─ 5. 搜索/导出 ──────────────────────────
│  searchWishlist()                        L350-L356
│  exportWishlist()                        L359-L361
│  importWishlistFile(input)               L364-L384
│
├─ 6. 自动刷新 ───────────────────────────
│  startAutoRefresh()                      L401-L408
│  refreshList()                           L394-L396
│  item-checkbox 监听                      L411-L413
│  beforeunload 清理                       L420-L422
│
└─ 初始化 ────────────────────────────────
│  loadWishlist(1); startAutoRefresh();    L416-L417
```

**删除内联函数：**
- `escapeHtml()` L387-L392 — 删除

---

#### 📄 `static/js/preview.js` (~267 行)

从 preview.html L150-L418 提取，IIFE 包装。

**包含函数：**

```
┌─ 状态 ──────────────────────────────────
│  pages, totalPages, currentPage, albumTitle
│  THUMBNAILS_PER_PAGE, thumbnailsLoaded   L155-L159
│  albumId ← 从模板注入                    L163
│  $() DOM 引用                            L166-L179
│
├─ 加载数据 ──────────────────────────────
│  loadPreview()                           L187-L221
│
├─ 渲染缩略图 ────────────────────────────
│  renderThumbnails()                      L231-L235
│  loadMoreThumbnails()                    L237-L265
│
├─ 翻页 ──────────────────────────────────
│  goPage(page)                            L268-L338
│  updateNavButtons()                      L340-L350
│  jumpToPage()                            L353-L358
│
├─ 图片错误处理 ──────────────────────────
│  onImageError()                          L361-L369
│  showImageError(msg)                     L371-L377
│
├─ 错误显示 ──────────────────────────────
│  showError(msg)                          L380-L385
│
├─ 键盘事件 ──────────────────────────────
│  keydown (← → Home End)                  L388-L405
│
├─ DOMContentLoaded 启动                   L408-L414
│
└─ window 暴露                             L182-L184
```

**删除内联函数：**
- `escapeHtml()` L223-L228 — 删除

---

### 5.2 模板加载依赖图（提取后）

```
base.html (inline: showToast + escapeHtml ← 保留不动)
├── index.html → [utils.js + sse-client.js + home.js]
├── search.html → [utils.js + search.js]                     ★ 新增 utils.js
├── settings.html → [utils.js + settings.js]                 ★ 新增 utils.js
├── downloads.html → [utils.js + sse-client.js + downloads.js]
├── library.html → [utils.js + library.js]                   ★ 替换内联
├── detail.html → [utils.js + detail.js]                     ★ 替换内联
├── wishlist.html → [utils.js + wishlist.js]                 ★ 替换内联
└── preview.html → [utils.js + preview.js]                   ★ 替换内联
```

### 5.3 `{{ album_id|tojson }}` 变量注入方案

对于 `detail.html` 和 `preview.html`，需要将 Jinja2 变量传给 JS 文件。**推荐方案**：

在模板 `<script>` 标签**之前**，用行内 `<script>` 定义变量，然后加载 .js 文件：

```html
{% block scripts_extra %}
<script>var albumId = {{ album_id|tojson }};</script>
<script src="/static/js/utils.js"></script>
<script src="/static/js/detail.js"></script>
{% endblock %}
```

该方案：
- 只有 1 行内联 JS（变量赋值），远少于当前的 450 行
- detail.js 和 preview.js 从 `window.albumId` 或直接用 `albumId` 读取（由于 script 是同步加载，变量在 .js 执行前已定义）
- 不引入 data-* 属性的复杂解析，性能无损耗

---

## 6. 验证清单

提取后必须验证以下路径不被破坏：

| 验证点 | 涉及模板 |
|--------|---------|
| 资源库列表加载、分页、筛选 | library |
| 标签云点击筛选、多选、清除 | library |
| 收藏图标切换（library 页面） | library |
| 添加/删除/同步标签（library 页面） | library |
| 专辑详情渲染（含章节列表） | detail |
| 全选/下载选中/下载全部 | detail |
| 标签管理（加载、添加、删除、同步） | detail |
| 收藏切换（detail 页面） | detail |
| 收藏列表加载、分页 | wishlist |
| 下载单个/批量下载/批量移除 | wishlist |
| 导入弹窗/批量导入/文件导入 | wishlist |
| 收藏导出 | wishlist |
| 图片加载、缩略图分页 | preview |
| 翻页、跳转、键盘导航 | preview |
| 图片加载失败、重试 | preview |

---

## 7. 分阶段实施步骤

### 第一阶段（低风险 — 提取 + 引用，保留所有逻辑）

**目标**：把内联 JS 完整搬到独立 .js 文件中，不修改任何逻辑，仅删除已在 utils.js 中存在的重复工具函数。

**步骤**：

1. **创建 `library.js`** — 从 library.html L78-L603 完整复制，IIFE 包装
   - 删除内联 `escapeHtml()` / `escapeHtmlAttr()`，改用 `window.escapeHtml` / `window.escapeHtmlAttr`
   - 修改 library.html：替换 `<script>...</script>` 为加载 `utils.js` + `library.js`

2. **创建 `detail.js`** — 从 detail.html L27-L476 完整复制，IIFE 包装
   - 删除内联 `escapeHtml()` / `escapeHtmlAttr()`
   - 在模板顶部保留 `<script>var albumId = {{ album_id|tojson }};</script>`（仅此一行内联）
   - 修改 detail.html：加载 `utils.js` + `detail.js`

3. **创建 `wishlist.js`** — 从 wishlist.html L103-L424 完整复制，全局作用域
   - 删除内联 `escapeHtml()`
   - 修改 wishlist.html：加载 `utils.js` + `wishlist.js`

4. **创建 `preview.js`** — 从 preview.html L150-L418 完整复制，IIFE 包装
   - 删除内联 `escapeHtml()`
   - 保留模板变量注入行
   - 修改 preview.html：加载 `utils.js` + `preview.js`

5. **补充 `search.html` 和 `settings.html` 缺失的 `utils.js`**（Phase 1 可选）
   - search.html 目前只加载 search.js，而 search.js 引用 `window.escapeHtml`（使用自己定义的局部版本）。如果局部 escapeHtml 删除，则需要 utils.js
   - 同理 settings.js 引用 `showToast`

**风险**：极低。只是把代码从一个文件复制到另一个，改了引用路径。每个文件独立验证即可回滚。

### 第二阶段（可选优化 — 重构与去重）

1. 统一 `getStatusBadge()` / 状态映射表
2. 将 `showToast` 从 base.html inline 移到 utils.js
3. 移除 base.html 中的 `escapeHtml`（确认 `showToast` 已移动后）
4. 考虑将 `downloads.js` 和 `wishlist.js` 中的通用下载功能（`downloadSingle` / `batchDownload`）合并
5. 类型安全增强：将 `var` 改为 `let`/`const`（可选）

---

## 8. 风险与回滚

### 风险

| 风险 | 概率 | 影响 | 应对 |
|------|------|------|------|
| `onclick` 函数未暴露到 window | 低 | 高（按钮点不动） | 验证清单逐项测试；提取时保留原有 window 暴露代码 |
| `escapeHtml` 在加载 utils.js 前被调用 | 低 | 中（XSS 风险） | base.html 保留内联版本作为兜底 |
| 模板变量 `{{ album_id|tojson }}` 在 JS 文件加载时 undefined | 中 | 高（页面无法加载） | 使用行内 `<script>` 先定义变量（见 5.3），保持同步顺序 |
| script 加载顺序错误（utils.js 没先加载） | 低 | 中 | 在模板中严格按顺序写 script 标签 |
| IIFE 内函数无法被 onclick 访问 | 低 | 高 | 每个 .js 文件底部保留 `window.funcName = funcName;` 暴露，提取时不要遗漏 |

### 回滚方案

```bash
# 每个文件都有对应的内联原始版本可恢复：
# 1. 删除对应 .js 文件
# 2. 从模板中移除 <script src="...">
# 3. 将原始的 <script>...</script> 内容（已通过 read_file 记录）恢复到模板中
```

---

## 9. 基线：当前 script 标签内容快照

为每份内联 JS 保留基准快照文件（用于直接替换回滚）：

```bash
# 已从以下文件中读取全文并记录：
# templates/library.html    L78-L603    → library.inline.js.bak
# templates/detail.html     L27-L476    → detail.inline.js.bak
# templates/wishlist.html   L103-L424   → wishlist.inline.js.bak
# templates/preview.html    L150-L418   → preview.inline.js.bak
```

建议在提取前先备份到 `docs/` 或 `static/js/bak/` 文件夹。
