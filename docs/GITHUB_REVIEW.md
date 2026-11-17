# 从 GitHub 获取并审核后端重构

公开仓库：[MK6657/jmcomic-local-console](https://github.com/MK6657/jmcomic-local-console)。
本次代码位于 [`codex/refactor-windows`](https://github.com/MK6657/jmcomic-local-console/tree/codex/refactor-windows)，`main` 保留原版本。

## 1. 获取正确的分支

```bash
git clone --branch codex/refactor-windows --single-branch https://github.com/MK6657/jmcomic-local-console.git jmcomic-backend-review
cd jmcomic-backend-review
git status --short --branch
git show -s --format="%H %s" HEAD
git diff --stat c310e3f..HEAD
git diff --stat e4a6057..HEAD
```

原审计产品代码为 `7ae6ccbaea7a91296df1e211e8920190235d0df7`，重构前基线为 `c310e3f584d077771324a02b8a611bddb1de404c`。2026-11-17起，本分支在审计交付版本 `e4a6057` 后继续加入[审计修复](AUDIT_FIXES.md)。复检当前版本用 `c310e3f..HEAD` 看完整重构，`e4a6057..HEAD` 看修复增量；需要复现原报告时明确使用旧提交。默认克隆保留提交历史，不加 `--depth 1`。

没有 Git 时，在分支页面选择 Code → Download ZIP。ZIP 可用于阅读和执行测试，但不含提交历史，不能在其中直接运行上述 Git 比较命令；静态差异可阅读 [GitHub 固定版本比较](https://github.com/MK6657/jmcomic-local-console/compare/c310e3f...7ae6ccb)。

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

每条命令均需成功退出。首批审计修复后的本机完整回归为 1688 passed、2 skipped；请记录你自己的结果及跳过原因。若环境为 Linux/macOS 或缺少 Windows 执行器，可以进行静态审查并运行适用的离线检查，但需明确哪些原生 Windows 路径、命名互斥锁、停机事件及 EXE 行为没有验证。不要把其他系统的结果写成 Windows 发行验收。

不要逐个直接运行被默认收集排除的历史 `test_smoke.py`、`test_endpoints.py`、`test_e2e_integration.py` 等脚本，它们可能访问网络或修改数据。详见 [独立审查委托](INDEPENDENT_BACKEND_AUDIT.md)。

## 3. 显式本机行为验证

下面两项使用临时实例和本机模拟请求，不访问真实上游，可在上述源码虚拟环境中执行：

```powershell
.\.venv\Scripts\python.exe desktop.py self-check
.\.venv\Scripts\python.exe desktop.py lifecycle-check
```

退出码应为 0，末行 JSON 的 `status` 应为 `ok`。其他下载停滞、后台请求排空和启动专项脚本的范围与命令见 [独立审查委托](INDEPENDENT_BACKEND_AUDIT.md)。真实上游搜索、详情、在线阅读和下载需要分别验收，以上本机检查不证明它们可用。

## 4. 可选的启动与构建

需要实际打开浏览器界面时，在独立克隆目录执行：

```powershell
.\.venv\Scripts\python.exe launcher.py --wait
```

新实例默认在克隆目录下生成 `runtime/` 和 `downloads/`。同一台 Windows 上的正常服务共用单实例锁；若已有日常实例，请先从它的原启动窗口正常退出。不要为了测试解除生产实例锁或批量结束 Python 进程。

Git 源码不含便携 EXE、个人数据库、下载文件或本地虚拟环境。需要自己构建 Windows 包时，使用 [Windows 构建说明](WINDOWS_RELEASE.md) 的独立 `.venv-build` 与 `requirements-build.txt`；需要操作数据时，先读 [数据维护说明](DATA_MAINTENANCE.md)。

## 5. 交给其他模型的任务文本

> 请克隆公开仓库 https://github.com/MK6657/jmcomic-local-console 的 codex/refactor-windows 分支并记录HEAD。先读 AGENTS.md、docs/GITHUB_REVIEW.md、docs/AUDIT_FIXES.md 和 docs/INDEPENDENT_BACKEND_AUDIT.md。审查当前完整差异 c310e3f..HEAD，尤其复核原审计交付版之后 e4a6057..HEAD 的修复。7ae6ccb是历史审计快照，不要用它代替最新版本。独立检查任务完成/取消的事务一致性、状态守卫、超时章节缓存、异常图片处理、HTTP错误及停机所有权。用独立环境和临时数据运行适用测试，保留默认离线边界，不访问真实上游、不自动下载、不修改产品代码。输出Markdown问题报告，包含严重性、准确文件行号、触发条件、影响、证据、修复建议及未验证范围；静态推断与实际复现分开，非Windows环境须明确原生验收未完成。
