---
name: JMComic 下载控制台 — 项目设计系统
version: 1.1
description: 基于母模板 eye-care 护眼风格，锁定本项目的 UI 规范

project_theme:
  theme_mode: eye-care
  fallback_theme: eye-care
  locked: true
  selected_by: user
  rule: "全站使用护眼暖色风格，暖灰/浅米色背景，降低蓝光刺激，所有 UI 修改必须遵守此规范"

# ============================================
# Eye-care 护眼风格 Token
# ============================================

theme-eye-care:
  bg-primary: "#F3F0E8"
  bg-secondary: "#FAF8F2"
  bg-tertiary: "#E8E2D6"
  text-primary: "#2B2A27"
  text-secondary: "#686256"
  text-tertiary: "#918A7D"
  border: "#D8D0C0"
  primary: "#3B82F6"
  success: "#2F855A"
  warning: "#B7791F"
  error: "#C53030"
  info: "#2B6CB0"

# ============================================
# 全局设计 Token（跨主题共享）
# ============================================

spacing:
  xs: 4px
  sm: 8px
  md: 16px
  lg: 24px
  xl: 32px
  2xl: 48px

rounded:
  none: 0px
  sm: 4px
  md: 6px
  lg: 8px
  xl: 12px
  full: 9999px

shadow:
  sm: "0 1px 2px 0 rgba(0, 0, 0, 0.1)"
  md: "0 4px 6px -1px rgba(0, 0, 0, 0.12)"
  lg: "0 10px 15px -3px rgba(0, 0, 0, 0.15)"
  xl: "0 20px 25px -5px rgba(0, 0, 0, 0.18)"

typography:
  font-sans: "'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', system-ui, -apple-system, 'Segoe UI', sans-serif"
  font-mono: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', Consolas, monospace"
  font-size-xs: 12px
  font-size-sm: 14px
  font-size-base: 16px
  font-size-lg: 20px
  font-size-xl: 24px
  font-size-2xl: 32px
  font-weight-normal: 400
  font-weight-medium: 500
  font-weight-semibold: 600
  line-height-tight: 1.25
  line-height-normal: 1.5
  line-height-relaxed: 1.75

---

# JMComic 本地下载控制台 — 设计文档

> 基于 jmcomic v2.7.0，Flask + Bootstrap 5，本地运行
> theme_mode: eye-care（护眼风格，已锁定）

---

## 一、项目概况

| 项目 | 内容 |
|------|------|
| 项目根目录 | `D:\Hermes\禁漫下载搜索插件\` |
| 下载目录 | `D:\Hermes\禁漫下载搜索插件\downloads\` |
| 核心引擎 | `jmcomic` v2.7.0（pip 包） |
| Web 框架 | Flask |
| 前端 | Bootstrap 5 + 原生 HTML/CSS/JS（本地资源，无 CDN） |
| 数据库 | SQLite（WAL 模式） |
| 实时推送 | Server-Sent Events (SSE) |
| 启动方式 | `python app.py` → `http://127.0.0.1:5000` |
| 生产服务器 | waitress（回退 Flask 开发服务器） |
| 监听地址 | 默认 `127.0.0.1`（仅本机） |
| 单实例保护 | PID 锁文件防多开 |
| 主题锁定 | **eye-care 护眼风格** — 暖灰/浅米色背景，降低蓝光刺激 |

---

## 二、产品定位

**JMComic 本地下载控制台**

核心流程：

```
用户打开本地网页
  → 搜索漫画或输入车号
  → 查看详情
  → 选择章节 / 收藏
  → 创建下载任务
  → 任务进入队列
  → 前端实时显示进度（SSE）
  → 下载完成后自动整理 / 打包 / 同步标签
  → 在资源库中按标签管理漫画
```

### 已实现功能清单

| 功能 | 说明 |
|------|------|
| 🔍 漫画搜索 | 4 种排序（最新/浏览量/点赞/图片数），原生标签语法（AND/OR/NOT） |
| 📄 漫画详情 | 章节列表、标签、作者、浏览量、点赞数 |
| ⬇️ 下载管理 | 多任务并发、暂停/继续/取消、指数退避重试 |
| 📦 CBZ 打包 | 下载后自动打包，可选删除原始文件 |
| 📂 目录整理 | 按作者整理 / 扁平化整理 |
| ⭐ 收藏清单 | 收藏/取消/搜索/导入/导出 |
| 🏷️ 资源库+标签 | 标签云筛选、多标签 AND 组合、自动同步（18comic）、用户自定义标签 |
| 🖼️ 图片预览 | 内嵌网页查看器 |
| ⏰ 定时调度 | 定时刷新/下载 |
| 🔒 单实例保护 | PID 文件防多开 |

---

## 三、页面结构

当前共 **11 个页面**：

| 页面 | 路由 | 功能 |
|------|------|------|
| 首页 | `/` | 搜索框 + 快捷入口 + 当前任务 + 最近下载 |
| 搜索 | `/search` | 关键词搜索 + 分页 + 结果列表 |
| 详情 | `/album/<id>` | 漫画信息 + 章节列表 + 下载选中章节 + 标签管理 |
| 下载管理 | `/downloads` | 进行中 / 排队 / 已完成 / 失败 |
| 设置 | `/settings` | 基础 / 网络 / 下载 / 输出 / 敏感配置 |
| 收藏清单 | `/wishlist` | 收藏漫画列表 + 搜索 + 批量操作 |
| 资源库 | `/library` | 标签云筛选 + 卡片网格 + 统计 + 收藏/下载状态 |
| 图片预览 | `/preview/<album_id>` | 内嵌图片查看器 |
| 预览（按任务） | `/preview/job/<job_id>` | 从任务跳转到预览 |
| 阅读 | `/read/<album_id>` | 已下载：本地图片连续滚动阅读（只读本地文件）；未下载：转到 `/online/<album_id>` |
| 在线阅读 | `/online/<album_id>` | 同一阅读页，图片经本程序按需在线获取，不下载 |

---

## 四、API 路由

### 4.1 HTML 页面

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 首页 |
| GET | `/search` | 搜索页 |
| GET | `/album/<album_id>` | 详情页 |
| GET | `/downloads` | 下载管理页 |
| GET | `/settings` | 设置页 |
| GET | `/wishlist` | 收藏清单页 |
| GET | `/library` | 资源库页 |
| GET | `/preview/<album_id>` | 图片预览页 |
| GET | `/preview/job/<job_id>` | 从任务跳转到预览 |
| GET | `/read/<album_id>` | 阅读：本地可读时连续阅读本地文件，否则 302 到 `/online/<album_id>`（保留 `?page=`） |
| GET | `/online/<album_id>` | 在线阅读（`?chapter=<photo_id>` 从该章第一页开始） |

### 4.2 搜索 API

```
GET /api/search?q=关键词&page=1&page_size=20&sort=latest

Response:
{
  "status": "ok",
  "items": [
    {
      "album_id": "123456",
      "title": "xxx",
      "author": "xxx",
      "tags": ["中文", "同人"],
      "cover_url": "/api/cover/123456"
    }
  ],
  "total": 42,
  "page": 1,
  "page_size": 20
}
```

```
GET  /api/search-history              → 最近搜索记录（去重）
DELETE /api/search-history/<keyword>  → 删除指定搜索历史
POST  /api/search-history/clear       → 清空全部搜索历史
```

### 4.3 详情 API

```
GET /api/album/<album_id>

Response:
{
  "status": "ok",
  "album": {
    "album_id": "123456",
    "title": "xxx",
    "author": "xxx",
    "tags": [],
    "actors": [],
    "works": [],
    "views": 546,
    "likes": 91,
    "description": "...",
    "cover": "/api/cover/123456",
    "photos": [
      {"photo_id": "123456", "title": "第1话", "page_count": 65}
    ],
    "chapter_count": 1
  }
}
```

### 4.4 封面 API

```
GET /api/cover/<album_id>  →  302 重定向到封面图片 URL
```

### 4.5 下载任务 API

```
POST /api/jobs
  Body: { "album_id": "123456", "photo_ids": ["123456"] }
  → { "status": "ok", "job_id": "job_xxxxx" }

GET  /api/jobs                       → 全部任务列表
GET  /api/jobs/<job_id>              → 单任务详情
GET  /api/jobs/<job_id>/events       → SSE 事件流（实时进度）
POST /api/jobs/<job_id>/cancel       → 取消任务
POST /api/jobs/<job_id>/pause        → 暂停任务
POST /api/jobs/<job_id>/resume       → 恢复任务
POST /api/jobs/<job_id>/retry        → 重试 / 重新下载（失败、已取消、已完成的任务；未结束的任务 400）
DELETE /api/jobs/<job_id>            → 删除任务
DELETE /api/jobs/clear               → 清空已完成/失败/已取消任务
POST /api/jobs/<job_id>/open-folder  → 打开下载目录
```

### 4.6 设置 API

```
GET  /api/settings   → { "status": "ok", "settings": {...} }
POST /api/settings    Body: { "key": "value", ... }  → { "status": "ok" }
```

### 4.7 收藏 API

```
GET    /api/wishlist?page=&q=&sort=             → 收藏列表（分页）
GET    /api/wishlist/<album_id>                  → 单个收藏
POST   /api/wishlist                              Body: { album_id, title, author, cover_url }
DELETE /api/wishlist/<album_id>                   → 取消收藏
POST   /api/wishlist/import                       Body: { "items": [...] }  → 批量导入
GET    /api/wishlist/export                       → 导出全部收藏
POST   /api/wishlist/batch-check                  Body: { "album_ids": [...] }  → 批量检查收藏状态
```

### 4.8 资源库 + 标签 API

```
GET    /api/library?page=&tag=&q=&status=&sort=  → 资源库列表
GET    /api/library/tags?min_count=               → 标签云
GET    /api/library/stats                          → 统计信息
GET    /api/library/<album_id>                     → 单个条目详情
GET    /api/library/<album_id>/tags                → 某漫画的标签列表
POST   /api/library/<album_id>/tags                Body: { "tags": [...] }  → 添加标签
DELETE /api/library/<album_id>/tags                Body: { "tags": [...] }  → 删除标签
POST   /api/library/<album_id>/tags/sync           → 从 18comic 同步标签
GET    /api/library/search-tags?q=                  → 标签自动补全
```

### 4.9 预览 API

```
GET /api/preview/<album_id>/list                    → 该漫画所有章节/图片列表
GET /api/preview/cover/                             → 预览用封面
GET /api/online/<album_id>                          → 在线页列表（结构同本地列表，另附 photo_id、skipped_chapters）
GET /api/online-img/<photo_id>/<index>              → 单页图片：首次从上游下载解码，缓存到 runtime/cache/online/
GET /api/preview-archive/<album_id>/<n>             → 只剩压缩包时的第 n 页（从 CBZ / 本程序的 ZIP 里读出，不解压到下载目录）
```

### 4.10 导出 API

```
GET /api/export/wishlist/format                     → 收藏导出（指定格式）
```

### 4.11 检查新章节 API

只有 `POST /api/updates/<album_id>/check` 会联网（只取一次章节列表，从不下载）；其余只读数据库、本地文件和内存里的运行状态。本地没有能读的已下载内容的漫画不返回任何检查结果。

```
GET  /api/updates/<album_id>            → { eligible:false } 或 { eligible:true, auto_enabled, checking,
                                             update:{ state: never/checking/baseline/no_update/new/changed/failed,
                                             new_count, new_chapters:[{photo_id,index,title,confirmed_at}], error, next_check_at, ... } }
POST /api/updates/<album_id>/check      → 立即检查（请求体忽略）：200 同上 + outcome（new/changed/no_update/baseline/
                                             failed/throttled/coalesced）；409 not_target 本地没有已下载的内容 / busy 正在检查别的漫画
POST /api/updates/states                  Body: { "album_ids": [...] }（最多 200）→ { updates:{ id:{state:new/changed, new_count,
                                             removed_count, confirmed_at, titles} } }（只含本地可读、已确认有新章节的）
GET  /api/updates/summary               → 设置页状态：{ enabled, runtime:{phase,...}, counts, checks_24h, last }
GET  /api/updates/pending               → 已确认、可以下载的新章节（给“下载新章节”用；不含“章节有变动”的）
```

资源库、收藏列表的条目另带 `update`（与 `/api/updates/states` 同一形状，只给本地可读的条目，否则 null）。

---

## 五、任务模型

每个下载请求创建一个独立 job，引入 `job_id` 而非直接用 `album_id`。

### 任务状态

```
queued    排队中
running   下载中
paused    暂停中
completed 已完成
failed    失败
canceled  已取消
```

### jobs 表

```sql
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT UNIQUE NOT NULL,
    album_id TEXT NOT NULL,
    title TEXT,
    selected_photo_ids TEXT,   -- JSON 数组
    status TEXT NOT NULL DEFAULT 'queued',
    total_pages INTEGER DEFAULT 0,
    done_pages INTEGER DEFAULT 0,
    current_photo TEXT,
    current_image TEXT,
    output_path TEXT,
    error_message TEXT,
    created_at TEXT,
    updated_at TEXT,
    completed_at TEXT
);
```

### 下载流程

```
创建任务(queued)
  → 调度器领取(running)
  → 逐页下载（指数退避重试，最多4次）
     → 检查暂停状态 → 等待直到恢复
     → 检查取消/删除 → 终止
  → 整理目录（按作者/扁平化）
  → CBZ 打包（可选）
  → 同步标签到 album_tags
  → 标记完成(completed) 或 部分失败(failed)
```

---

## 六、数据库设计

### 表结构

| 表 | 用途 | 关键字段 |
|----|------|----------|
| `jobs` | 下载任务 | job_id, album_id, status, total_pages, done_pages, output_path, superseded_at（目录被后来的下载重新建出） |
| `settings` | 键值对设置 | key, value |
| `search_history` | 搜索历史 | keyword, created_at |
| `wishlist` | 收藏清单 | album_id(UNIQUE), title, author, cover_url, download_status |
| `album_tags` | 标签 | album_id, tag, source(auto/user) |
| `album_meta` | 漫画元数据缓存 | album_id, title, author, cover_url, tags_synced_at |
| `album_update_checks` | 检查新章节（每部漫画一行） | album_id, baseline_ids（下载时记下的上游章节）, new_chapters / new_count（已确认的新章节）, removed_ids, result, last_success_at, error_kind, fail_count, next_check_at |
| `update_check_log` | 检查记录（只留最近 2000 条） | album_id, trigger（auto/manual）, started_at, outcome, error_kind, requests, upstream_count, new_count |

### 标签同步策略

| 来源 | 触发时机 | 优先级 |
|------|----------|--------|
| 18comic 自动同步 | 用户访问详情页时 / 下载完成时 | 覆盖 auto 标签 |
| 用户手动同步 | 点击"同步标签"按钮 | 覆盖 auto 标签 |
| 用户自定义标签 | 手动添加 | 永不被自动覆盖 |
| 批量重新同步 | 设置页触发 | 覆盖全部 auto |

---

## 七、项目结构

```
D:\Hermes\禁漫下载搜索插件\
│
├── app.py                    # Flask 入口 + PID 单实例锁
├── launcher.py               # 启动器
├── jmcomic_cli.py            # CLI 工具
├── requirements.txt          # Python 依赖
├── DESIGN.md                 # ← 本文件（设计规范，已锁定）
├── design_library_tags.md    # 资源库+标签系统设计方案（备查）
│
├── core\                     # 核心业务层
│   ├── __init__.py
│   ├── database.py           # SQLite CRUD（6 张表）
│   ├── jm_service.py         # jmcomic API 封装 + 下载引擎
│   ├── settings.py           # 设置管理 + jmcomic option 构建
│   ├── job_manager.py        # 后台任务调度器
│   ├── scheduler.py          # 定时下载调度器
│   ├── progress.py           # SSE 进度推送
│   ├── packer.py             # CBZ 打包
│   ├── path_utils.py         # 路径工具
│   ├── path_guard.py         # 路径安全守卫
│   └── logger.py             # 日志系统
│
├── routes\                   # Flask 蓝图（9 个）
│   ├── __init__.py
│   ├── page_routes.py        # 页面路由（9 个页面）
│   ├── api_search.py         # 搜索 + 搜索历史
│   ├── api_album.py          # 漫画详情
│   ├── api_jobs.py           # 任务 CRUD + SSE
│   ├── api_settings.py       # 设置管理
│   ├── api_wishlist.py       # 收藏管理
│   ├── api_library.py        # 资源库 + 标签管理
│   ├── api_preview.py        # 图片预览
│   └── api_export.py         # 收藏导出
│
├── templates\                # Jinja2 模板（9 个）
│   ├── base.html             # 基础布局（导航栏 + Toast）
│   ├── index.html            # 首页
│   ├── search.html           # 搜索页
│   ├── detail.html           # 漫画详情 + 标签编辑
│   ├── downloads.html        # 下载管理
│   ├── settings.html         # 设置页
│   ├── wishlist.html         # 收藏清单
│   ├── library.html          # 资源库 + 标签云
│   └── preview.html          # 图片预览
│
├── static\
│   ├── css/style.css         # 自定义样式（含资源库/标签/响应式）
│   ├── js/search.js          # 搜索页 JS
│   ├── images/no-cover.svg   # 缺省封面
│   └── vendor\               # Bootstrap 5 本地资源
│       └── bootstrap\...
│       └── bootstrap-icons\...
│
├── data\                     # 运行时数据
│   ├── app.db                # SQLite 数据库
│   └── flask.pid             # PID 锁文件
│
├── downloads\                # 下载文件存放
│
├── logs\                     # 应用日志
│   └── app.log
│
└── build\                    # PyInstaller 构建输出
```

---

## 八、Eye-care 护眼风格详细设计说明

> 以下规则适用于 theme_mode = eye-care 的本项目。

### 设计目标

护眼优先的工具界面，暖灰/浅米色基调，适合长时间使用。
- 暖色背景降低蓝光刺激，减少视觉疲劳
- 信息密度适中，层次通过颜色深度和间距区分
- 中文友好：字体栈优先中文字体

### 颜色使用规则

- 背景三层暖色：`bg-primary (#F3F0E8)` 画布 → `bg-secondary (#FAF8F2)` 卡片 → `bg-tertiary (#E8E2D6)` 表头
- 不要使用纯白色 `#FFFFFF` 作为背景
- 主色蓝色 `#3B82F6`，用于按钮、链接、选中态
- 语义色：`success (#2F855A)` 表示完成，`error (#C53030)` 表示错误，`warning (#B7791F)` 表示警告
- 不要使用高饱和/荧光色系

### 字体

默认 14px body，最小 12px。等宽字体用于代码和日志。
标题层级：h1(24px,600) → h2(20px,600) → h3(16px,500)

### 间距

8 点网格：4 → 8 → 16 → 24 → 32 → 48。不使用奇数间距。

### 圆角

sm(4px) 徽标 / md(6px) 按钮 / lg(8px) 卡片 / xl(12px) 弹窗 / full 圆形

### 阴影

暖色模式下使用暖灰色阴影，克制使用。

### 按钮

Primary(蓝色) / Secondary(暖灰+边框) / Ghost(全透明) / Danger(红色)
高度统一 36px，禁用态透明度 0.4。

### 表格

暖灰分割线，不设外边框。表头 `bg-tertiary`，行悬停 `bg-secondary`。

### 卡片

背景 `bg-secondary`，圆角 8px，暖色边框 `border`。

### 表单

输入框背景 `bg-primary`，焦點蓝色 + 2px 光晕，错误红色边框。

### 导航

选中态蓝色文字 + 10% 蓝色背景。悬停 `bg-secondary`。

### eye-care 模式禁止项

- ❌ 不使纯白色 `#FFF` 作背景
- ❌ 不使用高饱和/荧光色
- ❌ 不使用大面积极简留白
- ❌ 不使用彩色渐变背景
- ❌ 不使用自动播放动画
- ❌ 不使用弹窗作为主要交互方式
- ❌ 不使用小于 12px 的文字
- ❌ 不在暖色模式下使用冷白/亮白阴影
- ❌ 不为了花哨牺牲信息密度

---

## 九、验收标准

1. ✅ `python app.py` 启动，访问 `http://127.0.0.1:5000`
2. ✅ 首页显示正常，不依赖 Bootstrap CDN
3. ✅ 搜索输入关键词返回结果
4. ✅ 详情页显示漫画信息 + 章节列表 + 标签编辑
5. ✅ 可选择章节创建下载任务，返回 job_id
6. ✅ 下载管理页显示 queued / running / paused / completed / failed / canceled
7. ✅ SSE 实时推送进度
8. ✅ 刷新页面后任务状态仍存在（SQLite）
9. ✅ 下载完成后可打开目录（Windows）
10. ✅ 失败任务显示原因并可重试
11. ✅ 设置可保存，影响下载行为
12. ✅ 仅监听 127.0.0.1，路径有安全检查
13. ✅ 收藏清单：添加/取消/搜索/导入/导出
14. ✅ 资源库：标签云筛选、多标签组合、统计
15. ✅ 标签自动同步：详情页访问时 + 下载完成时
16. ✅ CBZ 打包 + 目录整理
17. ✅ 定时调度
18. ✅ 单实例 PID 保护

---

## 十、主题遵守规则

### 连续阅读（2026-09-08）

- 搜索卡片复用现有按钮、边框和 8px 间距，“详情 / 阅读”与“下载 / 收藏”分两行，避免窄屏挤压。
- `/read/<album_id>` 为独立的本地连续阅读页，原翻页预览保留；未下载时只提示，不自动下载。
- 使用 eye-care 背景、文字和边框 token，圆角复用 `--radius-md/lg`，不引入字体或颜色家族。
- 阅读区域最大宽度 64rem，属于内容布局尺寸；实际间距采用 8/16/24px。图片保持原比例，滚动加载无动画。
- 搜索返回恢复条件、页码、结果及滚动位置，搜索结果快照仅保存在会话范围且最长使用 30 分钟（2026-09-26 起：经导航/后退回来 12 小时，刷新仍为 30 分钟，见“导航回到上次离开的位置”）。
- 阅读标题区不吸顶，随页面滚走；返回、页码、跳转放入默认隐藏的底部阅读工具。轻触画面/空白处或按 M 切换，Esc/收起按钮关闭，跳页后自动收起。无常驻悬浮按钮或新增动画，继续使用现有主题 token。
- 图片上方的“页码＋章节标题”只在第一页显示，后续页保留无障碍页码但不再显示说明条。到顶/到底使用现有 Bootstrap 图标与按钮，放在隐藏工具内；到底定位末页图片底部，到顶返回页面顶部，定位后工具自动收起。
- 单页与连续阅读可以互切并保留页码，切换使用同一历史条目以免返回时反复跳模式。单页缩略图完整等比缩放（contain），80×128px 单元内图片高 96px、页码行高 16px；横向单独滚动，外层不限制高度。单页画布改为 eye-care 背景/边框，不再使用深色底。

### 阅读与搜索复查（2026-09-25）

- 两种阅读模式共用页头 `.reader-toolbar`（style.css，不吸顶，标题即漫画名 h1 20px/600、窄屏 16px）。单页模式页头：返回 ｜ 标题＋键盘提示（≥768px 显示）｜“单页翻页 / 连续滚动”分段按钮；连续阅读页头不变（返回仍在隐藏工具内）。两种模式页头上方统一留 16px。
- 单页模式顺序改为 页头 → 图片 → 翻页栏 → 缩略图；图片 `max-height: calc(100vh - 256px)`，桌面端首屏即可看到整页与翻页按钮。翻页时保留旧图直到新图就绪，超过 200ms 才显示加载指示（不新增动画），并预读下一页。
- 单页翻页栏 ≤768px 时页码独占一行，首/末页图标按钮最小宽 48px，四个翻页按钮同排。单页专用样式全部收拢到 `css/preview.css`，删除模板内联样式与已无用的深色 `--preview-bg`。
- 连续阅读工具面板：页码文字置首（14px/500，主文字色），到顶/到底合为按钮组，悬浮层加 `--shadow-md` 区分图片；≤576px 时页码独占一行。
- 按钮 `:focus-visible / :active / :disabled` 通过 `--bs-btn-*` 变量回到主题色（原先键盘聚焦显示 Bootstrap 灰 #6c757d），聚焦光晕统一主色蓝，禁用透明度按本文件取 0.4。
- 搜索卡片封面统一 120:170（与资源库卡片一致），标题最多两行，整卡可点击时显示指针；搜索历史关键词与删除改为真实按钮。
- 全部间距仍取 4/8/16/24/32/48，未新增颜色、字体、圆角或动画。

### 导航回到上次离开的位置（2026-09-26）

- 顶部导航的“搜索 / 收藏 / 资源库”回到本标签页上次离开时的样子（static/js/nav-memory.js，sessionStorage，12 小时内有效）：搜索恢复关键词、排序、每页数量、页码和结果（用快照直接画出，不重新请求上游，收藏/已下载标记重新判断）；收藏、资源库恢复地址栏里的筛选、排序、页码。三者都回到离开时看到的位置。
- 链接地址在被指向、获得焦点、按下或点击时才换成记住的地址；当前就在该页时用当前地址。只接受同一路径的站内地址，过期或取不到时退回 /search、/wishlist、/library。直接在地址栏输入这些地址仍是初始页面（新搜索的入口）。
- 导航栏不吸顶（布局不变），要点导航得先滚回最上方：离开时若在最上方 120px 内，记下的是之前停留 2 秒以上或点击、输入过的位置（滚动按键不算）；在最上方停留够久或在那里操作过就记最上方。这个位置只属于当时的地址，换页、换筛选后作废。刷新（F5）回到刷新前所在的位置，搜索结果刷新时超过 30 分钟重新搜索（经导航/后退回来 12 小时）。
- 还原按列表（`[data-nav-memory-list]`，搜索页为结果区）的偏移计算，上方的搜索历史、标签云后加载也回到同一处；收藏、资源库只在经记忆链接到达、后退/前进或刷新时还原，直接输入地址是初始页面。还原只在列表第一次画出后做一次，用户已经滚动/操作过就不动，瞬间跳转无动画。
- 详情页“返回”、阅读页“返回”（直接打开时的兜底）和空资源库的“去搜索页面”同样回到上次的搜索。

### 离线可读标记、收藏与资源库筛选（2026-09-26）

- “本地可读”全站同一标准（core/local_availability.py）：该漫画最近一次有效的已完成任务目录仍在下载目录内，且至少有一张页面图片；被后来的下载重新建出的目录（superseded_at）不算。搜索、详情、下载管理、收藏、资源库都用它。
- 标记统一为 `.offline-badge`：✓ 图标 + “已下载内容 · 可离线阅读”（表格内简写“可离线阅读”，完整文字放在提示与读屏文字里），只用 `--success / --success-bg`；搜索卡片封面角标同样是“✓ 已下载内容 · 可离线阅读”。下载管理里可读的任务另有“预览”（单页，图标 `bi-images`，只读本地文件）。
- “阅读”按钮每部漫画都有（搜索、资源库、收藏、下载管理的已完成与失败任务），统一由 utils.js `window.readLink` 生成，链接始终是 `/read/<id>`：服务端在点击时判断，本地可读 → 本地连续阅读，否则 302 到 `/online/<id>`（保留 `?page=`），页面打开后才下载完成或文件被删除也能去对地方。外观只表达判断结果：已下载 `btn-primary` + `bi-book`；未下载 `btn-outline-primary` + `bi-globe2`（与“在线观看”同一图标），读屏文字“阅读（在线）”；还没判断时描边 + `bi-book`。详情页保持“在线观看”常驻、“阅读”只在已下载时出现（未下载时两者去向相同，不重复放两个）。
- 详情页“返回搜索”改为“返回”：从站内页面进入时后退（保留结果与滚动位置），直接打开时回到本标签页最近一次搜索。
- 收藏：按下载状态筛选（未下载 / 下载过·文件已删除 / 排队中·下载中 / 已下载可阅读 / 失败，与资源库同样五档互不重叠；“未下载”只含从未下载或已取消的），排序新增最早添加与下载状态；筛选、排序、页码写入地址栏。资源库：状态分组与收藏页一致且互不重叠（可离线阅读 / 排队中·下载中 / 失败 / 下载过·文件已删除 / 未下载），新增“排序：作者”，点作者名只看该作者（沿用标签筛选的选中胶囊样式，可 ✕ 清除）。
- 所有含标题、作者、标签的动态内容用 textContent / data-* 渲染，事件统一委托，页面上不再有内联 onclick。

### “已下载内容”不等于整部漫画（2026-09-28）

- “本地可读”只说明至少有一页能读，不说明整部漫画都在（选章节下载、下载失败或取消、之后又出了新章节、手动删了章节都会只有一部分）。所以下载管理、收藏、资源库、搜索、详情的标记都写“已下载内容 · 可离线阅读”（收藏表格里仍简写“可离线阅读”，完整文字在读屏文字和提示里），提示统一为“本地有已下载的内容，可以离线阅读；不一定是整部漫画”；“阅读”按钮提示“已下载内容：打开本地文件阅读”；资源库统计“可离线阅读”的提示、两处状态筛选项同步改；下载任务完成的 Toast 说“下载任务完成”（任务可能只含部分章节）。任何地方都不再说“本地文件完整”。
- 详情页在能证明时另加“部分章节已下载 · M/N 话”（`.status-badge-partial`：`--text-primary` 字、`--info-bg` 底、`--info` 边框与 `bi-layers` 图标，与压缩包标记同一组 token，靠图标区分）。只读数据库和磁盘（`/api/local-chapters/<id>`，core/chapter_inventory.py），从不联网、不建下载任务。条件：N 来自详情页刚取到、没过期（1 小时）的章节列表；阅读器看到的每一页都能按章节目录 `<章节名>__<photo_id>` 和其中的章节标记归到一话、页名是连续编号；本地每一话的页号正好是 1..该话页数；本地各话都在章节列表里；且 0 < M < N。任何一条不满足（没有章节列表或已过期、扁平整理、旧版没有标记的目录、别的工具的压缩包、残缺的一话、中断的下载留下的临时文件、上游换了章节、这部漫画还有别的下载目录有内容（按作者整理、上游改了标题）、优先的压缩包暂时打不开）就不显示数量——不猜。只有一话的漫画不显示（也不扫描目录）。M == N 时也不说“全部已下载”。列表页不显示数量：那里没有刚取到的章节列表。

### 检查新章节（2026-09-28）

- 设置页“检查新章节”（`auto_update_check`，默认开，放在“定时下载”与“界面操作”之间）：后台慢慢检查本地有已下载内容的漫画（含只下载了部分章节的、只剩 CBZ / ZIP 的），一次只查一部、每部大约一天一次、两次之间至少 2 分钟，有下载在进行时先等。一次检查只取一次章节列表，**从不下载**；失败后逐渐拉长重试间隔，连不上服务器时暂停。只收藏、没下载的不查。开关下面是只读的状态面板（三行 textContent：后台在做什么 / 统计 / 最近一次）。关闭后详情页的“立即检查”、已有的结果和列表标记照常。
- “新章节”只指下载之后上游新出的章节：基线是下载时取到的完整章节列表，下载时没选的章节从不算新章节；更新前下载的漫画第一次检查只记下现有章节，不说有新章节。“有新章节”只在一次成功的检查确认了具体章节之后出现；同一次检查里既有新章节、又有以前的章节不见了时是“章节有变动”（只给用户核对）。
- 标记 `.badge.status-badge-update`：`--text-primary` 字、`--primary-bg` 底、`--primary` 边框与图标（`bi-bell`）；“章节有变动”复用 `.status-badge-warning`（`bi-exclamation-triangle`）。下载管理里它们是链接（`a.badge`，悬停才有下划线）。字号、圆角沿用 `.badge`。
- 详情页：状态行 `#album-update-status.update-status` 紧跟在“已下载内容 · 可离线阅读”“部分章节已下载 · M/N 话”下面（flex、换行，间距 4px / 8px，上方 8px，14px 字），第一行“徽章或图标 + 文字 ｜ 选中这些章节 ｜ 立即检查”，下面各占一整行的新章节名（`.update-new-list`，最多 5 个，之后“… 等 N 话”）和说明（`.update-status-meta`），两者 12px `--text-secondary`；没检查成功的原因用 `.update-status-warn`（图标 `--warning`）。本地没有已下载的内容时整行不显示。按钮都是 `btn-sm btn-outline-primary`：“立即检查”（`bi-arrow-repeat`，提示“只取一次章节列表核对，不会下载任何内容”，检查中禁用、`aria-busy`、静止的 `bi-hourglass-split` + “检查中…”）；“选中这些章节”（`bi-check2-square`）只勾选章节表里的这些复选框并瞬时定位，下载仍要点“下载选中章节”。章节表里已确认的新章节行在章节名后标“新”（同一个 `.status-badge-update`，提示“{时间} 检查确认的新章节”），不自动勾选。

  | 状态 | 第一行 | 说明（A = 自动检查开 / O = 关） |
  |------|--------|------|
  | 还没检查过（下载时记下了基线） | 还没检查过新章节 | A：下载时记下了上游的 B 话；后台会自动检查，每部漫画大约一天一次 · O：下载时记下了上游的 B 话 · 自动检查已关闭，可以点「立即检查」 |
  | 还没检查过（更新前下载的） | 还没检查过新章节 | A：后台会在一天内自动检查，之后每部漫画大约一天一次 · O：自动检查已关闭，可以点「立即检查」 |
  | 正在检查 | 正在检查新章节… | 只取一次章节列表核对，不会下载任何内容 |
  | 记下了基线 | 已记下上游现有的 U 话 | {时间} 检查 · 以后新出的章节会在这里提示 · 下次约 …（O：· 自动检查已关闭） |
  | 没有新章节 | 没有新章节 | 上次检查：{时间} · 上游共 U 话 · 下次约 …（O：· 自动检查已关闭） |
  | 有新章节 | 徽章“有新章节 · N 话” + 选中这些章节 | {时间} 检查确认 · 检查不会自动下载 · 下次约 …（O：· 自动检查已关闭） |
  | 章节有变动 | 徽章“章节列表有变动” + 新出现 N 话，另有 R 话已不在上游 + 选中新出现的章节 | {时间} 检查 · 请先核对下面的章节列表再决定是否下载 |
  | 没检查成功（没有待下载的新章节） | ⚠ 这次没检查成功：{原因} | A：约 … 自动重试（连不上服务器暂停时：连续几次连不上服务器，自动检查暂停，约 … 再试）· O：自动检查已关闭，可以稍后再点「立即检查」；成功检查过的再加 · 上次成功检查：{时间}（没有新章节 / 已记下 U 话） |
  | 没检查成功（带着新章节） | 照旧显示有新章节 / 章节有变动 | 最近一次检查没成功（{原因}），约 … 自动重试（O：可以稍后再点「立即检查」）；上面的新章节是 {时间} 确认的 |

  原因：连不上服务器 / 服务器响应太慢 / 上游暂时找不到这部漫画（可能已下架） / 服务器返回的章节列表无法识别。时间写“刚刚 / N 分钟前 / 今天 HH:mm / 昨天 HH:mm / M月D日 HH:mm”，将来的写“约 N 分钟后 / 约 N 小时后 / 今天 HH:mm / 明天 HH:mm”。“立即检查”之后的 Toast：发现 N 话新章节（只提示，不会自动下载）/ 章节列表有变动，请核对 / 没有新章节 / 已记下上游现有的 U 话 / 这次没检查成功：{原因} / 刚刚检查过，结果如上；409 时显示服务端的说明。后台检查不弹 Toast。
- 列表只显示确认过的事实：资源库、收藏、下载管理只在已确认有新章节（或章节有变动）时显示标记，还没检查、没有新章节、检查失败都不显示；本地不可读的漫画（只收藏的、文件删了的）什么都不显示。资源库卡片“有新章节 · N 话”（窄卡片可在徽章内换行），收藏表格短写“新章节 · N”（“有”“话”只给读屏），下载管理只放在这部漫画最新的已完成任务卡片上、点开详情选择下载。搜索、首页不变。
- 没有新增动画：沙漏图标静止，唯一的过渡是 Bootstrap `.btn` 自带的；颜色只用上面的 token，没有新的颜色、字体、圆角或间距。

### 只剩压缩包也能离线阅读（2026-09-27）

- 自动打包后删了原图、只剩 `<目录名>.cbz` / `<目录名>.zip`（本程序打包的）或目录第一层别的 `.cbz` 时，仍算“已下载内容 · 可离线阅读”：连续阅读、单页预览都直接从压缩包读页（`/api/preview-archive/<id>/<n>`，只在内存里解出这一页，不写回下载目录），页序与散图同一自然排序，章节按压缩包里的目录。规则在 `core/archive_pages.py`（条目路径安全、只接受不压缩 / DEFLATE、大小上限、逐页 CRC 校验、读的正是检查过的条目）与 `core/local_availability.py`，全站共用。
- 散图和压缩包都在时按页合并：同一章节同一页（相对路径相同，不看扩展名）用散图，压缩包只补散图没有的页，一页不会出现两次。所以自动打包后没删原图时只显示散图；重新下载失败留下几页散图、或只下载了新章节时，压缩包里其余的页照样能读、能导出。自动打包时也按同样规则把目录里所有压缩包（同名的旧包、切换格式前的另一种格式、别的 .cbz）里散图没有的页放进新包，只下载新章节、切换过打包格式都不会让以前打包的章节丢失或被新包“挡住”；有压缩包暂时打不开、有带不过来的页（不支持的压缩方式等）、或要被覆盖的那个打不开时不打包，原图也不删（日志里说明）。空的（中断的下载留下的）或超过单页上限的图片不打包，也不会被当作已打包的原图删掉；删原图前先核对新包（能打开、页数对得上），不对就保留原图。
- 压缩包暂时打不开（被别的程序占用等）时不说成“压缩包损坏”：列表仍按可离线阅读显示，阅读页、单页预览提示“本地压缩包暂时打不开（可能被别的程序占用），请稍后再试”并保留“重试”（单页预览整本打不开时，错误提示里另有“重试”按钮，`btn-outline-primary` + `bi-arrow-clockwise`，只在这种情况下出现）；导出提示“…请稍后再导出”。压缩包本身坏了（校验不对、条目位置不对）才算“压缩包损坏”。
- 0 字节的图片（被中断的下载留下的临时文件）在任何地方都不算页：不算“本地可读”，阅读器、导出不显示，自动打包不打进去也不删除。
- 压缩包重新打包后，已经打开的阅读页里还没加载的页按页名找回同一页；那一页已不在新包里时提示“本地压缩包已更新，请刷新页面后再读”并给“刷新页面”，不会显示别的页。阅读页打开后散图被自动打包进压缩包（删了原图）时同样提示刷新（“这一页已打包进本地压缩包…”），而不是重试不了的“加载失败”。
- 两种格式的压缩包都在（切换过打包格式后重新下载）时新写的优先；优先的那个打不开时用下一个能读的，都读不了才说明原因。
- 状态筛选仍是五档；“下载过 · 文件已删除”改名为 **“下载过 · 本地文件不可用”**，位置不变（失败与未下载之间）。它包含三种原因，每条用同一组徽章说明（utils.js `window.localBadges`，搜索卡片封面、详情、下载管理、收藏、资源库一致）：
  - 文件已删除：`.status-badge-muted`（与原来相同）。
  - 压缩包损坏、压缩包无可阅读图片：`.status-badge-warning`——`--text-primary` 字、`--warning-bg` 底、`--warning` 边框与 `bi-exclamation-triangle` 图标（12px 小字用正文色保证对比度，类别靠边框和图标区分，不只靠颜色）。
- 只剩压缩包也能读时，“可离线阅读”后面跟一个压缩包标记 `.status-badge-archive`：`bi-file-zip` + “CBZ” / “ZIP”，`--text-primary` 字、`--info-bg` 底、`--info` 边框与图标。下载管理里确认散图可读、另外打包过（压缩包本身能读）的任务把同样的标记放在最前（提示“已打包为 CBZ；本地还有散图，阅读时优先用散图”，原来是 `bg-info` + 📦 表情）；压缩包损坏 / 没有图片 / 文件不在、或还没判断完时不放，只由原因徽章说明。资源库窄卡片里原因与压缩包标记可以在徽章内换行。搜索卡片封面上这组标记用 `--bg-secondary` 实心底 + `--shadow-sm`，在任何封面上都看得清，放不下时换行（间距 4px）。
- 压缩包打不开 / 没有图片时不假装能离线读：`/read/<id>` 打开本地阅读页、`/preview/<id>` 打开单页预览，都说明原因并带压缩包文件名，另给“在线阅读”按钮（`bi-globe2`）；各页这类漫画的“阅读”按钮是描边 + `bi-book`（提示“本地压缩包打不开：打开后说明原因”），不说成“未下载：在线阅读”。按钮外观与 `/read` 的去向用同一个依据（只看本地文件：接口里的 `archive_problem(s)`），最近一次任务失败或正在排队时也一样。文件真的不在时照旧转去在线阅读。
- 压缩包整体能读、只有某一页坏了（校验不对）：连续阅读和单页预览在那一页显示服务端的说明（“压缩包里这一页读不出来…”）和“在线阅读这一页”（`bi-globe2`）：按章节（目录名里的 photo_id）和这一页在本章里的位置打开在线阅读的同一页（本地可能只下载了部分章节，不能按本地页码）；章节目录名里没有 photo_id 时按钮只写“在线阅读”，从开头读。不再给重试不了的“重新加载图片”。
- 导出：页的来源与阅读器相同（散图 + 压缩包补齐）。“导出 ZIP”给的是页（保留章节路径和页序，永远不把 .cbz / .zip 套进 ZIP），“导出 PDF”直接用压缩包里的页（只解到系统临时目录，导出后删除，压缩包只打开一次）。只剩压缩包而它损坏 / 没有图片时返回 422，Toast 说明原因和文件名；一页都没有时两种导出都是 404“下载目录中没有图片”（不再给一个空 ZIP）；写临时文件失败时提示“临时空间不足或无法写入临时文件”，不说成压缩包损坏。电脑上装了会接管下载的工具（如 IDM）时，浏览器拿到的是空的 204：页面提示“已交给下载工具保存”，不再另存一个空的 download.zip、也不说“导出完成”。
- 单页预览的错误提示：大图标只放大提示图标本身（原 `.preview-error i` 连按钮里的小图标也放大），“返回下载管理”“在线阅读”两个按钮一行、间距 8px，放不下时换行。

### 右下角快捷导航（2026-09-27）

- 普通页面右下角常驻一个 44px 圆形按钮（`bi-compass`，`--bg-secondary` 底、`--border` 边、`--shadow-md`），距右、下边 16px（含手机安全区）。44px 是触屏可点的最小尺寸，属“按钮高度统一 36px”的有意例外；按钮上方 60px（16 + 44）、页脚底部 76px（16 + 44 + 16）都由它推出，不是新的间距尺度。阅读页（`/read`、`/online`）和单页预览（`/preview`）有自己的底部工具，不显示（模板覆盖 `quick_nav` 块）。
- 展开后是按钮上方 8px 的面板（`--bg-secondary`、`--radius-xl`、`--shadow-lg`、内边距 8px）：第一行“返回 / 到顶 / 到底”，分隔线下两行是与顶部导航相同的六个去处和图标（首页、搜索、下载管理、收藏、资源库、设置）。每格 56×52px，图标 18px、下方 12px 文字（“下载管理”四个字正好放下，属布局尺寸）；当前页用 `--primary` 字色 + `--primary-bg` 底，并带 `aria-current="page"`；不可用的按钮用 `--text-tertiary`，以 `aria-disabled` 表达（保留键盘焦点）。
- 打开方式：鼠标移上去展开、移开 300ms 后收起；点按钮（触屏、键盘 Enter/Space）展开并保持；再点按钮、Esc、点页面别处或焦点离开面板时收起。只有 150ms 的淡入淡出，没有位移或滚动动画；到顶、到底是瞬间跳转。
- 搜索、收藏、资源库与顶部导航共用导航记忆（`data-nav-memory`），回到上次的结果、筛选、页码和位置；在快捷导航上点击和点顶部导航一样，不改“看到的位置”。
- “返回”（2026-09-27 按用户决定改为“上一页”）：回到本标签页里的上一页——和浏览器的后退一样，只在本程序的页面之间——并回到离开那一页时“看到的位置”（与导航记忆同一规则）。经快捷导航、顶部导航还是页面里的链接去的都算，一直按就一页页往回走，不会在两页之间来回；本标签页里前面没有本程序的页面（直接打开、新标签页）时不可用。提示与读屏名称带上一页的名字（如“返回：搜索“关键词””，取不到名字时为“返回上一页”）。每页离开时按浏览历史记录的标识记下名字和位置（本标签页、12 小时、最近 50 条）。与详情页 / 阅读页自己的“返回”和浏览器后退一致，不多出历史记录。
- 层级 1035：在页面内容之上，在 Bootstrap 弹窗（1050 起）与 Toast（1080）之下。Toast 容器上移到按钮上方（16px + 44px），面板展开时再让到面板上方（面板实际高度），不挡住面板里的去处，但至少留一条 Toast 在屏幕内（很矮的窗口里 Toast 盖在面板上，仍看得见、点得到关闭）；面板展开时在 Toast 上按下、悬停或把焦点移过去都不算离开，Toast 不会从指针下跑掉；页脚底部留出按钮的位置，滚到最底时文字不被压住；打印时隐藏。
- 很矮的窗口（横屏手机、放大很多倍）：面板最高到屏幕顶部为止，放不下时在面板里滚动。
- 连点“返回”只算一次；双击时第二下落到刚退回去的那一页上，那一页在点击后 1.2 秒内吞掉紧接着的一次鼠标按压——只要它是双击的第二下，或按在点“返回”的同一处（24px 内；换了页面后点击次数可能从 1 重新数），不会误开别的漫画。系统双击间隔最长可调到 900ms，所以时限取 1.2 秒；只吞这一次，别处的单击和键盘操作照常。
- 上一条历史是手动打开的 `/api/…` 接口、`/static/…` 文件等不算本程序的页面，“返回”不可用。阅读页、单页预览自己按页码回到读到的那一页，不按像素位置。名字取页头里的漫画标题（“阅读“标题””“在线阅读“标题””“预览“标题””）。
- 没有 Navigation API 的旧浏览器无法可靠判断上一条历史是不是本程序的页面（`history.length` 也数后面的记录，来源页在后退时不变），“返回”不可用，提示“这个浏览器用不了，请用浏览器的后退”。

### 界面操作设置（2026-09-27）

- 设置页新增“界面操作”一节，开关“点击资源库卡片空白处打开详情”（`library_card_click`，默认关）。关闭时资源库卡片正文空白处点了没有反应，也不显示手形指针；开启时空白处在新标签页打开详情，正文显示手形指针。封面、标题、“详情”按钮始终可以打开详情，作者、标签、收藏、阅读等按钮只做自己的事。已打开的资源库页面在切回该页、点回该窗口、经后退/前进回来时读取新设置。

### 日志与诊断（2026-09-25）

- 设置页在保存表单之后新增 `section#logs`「日志与诊断」：摘要行（位置、大小、保留 7 天、重复合并）、“只看问题 / 全部”切换、搜索框、刷新与打开日志文件夹按钮，条目按时间倒序。
- 级别徽标只用 `--error / --warning / --info` 及其 `-bg`；日志正文用 `font-mono` 14px，堆栈与附加字段 12px；间距 4/8/16/24；列表区域可滚动（70vh），无动画；所有日志文本用 textContent 渲染。
- 在线阅读中单页加载失败时，提示“在线获取这张图片失败，可以重试。”并附“查看日志”链接到 `/settings#logs`（本地模式文案不变）。
- 在线模式单页失败时先静默等待 3 秒自动重试一次，仍失败才显示上述提示；无新增动画或视觉元素。

### 详情页对齐与在线观看（2026-09-25）

- 详情页封面与信息卡片放在同一个 `.row g-4 align-items-center` 的两列里，垂直居中对齐：封面比信息卡片矮时封面居中，高时信息卡片居中（原先靠封面 float 并排，信息卡片比封面低 24px）。封面尺寸不一：保持原比例，宽不超过列宽和 300px、高不超过 400px，小图不放大，阴影贴合图片本身；章节列表在下方占满整行。页面顶部留 16px，与阅读页一致。封面去掉 Bootstrap `.shadow`，只用主题 `--shadow-md`；无封面或封面加载失败时显示同尺寸的暖色占位（`bg-tertiary` / `text-tertiary`，3:4）。窄屏封面 200px 居中。
- “在线观看”放在章节卡片底部操作栏（下载按钮之后，`btn-outline-primary` + `bi-globe2`，窄屏自动换行）；每个章节行末有同图标的小按钮，从该章第一页开始读。
- 在线阅读复用连续阅读页：页头说明“页面实时从网络加载，不下载、不保存到资源库”，没有“单页翻页”（单页模式只读本地），返回按钮兜底到详情页；阅读位置与本地分开记忆。取不到的章节会跳过并用 warning toast 提示。
- 本地阅读（`/read`、`/preview`）仍只读本地文件，从不在线取图。

### 收藏清单表格与按钮状态（2026-09-26）

- 收藏清单表格在 768–991px（半屏窗口、平板，Bootstrap 容器 696px）不显示“添加时间”列（仍可按添加时间排序），单元格左右内边距取 8px，操作区最小宽 100px（布局尺寸，正好容下“移除 + 阅读”）：表格放进容器，四个操作按钮最多两行。手机宽度下表格仍在 `.table-responsive` 内横向滚动。
- 表格文字经 `--bs-table-color` 等变量取 `--text-primary`（Bootstrap 默认纯黑）。
- 描边按钮（success / info / danger / warning）与已收藏的实心星形同样通过 `--bs-btn-*` 变量：键盘聚焦、按下时填充对应状态色、白字，聚焦光晕统一主色蓝。
- 收藏星形是切换按钮：读屏名称固定为“收藏”，状态只用 `aria-pressed` 表达，悬停提示仍说明点击效果。收藏页标题栏与资源库一样可以换行，窄屏时按钮组整体换到下一行。
- 收藏的状态选项顺序与资源库、与“排序：下载状态”相同（2026-09-27）：全部 → 已下载内容·可离线阅读 → 排队中/下载中 → 失败 → 下载过·文件已删除 → 未下载；筛选键、数量和已收藏的链接不变。（“下载过·文件已删除”后改名为“下载过·本地文件不可用”，见“只剩压缩包也能离线阅读”）
- 收藏筛选栏（2026-09-27）：≥1200px 搜索框、状态、排序、搜索按钮一行（4/3/3/2 列）；768–1199px 搜索框独占一行，状态、排序、按钮在第二行（5/5/2 列）；手机宽度每个控件一行。原来 768–991px 状态框只有约 166px，“下载过 · 文件已删除 / 已下载内容 · 可离线阅读 / 排队中 / 下载中”带上数量后显示不全，992–1199px 数量到三位数时也会截断。只改栅格列，不改文案、颜色、间距。

本项目已锁定 `project_theme.locked = true`，后续所有 UI 修改必须遵守：

1. **颜色**必须使用 eye-care token 中的色值
2. **不可混用** light / dark 色值
3. **间距**必须遵循 8 点网格
4. **字体**层级必须遵守 typography token
5. **圆角/阴影**必须遵守对应 token
6. 如需覆盖上述规则，必须**先更新本 DESIGN.md**，再改代码
7. AGENTS.md 的行为规范完全适用（见 `D:\Hermes\templates\AGENTS.md`）

---

*本文件由 DD 自动生成，基于 `D:\Hermes\templates\DESIGN.md` 母模板。*
*生成时间：2026-07-02 · 基于代码实际状态*
