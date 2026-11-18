# Windows 后端候选版：从副本复检

本版的验收问题是：**同一本书的 A 已暂停，B 能否独立完成、马上阅读和导出，C 能否继续；设置在重启后是否还在。** 页面仍使用原来的 Flask 模板，下载中心增加整册整理状态和重试入口。数据库版本从 v4 升至 v5。Windows 包自带运行时，普通使用者无需 Python、Node 或 Docker。

## 先选一份可丢弃的实例

1. 保留旧版 ZIP、旧程序目录和原实例。记下旧程序 `JMComic.exe instance` 显示的实例与下载位置。想继续使用日常实例时，不要为了复检强行结束它。
2. 在原启动窗口正常退出。用**旧版** EXE 在原实例环境制作停机完整备份；备份目标放在下载目录和程序目录以外的私有位置。仅含数据库的备份不含图片。备份内可能有代理设置和个人历史，不要分享。
3. 将候选 ZIP **完整解压**到另一个目录。用候选 EXE 将完整备份恢复到一个**尚不存在**的目录。不要只复制数据库和图片，库内的历史绝对路径需要由恢复工具重映射。
4. 从一个新 PowerShell 窗口清除继承的目录覆盖，核对 `instance --home` 的输出确实是副本，再 `launch --home`。新版首次打开副本时才会升级，原实例不参与。

```powershell
# 第一个命令在旧版程序目录执行，保持旧实例原来的环境设置。
.\JMComic.exe cli backup --include-downloads --output 'D:\PrivateBackups\before-vnext.zip'

# 以下在候选版目录的新 PowerShell 窗口执行；示例盘符和目录可替换。
Remove-Item Env:\JMCONSOLE_HOME, Env:\JMCONSOLE_DOWNLOAD_DIR, Env:\JMCONSOLE_CACHE_DIR, Env:\JMCONSOLE_BACKUP_DIR -ErrorAction SilentlyContinue
.\JMComic.exe cli backup-info 'D:\PrivateBackups\before-vnext.zip'
.\JMComic.exe cli restore 'D:\PrivateBackups\before-vnext.zip' --to 'D:\JMComic Review Instance'
.\JMComic.exe instance --home 'D:\JMComic Review Instance'
.\JMComic.exe launch --home 'D:\JMComic Review Instance'
```

## 手工验收顺序

| 操作 | 应当看到什么 |
| --- | --- |
| 先打开副本里一本旧散图书和一本只剩 CBZ/ZIP 的书，翻到末页，导出 ZIP | 阅读与导出仍有原页；启动不搬动整座旧书库。 |
| 两个设置标签页同时打开。甲改超时并保存，乙只改重试为 `0` 并保存。正常退出，再从同一副本启动 | 重新打开设置，两项都保留。代理若从有值改为空，后续新客户端直连；不要在问题报告里粘贴代理凭据。 |
| 在有多个章节的同一本书中分别选两组不同章节为 A、B，再排另一本 C。把并行任务数设为 1，关闭“跳过已存在”，A 下载部分页后暂停 | 等在途写入结束，B 和 C 能继续。B 完成时打开书库；A 尚未完成的页不出现。从 B 的任务导出 ZIP 和 PDF，只能得到 B 所选页。 |
| 恢复 A，打开整册，再分别导出任务和整册 | 已由 A 确认且未变的页不重复请求。整册合并已发布章节；有部分重叠时，同章同原页号只显示一份，采用较新**创建**的任务版本。两次任务的各自导出保持原内容。 |
| 启用自动打包及删原图；同册仍有暂停任务时观察 B；之后让全部任务结束 | B 可以先用，整理稍后进行。在 Windows 占用文件导致替换失败时，先解除占用，再点“重试整理”；不需要重新下载。 |
| 清除 B 的任务历史。重进书库再读、导出。回到首页取消一个测试任务 | 书仍可用；取消任务的进度订阅结束。 |

需要严格复核请求次数或中断窗口的审查者可用源码离线重跑 `tests/test_queue_recovery.py` 和 `tests/test_publication_reader_export.py`。`tests/check_download_queue.py --two-stalls`、`--pause`、`--quick-resume` 各单独运行，使用真正的回环 HTTP 和 SDK 解码，不依赖上游；无需在个人实例制造故障。`JMComic.exe self-check`、`JMComic.exe lifecycle-check` 是便携包自检。实际 ZIP 的验收证据见分支上的 `docs/WINDOWS_VNEXT_ACCEPTANCE.md`。

## 存储与回退

- `downloads/.jmcomic-work/` 保存按任务隔离的断点页和该任务的已发布快照。`.jmcomic-views/` 可能保存待归位的整册版本或为避免覆盖已有文件而保留的版本。不要手动删除、移动这些目录，也不要只复制可见书名目录来搬迁实例。
- “删原图”只针对本次已安全打包的公开散图；为保证不同版本的任务还能精确导出，会保留独立快照。完成后清理已确认且未变的工作副本。旧包、不明文件、失败或中断留下的副本可能继续占用空间。做大批重下或完整备份前，检查目标卷的可用空间。
- v4 的老任务没有 v5 的发布清单，历史导出保持原目录语义；无法核实的旧断点可能要重新请求。v5 之后的停机完整备份包含私有目录和数据库内的页摘要。数据库备份可恢复设置和记录，不能恢复图片字节。
- 回退时，正常关闭新版，用保留的旧程序和**原实例**重新启动。不要让旧程序打开 v5 副本，不要修改 SQLite 的 `user_version`。试用期间新增的任务、设置和下载不会自动反向同步。
- 反馈请记下包的 SHA-256、任务状态、选择了哪些章节、预期和实际页数、发生时间、页面显示的请求编号。不要发原数据库、完整日志、Cookie、个人资源或带用户名密码的代理地址。
