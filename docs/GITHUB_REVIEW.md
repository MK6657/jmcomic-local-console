# 从 GitHub 获取并审核后端重构

公开仓库：[MK6657/jmcomic-local-console](https://github.com/MK6657/jmcomic-local-console)。
本次代码位于 [`codex/refactor-windows`](https://github.com/MK6657/jmcomic-local-console/tree/codex/refactor-windows)，`main` 保留原版本。

当前 Windows 版的产品提交、ZIP 校验和实测场景见 [Windows 发行验收](WINDOWS_ACCEPTANCE.md)。历史审计基线为 `72783a2`；项目背景、代码地图和保留审计项另见[项目交接说明](PROJECT_HANDOFF.md)。

## 1. 获取正确的分支

```bash
git clone --branch codex/refactor-windows --single-branch https://github.com/MK6657/jmcomic-local-console.git jmcomic-backend-review
cd jmcomic-backend-review
git status --short --branch
git show -s --format="%H %s" HEAD
git diff --stat c310e3f..HEAD
git diff --stat 5fdc984..HEAD
```

`c310e3f..HEAD` 看相对原主分支的全部改动；`5fdc984..HEAD` 只看首个 Windows 候选之后的改动。若专门复核旧报告，原审计产品代码为 `7ae6ccb`，四批[审计修复](AUDIT_FIXES.md)可用 `e4a6057..72783a2` 比较。默认克隆保留提交历史，不加 `--depth 1`。

没有 Git 时，在分支页面选择 Code → Download ZIP。ZIP 可用于阅读和执行测试，但不含提交历史，不能在其中直接运行上述 Git 比较命令；可在 GitHub 阅读[原主分支到本版产品提交的差异](https://github.com/MK6657/jmcomic-local-console/compare/c310e3f...d01bc0e)。如果只想像用户一样运行程序，下载[发行验收](WINDOWS_ACCEPTANCE.md)中链接的便携包。

## 2. 独立环境安装与离线测试

完整运行验收以 Windows 10/11 x64 为目标，现有本机验证使用 Python 3.11.15。安装依赖需要联网，测试自身由 `tests/conftest.py` 阻断 Python socket 和 libcurl，并使用临时数据库及下载目录。

在全新克隆目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q app.py launcher.py core routes
Get-ChildItem -LiteralPath '.\static\js' -Filter '*.js' -File | ForEach-Object {
    & node --check $_.FullName
    if ($LASTEXITCODE -ne 0) { throw "JS语法检查失败：$($_.Name)" }
}
```

每条命令均需成功退出。第四批本机完整套件2003 passed、2 skipped（213.65秒），跳过为文件符号链接权限。请记录自己的结果及跳过原因，详见 [修复证据](AUDIT_FIXES.md)。若环境为 Linux/macOS 或缺少 Windows 执行器，可以进行静态审查并运行适用的离线检查，但需明确哪些原生 Windows 路径、命名互斥锁、停机事件及 EXE 行为没有验证。不要把其他系统的结果写成 Windows 发行验收。

不要逐个直接运行被默认收集排除的历史 `test_smoke.py`、`test_endpoints.py`、`test_e2e_integration.py` 等脚本，它们可能访问网络或修改数据。详见 [独立审查委托](INDEPENDENT_BACKEND_AUDIT.md)。

## 3. 显式本机行为验证

第二批容量专项可运行以下命令。前者使用临时实例和本机Waitress验证SSE拒绝后健康/分页仍可响应；后者在Windows独立进程生成128张随机图片，校验PDF完整页数和工作集增长，不访问真实上游。具体预算及其协作式边界见 [审计修复记录](AUDIT_FIXES.md)。

```powershell
.\.venv\Scripts\python.exe tests/check_capacity_boundaries.py
.\.venv\Scripts\python.exe tests/check_pdf_memory.py
.\.venv\Scripts\python.exe tests/check_metadata_budget.py
.\.venv\Scripts\python.exe tests/check_backend_shutdown.py
```

后两项是第三批的共享元数据重试总预算及后台停止验证，仍仅访问本机。下面两项使用临时实例和本机模拟请求，不访问真实上游，可在上述源码虚拟环境中执行：

```powershell
.\.venv\Scripts\python.exe desktop.py self-check
.\.venv\Scripts\python.exe desktop.py lifecycle-check
```

退出码应为 0，末行 JSON 的 `status` 应为 `ok`。其他下载停滞、后台请求排空和启动专项脚本的范围与命令见 [独立审查委托](INDEPENDENT_BACKEND_AUDIT.md)。真实上游搜索、详情、在线阅读和下载需要分别验收，以上本机检查不证明它们可用。

第四批的启动/恢复结构专项可单独运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_schema_startup.py tests/test_restore_schema.py
```

重点观察v4缺表/索引/trigger时是否在写连接前拒绝、合法v0–v3是否先快照再迁移、修改约束的有效SQLite备份是否拒绝且无目标/暂存残留，以及v3恢复后首次启动是否保留独立目录记录。正常哈希和`quick_check`不能代替这些结构与业务检查；不要手工改动个人数据库来测试。

## 4. 可选的启动与构建

需要实际打开浏览器界面时，在独立克隆目录执行：

```powershell
.\.venv\Scripts\python.exe launcher.py --wait
```

新实例默认在克隆目录下生成 `runtime/` 和 `downloads/`。同一台 Windows 上的正常服务共用单实例锁；若已有日常实例，请先从它的原启动窗口正常退出。不要为了测试解除生产实例锁或批量结束 Python 进程。

Git 源码不含便携 EXE、个人数据库、下载文件或本地虚拟环境。需要自己构建 Windows 包时，使用 [Windows 构建说明](WINDOWS_RELEASE.md) 的独立 `.venv-build` 与 `requirements-build.txt`；需要操作数据时，先读 [数据维护说明](DATA_MAINTENANCE.md)。

## 5. 交给其他模型的任务文本

> 请从 https://github.com/MK6657/jmcomic-local-console 克隆 `codex/refactor-windows` 分支，记录完整 HEAD。先读 AGENTS.md、docs/WINDOWS_ACCEPTANCE.md 和 docs/PROJECT_HANDOFF.md。用 `5fdc984..HEAD` 看本次 Windows 收尾，用 `c310e3f..HEAD` 看完整重构。在独立环境和临时实例中优先验证：两页停滞时后一本能否开始；暂停后迅速恢复是否重复写页；两个设置标签分别修改后重启是否都保留；清任务历史后能否导出资源；从旧库备份恢复的 EXE 能否启动且不写回原目录。核对构件自带的 SHA256SUMS.txt。保留默认离线边界，不访问真实上游、不接触个人数据、不修改产品代码。输出 Markdown 报告，标明提交、平台、实际执行的命令、复现条件、证据、准确文件行号和未验证范围；非 Windows 环境标明未执行的原生检查。
