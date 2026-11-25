# Windows 便携版

本包是藏书阁 Windows x64 便携版。双击 `Preview-Atelier.cmd` 先看离线样书，或直接打开 `JMComic.exe` 使用普通实例。九套主题（6 套护眼 + 3 套个性风格）、经典界面回退及数据目录见同目录的 `ATELIER_WINDOWS.md`。程序自带 Python 运行时，使用本机浏览器，无需安装 Node。

拿到 `JMComic-windows-x64.zip` 后，可以用 PowerShell 的 `Get-FileHash .\JMComic-windows-x64.zip -Algorithm SHA256` 与该发行目录的 `SHA256SUMS.txt` 核对。双击运行时无需另外安装 Python、Node 或 Docker。

## 使用

将ZIP完整解压到一个有写入权限的独立目录，双击 `JMComic.exe`。不要只复制EXE，旁边的 `_internal` 目录包含运行时、原生DLL、模板、静态资源和证书。程序打开本机页面；新启动的服务由控制台持有，按Enter或Ctrl+C发起协作停机：停止领取任务、等待在途请求与HTTP工作线程退出、写回中断状态后关闭。超时会明确报错并保留服务进程，不自动强杀。若复用了已有的当前版本服务，启动器只打开页面，不接管那个进程。

默认数据位于EXE同级的 `runtime/` 与 `downloads/`，不要放在Program Files等普通用户不可写的目录。`JMCONSOLE_HOME`、`JMCONSOLE_DOWNLOAD_DIR`、`JMCONSOLE_CACHE_DIR`、`JMCONSOLE_BACKUP_DIR` 沿用源码版的配置方式，启动前设置，运行中不能切换。环境变量只改变位置，不搬迁数据。

Windows 10 1903 及以上和 Windows 11 可将程序解压至含中文或其他 Unicode 字符的目录。EXE 自己使用 UTF-8 读写提供给网络库的证书路径，无需更改系统区域设置。更早的 Windows 10 版本未验证。

服务只监听127.0.0.1。源码版与便携版仍共用Windows单实例锁，不能同时启动两个正常服务。解压此包不会替换正在使用的版本，也不会自动导入个人数据。

**先看位置，再决定用哪个实例。** 双击默认使用当前环境指定的实例。下面的 `--home` 只对这一次启动有效，`instance` 会列出实际数据库、下载、缓存、备份和日志目录；单独设置过的 `JMCONSOLE_*_DIR` 仍然有效。

```powershell
.\JMComic.exe instance
.\JMComic.exe instance --home 'D:\JMComicRestored'
.\JMComic.exe launch --home 'D:\JMComicRestored'
```

`launch --home` 要求所选目录里已有 `runtime/data/app.db`，打印位置后输入 `y` 才启动并允许升级，直接回车取消。只想打开一个已经运行的对应实例，可用 `JMComic.exe attach --home 'D:\JMComicRestored'`。升级前若有排队、运行、暂停任务或仍在使用的客户端，启动器暂缓自动替换。需要主动升级时请从旧服务的原窗口正常退出。

下载时暂停一个任务，在途写入结束后，下一个任务可自动开始，包括同册的其他章节。恢复不会新建任务；已确认且未变的图片即使关闭“跳过已存在的文件”也可以复用。新任务完成即可按任务导出，书库只显示已发布内容。整册打包、移目录与清理在同册任务都结束后单独进行，失败可从下载中心重试整理。

自检命令不需要日常实例停机，使用临时目录与本机临时端口，不启动任务调度器：

```powershell
.\JMComic.exe self-check
```

退出码0且JSON中 `status` 为 `ok` 才表示自检通过。它检查打包后的页面、静态资源、设置API、WebP/GIF、PDF组件及SDK/libcurl本机取图，不证明真实上游搜索或下载可用。

维护命令沿用现有CLI，正常备份/恢复需先关闭本程序；恢复只写入不存在的新实例目录：

```powershell
.\JMComic.exe cli backup --include-downloads --output 'D:\PrivateBackups\jmcomic-with-pages.zip'
.\JMComic.exe cli backup-info 'D:\PrivateBackups\jmcomic-with-pages.zip'
.\JMComic.exe cli restore 'D:\PrivateBackups\jmcomic-with-pages.zip' --to 'D:\JMComicRestored'
```

只需数据库和文件清单时，省略 `--include-downloads`；这种备份不包含漫画图片。

升级时，先从原启动窗口正常关闭旧服务，再用可读取旧库的维护命令制作完整备份。将新包解压到另一个目录，把备份恢复到不存在的新实例，用 `launch --home` 试用这份副本。这样要回退时，只需正常关闭新版，再以原位置的旧程序打开原实例。回退不会把试用期间的新记录自动合回旧库；不要用旧程序打开已升级的数据库。仅复制目录或改环境变量不会重映射库里的旧下载路径。

启动器会先只读检查数据库版本，再决定是否替换已有服务。支持协作停机的新服务通过带随机令牌的Windows命名事件接收通知；重新核实PID和实例记录后才发送，等待旧进程实际退出才启动替代进程。如果旧服务没有实例身份摘要，或它的下载、缓存、备份等目录与所选配置不匹配，启动器不会打开或替换它；请回到原启动窗口正常退出后再升级。系统强制终止、关机和直接关闭控制台窗口不等于这一正常停机流程。

正常退出会关闭HTTP与后台调用准入，结束SSE订阅、唤醒HTTP输出等待，等待在线阅读、预取、共享元数据调用和连接归还；HTTP工作线程结束后再确认最后一次连接清理。各组件共用剩余停机预算，未完成就明确报错并保留所有权。实例互斥锁由操作系统在进程实际结束时释放，避免等待期间另一个服务或维护命令并发写入。启动器等待超时会保留进程并取消升级，不承诺任意上游故障下固定时长退出，也不会并发关闭正在请求的curl句柄。

完整生命周期自检同样不需要停止日常服务：

```powershell
.\JMComic.exe lifecycle-check
```

它使用专用临时实例和独立互斥锁，两次运行真实服务入口，验证设置保存、协作退出、启动记录清理和重启恢复。隐藏的测试worker仅接受一次性随机凭据对应的临时目录；普通服务继续使用原单实例保护。所有测试连接限制在本机，检查结束后清理临时实例；退出码0才表示通过。

## 从源码构建

本次发行环境为 Windows x64 / CPython 3.11.15。创建独立构建环境，构建依赖不装到日常服务的虚拟环境：

```powershell
python -m venv .venv-build
.\.venv-build\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv-build\Scripts\python.exe -m pip check
.\.venv-build\Scripts\python.exe tools/build_windows.py
```

构建脚本生成清单和源码指纹，使用PyInstaller文件夹模式，包含SDK动态模块、图片/PDF与curl原生依赖。构建后从不含Python的PATH启动产物自检与完整生命周期自检，两项成功才发布到新的 `releases/windows-x64-指纹/`；已存在的目标不覆盖。自定义 `--output` 必须与项目在同一盘，跨盘会在构建前拒绝；完成后可将ZIP复制到任意盘。输出包括可运行目录、两项自检结果、ZIP、SHA-256与依赖版本清单。运行目录、数据库、下载、日志、Cookie和代理配置不在打包输入中。

`requirements-build.txt` 自动使用 `requirements-windows-build-constraints.txt` 锁定本次发行所用的直接与间接依赖。构建脚本核对实装版本，发现不符会指出包名并停止。只在维护升级时刷新约束，再执行离线回归及解压验证；这组约束不限制日常源码开发。源码指纹用于区分打包输入，不是可逐字节复现的 ZIP 哈希。

自 format 2 起，构建会把实装 Windows wheel 的许可文本、CPython 许可证、PyInstaller bootloader 例外及补充的原生库声明装入 `_internal/licenses/`。例如 PDF 合并既需 pikepdf 的 `LICENSE.txt`，也需它的 `third-party-licenses/qpdf.txt` 和 Windows qpdf 所含 OpenSSL、zlib 的声明。`release.json` 的 `license_sha256` 给出各文件的相对路径和摘要；发布前检查实际 PyInstaller 输出，解压验证时再次对照包内外清单。包根 `THIRD_PARTY_NOTICES.md` 给出来源。升级原生依赖时须复核二进制及其对应文本。成功发布后只清理本次创建的 `build/windows-<uuid>`，构建失败保留现场；现有 `releases/` 不覆盖。

GitHub Actions 的 **Windows portable package** 在重构分支的服务源码、模板、静态资源、发行输入或验收脚本变化时运行，也支持 `windows-v*` 标签。工作流另支持手动触发，GitHub 的手动运行入口要求默认分支也包含这个工作流。它在 Windows runner 上安装约束版本，运行离线测试、本机队列用例、实际 EXE 及 ZIP 的启停和恢复检查。成功时保留 ZIP、SHA-256 和依赖清单供下载。GitHub Release 由维护者单独发布。

只检查发行输入、不创建 EXE 或 ZIP 时，可在 `.venv-build` 安装 `requirements-build.txt` 和 `requirements-dev.txt` 后运行 `python -m pytest -q tests/test_portable_constraints.py tests/test_release_licenses.py`。它检查直接 import、锁定范围，以及 PyInstaller 数据文件的目标路径、qpdf 原文和漏收/篡改检测。首页取消终态的独立脚本是 `node tests/js/home_sse.cjs`。这些检查不能代替最终 ZIP 及恢复验收。

需要验证实际ZIP，可显式运行以下脚本（普通pytest仍禁止网络）：

```powershell
.\.venv-build\Scripts\python.exe tests/checks/check_windows_release.py 'releases\windows-x64-实际指纹' --check-launcher
```

它核实ZIP哈希/CRC，解压到含中文和空格的临时目录，再从无Python的PATH及无关工作目录运行EXE自检和两次实际启停；检查继承的实例目录未被读取或创建，以及便携CLI能校验一份合成旧库的备份。`--check-launcher` 额外验证单实例锁被占用时，启动器的实际EXE子进程会退出，不接管或停止已有服务。另加 `--check-restore` 可让解压出的 EXE 从 v2 临时实例制作完整备份，再故意切换下载根向同名 ZIP 备份。后一次必须拒绝，原包哈希不变，且能恢复到新目录；脚本会核对恢复后的图片字节和任务路径。这一项调用普通维护命令，须在服务锁空闲时执行。

**vNext 候选包交接：** 若构建时存在 `docs/WINDOWS_VNEXT_REVIEW.md`（或归档路径 `docs/archive/WINDOWS_VNEXT_REVIEW.md`），其内容计入指纹，并以 `WINDOWS_VNEXT_REVIEW.md` 原名放在 EXE 旁。发布后另写的 `docs/archive/WINDOWS_VNEXT_ACCEPTANCE.md` 不参与指纹，也不入包。

在构建所用的源码检出目录运行下面的专项，把路径换成**从待验 ZIP 解压出的 EXE**。驱动和样本生成器使用 `.venv-build`；服务进程直接运行该 EXE，工作目录在临时实例内，`PATH` 只含 System32。

```powershell
.\.venv-build\Scripts\python.exe tests/checks/check_vnext_portable.py 'D:\候选包解压目录\JMComic\JMComic.exe'
```

脚本先让 EXE 从空目录建库，核对 v5 和三张新表，再正常停机。源码只在离线样本准备阶段写入同一书目的两个任务：A 暂停且留有一页和回执，B 已发布两页。随后两次由 EXE 的隔离前台入口运行正常应用：检查本地阅读返回 B 的原始页，A 的真实工作页不可直读或按任务导出；分别核对 B 任务和书目的 ZIP 成员与字节、PDF 页数与图像内容。首次通过普通设置接口保存回环代理，重启再次读取设置、阅读和导出，并核对 A 的工作页与回执仍在。

前台入口使用独立互斥体，并限制 Python socket 和 libcurl 只能访问本机；不使用日常启动器、浏览器、生产实例或真实上游。每次停机前核对启动记录、PID、健康响应中的实例目录身份和一次性令牌，只通知该次进程并等待其退出。无法核实退出时返回非零并报告保留的临时目录和 PID。退出码 0 后，把末行 JSON 中的 EXE SHA-256、候选源码指纹及检查项与发行 ZIP 的 SHA-256 一起记入独立验收记录。该专项不验证真实 SDK 下载、外部代理转发或日常启动器交互。

产物尚未签名。当前机器构建及自检不能替代干净Windows虚拟机的兼容性验收；真实上游、完整安装/卸载体验、Docker仍需分别验收。

后端收尾后的人工复检范围和检查顺序见 [后端复检交接](archive/BACKEND_REVIEW.md)。
