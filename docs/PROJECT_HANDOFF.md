# JMComic Local Console 项目接手与复检说明

**接手 2026-11-18 后端候选：** 本地分支 `codex/windows-vnext` 从 `552bb65` 分出。先读[副本复检步骤](WINDOWS_VNEXT_REVIEW.md)和[本次实际验收](WINDOWS_VNEXT_ACCEPTANCE.md)，用 `git diff 552bb65..HEAD` 审查。下文的分支、命令和通过数是 2026-11-17 的旧版基线；回退用的旧包见[旧版发行验收](WINDOWS_ACCEPTANCE.md)。

## 1. 当前结论与产品目标

这是面向个人使用的Windows本地下载控制台：Python核心、Flask页面/API、Waitress服务、SQLite数据，以及jmcomic上游SDK。它支持搜索、详情、下载队列、收藏、资源库、本地/在线阅读、更新检查、ZIP/PDF导出和备份恢复。

用户目标是提高日常可靠性、可维护性与使用体验，同时控制以后同步上游的工作量。Windows优先，后续Docker与Windows共用业务核心。当前选择保留Python及SDK边界，按业务职责拆分；Rust重写、前端风格改版、新客户端外壳及Docker部署均未交付。

后端已经完成四批审计修复并生成Windows复检包，正在等待独立复检。原报告的33项并非全部已修，也不能把每一项都视为已经成立的安全漏洞。第8节明确列出已修范围，第9节列出保留项及证据等级。

## 2. 获取正确代码与固定版本

| 项目 | 值 |
|---|---|
| 公开仓库 | <https://github.com/MK6657/jmcomic-local-console> |
| 当前开发/审查分支 | `codex/refactor-windows` |
| 四批审计修复的产品基线 | `72783a287e13afbaba29de03a7d88fa0977bc708`，简称`72783a2` |
| 重构前/main基线 | `c310e3f584d077771324a02b8a611bddb1de404c` |
| 原审计产品快照 | `7ae6ccbaea7a91296df1e211e8920190235d0df7` |
| 原审计交付版 | `e4a6057`，审计修复增量从此之后查看 |
| 完整重构差异 | `c310e3f..72783a2` |
| 四批审计修复差异 | `e4a6057..72783a2` |
| 本机开发目录 | `C:\Users\ggy\.codex\worktrees\windows-backend-refactor\jmcomic-local-console` |

文档交接提交可能晚于`72783a2`，不表示产品代码又发生变化。克隆后记录自己的完整HEAD与工作区状态；若后续出现产品修复，以实际差异为准。`main`仍保留原版本，日常实例`D:\jmcomic-local-console`不是待审工作区。

```powershell
git clone --branch codex/refactor-windows --single-branch https://github.com/MK6657/jmcomic-local-console.git jmcomic-backend-review
Set-Location jmcomic-backend-review
git status --short --branch
git show -s --format="%H %s" HEAD
git diff --stat c310e3f..72783a2
git diff --stat e4a6057..72783a2
git diff --name-only 72783a2..HEAD
```

保留提交历史，不加`--depth 1`。没有Git时可下载正确分支的ZIP，但ZIP不含历史，不能在其中运行提交比较命令。GitHub源码不包含本机虚拟环境、个人数据或便携EXE；异机不要依赖本文中的开发机绝对路径。

## 3. 技术栈与版本证据

| 层 | 当前实现/版本 |
|---|---|
| 本机验证Python | 3.11.15，Windows x64 |
| 云端验证 | GitHub Windows runner，Python 3.12 |
| HTTP/页面 | Flask 3.1.3、Waitress 3.0.2、Jinja模板、原生JavaScript |
| 上游SDK | jmcomic 2.7.10，协议/解密/客户端类型与域名由SDK维护 |
| 数据库 | SQLite，当前`user_version=4` |
| 图片/PDF | Pillow、img2pdf 0.6.0；PDF合并使用pikepdf |
| Windows构建 | PyInstaller 6.22.3，contrib hooks 2026.10，文件夹式便携包 |
| UI约束 | 根目录`DESIGN.md`锁定`eye-care`，`project_theme.locked=true` |

直接运行依赖见[requirements.txt](../requirements.txt)，测试见[requirements-dev.txt](../requirements-dev.txt)，构建见[requirements-build.txt](../requirements-build.txt)。源码开发环境允许部分依赖浮动，例如 Pillow 使用下限；Windows 发行另有[构建版本约束](../requirements-windows-build-constraints.txt)，包含间接依赖。实际发行清单在对应包的 `release.json`。

Python 3.10在声明支持范围内，但本轮没有3.10实机验收；Future超时兼容由专项模拟覆盖。Linux/macOS结果也不能代替原生Windows互斥锁、junction、停机事件或EXE验收。

## 4. 结构与阅读顺序

### 入口和运行关系

```text
start.bat / Windows便携EXE
  -> launcher.py：定位环境、核实服务身份、启动/打开/协作退出
  -> app.py main()：实例锁、监听端口、Flask应用、后台Runtime、HTTP排空
       -> create_app()：数据库初始化、注册页面与API；不启动下载调度
       -> ApplicationRuntime：恢复中断任务，启动消费者/生产者，统一停止
       -> routes/：HTTP输入输出
            -> task_commands / job_manager / jm_service / 数据与文件模块
                 -> sdk_adapter -> jmcomic -> 上游
```

`jmcomic_cli.py`是显式命令入口，复用配置、SDK构造和维护模块。CLI直接下载与Web任务队列仍是不同入口，不要假设CLI下载自动成为Web队列任务。

### 关键文件地图

| 文件/目录 | 职责和接手重点 |
|---|---|
| [app.py](../app.py)、[launcher.py](../launcher.py)、[desktop.py](../desktop.py) | 应用入口、服务身份、端口与Windows启动外壳；修改后检查真实启动/退出流程 |
| [core/runtime.py](../core/runtime.py)、[core/http_server.py](../core/http_server.py) | 后台所有权、部分启动回滚、停止预算、Waitress连接/工作线程排空 |
| [core/instance_lock.py](../core/instance_lock.py)、[core/process_control.py](../core/process_control.py) | Windows命名互斥锁和私有停机事件；主进程未退出时不能提前放弃所有权 |
| [core/task_commands.py](../core/task_commands.py) | 入队、复用、重试、完成、取消、恢复的事务命令；不负责联网或创建线程 |
| [core/job_manager.py](../core/job_manager.py) | 调度、执行器、并发名额、逐任务控制门；SQLite等待不应占用全局管理锁 |
| [core/jm_service.py](../core/jm_service.py) | SDK业务调用、共享client租约、详情缓存、下载编排；仍是较大的模块，避免继续堆入无关职责 |
| [core/sdk_adapter.py](../core/sdk_adapter.py)、[core/upstream.py](../core/upstream.py) | SDK配置/构造和稳定错误映射；不要在各路由重复处理SDK细节 |
| [core/transfer.py](../core/transfer.py)、[core/image_download.py](../core/image_download.py) | 独占client的图片传输预算、停滞处理、图片主机切换 |
| [core/background_calls.py](../core/background_calls.py)、[core/metadata_budget.py](../core/metadata_budget.py) | 元数据在途调用容量、总deadline、停止准入与client归还 |
| [core/database.py](../core/database.py)、[core/database_schema.py](../core/database_schema.py) | DAO、事务、迁移，以及启动/恢复共用的可信schema；改结构必须同时考虑旧库和备份 |
| [core/local_availability.py](../core/local_availability.py)、[core/chapter_inventory.py](../core/chapter_inventory.py) | 已成功资源是否真的可读、章节可用性，不能仅凭目录存在或旧任务状态判断 |
| [core/storage.py](../core/storage.py)、[core/path_guard.py](../core/path_guard.py) | 同盘暂存/校验发布、临时文件过滤、路径边界；保留第9节的本机替换竞态限制 |
| [core/packer.py](../core/packer.py)、[core/archive_pages.py](../core/archive_pages.py) | CBZ/ZIP打包、归档成员索引与读取；成员名和外部解压语义仍有保留项 |
| [routes/api_export.py](../routes/api_export.py)、[core/export_budget.py](../core/export_budget.py) | ZIP/PDF租约、扫描/图片/输出预算、逐页PDF临时文档及清理 |
| [core/settings.py](../core/settings.py)、[routes/api_settings.py](../routes/api_settings.py) | 校验、事务持久化、缓存失效和运行时应用反馈；保存成功与立即应用失败要区分 |
| [core/online_reader.py](../core/online_reader.py) | 在线章节、图片缓存与预取；停止后不得继续发布新缓存 |
| [core/update_checker.py](../core/update_checker.py)、[core/update_store.py](../core/update_store.py) | 新章节检查、基线、租约、结果与停止门槛；检查不自动创建下载任务 |
| [core/backup.py](../core/backup.py)、[core/restore.py](../core/restore.py) | SQLite快照、资源清单/文件、校验与恢复到全新实例目录 |
| [core/path_utils.py](../core/path_utils.py)、[core/desktop_platform.py](../core/desktop_platform.py) | 启动时路径配置、可选Windows打开文件夹能力；为后续Docker保留适配边界 |
| `routes/`、`templates/`、`static/js/` | HTTP契约、现有页面和交互；UI修改前必须读`DESIGN.md`及现有页面 |
| `tests/`、`tools/build_windows.py`、`.github/workflows/tests.yml` | 离线回归、显式本机验收、发行与Windows CI |

建议先读`AGENTS.md`、本文、`task_commands.py`、`runtime.py`、`database_schema.py`，再沿具体问题进入业务模块和对应测试。不要把[BACKEND_REFACTOR.md](BACKEND_REFACTOR.md)中较早批次的中间状态当成当前完整结论。

## 5. 必须保留的业务规则

1. **数据库是任务状态依据。** `update_job`不允许直写status，状态改变经过条件转换或业务命令；取消/完成竞态不能由迟到线程覆盖终态。
2. **任务和成功资源是两类记录。** 清任务历史保留`resource_catalog`与实际文件；撤销完成状态会撤销该次证据。兼容函数`get_completed_job_by_album_id()`可能返回资源快照，不保证原任务历史存在。
3. **完成/失败/取消是组合事务。** 任务、资源trigger、收藏派生状态及最终章节基线不能部分提交；通知不能伪造一个未持久化的成功终态。图片文件与数据库不构成跨介质事务，有效文件可保留供重试。
4. **入队有活跃任务范围的幂等。** 同专辑相同章节集合复用queued/running/paused；整部与显式部分章节不同。同专辑不能并行下载。不存在跨完成生命周期的永久幂等键承诺。
5. **队列时间与创建时间分开。** `queued_at`用于队列；重启将中断running排到原等待队列之后，created_at不改，用户paused保持暂停。
6. **停止是关闭准入并等待实际结束。** 超时仍在途的future、执行器或curl句柄保留所有权；不强杀Python线程，不并发关闭正在请求的client，也不以分离后台线程冒充已排空。
7. **暂停、取消、正常停机不同。** 停机不是下载失败或用户取消；暂停通知与恢复唤醒要串行。已通过结果发布门槛的事务允许完成，停止继续等待它。
8. **未知页数不能当作已知0页缓存。** 元数据补全超时返回稳定副本；未完成详情不写长期完整缓存。
9. **文件先暂存、校验后发布。** 失败保留旧文件，暂存页不进入阅读、导出和可读判断；当前不是任意本机攻击者模型下的句柄级文件系统隔离。
10. **设置先持久化，再报告立即应用结果。** 网络/调度应用失败应返回已保存及警告，不能让用户误以为没有保存。0值、关闭开关及未修改字段必须保留。
11. **检查新章节不代替下载。** 自动/手动检查共用受控入口，停止前未获发布许可的迟到结果丢弃，原检查租约保留。
12. **构造Flask应用不启动后台任务。** 测试客户端、源码导入和诊断不能隐式开始下载；服务启动由正式入口统一拥有。

### 数据库概要

| 表 | 作用 |
|---|---|
| `jobs` | 六种任务状态：queued/running/paused/completed/failed/canceled；含创建/队列时间、输出位置与进度 |
| `resource_catalog` | 独立的成功资源证据、路径、章节选择、排序及superseded标记 |
| `settings` | 持久化设置；可含私密代理配置 |
| `wishlist` | 收藏；最终展示状态结合活跃任务和资源可读性推导 |
| `album_tags`、`album_meta` | 标签、标题、作者、封面等本地元数据 |
| `album_detail_cache` | 可重建的详情缓存；不要把它当作完成资源证据 |
| `album_update_checks`、`update_check_log` | 章节基线、检查结果/退避/租约和检查记录 |
| `search_history` | 本地搜索历史 |

schema v4要求受支持的完整表、索引和三条资源trigger。历史v0–v3按可信定义兼容；各版本都要求jobs/settings，v3资源表/索引/trigger必须齐全。启动在应用写连接前检查，迁移先保存原库快照；不以建空业务表或降低版本号绕过异常。v3队列回填隔离资源trigger并在同一事务回滚。恢复按rowid映射路径，涵盖SQLite允许的NULL文本主键。

## 6. HTTP契约与容量

| 接口 | 当前接手重点 |
|---|---|
| `GET /api/system/health` | 启动身份与健康检查；200不等于真实上游可用 |
| `GET /api/search`、`GET /api/album/<album_id>` | 结构化上游错误、共享调用容量、总时间预算及详情缓存 |
| `POST /api/jobs` | album_id/photo_ids校验；创建201、活跃任务复用200，返回job_id/created/reason |
| `GET /api/jobs` | page/page_size/status；默认50、上限200，同一快照计数与取页；不能继续假设一次取全历史 |
| `GET /api/jobs/<job_id>/events` | SSE HTTP租约，关闭/异常后归还；tracker删除不能提前释放流容量 |
| `POST /api/jobs/<job_id>/{pause,resume,cancel,retry}` | 状态守卫、逐任务控制与执行器实际归属；缺失对象的状态码统一仍有保留项 |
| `GET/POST /api/settings` | 完整编辑配置、no-store、持久化与运行时应用警告；不是服务器版凭据掩码协议 |
| `GET /api/settings/export`、`POST /api/settings/import` | 常规导出排除proxy；手工导入文件仍允许显式proxy，这是已文档化行为 |
| `GET /api/preview/<album_id>`、`GET /api/preview-img/<path>` | 下载资源可读性与本地路径规则 |
| `GET /api/online/<album_id>`、`GET /api/online-img/<photo_id>/<index>` | 在线章节、前台取图/预取及缓存停止规则 |
| `POST /api/export/<job_id>/{zip,pdf}` | 共用导出租约及大小/页数/暂存预算，响应结束前不提前释放 |

其他收藏、标签、批量预览/确认及更新检查接口，以各`routes/*.py`的Blueprint前缀和装饰器为准。API保留`status/message`，按场景附`reason/error_kind/upstream_status`；不要在公开响应附带原始上游正文或代理异常文本。

主要边界：元数据调用最多16个，搜索最多25秒，详情/在线专辑查询最多20秒且受保存超时限制；章节补全最多4个future滚动提交。单次图片操作所有尝试共享预算，上限120秒。更新检查共用45秒传输预算，单次调用上限20秒。SSE全局32/单任务8、客户端最多5条，流迭代寿命300秒。ZIP/PDF共用1个并发租约，主要限制为10000文件/2000页/50000扫描项，各类总字节预算及300秒协作预算见[AUDIT_FIXES.md](AUDIT_FIXES.md)。

这些预算不保证中断任意native解码、磁盘挂起或已准入数据库事务；SSE迭代寿命也不等于强制终止底层输出背压。SDK构造与HTML首次域名发现仍是单独验证边界。

## 7. 已有验证与交付物

以下证据固定对应产品代码`72783a2`，不能自动用于后来发生变化的代码。

| 验证 | 结果/范围 |
|---|---|
| 本机完整离线pytest | 2003 passed、2 skipped，213.65秒；两项跳过为本机文件符号链接权限 |
| Windows/Python 3.12 CI | [运行44025467233](https://github.com/MK6657/jmcomic-local-console/actions/runs/44025467233)，2005 passed、417.52秒，无跳过 |
| 静态/依赖检查 | Python compileall、16个JS逐文件node --check、pip check、Git差异检查通过 |
| EXE自检 | 资源、设置API、图片/PDF/SDK/libcurl的本机模拟；upstream_requests=0 |
| EXE生命周期 | 两次真实服务启动/协作退出、设置持久化、任务恢复、临时数据清理 |
| 实际ZIP | 哈希/CRC、中文空格路径、无系统Python PATH、便携backup-info、继承实例不触碰、忙锁启动拒绝 |
| 专项行为 | 下载停滞后下一任务继续、后台请求排空、SSE拒绝后健康响应、分页/本页重试、逐页PDF、元数据总预算、schema与恢复故障；细节见修复记录 |

Windows包在开发机的相对路径为`releases/windows-x64-5f04eae8af14/JMComic-windows-x64.zip`，Git源码克隆中不含它。包大小34,159,517字节，SHA-256：

```text
a00e07cab3845e92d17cbdead9aa3752e1c5e09eb0f6ed577b466da1c19aab79
```

源码输入指纹为`5f04eae8af14fa4f4398b51c231a2f1bdffaf109787d73d13750060c17d61778`。相同发行目录还有`release.json`、`SHA256SUMS.txt`和`BUILD_VERIFICATION.md`；不要使用旧批次包的哈希。

本轮审计修复的真实上游请求为0。早期个别搜索成功属于当时历史记录，不代表现在的搜索、详情、阅读、下载已逐项验收。尚未完成干净Windows虚拟机、签名安装包、Python 3.10实机、Linux/Docker、跨RDP会话或大资源库压力验收。

## 8. 已处理的原审计项

“已处理”只指下列范围已有实现和回归，不是原报告每一种推断均成立，也不保证任意环境没有其他问题。

| 批次 | 原编号 | 已处理范围 |
|---|---|---|
| 第一批 | F02/F04/F05/F06/F08/F12/F14/F18/F19 | 防嵌入、业务组合事务、状态写入口、API JSON错误、稳定详情副本/未知页数、分支CI、Future超时兼容、异常图片逐页处理、取消后的收藏一致性 |
| 第二批 | F09/F11/F15 | 导出并发/预算/逐页PDF、任务分页、SSE准入与客户端连接释放 |
| 第三批 | F07/F13/F16 | 元数据容量/总预算/租约、更新检查停止与结果发布、数据库等待移出管理锁 |
| 第四批 | F24/F30 | 启动/恢复共用可信schema、旧结构兼容、写前拒绝、资源目录保留及恢复路径完整映射 |

共涉及原报告17个编号。实现及边界见[AUDIT_FIXES.md](AUDIT_FIXES.md)，不应把“覆盖17个编号”理解为修复17个同等严重的独立漏洞。

## 9. 保留项、条件性风险与建议顺序

下面覆盖其余16个编号。证据口径为：早期独立复核曾做过相关临时实验，本次交接核对当前代码/历史记录，**没有重新执行全部攻击或压力矩阵**。接手者应在新提交上独立复现，不直接沿用外部报告的严重性。

### 优先研究的文件及Windows边界

| 编号 | 保留情况与证据边界 | 入口和完成条件 |
|---|---|---|
| F26 | 路径校验后、读取前可被另一具有本机目录修改权限的执行者替换junction；历史临时实验复现根外合成标记被读取。不是已确认的远程路径穿越。 | `core/path_guard.py`、`routes/api_preview.py`等读取调用方。研究同一文件句柄校验/读取，覆盖根目录、祖先目录替换与Windows junction；不能只增加一次realpath检查。 |
| F31 | 普通异常清理已有；进程硬退出仍可能留下暂存，暂存路径长度增加也可能先于最终路径失败。历史实验确认过这两点。 | `core/storage.py`、下载/打包调用方。短路径及有活跃任务保护的残留回收；不得删除另一执行器仍使用的attempt或合法图片。 |
| F10/F25 | 归档页名仍可传到新ZIP/CBZ；Windows特殊设备名覆盖尚不完整，CONIN$/CONOUT$是明确保留例子。历史合成样本验证规则缺口，未证明外部解压器已产生设备访问/ADS。 | `core/archive_pages.py`、`core/packer.py`、`routes/api_export.py`、`core/validation.py`、`core/restore.py`。统一成员名/路径组件规则，兼顾章节顺序、重名和既有合法归档。 |
| F03 | 互斥体仍在Windows Local会话命名空间；跨会话共享同一可写实例目录没有完成实机验收。同会话互斥已有测试。 | `core/instance_lock.py`、`app.py`、CLI维护入口。若支持跨会话，设计实例目录所有权与权限，验证服务/备份/迁移/CLI一致互斥及退出释放。 |
| F27 | SO_REUSEADDR与探测后绑定仍有时间窗口；绑定失败已有重试/回退，不等于必然接管错服务。历史验证的是机制及失败处理。 | `app.py::_bind_preemptive`。评估独占绑定/同一socket完成选择与交给Waitress，保留占用提示、端口回退和服务身份核实。 |
| F28 | 启动竞争、OpenProcess权限拒绝和清理异常仍需组合验收。只有组件级模拟证据，没有完整跨权限/跨会话双启动实测。 | `launcher.py`、`core/process_control.py`、运行身份记录。身份未知时不按端口/Python名结束进程；不要以Job Object强杀替代当前协作停机语义。 |
| F29 | 资源完整备份有多遍哈希/复核成本，共用预算；旧备份在失败时保留。未用代表性大型库证明性能上限。迁移快照为DB-only且不扫描漫画库。 | `core/backup.py`。先测实际规模、耗时、空间与中途失败，再优化；保留一致性检测，不为提速去掉完整性校验。 |

### 后续契约、产品策略与维护项

| 编号 | 保留情况与建议 |
|---|---|
| F01 | 常规导出排除proxy，手工导入显式proxy仍会更改网络配置，README已说明；当前不是原报告声称的无条件导入漏洞。`routes/api_settings.py`存在过度概括注释，后续可统一说明/增加导入预览。不要直接删除用户已允许的导入能力。 |
| F17 | SQLite默认自动checkpoint已存在；长读事务可阻止推进，简单再加一次PASSIVE不能解决。显式维护与定时开关的关系可整理，先量测长读、备份和WAL增长。 |
| F20 | `_RETRY_TIMEOUT=600`与真实单图预算上限120秒语义不一致；当前传输受实际预算约束。宜改名/统一常量，不要恢复成600秒而再次拖住队列。 |
| F21/F22 | `flush_server_logs`、`require_job_id`、`safe_resolve`等有未调用/过时注释问题；后者的junction说明也应校正。属于维护债务，未据此确认活跃外部漏洞。清理前核对调用与兼容用途。 |
| F23/F32 | 不存在收藏返回200+null，任务控制接口对不存在对象仍有400等不统一语义。空批量清单原本就会被拒绝；不要把原报告声称的“空清单创建任务”当成已确认事实。统一状态码前检查前端和调用方。 |
| F33 | 本机设置编辑API返回原proxy并使用no-store；本地诊断含PID/路径。尚未证明跨站可读取这些值。公开服务器版需要认证、权限和敏感字段更新协议，不能只把值替换成`***`再覆盖保存。 |

另有架构层面的维护注意：`jm_service.py`和`database.py`仍较大、依赖未完全锁定、真实上游/干净Windows/Docker验收未完成。建议先让独立审查确认现有修复，再按文件正确性、Windows所有权、兼容契约的顺序分批推进，暂不把剩余清单全部塞进一次重写。

## 10. 从源码运行与离线复检

在独立克隆目录、独立环境执行。安装依赖需要网络；pytest由`tests/conftest.py`重定向临时路径并同时阻断Python socket和libcurl，HTML域名使用测试专用列表。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q -rs
.\.venv\Scripts\python.exe -m compileall -q app.py launcher.py core routes
Get-ChildItem -LiteralPath '.\static\js' -Filter '*.js' -File | ForEach-Object {
    & node --check $_.FullName
    if ($LASTEXITCODE -ne 0) { throw "JS语法检查失败：$($_.Name)" }
}
git diff --check
```

每条命令都应成功退出。失败、缺依赖和权限跳过如实记录；不要解除网络阻断或指向个人数据库来让测试通过。历史`test_smoke.py`、`test_endpoints.py`、`test_key_e2e.py`、`test_e2e_integration.py`、`test_export_integrity.py`、`test_stress.py`被默认排除，可能在导入时联网/写数据，不要当作普通pytest模块单独导入。

### 建议定向阅读的回归

| 主题 | 测试入口 |
|---|---|
| 完成/取消事务与错误 | `tests/test_atomic_job_results.py`、`tests/test_audit_correctness.py`、`tests/test_audit_http_contract.py` |
| SSE/分页/导出 | `tests/test_sse_capacity.py`、`tests/test_jobs_pagination.py`、`tests/test_export_capacity.py`、`tests/test_pdf_spooling.py` |
| 元数据/停止/数据库竞争 | `tests/test_metadata_capacity.py`、`tests/test_metadata_lifetime.py`、`tests/test_update_check_shutdown.py`、`tests/test_job_manager_contention.py` |
| schema/恢复/迁移 | `tests/test_schema_startup.py`、`tests/test_restore_schema.py`、`tests/test_restore.py`、`tests/test_migration_backup.py`、`tests/test_resource_catalog.py` |

### 显式本机行为检查

以下使用临时实例、本机服务或合成图片，允许的loopback访问不同于默认pytest网络阻断；不验证真实上游。按问题选用，不把它们混入普通离线测试：

```powershell
.\.venv\Scripts\python.exe desktop.py self-check
.\.venv\Scripts\python.exe desktop.py lifecycle-check
.\.venv\Scripts\python.exe tests/check_download_queue.py
.\.venv\Scripts\python.exe tests/check_backend_shutdown.py
.\.venv\Scripts\python.exe tests/check_metadata_budget.py
.\.venv\Scripts\python.exe tests/check_capacity_boundaries.py
.\.venv\Scripts\python.exe tests/check_pdf_memory.py
```

PDF专项会生成合成数据并量测Windows独立进程内存；原生Windows相关脚本需相应平台。正常打开界面可运行`python launcher.py --wait`，普通服务仍共用单实例锁；已有日常服务时应从其原窗口正常退出，不为复检强行解除锁。

人工复检顺序：设置打开后立即修改/保存并重启；失败任务后下一任务继续；暂停/恢复/取消/重启队列；在线阅读和在途退出；删除任务历史后资源仍可读；临时实例备份、恢复及恢复后首次启动。具体操作见[BACKEND_REVIEW.md](BACKEND_REVIEW.md)。

## 11. 数据、备份、发行和诊断

默认可写目录为程序根下的`runtime/data`、`runtime/logs`、`runtime/cache`、`runtime/backups`和`downloads`。程序资源及虚拟环境仍属于代码位置。

| 启动前环境变量 | 用途 |
|---|---|
| `JMCONSOLE_HOME` | 实例根目录；数据库和日志随该实例 |
| `JMCONSOLE_DOWNLOAD_DIR` | 独立下载目录 |
| `JMCONSOLE_CACHE_DIR` | 独立缓存目录 |
| `JMCONSOLE_BACKUP_DIR` | 独立备份目录 |

只接受非空绝对路径，导入模块时固定；运行中修改环境不会热迁移文件。页面的download_root是实际配置的只读展示。不要把新配置指向旧个人实例来做破坏性试验。

停机维护使用CLI的`backup`、`backup --include-downloads`、`backup-info`和`restore ... --to <全新绝对目录>`。常规数据库/清单备份不包含漫画文件；迁移快照是DB-only、不扫描资源。恢复不覆盖现有目录，不自动启动服务，不猜测根外路径；成功发布后也应核实目标实例的环境变量。

哈希/CRC/SQLite quick_check通过不代表schema受支持，更不代表资源文件已包含。备份可能含代理凭据和个人记录，当前不加密。完整命令、包含范围和启动恢复实例的方法见[DATA_MAINTENANCE.md](DATA_MAINTENANCE.md)。

Windows包从独立`.venv-build`按[WINDOWS_RELEASE.md](WINDOWS_RELEASE.md)构建；成功通过EXE和生命周期自检才发布新指纹目录，不覆盖旧包。实际ZIP另运行`tests/check_windows_release.py <发行目录> --check-launcher`。包未签名，当前本机验收不能替代干净Windows兼容性检查。

故障按request_id与时间对照app/error日志及请求耗时，提供必要脱敏摘要。健康200或浏览器能打开上游首页不能证明API、搜索或解密可用。不要仅凭504判断要换代理/域名；需要区分上游HTTP、非JSON、TLS、连接与真正超时。

## 12. 后续维护与交接产出

### 与上游同步

- SDK升级与控制台源码合并分开记录；不要把本项目业务修改塞进已安装SDK，也不要未经验证永久写死测试可用域名。
- API/HTML域名由SDK按`client.impl`分别选择；自定义也分别使用`client.domain.api/html`。构造client时的域名更新和setting Cookie请求当前关闭。
- 升级前查SDK发布记录/配置文档，改声明依赖并在独立环境安装、pip check；分别检查构造、离线回归和少量获授权真实接口。不能因为搜索成功就宣布详情/阅读/下载全部正常。
- 正在运行的Python不会自动加载新依赖，依赖变化也必须核实服务身份和重启；先检查活跃任务，不按端口或进程名批量终止。
- 合并冲突优先检查task_commands事务、resource_catalog trigger、schema合同、停机所有权、client租约及配置时序。保留AGENTS的测试与数据边界。

### 下一阶段建议

1. 独立模型固定当前产品提交，提交复检报告；优先验证事务/资源目录、恢复后启动、超时后的容量及停机归属。
2. 每个确认问题给出最小复现及有意义回归，按第9节条件选择下一批，不预设外部报告一定正确。
3. 文件/Windows边界收敛后，用新实例与用户指定的少量授权内容分别验收真实搜索、详情、阅读和下载，再考虑日常实例迁移。
4. 前端风格/客户端外壳另行与用户讨论；Docker阶段再补认证、域名/反向代理、持久化卷、容器信号、非Windows锁/时间语义和平台验收，继续共用核心。

### 审查报告格式

每项写清：提交SHA、环境/依赖、严重性与前提、文件/准确行号、操作步骤或最小脚本、预期与实测、是否已复现、影响边界、建议修复及回归、未验证范围。区分产品缺陷、条件性风险、体验/契约不一致和维护建议。不要把本机目录写权限条件隐藏成远程未授权攻击，也不要因现有测试通过就跳过独立验证。

报告不附个人数据库、下载内容、完整日志、Cookie或代理凭据。旧审计探针与原始报告不在公共源码中；接手者用自己的临时样本重建证据，不依赖开发机`_preview/`或`docs/reports/`。

### 可直接转交其他模型的任务文本

当前 Windows 版的委托文本在[从 GitHub 获取并审核](GITHUB_REVIEW.md)第 5 节，可直接复制。复核旧审计报告时，再给审查者指定历史区间 `e4a6057..72783a2` 和本文件第 9 节的保留项。

## 13. 其他文档

| 文档 | 用途 |
|---|---|
| [GITHUB_REVIEW.md](GITHUB_REVIEW.md) | 克隆、环境和测试的简明入口 |
| [AUDIT_FIXES.md](AUDIT_FIXES.md) | 四批修复的精确行为、预算与证据 |
| [BACKEND_REVIEW.md](BACKEND_REVIEW.md) | 给用户的Windows行为复检顺序 |
| [DATA_MAINTENANCE.md](DATA_MAINTENANCE.md) | 数据路径、备份、恢复与迁移 |
| [WINDOWS_RELEASE.md](WINDOWS_RELEASE.md) | 便携版使用、构建和验收 |
| [WINDOWS_ACCEPTANCE.md](WINDOWS_ACCEPTANCE.md) | 本次 Windows 成品、校验值和实际执行结果 |
| [BACKEND_REFACTOR.md](BACKEND_REFACTOR.md) | 分批演进历史；旧测试数和旧包为历史记录 |
| [INDEPENDENT_BACKEND_AUDIT.md](INDEPENDENT_BACKEND_AUDIT.md) | 原审计委托及历史快照；当前入口以本文为准 |
