# 项目结构与文档索引

为方便日常维护与上游同步，仓库与文档已按职责分层归档；同时完整兼容 Windows 便携打包脚本（`tools/build_windows.py`、`tools/package_atelier_source.py`）与 CI 流程。

---

## 一、仓库目录结构一览

```text
.
├── app.py / desktop.py / launcher.py / launcher.pyw / jmcomic_cli.py   # 应用入口与启动器
├── start.bat / start-demo-preview.bat                                  # Windows 一键启动 / 样书预览
├── DESIGN.md / AGENTS.md / README.zh-CN.md / README.md                 # 设计规范与使用说明
├── core/                      # 下载内核、SQLite 数据层、主题注册(ui_themes.py)、缩略图缓存(thumb_cache.py)
├── routes/                    # 页面路由与 REST / SSE 接口
├── templates/                 # Atelier 主模板（partials/ 公共组件；classic/ 经典兜底）
├── static/                    # css/ js/ atelier-ui/ classic/ shared/ vendor/
├── tests/
│   ├── README.md              # 测试分层说明
│   ├── test_*.py              # 离线 pytest 自动回归套件
│   ├── check_startup.py       # 本地 HTTP 启停验证
│   ├── checks/                # 13 份专项性能探针与 Windows 便携包验收脚本
│   ├── js/                    # 5 份 Node / 浏览器行为测试脚本
│   └── legacy/                # 6 份历史联调/压测归档（pytest 自动排除）
├── tools/
│   ├── build_windows.py       # Windows 便携 EXE 构建与自检
│   ├── package_atelier_source.py / release_licenses.py / seed_atelier_demo.py
│   ├── licenses/              # 第三方许可证原文
│   └── legacy-vue-transfer/   # 旧版 Vue 3 统计组件归档（已由 2.2 KB 原生 JS 替代）
└── docs/
    ├── README.md              # 本索引
    ├── ATELIER_WINDOWS.md / WINDOWS_RELEASE.md / DATA_MAINTENANCE.md / UPSTREAM_SYNC.md
    ├── architecture/          # 7 份长期架构与排障技术分析
    └── archive/               # 16 份历史审计与验收归档
```

---

## 二、文档分类索引

### 1. 现行指南（`docs/`）
- [ATELIER_WINDOWS.md](ATELIER_WINDOWS.md) — Windows 藏书阁版（Atelier Edition）使用与离线样书预览说明
- [WINDOWS_RELEASE.md](WINDOWS_RELEASE.md) — Windows 便携版构建、备份、升级与自检指南
- [DATA_MAINTENANCE.md](DATA_MAINTENANCE.md) — 数据库、下载目录、缓存与本地数据维护规范
- [UPSTREAM_SYNC.md](UPSTREAM_SYNC.md) — 上游仓库同步、SDK 协议升级与主题/特效扩展维护指南

### 2. 架构与技术分析（`docs/architecture/`）
长期有参考价值的设计与排障分析文档：
- [BACKEND_REFACTOR.md](architecture/BACKEND_REFACTOR.md) — 后端重构方案记录
- [download_pipeline_design.md](architecture/download_pipeline_design.md) — 下载管线与并发调度设计
- [download_link_analysis.md](architecture/download_link_analysis.md) — 下载链路分析
- [READER_SEARCH_RETURN.md](architecture/READER_SEARCH_RETURN.md) — 阅读页与搜索状态记忆机制
- [JS_EXTRACTION_PLAN.md](architecture/JS_EXTRACTION_PLAN.md) — 前端脚本抽离与模块化记录
- [bfcache_analysis.md](architecture/bfcache_analysis.md) — 浏览器 bfcache 行为分析
- [前端卡死分析-多次跳转后卡死.md](architecture/前端卡死分析-多次跳转后卡死.md) — 前端多次跳转卡顿排障记录

### 3. 历史验收与审计归档（`docs/archive/`）
按里程碑归档的历史复检、审计处置与验收记录（不影响现行代码运行；构建脚本仍自动兼容回退查找所需文件）：
- **Atelier / vNext 验收**：`ATELIER_ACCEPTANCE.md`、`ATELIER_DOWNLOAD_PATH_QA.md`、`WINDOWS_VNEXT_ACCEPTANCE.md`、`WINDOWS_VNEXT_MEASUREMENTS.md`、`WINDOWS_VNEXT_REVIEW.md`、`AUDIT_DISPOSITION_2026-11-18.md`
- **Windows 历史构建**：`WINDOWS_ACCEPTANCE.md`、`WINDOWS_CANDIDATE_B.md`
- **后端审查与历史维护**：`AUDIT_FIXES.md`、`BACKEND_REVIEW.md`、`GITHUB_REVIEW.md`、`INDEPENDENT_BACKEND_AUDIT.md`、`PROJECT_HANDOFF.md`、`EDGE_FIXES_2026-09-08.md`、`MAINTENANCE_2026-09-08.md`、`OPTIMIZATION_2026-09-08.md`
