# JMComic 本地下载控制台

[English](README.md) | [简体中文](README.zh-CN.md)

面向 Windows 的 jmcomic 本地浏览器控制台，提供搜索、下载队列、实时进度、
收藏、资源库、本地图片阅读和压缩包/PDF 导出。程序界面目前为中文，说明文档提供中英文。

这是一个**本地 Python 应用**，不是浏览器扩展，也不是在线托管服务。
请仅访问和下载你有权使用的内容。仓库不包含下载内容、个人凭据或第三方参考项目。

## 环境要求

- Windows 10/11，Python 3.10–3.12；本次维护使用 Python 3.11 验证。
- 安装依赖需要联网；搜索、下载还依赖上游服务和本机网络状况。
- 使用项目独立虚拟环境，避免与 Hermes 或系统 Python 的依赖互相影响。

## 快速启动

克隆或解压仓库后，双击 **start.bat**。脚本会切换到项目目录，按需创建 `.venv`，
安装或检查全部运行依赖，再启动服务并打开浏览器。首次运行需要 PATH 中能找到 `python`。

保持启动窗口打开，按 **回车** 或 **Ctrl+C** 可停止该窗口启动的服务。
若识别到已运行的本项目服务，只会打开它，不会接管或停止已有进程。

也可以在项目目录打开 PowerShell 执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe launcher.py --wait
```

希望直接查看服务输出、不自动打开浏览器时：

```powershell
.\.venv\Scripts\python.exe app.py
```

访问 [http://127.0.0.1:5000](http://127.0.0.1:5000)。端口被占用时，服务会尝试
5001–5003，以终端显示的地址为准。启动器不会为了释放端口而强杀其他程序，
且会核对服务身份和 PID。更新前的旧服务请先手动停止，再启动新版本。

安装依赖后，可双击 `launcher.pyw` 无窗口启动。`launcher.py` 不带 `--wait`
时同样在后台运行。停止后台服务前，请核对 `runtime/data/flask.json` 中的 PID
确实属于本程序，只结束对应进程。

## 功能与设置

- 搜索、详情、下载队列、失败重试、取消、SSE 实时进度。
- 收藏清单、下载资源库、本地图片阅读器。
- ZIP/PDF 导出、可选的 CBZ/ZIP 自动打包。
- 定时下载、并发数、超时、重试次数、代理设置。
- 设置导入和导出：**导出不包含代理配置**，重新导入也不会覆盖目标机器的代理。
  手工制作的导入文件仍可包含代理，分享前务必检查。

如需代理，在设置页填写。代理认证信息会保存在本地 SQLite 数据库中，不要分享数据库。
下载目录固定为项目内的 `downloads/`。开启“打包后删除原图”会删除源图片，
没有明确需要时请保持关闭。

## 数据与隐私

| 路径 | 用途 | 是否提交 |
| --- | --- | --- |
| `runtime/data/app.db` | 设置、任务、历史记录 | 否 |
| `runtime/logs/` | 应用、错误和启动器日志 | 否 |
| `downloads/` | 下载文件与导出内容 | 否 |
| `.venv/`、`.worktrees/` | 本地环境和工作树 | 否 |
| `参考项目/`、`docs/reports/` | 参考仓库和历史本地审计资料 | 否 |

服务仅监听 `127.0.0.1`，没有用户认证。不要通过端口转发、公共反向代理或共享服务器
暴露给其他人。日志即使对认证信息脱敏，也可能包含关键词、标题和本地路径，不宜公开分享。
`runtime/logs/launcher.log` 保存启动及后台输出；它不像应用日志那样自动轮转，
可在服务停止后手动归档。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe tests/check_startup.py
.\.venv\Scripts\python.exe -m compileall -q app.py launcher.py launcher.pyw core routes tests
```

默认回归测试使用临时运行目录和数据库，不执行真实搜索、下载。
`tests/conftest.py` 列出的旧脚本在导入时就会联网或修改数据，因此不参与默认收集。
不要对个人实例运行这些脚本；需要调查时，请使用可丢弃的项目副本。

`check_startup.py` 另做本地 HTTP 检查：启动和停止临时数据实例，验证端口回退，
不会访问上游服务。

项目没有 `package.json`，因此不提供 npm lint/typecheck/build 脚本。
`build.spec` 是保留的 PyInstaller 打包方案，不代表本次已验证 EXE 产物。

## 故障排查与限制

- 启动失败：查看 `runtime/logs/launcher.log`，或直接运行 `app.py` 获取报错。
- 缺少依赖：使用同一个 Python 解释器安装完整的 `requirements.txt`。
- 搜索失败：检查网络、代理和日志；`core/settings.py` 中的上游域名可能随时间失效。
- 离线测试通过不代表上游联网、真实下载或 EXE 打包已经验证。
- 打开文件夹和单实例互斥锁包含 Windows 专用实现，不保证其他平台可直接运行。

## 本次维护

### 第二轮整体检查

- 无效 JSON 和设置明确返回错误；设置整批校验后以事务保存，不再静默跳过。
- 增加浏览器来源和 Host 校验，拒绝跨站访问本地控制台。
- ZIP/PDF/CBZ 文件扫描跳过符号链接和 Windows 目录联接；预览支持多层章节、
  自然页序，以及含空格、井号等字符的文件名。
- 不完整或取消的下载不会自动打包、删除原图；重新下载失败会保留旧的有效图片。
- 自动 ZIP/CBZ 留在任务对应目录；清理原图时保留归档、备注等文件。删除原图后，
  本地阅读器没有图片可展示，可通过导出取得归档。
- 同专辑任务避免并发写入；下载线程启动失败会释放队列容量。
- 修改设置后，共享连接等使用者及超时后台请求结束再释放。
- 单实例锁覆盖端口绑定前的启动阶段；缺少 Waitress 时明确提示安装依赖，
  不再回退到无法被启动器跟踪的开发服务器。

### 首轮维护

- 修复代理参数层级错误，代理传入 jmcomic 的 HTTP 客户端元数据。
- 设置导出排除代理和未知私有字段。
- 移除强制杀端口进程，增加服务身份验证与安全端口回退。
- 两个启动器复用同一逻辑，后台输出写日志，启动失败清理自身子进程。
- 增加隔离的离线回归测试及本地、私有文件忽略规则。
- 保持 DESIGN.md 锁定的 `eye-care` 护眼主题，没有修改 UI 样式。

## 第三方组件

运行依赖见 `requirements.txt`。随仓库保存的前端资源保留原作者声明，详见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。项目原创代码尚未选定许可证，
这不构成开源授权；如要按开源项目公开分发，应先选择许可证。
