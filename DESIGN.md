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

当前共 **9 个页面**：

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
POST /api/jobs/<job_id>/retry        → 重试任务
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
| `jobs` | 下载任务 | job_id, album_id, status, total_pages, done_pages |
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
- 搜索返回恢复条件、页码、结果及滚动位置，搜索结果快照仅保存在会话范围且最长使用 30 分钟。

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
