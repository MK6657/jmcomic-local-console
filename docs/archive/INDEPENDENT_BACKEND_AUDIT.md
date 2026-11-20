# JMComic 后端重构独立审查委托

本文可直接交给另一位开发者或AI审查代理。请先独立审查并提交问题报告，默认不修改代码、不自动修复、不部署、不切换日常实例。

**当前交接入口：[PROJECT_HANDOFF.md](PROJECT_HANDOFF.md)。** 它固定产品验收提交`72783a2`，提供最新架构、四批修复、剩余问题与验证证据。下文的`7ae6ccb`、旧导出命令和旧包用于历史复现，不是当前版本的下载/验收指引。

**2026-11-17更新：原审计后已获授权开始修复。** `7ae6ccb` 是历史产品快照，`e4a6057` 是原审计交付版；当前分支已继续加入产品修复。最新审查范围及已修/待修项以 [GitHub复检入口](GITHUB_REVIEW.md) 和 [审计修复记录](AUDIT_FIXES.md) 为准，记录当前HEAD，并使用 `c310e3f..HEAD` 与 `e4a6057..HEAD`。下文固定提交、原产物和原验证数量保留作为历史审计证据，不应被理解为最新版本承诺。

## 1. 审查哪一个项目

| 项目 | 固定位置或版本 |
|---|---|
| 本机待审源码 | `<worktree-dir>` |
| 开发分支 | `codex/refactor-windows` |
| 原审计产品代码提交 | `7ae6ccbaea7a91296df1e211e8920190235d0df7`，简称 `7ae6ccb` |
| 当前待审代码 | 克隆后记录 `git rev-parse HEAD`；首批修复范围见 `e4a6057..HEAD` |
| 重构前基线 | `c310e3f584d077771324a02b8a611bddb1de404c`，简称 `c310e3f` |
| 完整审查差异 | `c310e3f..7ae6ccb` |
| 最后一批收尾差异 | `c64a1bc..7ae6ccb`；只看这一段会遗漏前面的重构 |
| 原日常项目，**不是待审工作区** | `<daily-instance-dir>`，原 `main` 分支 |
| GitHub公开分支 | `https://github.com/MK6657/jmcomic-local-console/tree/codex/refactor-windows` |

本次通过公开仓库的独立分支交付，`main` 保持重构前版本。**必须显式克隆 `codex/refactor-windows`；只下载GitHub默认分支，或在本机原日常项目 `<daily-instance-dir>` 审查，会拿到旧代码。** 需要复现原审计结论时使用 `7ae6ccb`；复核修复时使用当前HEAD。本机绝对路径仅用于原开发机定位，异机请使用自己的克隆目录，详见 [GitHub克隆与测试指南](GITHUB_REVIEW.md)。

```bash
git clone --branch codex/refactor-windows --single-branch https://github.com/MK6657/jmcomic-local-console.git jmcomic-backend-review
cd jmcomic-backend-review
git show -s --format="%H %s" HEAD
git diff --stat e4a6057..HEAD
```

最后一条展示原审计交付之后的修复增量。不要再假定 `7ae6ccb..HEAD` 只有文档变更。

本机先核实位置和版本，不切分支、不重置工作区：

```powershell
Set-Location '<worktree-dir>'
git status --short
git branch --show-current
git show -s --format='%H %s' 7ae6ccb
git log --reverse --oneline c310e3f..7ae6ccb
git diff --stat c310e3f..7ae6ccb
```

若存在未提交改动，先记录并与提供者确认其归属，不覆盖、不混入既定审查版本。若HEAD晚于 `7ae6ccb`，用 `git diff --name-only 7ae6ccb..HEAD` 核对新增内容；需要固定快照时可使用下一节的源码导出。

## 2. 异机或云端审核怎么交付

首选按上一节克隆公开分支；没有Git时，可在GitHub分支页面选择Code → Download ZIP。分支ZIP不含Git历史，完整差异仍应通过仓库比较或下列patch取得。需要离线交付时，请提供**源码快照、完整差异文件和本文**。便携EXE用于行为验证，不能代替源码审查。不要直接压缩整个工作目录，也不要打包日常项目目录。

可在上述工作树中执行以下导出命令。它们只导出固定提交中的Git跟踪文件及两个提交之间的差异，不打包虚拟环境、未跟踪的数据库或下载目录：

```powershell
$auditExport = Join-Path $env:TEMP ('jmcomic-audit-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $auditExport -ErrorAction Stop | Out-Null
git archive --format=zip --output="$auditExport\source-7ae6ccb.zip" 7ae6ccb
if ($LASTEXITCODE -ne 0) { throw '源码导出失败' }
git diff --binary --output="$auditExport\refactor-c310e3f-to-7ae6ccb.patch" c310e3f..7ae6ccb
if ($LASTEXITCODE -ne 0) { throw '差异导出失败' }
Copy-Item -LiteralPath '.\docs\INDEPENDENT_BACKEND_AUDIT.md' -Destination $auditExport
Write-Output $auditExport
```

上述新目录就是交付位置，发送前检查内容。不要另外附带个人数据库、完整日志、下载文件、搜索历史、Cookie、代理凭据或 `.env`。只收到源码ZIP时，审核者可直接解压阅读和测试；需要比较变更时同时阅读patch，不能假设ZIP里有Git历史。

## 3. 本轮目标与范围

目标是提高可维护性和日常可靠性，同时维持与上游SDK同步的成本。Windows优先，后续Docker共用核心；当前继续使用Python、Flask和 `jmcomic`，没有进行Rust重写。

本轮涵盖资源目录、任务队列、下载传输、设置保存、文件发布、数据目录、备份恢复、SDK边界、应用生命周期及Windows便携构建。前端只有必要的设置行为修复，主题仍为已锁定的 `eye-care`。

以下不作为本次必须补做的功能：新UI或客户端外壳、Docker/域名/认证部署、签名安装包、自动更新和跨设备数据同步。可记录相关架构约束，但不要因此扩大实现范围。

先读项目 `AGENTS.md`，再读：

- `docs/BACKEND_REFACTOR.md`：变更边界与逐批验证记录；早期批次的限制应结合第七批最终状态阅读。
- `docs/BACKEND_REVIEW.md`：人工复检步骤。
- `docs/DATA_MAINTENANCE.md`：数据位置、备份包含范围及恢复规则。
- `docs/WINDOWS_RELEASE.md`：便携版启动、维护与自检。

以上记录是已有验证证据，**不代替审核者对实现和测试有效性的独立判断**。

## 4. 建议的代码阅读顺序

下表路径均相对于待审源码根目录。

| 审查主题 | 主要实现 | 优先关注 |
|---|---|---|
| 应用与HTTP生命周期 | `app.py`、`core/runtime.py`、`core/http_server.py`、`core/progress.py` | 先关闭准入再排空；启动失败回滚；HTTP背压/SSE退出；停机预算；错误是否被误报为成功 |
| 共享调用与客户端归属 | `core/background_calls.py`、`core/jm_service.py`、`core/sdk_adapter.py` | 请求超时后真实调用仍在运行；退休代际；租约计数；关闭失败重试；清理线程真实退出；锁顺序 |
| 在线阅读 | `core/online_reader.py`、`routes/api_online.py` | 前台取图、预取、lookup回调与连接池的停止；晚到结果；缓存发布；部分线程启动失败 |
| 下载与队列 | `core/job_manager.py`、`core/task_commands.py`、`core/transfer.py`、`core/image_download.py` | 底层请求预算与重试；失败是否放行后续任务；重启队尾恢复；取消/暂停/停机的区别 |
| 数据与资源记录 | `core/database.py`、`core/local_availability.py`、`core/chapter_inventory.py` | schema v4迁移；`resource_catalog`与任务历史分离；`queued_at`公平性；成功资源的判定 |
| 文件与归档安全 | `core/storage.py`、`core/file_tree.py`、`core/path_guard.py`、`core/archive_pages.py`、`core/packer.py` | 暂存校验及原子发布；旧文件保护；符号链接/junction；路径逃逸；归档成员与临时页过滤 |
| 数据维护 | `core/path_utils.py`、`core/backup.py`、`core/restore.py`、`jmcomic_cli.py` | 导入时固定路径；迁移前快照；WAL一致性；恢复只写新目录；失败不发布半成品 |
| 设置行为 | `core/settings.py`、`routes/api_settings.py`、`static/js/settings.js` | 初始读取慢时误存默认值；0/false保留；重复提交；响应覆盖草稿；持久化与即时应用失败的区分 |
| Windows能力与启动 | `launcher.py`、`desktop.py`、`core/process_control.py`、`core/instance_lock.py`、`core/desktop_platform.py` | 身份/令牌复核；venv重定向PID；事件通知；进程真实退出前保留锁；未来schema预检；平台不支持时无副作用 |
| 发行与自检 | `build.spec`、`tools/build_windows.py`、`core/release_check.py`、`core/release_lifecycle.py` | 资源/DLL收集；私密数据排除；源码指纹；全新目录发布；自检隔离；测试专用锁豁免是否被严格限制 |

完整差异包含六个里程碑：`94b94b4`、`c385a28`、`74d1510`、`52e1d10`、`c64a1bc`、`7ae6ccb`。可按提交理解变更，但应基于最终代码判断问题是否仍然存在。

## 5. 重点寻找的真实故障

1. 下载中的某张图片不返回时，是否真正约束底层传输；有没有仅让Future等待超时、随后仍由旧线程写文件的情况？队列容量是否只在执行器真实退出后释放？
2. 旧的中断任务是否会反复占据队首？正常重启、恢复备份、重试与恢复暂停是否保持合理顺序和幂等性？
3. 设置是否仍可能因异步GET/POST顺序被旧响应覆盖？保存期间的新编辑是否保留？数据库已提交但应用失败时，反馈是否准确？
4. 停机是否遗漏共享future、预取、lookup回调或连接关闭线程？`cancel()`、关闭函数或hook是否在停机线程同步执行并突破预算？
5. 关闭失败时，是否仍保留连接引用、租约与计数？HTTP最后归还的租约能否触发晚到清理并被最终排空检查发现？
6. 停止准入后，是否还能建立共享客户端、开始新预取或发布缓存？是否存在锁顺序反转或错误的已排空状态？
7. 是否可能把失败/残缺下载冒充完成资源，或者清理任务历史时丢失已有资源记录？
8. 备份/迁移/恢复是否可能修改源实例、覆盖已有目标、漏掉WAL、越过目录边界或在失败时留下伪成功产物？
9. 启动器是否可能操作其他进程、因旧版本启动失败而强杀仍在工作的服务，或提前释放实例锁？
10. 测试是否只验证了自己的Mock实现？是否保留了实际线程退出、真实所有权和socket/libcurl双重离线边界？

请按可触发条件和实际影响判断。把代码风格、未来需求、已明确保留的产品边界与真正的缺陷分开记录。

## 6. 数据保护与运行限制

- 不在 `<daily-instance-dir>` 安装依赖、启动候选、迁移、备份恢复或结束进程；不要把日常虚拟环境当作审核环境修改。
- 不执行 `git reset --hard`、清理工作区、切换现有工作树分支、自动提交修复、推送或部署。发现问题先报告。
- 普通服务使用原Windows单实例锁。不要为并行运行候选而手动取消生产锁、改生产mutex名或按端口/Python进程名批量结束程序。
- 不直接双击 `start.bat`、`JMComic.exe` 或运行 `app.py` 作为第一步；普通启动可能检查/安装依赖、读取实例路径或与现有服务互斥。先使用下面的隔离测试。
- `JMCONSOLE_HOME` 等路径在导入业务模块时固定；测试隔离必须在导入前完成。不要在同一Python进程里修改环境变量后假定数据位置已经切换。
- 不逐个直接运行历史 `tests/test_smoke.py`、`tests/test_endpoints.py`、`tests/test_e2e_integration.py` 等脚本；部分旧脚本会联网或修改数据，已被 `conftest.py` 排除出默认收集。
- 普通pytest必须保留socket和libcurl的阻断。专门的 `check_*.py` 是显式本机网络验证，不应据此解除普通测试的网络边界。
- 本次默认不进行真实上游搜索、阅读或下载；需要扩展真实接口验收时由用户另行指定范围。尤其不要自动创建下载任务。

普通窗口关闭、系统强制结束、系统关机和干净Windows系统兼容性不等同于已验证的协作停机。底层SDK持续不返回时允许排空超时；要检查的是所有权保留和错误反馈，不能要求用不安全的线程强杀来实现固定退出时长。

## 7. 复现已有验证

### 本机已有审核环境

在待审源码根目录执行。下列 `.venv-build` 已用于当前候选的构建和完整回归，不要在其中升级或重装依赖：

```powershell
$auditPython = Join-Path (Get-Location) '.venv-build\Scripts\python.exe'
& $auditPython -m pip check
& $auditPython -m pytest -q
& $auditPython -m compileall -q app.py launcher.py core routes
Get-ChildItem -LiteralPath '.\static\js' -Filter '*.js' -File | ForEach-Object {
    & node --check $_.FullName
    if ($LASTEXITCODE -ne 0) { throw "JS语法检查失败：$($_.Name)" }
}
git diff --check
```

本次固定代码最近一次重新构建时的结果：**1571 passed，2 skipped，147.17秒**（2026-11-16）；两项跳过仍为Windows环境条件。Python编译、16个JS语法检查和pip依赖检查通过。审核者应记录自己实际执行的结果；不同环境的跳过项需单独说明，不必把数量一致作为唯一判断标准。

如在异机解压源码，使用独立环境安装 `requirements-dev.txt`；构建EXE时再使用 `requirements-build.txt`。不要复用个人服务环境。本次构建验证使用Windows x64、Python 3.11.15；具体依赖组合见候选旁的 `release.json`，其中 `jmcomic` 为2.7.10、`curl_cffi` 为0.17.0。不要把本机通过当成所有Python/Windows版本均已通过。

### 显式本机网络验证

以下脚本使用临时数据和本机模拟请求，与普通pytest分开运行。每条命令分别检查退出码；任何命令失败都不算验收通过：

```powershell
& $auditPython tests/check_backend_shutdown.py
& $auditPython tests/check_download_queue.py
& $auditPython tests/check_startup.py
& $auditPython desktop.py self-check
& $auditPython desktop.py lifecycle-check
```

对应证据范围：

| 验证 | 应核对的行为 |
|---|---|
| `check_backend_shutdown.py` | 阅读图片与共享请求同时在途；先返回未排空且不提前close；真实请求结束后排空；停止后不发布缓存 |
| `check_download_queue.py` | 本机服务器故意不发图片正文；坏任务失败后下一个正常任务完成，真实SDK/libcurl及生成图片验证 |
| `check_startup.py` | 临时服务身份与端口检查；端口环境不满足的案例可能明确跳过 |
| `self-check` | 临时实例、页面/资源、设置API、WebP/GIF/PDF、AES、CA文件及SDK本机取图 |
| `lifecycle-check` | 专用临时实例两次运行真实入口；保存设置、协作退出、记录清理、重启恢复、暂停保留 |

这些检查不验证真实上游搜索、详情、阅读或下载，也不应被报告为真实上游全链路验收。

## 8. 当前便携复检包

本机发行目录：

```text
<worktree-dir>\releases\windows-x64-2e06f6438219
```

ZIP：`JMComic-windows-x64.zip`，34,104,283字节。

SHA-256：

```text
b51a0d0354d261d296840f72e9d8b3d12450e40ba3ca5af4e9e8c4fc1c04098b
```

该目录含 `release.json`、`SHA256SUMS.txt`、ZIP和解压后的 `JMComic`。源码输入指纹：`2e06f6438219a72e3dcd98f2dec1c630ba28435921b169b4388f24ab43f36188`。这是2026-11-16恢复工作区后重新构建的候选，原 `2f43ae06f9e8` 产物已不在本地，勿混用其哈希。这个指纹与Git提交ID不同，是构建输入的内容摘要；本说明作为后续独立文档加入，不改变该候选的产品代码。

`releases/` 未加入Git，源码ZIP不会自动包含发行包，需要行为复检时由提供者另附。核对SHA-256后完整解压，保留 `_internal`，运行：

```powershell
.\JMComic.exe self-check
.\JMComic.exe lifecycle-check
```

每条均需退出码0，最后一行JSON为 `status: ok`。也可在源码工作树中显式验证整个发行目录：

```powershell
& $auditPython tests/check_windows_release.py '.\releases\windows-x64-2e06f6438219' --check-launcher
```

该检查覆盖ZIP哈希/CRC、中文及空格路径、去除系统Python路径后的EXE自检、空备份校验和生产锁忙时拒绝启动。不是干净虚拟机验收，也不是签名或安装/卸载验收。

## 9. 期望的审查输出

请提交Markdown报告，先给出明确结论，再列真实问题与未覆盖范围。按严重性排序，每项至少包含：

```text
标题：[P1] 具体问题
位置：仓库相对路径 + 准确行号
触发条件：操作顺序、并发时序、输入或环境要求
实际结果 / 应有结果：具体到状态、文件、任务或进程
影响：数据安全、队列阻塞、资源泄漏、兼容性或错误反馈
证据：复现步骤、最小测试或必要的脱敏输出
修复方向：简要建议，不直接改动现有审查快照
```

严重性建议：P0为可能导致严重数据丢失或越权的紧急问题；P1为应阻止日常替换的关键正确性问题；P2为需要修复的普通缺陷；P3为低影响问题。单独列出非阻塞的维护建议，不用数量替代判断。

报告还应明确：

- 实际审查的提交及差异范围。
- 实际阅读的模块、运行的命令、环境和结果。
- 哪些结论来自静态分析，哪些经过复现。
- 哪些功能没有验证，以及是否建议进入下一轮人工日常使用复检。

没有发现缺陷时直说“在本次范围内未发现阻塞问题”，并列出覆盖范围和剩余不确定性；不要表述为程序绝对无缺陷或真实上游永久可用。

## 10. 原审计快照的简短任务（历史）

审核最新修复时，请使用 [GitHub复检指南](GITHUB_REVIEW.md) 第5节的任务文本；下面仅用于复现原审计范围。

> 请从公开仓库 `https://github.com/MK6657/jmcomic-local-console` 显式克隆 `codex/refactor-windows` 分支，先阅读 `docs/GITHUB_REVIEW.md`、这份 `INDEPENDENT_BACKEND_AUDIT.md` 和项目 `AGENTS.md`，独立审查后端重构。产品代码目标固定为 `7ae6ccb`，完整差异为 `c310e3f..7ae6ccb`，后续仅为文档提交；不能只审GitHub main。默认只读审查；必要验证使用临时实例，保留pytest的socket/libcurl阻断，不操作日常实例、不自动真实下载、不修改/提交/部署。完整运行验收使用Windows，其他系统的验证边界须如实说明。重点寻找数据安全、队列卡死、设置丢失、后台调用与客户端所有权、停机竞态、迁移/备份恢复及测试有效性问题。输出按严重性排序的Markdown审查报告，附准确位置、触发条件、影响与证据，区分真实缺陷、建议和未验证范围。
