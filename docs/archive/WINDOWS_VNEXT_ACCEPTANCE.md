# Windows v5 后端候选包验收（2026-11-18）

**结论：交付本地 Windows x64 候选包，供用户在恢复出的副本上复检。** 同册不同任务可分别发布；阅读器、任务 ZIP/PDF 和书库导出均检查了实际页内容。未更换前端框架，未部署 Docker。首次使用请先读[复检与回退手册](WINDOWS_VNEXT_REVIEW.md)。

| 构件 | 标识 |
| --- | --- |
| 工作分支与产品提交 | `codex/windows-vnext`，`f30f1f5a3ba037e3201d56c08a96fa936d43475d`；在 `552bb65` 基础上开发。后续仅有验收工具和文档提交。 |
| 本机候选 ZIP | [`releases/windows-x64-66390ccf9731/JMComic-windows-x64.zip`](../releases/windows-x64-66390ccf9731/JMComic-windows-x64.zip)，**34,550,917 字节**。此链接是本地构件，不是 GitHub Release。 |
| 候选 ZIP SHA-256 | `94f3744578cc09ebca15dc0a8ed4f67e612cf66b702fa662fc41a6aea310536a` |
| 构建输入 SHA-256 | `66390ccf97312b6b5ab517b0193ca7c50cbc3780741084b6a0f3d4a32f81ea68`，与包内、包外 `release.json` 和冻结源码一致。 |
| 运行基线 | Windows x64，CPython 3.11.15，官方 `jmcomic 2.7.10`；发行清单 **31** 个锁定包，解压后核对 **87** 份第三方许可文件。未改 SDK 安装目录，未写死生产域名。 |
| 旧版与回退 | `codex/refactor-windows@552bb65`；[旧版便携包](../releases/windows-x64-12fedbe5217a/JMComic-windows-x64.zip) 已复核仍为 `6bba6a7f29f21fae091318c72c6fe3d854c537229b38c9da81073cea00b8bcf9`。旧程序只打开原实例。 |

## 哪些行为已实际验证

| 场景 | 证据与结果 |
| --- | --- |
| A 暂停、B 同册完成、C 继续 | 在真实下载调度、文件发布和 Flask 路由上用合成图片复现。旧版 B 只选一页却看到了三页；现在 B 的任务导出和当时的书库都恰好一页，A 的半章不出现，C 完成。A 恢复后，已确认的好页额外请求数为零。 |
| 部分重叠与重启 | A 比 B 早创建、晚提交。两个任务各导出自己的三页，合并书库共四页；共享章的两页取 B 的版本，逐页核对字节。分别在快照封存后、整册改名后未落库、完成事务提交后注入中断；重跑不重复登记。另测中断任务重新排到已等待的同册任务后面。这里模拟进程边界，不代表断电持久性认证。 |
| 实际卡页 | 真实 SDK、libcurl、本机 HTTP：坏任务两张图停滞，设零重试时每页请求一次，约 10.8 秒后坏任务失败、后一本完成、执行器归零。普通暂停及在途快速恢复两种模式都让后一本先运行，四张测试图片各只请求一次。耗时只代表本机样例。 |
| 旧书与新整理 | v4 的散图、CBZ 和 ZIP 可读；同册有不明半成品时只迁入能证明归属的旧章，保留原处文件。覆盖多版本重叠、整册归位、Windows 文件占用、手工补图、被替换的同名图、取消和整理重试。清任务历史后仍可从书库阅读和导出。 |
| 设置、上游与停机 | 同一进程执行代理 A → 留空直连 → 代理 B，检查实际传输配置；缓慢的 HTML 发现不会阻塞设置保存。库内 105 本的标签遍历覆盖末页。正常启动、5000 端口冲突回退、停机未排空返回失败与最终排空通过。 |
| ZIP 内实际 EXE | 中文、空格、emoji 路径下解压，通过 CRC、SHA-256、许可、无系统 Python 的自检和启停。EXE 创建 v5 空库；停机后由测试夹具写入 A 暂停、B 已完成的合成书，再启动。A 的私有页不可访问且仍有回执；B 的两页均可读，任务/整册 ZIP 比较字节，PDF 解出图像比对尺寸与颜色。重启后复验。另由 EXE 使用 CLI 对 v2 样例制作、验证完整备份并恢复到新目录。 |
| 新旧两版 EXE | 真实 `12fedbe5217a` 旧服务仅有一个暂停任务，真实 `66390ccf9731` 启动器识别版本差异、暂缓升级并打开原实例。原 PID、暂停任务和 v4 库不变；未发出停机事件，未启动替代进程。 |
| Chromium 实际页面 | 在候选 EXE 的临时实例中打开两个已加载的设置页，一个表单保存超时 49，另一个只保存重试 0。刷新后表单读回 **49 / 0**。下载中心出现 A「已暂停」、B「已完成／可离线阅读」；打开连续阅读，浏览器实际解码 B 的两张 `24 × 16` WebP，显示的两张样书图片均属于 B。 |

## 复跑与范围

冻结源码全量：**2,361 通过、2 跳过、0 失败，292.94 秒**。两条跳过均因本 Windows 账户无创建文件符号链接的权限（`test_audit.py`、`test_restore.py`）。Python 编译、17 个 JS 文件分别执行语法检查及 `pip check` 均通过。实际 ZIP 和两个版本 EXE 的专项均退出 0。

在项目根目录运行。源代码测试使用 `.venv-build`，普通用户运行发行包无需这个环境：

```powershell
.\.venv-build\Scripts\python.exe -m pytest -q -rs --tb=short
.\.venv-build\Scripts\python.exe -m compileall -q app.py launcher.py core routes
.\.venv-build\Scripts\python.exe -m pip check
Get-ChildItem static/js/*.js | ForEach-Object { node --check $_.FullName; if ($LASTEXITCODE) { throw $_ } }
.\.venv-build\Scripts\python.exe tests/check_startup.py
.\.venv-build\Scripts\python.exe tests/check_backend_shutdown.py
.\.venv-build\Scripts\python.exe tests/check_download_queue.py --two-stalls
.\.venv-build\Scripts\python.exe tests/check_download_queue.py --pause
.\.venv-build\Scripts\python.exe tests/check_download_queue.py --quick-resume
.\.venv-build\Scripts\python.exe tests/check_windows_release.py releases/windows-x64-66390ccf9731 --check-launcher --check-restore --check-vnext
.\.venv-build\Scripts\python.exe tests/check_vnext_portable.py releases/windows-x64-66390ccf9731/JMComic/JMComic.exe --previous-exe releases/windows-x64-12fedbe5217a/JMComic/JMComic.exe --busy-upgrade-only
```

未使用日常数据库、下载目录、代理或真实上游。回环成功不证明今天的线上搜索、详情、在线阅读或下载可用。没有在干净的 Windows 虚拟机、网络盘或全部长路径策略组合上实机验收，也未验证公网服务。便携包使用原静态 JS，不需要前端构建或 CDN。

**容量取舍：**为了保持每个任务可精确导出，保留不可变快照，并且不自动删除无法证明整棵目录归属、又可能仍有读者持有引用的旧整册视图。在 5 次逐章发布的合成样例中，旧视图约占 7.81 MiB，约为当时书架大小的两倍，另有任务快照占用。建议复检时也观察自己的增量下载频率和可用空间。见[测量方法、原始数值与限制](WINDOWS_VNEXT_MEASUREMENTS.md)。
