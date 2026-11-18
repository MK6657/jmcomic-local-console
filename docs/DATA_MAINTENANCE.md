# 数据目录、备份与恢复

下文区分历史 Windows 版（数据库 v4）与 vNext 候选（v5）。数据库版本指 SQLite `PRAGMA user_version`，与备份 ZIP 格式版本无关。打开旧实例可能触发升级，请先备份。

## 程序与数据目录

未配置环境变量时，保持原有的可移植布局：项目/可执行文件目录下的 `runtime/data`、`runtime/logs`、`runtime/cache`、`runtime/backups` 和 `downloads`。

程序资源（模板、静态文件、源代码、虚拟环境、代码指纹）仍在程序目录。可以在**启动新进程之前**设置以下环境变量，把可写数据放到独立位置：

| 环境变量 | 用途 | 未指定时 |
|---|---|---|
| `JMCONSOLE_HOME` | 实例根目录；数据库与日志随此目录 | 原项目/可执行文件目录 |
| `JMCONSOLE_DOWNLOAD_DIR` | 下载文件 | `HOME/downloads` |
| `JMCONSOLE_CACHE_DIR` | 可重建缓存 | `HOME/runtime/cache` |
| `JMCONSOLE_BACKUP_DIR` | 备份输出 | `HOME/runtime/backups` |

只接受非空绝对路径。运行中改变环境变量不会热切换目录；需要停止并以新配置启动。仅设置新目录不会复制旧数据库或文件，也不会扫描其他目录导入资源。旧数据库里的 `download_root` 设置不能覆盖当前进程配置。

- **v4：** `runtime/data/resume-*.json` 记录已确认页面的文件身份、大小和时间，不在备份内。恢复后没有这份确认；开启“跳过已存在的文件”时仍可校验并复用有效散图，关闭时未完成的页可能重新下载。升级到 v5 不会凭旧任务状态补造页面回执或成品快照。
- **v5：** 未完成任务的页在下载根的 `.jmcomic-work/`，不供读者或导出访问。SQLite 的 `task_page_receipts` 按任务 ID、任务内相对页路径保存 SHA-256。恢复后，即使关闭“跳过已存在的文件”，匹配的任务页仍可复用：文件必须在对应位置、为普通文件、摘要一致且图片校验通过。单有回执不能代替页面，缺失或校验失败的页须重新获取。旧式 `resume-*.json` 仍不随包恢复。

PowerShell 示例（路径为示例，按实际位置替换）：

```powershell
$env:JMCONSOLE_HOME = 'D:\JMComicData'
$env:JMCONSOLE_DOWNLOAD_DIR = 'E:\JMComicDownloads'
.\.venv\Scripts\python.exe launcher.py --wait
```

当前 Windows 单实例锁沿用原名称：启动、维护及旧版程序互斥。本批没有开放同时运行多套服务或交付 Docker 模式。

## 备份

先正常关闭本程序，再运行维护命令。CLI 获取与服务相同的单实例锁，程序仍运行时拒绝备份/恢复，不会结束其他进程。底层数据库快照使用 SQLite backup API，包含 WAL 中已提交的数据；完整资源备份要求停机，并复核数据库和文件是否在备份期间变化。

```powershell
# 数据库 + 资源路径、大小、SHA-256清单，不包含漫画文件内容
.\.venv\Scripts\python.exe jmcomic_cli.py backup

# 完整备份：额外包含下载目录里的实际文件
.\.venv\Scripts\python.exe jmcomic_cli.py backup --include-downloads

# 指定备份文件与总时间预算（秒）
.\.venv\Scripts\python.exe jmcomic_cli.py backup --include-downloads --output 'D:\PrivateBackups\jmcomic.zip' --timeout 600

# 只读完整校验并显示摘要，不恢复或启动任务
.\.venv\Scripts\python.exe jmcomic_cli.py backup-info 'D:\PrivateBackups\jmcomic.zip' --timeout 600
```

备份为版本化 ZIP，包含清单和 SQLite 快照。v5 的完整备份还复制下载根中的任务工作页、已提交任务的私有快照，以及供资源库读取的公开目录（包括使用 `.jmcomic-views/` 的版本）。搬迁时要保留这些目录的相对位置；只复制书架上可见的图片会丢失续传或按任务导出所需的文件。缓存、日志、`.jmcomic-staging/` 和已识别的旧式临时页不进入备份。失败不会发布一个伪完整的新包，也不会覆盖已有的有效备份。

普通备份（含或不含资源文件）会在发布前检查快照里任务和资源目录的下载路径。即使已清除任务历史，资源记录也可能仍指向以前的下载根。如果提示路径无法映射，请保留现有备份，核对 `JMCONSOLE_DOWNLOAD_DIR` 与制作备份时使用的实例。所有记录都属于旧根时，可用原根配置制作完整备份，再恢复到新目录。若数据已分属两个根，先分别保留原库和两处文件；仅复制图片、改备份文件名或清理历史都不能修正这些记录。升级前的数据库快照另有用途，不能代替完整资源备份。

`backup-info`的哈希、CRC和SQLite完整性校验不代替恢复入口的受支持结构检查。外部工具改过表定义、索引或约束的备份，即使完整性校验通过，也可能被恢复入口以`unsupported_schema`拒绝；目标目录不会发布。

备份包含数据库内的私密设置、代理凭据和个人记录，当前不加密。请使用私有目录，不要提交或公开分享；默认 `runtime/backups/` 已被 Git 忽略，自定义目录应放在仓库之外。

数据库/清单备份有助于保护记录，**不能代替漫画文件备份**。资源目录不存在、含链接/目录联接、读取失败、文件变化、空间不足或超时会明确失败；不会静默漏掉无法读取的资源并宣称完整成功。

## 恢复与搬迁

恢复默认只写入**不存在的全新绝对目录**。它先校验 ZIP、清单、文件 SHA-256/CRC 和 SQLite，再在同盘私有目录恢复，成功后发布。已存在的目录（即使为空）也不会覆盖；不会调用 `extractall()`。

```powershell
.\.venv\Scripts\python.exe jmcomic_cli.py restore 'D:\PrivateBackups\jmcomic.zip' --to 'D:\JMComicRestored' --timeout 600
```

- 以清单里的源下载根为依据，按路径组件映射任务和资源位置到新实例 `downloads/`；根外路径、非法路径或不支持的数据库结构拒绝恢复。
- 保留收藏、标签、标题、历史和新章节检查基线。中断的 `running` 改为 `queued`，用户 `paused` 保持暂停。v4 和 v5 的中断任务追加到原等待队列之后，原创建时间保留；v0–v3 沿用旧恢复顺序。
- 不含资源文件的备份仍能恢复数据库，但新下载目录为空。v5 的回执、发布清单和维护记录保留在数据库里；恢复对应文件前，不能据此续传已确认的页、离线阅读或导出成品。已登记但缺失的任务快照不会改用其他任务的页面导出。
- 恢复不自动启动服务或创建下载任务；用户之后启动实例时，原有队列会遵守下载调度规则继续运行。
- 不覆盖源实例。验证新实例后再自行决定旧实例的保留方式；本操作不会删除原数据。

启动恢复实例前，设置新 `HOME` 并清除其他路径覆盖，避免沿用原实例的外部目录：

```powershell
$env:JMCONSOLE_HOME = 'D:\JMComicRestored'
Remove-Item Env:\JMCONSOLE_DOWNLOAD_DIR -ErrorAction SilentlyContinue
Remove-Item Env:\JMCONSOLE_CACHE_DIR -ErrorAction SilentlyContinue
Remove-Item Env:\JMCONSOLE_BACKUP_DIR -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe launcher.py --wait
```

## 升级前快照

打开旧版数据库时，程序在 WAL 配置、建表、索引或迁移写入前保存 `pre-migration-v旧版-to-v新版-*.zip`。快照失败就停止初始化；不对新空库或已经匹配的数据库反复创建迁移备份。高于当前程序支持的数据库版本直接拒绝打开，避免旧程序修改新库。

vNext 启动先只读检查结构，再决定是否迁移。受支持的 v0–v4 旧库在升级前生成数据库快照；v4 升到 v5 时新增任务页回执、任务成品和书目维护表及发布索引。按声明版本检查的表、索引、资源目录触发器或定义/约束不匹配时拒绝启动，不会通过重建空表掩盖数据丢失。v4 程序不能打开已升级到 v5 的数据库；回退须用保留的旧库和对应文件。不要手工降低 `user_version` 或删表绕过检查。

历史结构也要求原有jobs/settings完整；v3资源目录的表、索引和trigger属于同一迁移，缺少其中之一同样拒绝。v3恢复后的首次队列升级以事务隔离目录trigger的副作用，保留独立资源目录已有的路径、标题和替换标记。

迁移快照只保存数据库和用于路径映射的源下载根，**不扫描、不复制资源文件**，不会因大资源库而自动做一次全量资源备份。它可以按上述 `restore` 命令恢复到新实例，但漫画文件必须从原下载目录另行保留/恢复。没有源下载根的通用数据库快照只允许校验，不自动猜测迁移路径。

当前恢复入口不提供原地覆盖、自动删除旧目录、自动降级数据库或隐式双向同步。当前实现以 Windows 维护流程验收；Linux/Docker 的发行和部署验证仍在后续阶段。
