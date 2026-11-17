# Windows x64 便携版发行验收

发行对象是 `codex/refactor-windows` 的个人 Windows 本地控制台：双击 EXE，使用浏览器访问本机服务。Python、Flask、SQLite 与官方 jmcomic SDK 继续共用一份源码。实际使用、备份恢复、选择实例和重新构建见 [Windows 使用说明](WINDOWS_RELEASE.md)。

| 本机验收包（2026-11-18） | 核对值 |
| --- | --- |
| 产品与随包输入提交 | `d01bc0eeb2430bbdc02bf212e607de6226e260a7` |
| 发行目录 | `releases/windows-x64-12fedbe5217a/`；以此目录区分本版与之前的本机候选包 |
| 本机 ZIP | `JMComic-windows-x64.zip`，34,174,806 字节（32.59 MiB） |
| 本机 ZIP 的 SHA-256 | `6bba6a7f29f21fae091318c72c6fe3d854c537229b38c9da81073cea00b8bcf9` |
| 本机构建输入指纹 | `12fedbe5217a7a628ca6605098a0fd621ac37eadd921e3d52d2b1bf5b2632be3` |

**[下载 GitHub 验收过的 Windows x64 构件](https://github.com/MK6657/jmcomic-local-console/actions/runs/44194738265/artifacts/14380415430)**（Actions 通常需要登录，当前记录的保留期限为 2027-02-15 22:55 UTC）。先解开外层压缩包，再把其中的 `JMComic-windows-x64.zip` 完整解压到可写目录，保留 `JMComic.exe` 旁的 `_internal`。校验内层 ZIP 时使用和它一同下载的 `SHA256SUMS.txt`。本机可直接解压上表列出的 ZIP。从分支页面点 **Code → Download ZIP** 得到的是源码。

## 本版解决的问题

| 场景 | 行为 |
| --- | --- |
| 一本书中的某些图停止响应，后面还有书 | 失败页受超时和重试设置约束；已成功的图保留，后一本自动开始。主动暂停会在在途工作排空后让位。恢复复用原任务号。 |
| 并发下载、补章和整理 | 图片并发数是所有后台下载共用的名额；保留在线阅读的独立容量。扁平整理按章节身份和原页号识别同一页，相同内容不增份，冲突留两份；不自动清理无法认领的历史文件。 |
| 两个旧设置页先后保存；收藏超过 200 条；对失败任务重试两次 | 设置只提交本次改动。收藏文件顺序分批导入，显示新增、已存在与失败；未确认批次可重试。任务反馈区分新建与已有活动任务。 |
| 清空已完成的任务历史 | 资源库的可读书目仍可导出 ZIP、PDF，不创建新任务。两个入口共用导出限额和附件清理；旧任务号仍返回 404。 |
| 后续升级上游 SDK | 实装版本参与源码服务复用判断。客户端使用的传输会话、域名以及图片请求头由小型适配层取得；共享和独占客户端的归还、超时、停机仍由各调用方负责。 |
| 维护或搬迁 | 可查询、选择实例。完整备份再恢复到新目录会重映射下载路径；直接复制带绝对路径的旧数据库不等于迁移。新程序和旧程序可分目录保留。 |
| 解压路径包含当前 Windows 区域设置不能编码的字符 | EXE 在 Windows 10 1903 及以上、Windows 11 声明进程代码页为 UTF-8，libcurl 因此能取得证书文件名。无需给系统切区域或关闭证书验证。 |

## 验收口径

本机为 Windows 11 专业版 x64。最终包使用新的 CPython 3.11.15 虚拟环境：从本机已有的 wheel 离线安装了发行所需的 31 个包，`pip check` 和构建约束均通过。具体版本在对应包的 `release.json` 中。开发环境的离线测试会阻断 Python socket 和 libcurl；需 HTTP 的验收单独运行，使用临时实例、合成图片和本机地址。

| 实际检查 | 结果 |
| --- | --- |
| 冻结后的源码 | `python -m pytest -q -rs --tb=short`：**2,170 passed，2 skipped，293.16 秒**。两项均为该 Windows 账号缺少文件符号链接权限。Python 编译、17 个页面 JS 各自的 `node --check`、`pip check`、分支差异的 `git diff --check` 通过。源码服务用临时实例实际启动，在受控占用 5000 时能转到 5001，并核验 PID、正常退出。 |
| 本机 HTTP + 官方 SDK/libcurl | 重试为零，三页书有两页停滞：10.75 秒内首本记失败、保存好页，下本完成，四页各请求一次。普通暂停和快速恢复两组顺序都是 **A1、A2、B1、A3**，每页仅一次，四张 WebP 均能解码，结束时执行器数为零。 |
| 并发和文件 | 两本各两章，后台设 3／7 时实测峰值分别为 3／7，同时仍容纳 4 个前台阅读请求和 2 个预取。扁平化的散图、CBZ、ZIP 三组：整本 4 页，重下或改章名仍 4 页；同一页内容变了保留两版，总数 5，再下仍 5。 |
| 实际 ZIP | 核对 SHA-256 与 CRC，解压到 `中文 📚 解压验证`，从无 Python 的 PATH 运行 EXE 自检、两次启停和任务恢复。旧包在同类特殊字符目录的 SDK 取图可复现 `UnicodeEncodeError`，本版通过。查看/取消选择实例未写旧库，缺失实例不被创建，忙锁拒绝第二个普通服务。v2 完整备份可恢复；改下载根向旧 ZIP 写入会拒绝，旧包哈希不变，恢复图片字节匹配。 |
| 本机 HTTPS 证书验证 | 在独立测试入口中使用与 EXE 相同的进程声明和最终 ZIP 的 `_internal` 原生库。给回环 HTTPS 服务使用临时自签证书，在特殊字符目录显式指定这份 CA 时返回 200；改回包自带的默认信任列表后，libcurl 返回证书校验错误 60，服务只收到前一次 HTTP 请求。 |
| 普通启动器 | 不使用测试 worker，从发行目录运行 `launch --home` 并确认，临时实例由 v2 升 v4；用 `attach --home` 再打开，健康响应 PID 未变。系统浏览器打开请求均记录为同一回环地址。附着进程退出不影响服务；在原启动窗口发送回车后，服务正常停止，运行记录移除。 |
| 实际浏览器 + 发行 EXE | 两个设置标签从 `30/2` 起步，分别保存为 `47/2`、`47/0`，服务重启后页面仍显示 **47/0**。对同一失败任务点两次重试，一次新建、一次复用，重启后仅一个排队后继。201 条收藏连续导入两次，共 4 个请求，第一次新增 201、第二次已存在 201。界面清理已完成历史后，旧任务查不到，资源库两个按钮都能保存文件；打开导出的 ZIP 比对原图字节、解析 PDF 确认 1 页。 |

GitHub 在最终产品提交 `d01bc0e` 上另行验收：

| 独立 Windows 构建机 | 结果 |
| --- | --- |
| [Python 3.12 源码工作流](https://github.com/MK6657/jmcomic-local-console/actions/runs/44194738416) | Python 编译、JS 语法检查通过；离线套件 **2,172 passed，0 skipped，729.31 秒**，包括本机因权限跳过的两项。 |
| [Python 3.11.15 便携包工作流](https://github.com/MK6657/jmcomic-local-console/actions/runs/44194738265) | **2,172 passed，0 skipped，814.15 秒**。三项队列专项、构建、EXE 自检与重启、特殊字符路径下解压验包、忙实例与 v2 备份恢复均通过，随后上传构件。 |
| 从 GitHub 下载后在本机重新验包 | 外层 SHA-256 与 GitHub 构件摘要一致。内层 ZIP 为 **34,150,340 字节**，SHA-256 为 `0755c2278e51f27418bb83572a81ff8d4ca66e716282a831bb179d7bc1de89fa`，与所附 `SHA256SUMS.txt` 一致。ZIP 的 CRC、包内外清单、31 个发行依赖一致性，以及这份下载件的 EXE 启停和旧库恢复再次通过。 |

本机与云端基于同一产品提交，独立打包。云端源码指纹 `44ed59e84116285cb08c015f4230685fa140bca2688b7a9b89d6b3a5b11a3923` 已用该提交全新 Windows 检出的文件独立算出；源码指纹按实际文件字节计算，会反映检出的换行符。构建时间和产物字节也可能不同。请按下载来源核对对应的 SHA-256。

本轮未接触日常实例，未向真实上游发送搜索、详情、在线阅读或下载请求。所有实测时间来自上述机器与小样本。包未签名；本机及云端 runner 不能代替所有 Windows 10/11 版本、企业策略和 SmartScreen 环境下的试用。

历史版的通过数与包哈希见[路线 B 首个候选](WINDOWS_CANDIDATE_B.md)，不能代替本版验收。更多审计的条件性保留项在[项目交接](PROJECT_HANDOFF.md)。压缩包读页即使是条件请求，也保留成员 CRC 校验；已做的索引优化不以跳过坏页检验换取性能。

## 给接手者

从 [GitHub 重构分支](https://github.com/MK6657/jmcomic-local-console/tree/codex/refactor-windows) 克隆并记录 `git rev-parse HEAD`。`git diff 5fdc984..HEAD` 是从首个 Windows 候选继续完成的工作；`git diff c310e3f..HEAD` 才是相对旧主分支的全部改动。源码 ZIP 不含便携 EXE。

- 下载任务先看 `core/job_manager.py` 的执行器所有权、`core/task_commands.py` 的状态变更，再看 `core/download_checkpoint.py`、`core/download_capacity.py` 与 `core/download_finalize.py`。维护扁平布局时，请保留章节标记与页面身份；不要按文件名或页数批量清理旧书。
- SDK 升级先看 `core/sdk_adapter.py` 和两组域名的设置。先重跑行为测试，再分别验获授权的真实搜索、详情、在线阅读和下载；不要用本地健康检查证明上游可用。
- 使用 `.venv-build` 运行 `python -m pytest -q -rs`。`python tests/check_download_queue.py --two-stalls`、`--pause`、`--quick-resume` 是三次独立命令：分别核实坏页不挡下本、暂停让位、快速恢复不会产生第二个写入者。三者只使用本机回环和临时库。
- 最终构建流程、ZIP 启停和旧库恢复命令在 [Windows 使用说明](WINDOWS_RELEASE.md)；复检具体操作见 [后端复检交接](BACKEND_REVIEW.md)。签名、安装器、新 UI 和 Docker 不是这份便携版的交付项。
