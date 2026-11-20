# 跨页面全链路分析和 bfcache 兼容性审查

## 目录
1. [页面加载依赖链](#1-页面加载依赖链)
2. [命名冲突和变量泄漏](#2-命名冲突和变量泄漏)
3. [bfcache 兼容性分析](#3-bfcache-兼容性分析)
4. [服务器端状态检查](#4-服务器端状态检查)
5. [根因判断](#5-根因判断)
6. [修复建议](#6-修复建议)

---

## 1. 页面加载依赖链

### 各页面 JS 加载顺序

#### 搜索页（search.html）
```
base.html:
  1. /static/vendor/bootstrap/js/bootstrap.bundle.min.js
  2. <inline> function escapeHtml() / window.showToast

scripts_extra block:
  3. /static/js/utils.js          → window.escapeHtml / apiFetch / openFolder
  4. /static/js/search.js         → IIFE: 搜索+渲染+分页
```

#### 详情页（detail.html）
```
base.html:
  1. bootstrap.bundle.min.js
  2. inline escapeHtml / showToast

scripts_extra block:
  3. <inline> var albumId = <tojson>       ← 全局变量
  4. /static/js/utils.js                    ← window.escapeHtml / apiFetch
  5. /static/js/detail.js                   ← IIFE(albumId)
```

#### 首页（index.html）
```
  ... (同 base.html)
  3. /static/js/utils.js
  4. /static/js/sse-client.js     ← SSE 连接管理 + beforeunload
  5. /static/js/home.js
```

#### 下载管理（downloads.html）
```
  ... (同 base.html)
  3. /static/js/utils.js
  4. /static/js/sse-client.js     ← SSE 连接管理 + beforeunload
  5. /static/js/downloads.js      ← also has beforeunload
```

### 静态资源缓存策略

`app.py:199` — `SEND_FILE_MAX_AGE_DEFAULT = 31536000`（1 年强缓存）

所有 `/static/` 下的 JS/CSS 文件被浏览器缓存 1 年。**没有版本号或 hash 指纹**——如果 JS 文件更新，用户浏览器不会自动拉取新版本。

---

## 2. 命名冲突和变量泄漏

### 2.1 escapeHtml 的三重定义

| 来源 | 定义方式 | 作用域 |
|---|---|---|
| `base.html` L101 | `function escapeHtml(str) {…}` | 全局函数 |
| `utils.js` L13 | `window.escapeHtml = function (str) {…}` | `window.escapeHtml` |
| `search.js` L478 | `function escapeHtml(text) {…}` (IIFE 内部) | IIFE 局部 |

**谁覆盖谁？**

加载顺序决定的覆盖链：
```
base.html function escapeHtml         → 全局 escapeHtml 和 window.escapeHtml（两者等价）
utils.js  window.escapeHtml = fn      → 覆盖 window.escapeHtml
search.js (IIFE) 内部 escapeHtml      → IIFE 局部变量，不影响全局
```

**结论：`utils.js` 最终覆盖 `base.html` 的版本。实现代码完全一致，无功能影响。**

### 2.2 search.js IIFE 内部调用 `escapeHtml()` 用的是哪个？

在 search.js 内部（如 L33 `showGlobalAlert`、L109 `err.message`），`escapeHtml()` 解析到的是：

- **IIFE 内部的 `function escapeHtml`（L478）**——因为它是函数声明（hoisted），在同一 IIFE 的词法作用域内优先于全局变量。
- 这是 **正确的行为**，因为 IIFE 的设计意图就是自包含。

### 2.3 detail.js 调用 `escapeHtml` 用的是哪个？

detail.js 用的是 `window.escapeHtml()`（L60、L61、L69、L72、L74、L77 等显式调用），这是 `utils.js` 覆盖后的版本。**正确**。

### 2.4 `var` 全局变量泄漏

搜索页上存在以下全局变量泄漏（从 IIFE 显式导出到 `window`）：
- `window.searchGoTo` (search.js L245)
- `window.quickDownload` (search.js L249)
- `window.toggleWishlist` (search.js L293)
- `window.updateWishlistButtons` (search.js L347)
- `window.searchHistoryClick` (search.js L424)
- `window.deleteSearchHistory` (search.js L430)
- `window.clearSearchHistory` (search.js L447)

详情页：
- `window.addCustomTag` (detail.js L436)
- `window.removeAlbumTag` (detail.js L437)
- `window.syncAlbumTags` (detail.js L438)
- `window.loadAlbumTags` (detail.js L439)

这些是 **有意暴露** 给 `onclick=` 属性使用的。没有意外的 `var` 全局变量泄漏，因为各页面 JS 都用 IIFE 包裹。

### 2.5 关键冲突：`var albumId` 全局变量

**detail.html L27**: `<script>var albumId = {{ album_id|tojson }};</script>`

这是一个 **全局 `var` 声明**，会挂在 `window` 上。它只存在于 detail 页面的作用域。如果用户从 search 页面导航到 detail，`albumId` 只在 detail 页面生命周期内存在，导航回 search 时该变量随页面卸载而消失。**但若 bfcache 介入**（见下文 3.3），情况会不同。

---

## 3. bfcache 兼容性分析

### 3.1 当前状态速查

| 因素 | 搜索页 (search) | 详情页 (detail) | 首页 (index) | 下载管理 (downloads) |
|---|---|---|---|---|
| `beforeunload` 监听器 | ❌ 无 | ❌ 无 | ✅ **有** (sse-client.js) | ✅ **有** (sse-client.js + downloads.js) |
| `Cache-Control: no-store` | ❌ 未设置 | ❌ 未设置 | ❌ 未设置 | ❌ 未设置 |
| `unload` 监听器 | ❌ 无 | ❌ 无 | ❌ 无 | ❌ 无 |
| `pageshow` 事件处理 | ❌ 无 | ❌ 无 | ❌ 无 | ❌ 无 |
| **bfcache 可用性** | **✅ 可用** | **✅ 可用** | ❌ 被 beforeunload 阻止 | ❌ 被 beforeunload 阻止 |

**关键发现：搜索页和详情页没有 beforeunload + 没有 no-store → bfcache 可用。**

### 3.2 `beforeunload` 阻止 bfcache 的页面

以下文件注册了 `beforeunload` 事件监听器，该事件会 **阻止浏览器使用 bfcache**：

1. **`sse-client.js` L178** — `window.addEventListener('beforeunload', disconnectAllSSE)` — 加载于首页和下载管理页
2. **`downloads.js` L350** — `window.addEventListener('beforeunload', clearInterval(pollTimer))`
3. **`library.js` L513** — `window.addEventListener('beforeunload', clearInterval/clearTimeout)`
4. **`wishlist.js` L312** — `window.addEventListener('beforeunload', clearInterval(_refreshTimer))`

对搜索页和详情页的结论：**它们没有被 beforeunload 阻止 bfcache**。

### 3.3 bfcache 启用时的关键冻结/恢复问题

#### 3.3.1 search.js IIFE 的 DOM 引用缓存

search.js L8-L14 在 IIFE 执行时快照了 DOM 元素：
```javascript
var searchForm = document.getElementById('search-form');
var searchInput = document.getElementById('search-input');
var searchBtn = document.getElementById('search-btn');
var resultsDiv = document.getElementById('search-results');
var paginationDiv = document.getElementById('pagination');
var statusDiv = document.getElementById('search-status');
```

**bfcache 恢复的行为**：bfcache 保存完整的 DOM 树，恢复后这些 `getElementById` 缓存的引用仍然指向同一个 DOM 元素。**不会导致卡死**。

#### 3.3.2 detail.js 的 AbortController

detail.js L15-16:
```javascript
var controller = new AbortController();
var timeoutId = setTimeout(function () { controller.abort(); }, 15000);
```

**bfcache 恢复的行为**：

| 场景 | behavior |
|---|---|
| 用户导航离开时 fetch 已结束 | AbortController 已闲置，恢复后无影响 |
| 用户导航离开时 fetch 未结束 | 浏览器可能继续或暂停请求；恢复后 promise 继续。但 `controller.abort()` 的 timeout 在冻结期间可能不触发，恢复后可能立即触发 → **可能导致超时错误** |

**风险等级：低**。因为用户通常在 detail 页面看到完整内容后才导航离开，此时 fetch 早已结束。

#### 3.3.3 bfcache 恢复后 `click` 绑定 vs `onclick` 属性

搜索页的按钮事件有两种绑定方式：

**事件监听器绑定（正确）**：
- L367: `searchBtn.addEventListener('click', doSearch)`
- L370-372: `searchForm.addEventListener('submit', ...)`
- L374-379: `searchInput.addEventListener('keydown', ...)`

这些事件监听器 **绑定在 DOM 元素上，和 DOM 一起被 bfcache 保存和恢复**。恢复后正常工作。

**`onclick` 内联属性（有隐患）**：
搜索结果 DOM 通过 `innerHTML` 注入，使用 `onclick="quickDownload(this)"` 和 `onclick="toggleWishlist(this)"`。这些属性在 bfcache 恢复后 **仍然存在于 DOM 中**，`window.quickDownload` 和 `window.toggleWishlist` 也作为 IIFE 导出的全局变量被保存。**不会导致卡死**。

### 3.4 SSE 连接和 bfcache

搜索页和详情页 **不加载 sse-client.js**，所以没有 SSE 连接问题。

但首页和下载管理页有 SSE（被 beforeunload 阻止了 bfcache，所以不会出现冻结中的 SSE 连接问题）。

### 3.5 bfcache 导致的关键隐患

#### 1. 搜索页的 `autoSearchFromUrl` 不会在 bfcache 恢复时执行

search.js L463-471:
```javascript
(function autoSearchFromUrl() {
    var kw = searchInput ? searchInput.value.trim() : '';
    if (kw) {
        currentQuery = kw;
        currentSort = sortSelect ? sortSelect.value : 'latest';
        currentPageSize = parseInt(pageSizeSelect ? pageSizeSelect.value : '20') || 20;
        fetchResults();
    }
})();
```

**问题**：这是一个 IIFE，只在 **首次 JS 执行**（即首次页面加载）时运行。bfcache 恢复时 **JS 不重新执行**，所以它不会运行。

**影响**：如果用户通过 `/search?keyword=xxx` 链接进入搜索页，自动搜索会触发。但如果转了一圈（搜索→详情→返回搜索→bfcache 恢复），用户看到的仍然是上次搜索的结果。点击"详情"是 `<a>` 链接，正常工作。**这不会导致卡死，但会导致用户体验不一致**。

#### 2. 详情页的 `var albumId` 污染全局

detail.html L27 在全局声明 `var albumId`。如果 bfcache 恢复的是搜索页，这个变量不存在于搜索页的 JS 上下文中，**无影响**。

但如果 bfcache 恢复的是详情页本身（用户前进/后退），`var albumId` 仍然存在且值正确。**无影响**。

---

## 4. 服务器端状态检查

### 4.1 Flask `before_request` / `after_request` 钩子

```python
@app.before_request
def _init_request_id():
    set_request_id()          # 每个请求分配 request_id，无状态泄漏

@app.before_request
def _start_timer():
    request._request_start_time = _time.time()  # 请求级属性

@app.after_request
def _log_request(response):
    # 仅记录日志，不修改 response
    return response
```

**结论：没有状态泄漏。** `before_request` 设置的是 `request` 对象级属性，不跨请求共享。`after_request` 只读不写。

### 4.2 SSE 连接管理

```python
# api_jobs.py L151-184
@api_jobs_bp.get("/api/jobs/<job_id>/events")
def job_events(job_id):
    # 通过 stream_with_context 提供 SSE
    client_queue = tracker.subscribe()
    ...
        for event_line in tracker.iter_events(client_queue):
            ...
    ...
        tracker.unsubscribe(client_queue)
```

**SSE 仅在 active 的下载任务页面（首页/下载管理页）有连接**。搜索页和详情页没有 SSE 连接。

SSE 与 bfcache 的交叉问题：因为这些页面有 `beforeunload` 阻止了 bfcache，所以不存在 bfcache 导致的 SSE 悬挂问题。**正常**。

### 4.3 页面缓存控制

**当前没有任何页面设置 `Cache-Control: no-store` 或 `no-cache`。**

唯一设置了 `Cache-Control` 的是 `api_album.py` L78 的封面重定向响应，这不会影响页面是否被 bfcache。

**这是一个修复点**：如果要禁止 bfcache，需要在 Flask 响应中添加 `Cache-Control: no-store`。

---

## 5. 根因判断

### 5.1 "搜索页第二次访问时点击搜索结果会卡死" 的排除分析

| 怀疑原因 | 分析 | 是否根因 |
|---|---|---|
| bfcache 导致 JS 状态错误 | 搜索页无 beforeunload，bfcache 可用。但 bfcache 恢复后 DOM 引用、事件监听都正常 | ❌ 不直接导致卡死 |
| 命名冲突 / 变量覆盖 | 三重 escapeHtml 实现完全一致，无功能影响 | ❌ |
| IIFE 内部 DOM 引用在 bfcache 恢复后失效 | bfcache 保存完整 DOM 树，引用有效 | ❌ |
| `showToast` 的 bootstrap.Toast 初始化 | 使用 `typeof bootstrap !== 'undefined'` 进行防御性检查，不会崩溃 | ❌ |
| 搜索结果中的 `onclick` 内联属性 | 通过 `window.xxx` 正确导出了函数 | ❌ |
| 搜索结果中的 `<a>` 链接 | 标准 `<a href="/album/...">` 导航，不依赖 JS | ❌ |
| 服务器端状态泄漏 | before_request/after_request 无状态泄漏 | ❌ |
| **fetch 请求被浏览器阻塞/悬挂** | 可能的根本原因——bfcache 对 fetch 请求的影响比较复杂 | **⚠️ 最有嫌疑** |
| **浏览器将搜索页放入 bfcache 后，恢复时 fetch 的 AbortController 或 Promise 状态异常** | 触发网络错误链式反应，导致界面无响应 | **⚠️ 潜在根因** |
| **DETAIL 页面的标签同步请求在返回搜索页后仍然活动** | L29-31 的 `fetch('/api/library/' + ... + '/tags/sync', {method: 'POST'})` 是 **异步发后即忘** 的请求，可能在页面卸载后仍然进行，导致网络层竞争 | **⚠️ 潜在根因** |
| **浏览器 tab 的后台节流** | Chrome 在后台 tab 节流定时器，bfcache 恢复后 setTimeout/setInterval 行为不一致 | **⚠️ 辅助因素** |
| **静态资源 1 年强缓存导致浏览器使用过时的 search.js** | `SEND_FILE_MAX_AGE_DEFAULT = 31536000` 让浏览器缓存 JS 长达 1 年。如果搜索页之前加载了旧版 search.js，新版部署后浏览器仍用缓存版本 | ❌ 但仍是隐患 |

### 5.2 最有可能的根本原因链

由于代码审查没有发现明显的 JS 错误，**"卡死" 很可能是浏览器网络层的行为**：

1. 搜索页做的 `fetch('/api/search?q=...')` 或注册的 `fetch(url)` 在页面导航离开时被悬挂
2. bfcache 恢复后，某些 fetch 请求的 Promise 仍然在 pending 状态或处于重试循环
3. 或用户反复点击"下载"按钮，多个 `fetch('/api/jobs', {method: 'POST'})` 请求被快速发出，服务器端 waitress 线程池（128 线程）的某个部分被阻塞

### 5.3 确凿发现的问题

1. **⚠️ 搜索页和详情页无 bfcache 防护**，bfcache 默认可用
2. **⚠️ 搜索页无 `pageshow` 事件处理**，bfcache 恢复后不会重新初始化
3. **⚠️ 静态资源 1 年强缓存**，JS 更新后用户浏览器可能使用旧代码
4. **⚠️ `detail.js` L29-31 的标签同步 fetch 是"发后即忘"**，页面卸载后可能继续运行
5. **✅ `beforeunload` 实际上阻止了"应该有" bfcache 的页面**（首页/下载管理），这是好的

---

## 6. 修复建议

### 6.1 关键修复：为搜索页添加 bfcache 防护（解决可能的卡死）

在 `templates/search.html` 中添加：

```html
{% block head_extra %}
<!-- 禁止 bfcache，确保每次访问重新加载 JS -->
<meta http-equiv="Cache-Control" content="no-store">
{% endblock %}
```

或在 Flask 的 page 路由中设置响应头（更可靠）：

```python
@app.after_request
def _disable_bfcache(response):
    if response.mimetype == 'text/html':
        response.headers['Cache-Control'] = 'no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response
```

### 6.2 ❌ 不要添加 `beforeunload`（它会阻止 bfcache 但引入新问题）

`beforeunload` 会弹出一个确认对话框，影响用户体验。使用 `Cache-Control: no-store` 更优雅地禁用 bfcache。

### 6.3 修复：添加 `pageshow` 事件处理（备选方案）

如果希望保留 bfcache 的性能优势但确保状态正确，在 `search.js` 中添加：

```javascript
window.addEventListener('pageshow', function (event) {
    if (event.persisted) {
        // 页面从 bfcache 恢复，重新初始化
        currentQuery = searchInput ? searchInput.value.trim() : '';
        currentPage = 1;
        if (currentQuery) {
            fetchResults();
        } else {
            // 重置为初始状态
            resultsDiv.innerHTML = '<div class="text-center text-muted py-5"><i class="bi bi-search" style="font-size: 3rem;"></i><p class="mt-3">在上方输入关键词开始搜索</p></div>';
            paginationDiv.innerHTML = '';
            statusDiv.classList.add('d-none');
        }
    }
});
```

同时在 `sse-client.js` 中也要添加（如果它所在的页面要支持 bfcache）：
```javascript
window.addEventListener('pageshow', function (event) {
    if (event.persisted) {
        window.connectAllSSE();
    }
});
```

### 6.4 修复：为 detail.js 的 AbortController 添加 bfcache 感知

```javascript
window.addEventListener('pageshow', function (event) {
    if (event.persisted) {
        // 从 bfcache 恢复，跳过 AbortController（fetch 已完成或已超时）
        return;
    }
});
```

### 6.5 修复：detail.js 的"发后即忘"标签同步请求

detail.js L29-39 的标签同步 fetch 在页面卸载后应该被取消：

```javascript
// 在页面卸载时 abort 标签同步请求
var _tagSyncController = new AbortController();
window.addEventListener('beforeunload', function () {
    _tagSyncController.abort();
});

fetch('/api/library/' + encodeURIComponent(albumId) + '/tags/sync', {
    method: 'POST',
    signal: _tagSyncController.signal
})
```

### 6.6 修复：静态资源版本化

在 `app.py` 中为静态资源添加版本参数，或使用 hash 文件名：

```python
# 选项1: 在模板中使用 ?v= 参数
# <script src="/static/js/search.js?v={{ version }}"></script>

# 选项2: 降低强缓存时间
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600  # 1 hour instead of 1 year
```

### 6.7 修复：搜索结果按钮防重复点击

搜索页的 `quickDownload` 没有防重复点击，用户可以连续点击导致多个 fetch 并发：

```javascript
window.quickDownload = function (btn) {
    if (!btn || btn.disabled) return;   // 防重复
    btn.disabled = true;
    // ... 现有逻辑 ...
    // 在 finally 中恢复 btn.disabled = false;
};
```

---

## 附录：bfcache 兼容性速查表

| 检查项 | 搜索页 | 详情页 | 首页 | 下载管理 | 资源库 | 收藏页 |
|---|---|---|---|---|---|---|
| beforeunload 监听 | ❌ | ❌ | ✅ (sse-client) | ✅ (sse-client+downloads) | ✅ (library) | ✅ (wishlist) |
| Cache-Control: no-store | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| pageshow 处理 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| SSE 连接 | ❌ | ❌ | ✅ | ✅ | ❌ | ❌ |
| AbortController | ❌ | ✅ (detail.js) | ❌ | ❌ | ❌ | ❌ |
| 自动恢复安全 | ❌ | ❌ | N/A (blocked) | N/A (blocked) | N/A (blocked) | N/A (blocked) |

> N/A = bfcache 已被 beforeunload 阻止，无需担心

---

*分析日期: 2026-07-06*
*分析范围: templates/ (html), static/js/ (js), app.py, routes/ (api)*
