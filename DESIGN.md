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
```

### 4.10 导出 API

```
GET /api/export/wishlist/format                     → 收藏导出（指定格式）
```

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
- 标记统一为 `.offline-badge`：✓ 图标 + “已下载 · 可离线阅读”（表格内简写“可离线阅读”，完整文字放在提示与读屏文字里），只用 `--success / --success-bg`；搜索卡片封面角标同样是“✓ 已下载 · 可离线阅读”。下载管理里可读的任务另有“预览”（单页，图标 `bi-images`，只读本地文件）。
- “阅读”按钮每部漫画都有（搜索、资源库、收藏、下载管理的已完成与失败任务），统一由 utils.js `window.readLink` 生成，链接始终是 `/read/<id>`：服务端在点击时判断，本地可读 → 本地连续阅读，否则 302 到 `/online/<id>`（保留 `?page=`），页面打开后才下载完成或文件被删除也能去对地方。外观只表达判断结果：已下载 `btn-primary` + `bi-book`；未下载 `btn-outline-primary` + `bi-globe2`（与“在线观看”同一图标），读屏文字“阅读（在线）”；还没判断时描边 + `bi-book`。详情页保持“在线观看”常驻、“阅读”只在已下载时出现（未下载时两者去向相同，不重复放两个）。
- 详情页“返回搜索”改为“返回”：从站内页面进入时后退（保留结果与滚动位置），直接打开时回到本标签页最近一次搜索。
- 收藏：按下载状态筛选（未下载 / 排队中·下载中 / 已下载可阅读 / 失败），排序新增最早添加与下载状态；筛选、排序、页码写入地址栏。资源库：状态分组与收藏页一致且互不重叠（可离线阅读 / 排队中·下载中 / 失败 / 下载过·文件已删除 / 未下载），新增“排序：作者”，点作者名只看该作者（沿用标签筛选的选中胶囊样式，可 ✕ 清除）。
- 所有含标题、作者、标签的动态内容用 textContent / data-* 渲染，事件统一委托，页面上不再有内联 onclick。

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
