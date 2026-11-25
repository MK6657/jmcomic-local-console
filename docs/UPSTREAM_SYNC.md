# 上游同步与 UI 维护指南（Fresh Eye-Care Atelier v3.7）

本重构严格采用 **「核心层最小钩子 + 表现层独立隔离」** 分层设计，使后续同步上游 `MK6657/jmcomic-local-console` 提交及 `jmcomic` SDK 版本升级尽可能做到无冲突或极小冲突。目录总览见 [docs/README.md](README.md)。

---

## 一、分层与文件边界

### 1. 上游核心层（极小改动，合并上游直接快进）
以下模块与逻辑脚本基本保持原样，同步上游时可直接接受上游变更：
- **核心下载 / 抓取 / 存储 / SDK 适配**：`core/` 下除独立扩展模块（`ui_themes.py`、`thumb_cache.py`）以及极小钩子（`settings.py` 主题/特效默认项、`download_folios.py` 标题回填、`jm_service.py` 完成后写回真实标题）以外的全部模块。
- **页面与业务 API 路由**：`routes/` 下仅保留 4 处极小薄钩子：`api_album.py`（封面 `?w=` 缩略图）、`api_settings.py`（`ui_fx_*` 快路径与 `POST /api/settings/reset`）、`api_jobs.py` & `page_routes.py`（调用 `resolve_job_titles` 修正缺失/纯数字标题）。
- **经典版兜底与核心业务 JS**：`templates/classic/`、`static/classic/`、`static/css/style.css`，以及绝大多数业务脚本保持原接口不变（`library.js`、`downloads.js` 保留原有 `export-zip` / `export-pdf` 动作兼容的同时新增 `.export-picker` 下拉导出；`detail.js` 仅补充封面 `fetchpriority="high"`）。

### 2. 本版独立扩展层（UI / 主题 / 混搭特效 / 轻量化）
所有新设计与性能优化集中收拢于独立文件：
- **设计系统与单源注册表**：`DESIGN.md`、`core/ui_themes.py`（9 套主题：6 套护眼 + 像素/科技/二次元 3 套风格；4 套特效风格：`calm` / `pixel` / `cyber` / `anime` + `auto` 联动与总开关）
- **本地封面等比缩略图缓存**：`core/thumb_cache.py`（含进程内有界 LRU 命中缓存，减少热路径文件系统 `stat`）
- **零依赖轻量化传输统计**：`static/atelier-ui/transfer.js`（2.2 KB 原生 JS，替代 53.8 KB Vue 运行时）、`static/atelier-ui/transfer.css`
- **视觉样式与解耦特效层**：
  - 主题与页面样式：`static/css/{atelier,atelier-shelf,atelier-downloads,atelier-settings,reader,preview}.css`
  - 风格外观与动效过渡：`static/css/atelier-fx.css`（纯 CSS 过渡、页面入场动画与一键关停 `html[data-fx="none"]`）
  - 零空闲开销交互反馈：`static/js/atelier-fx.js`（事件委托 + 节流点击微效 + `/` 快速聚焦搜索）
- **页面结构与外壳联动**：`templates/{base,index,library,settings}.html`、`static/js/{atelier-shell,home,settings}.js`、`app.py`（`_inject_ui_theme` 服务端直出主题与特效属性，零闪屏）

### 3. 文档归档层（`docs/`）
- 现行使用/发行/维护文档保留在 `docs/` 根目录；
- 长期架构分析归类到 `docs/architecture/`；
- 历史验收与审计记录归档到 `docs/archive/`；
- `tools/build_windows.py` 与 `tools/package_atelier_source.py` 均兼容新旧文档目录结构。

---

## 二、同步上游提交建议流程

### 同步上游仓库代码
```powershell
git fetch origin
git merge origin/main   # 或上游新分支
```
- `core/`、`routes/`、`static/css/style.css` 和各核心业务 JS 通常可直接无冲突合并。
- 若上游改动 `core/settings.py`、`routes/api_album.py`、`routes/api_settings.py` 或 `routes/api_jobs.py`，只需保留薄钩子调用即可。
- 若上游向 `static/css/style.css` 底部追加了新功能样式，本版会自动继承，无需搬运。

### 同步上游 `jmcomic` 节点 / 协议升级（遵守 `AGENTS.md` 第 28–34 条）
1. 查阅 [JMComic-Crawler-Python Releases](https://github.com/hect0x7/JMComic-Crawler-Python/releases) 核实协议版本与默认分流策略（保持由 SDK 按 `client.impl` 选域名，不写死旧 API 地址）。
2. 更新 `requirements.txt` 中的锁定版本并在项目 `.venv` 中安装与核验：
   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
   .\.venv\Scripts\python.exe -m pip check
   ```
3. 运行离线回归与编译检查：
   ```powershell
   .\.venv\Scripts\python.exe -m compileall -q app.py launcher.py core routes
   .\.venv\Scripts\python.exe -m pytest -q
   ```
4. 重启本地服务进程并核实健康检查 PID 与少量真实搜索请求。

---

## 三、扩展主题或动效风格

### 新增主题（3 处同步修改）
自动受到 `tests/test_atelier_shell.py` 的全表面 WCAG AAA/AA 对比度校验保护：
1. 在 `core/ui_themes.py` 的 `THEME_SPECS` 中登记一条 `ThemeSpec`（指明 `group`、`scheme` 与 `default_fx`）。
2. 在 `static/css/atelier.css` 与 `static/css/atelier-settings.css` 添加 `[data-theme="<key>"]` 与 `[data-pref-theme="<key>"]` 令牌，如需专属几何造型可在 `static/css/atelier-fx.css` 添加规则，并同步记录到 `DESIGN.md`。
3. 运行 `.\.venv\Scripts\python.exe -m pytest -q tests/test_atelier_shell.py tests/test_settings_persistence.py` 验证对比度（正文 ≥ 10:1，次要/语义色 ≥ 4.5:1）与零闪屏直出。

### 新增动效预设
1. 在 `core/ui_themes.py` 的 `FX_PRESET_SPECS` 登记新 `key`。
2. 在 `static/css/atelier-fx.css` 添加 `html[data-fx="<key>"]` 过渡/微效样式，并在 `static/js/atelier-fx.js` 添加点击微粒逻辑。
3. 保证 `html[data-fx="none"]` 与 `prefers-reduced-motion: reduce` 下所有特效零开销关闭。
