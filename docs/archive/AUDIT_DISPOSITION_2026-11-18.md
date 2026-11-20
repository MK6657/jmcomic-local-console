# Windows vNext 审计处置基线（2026-11-18）

对象：[原报告](FULL_AUDIT_AND_FRONTEND_REFACTOR_2026-11-18.md)（原件 SHA-256 `4855da4232ecd9fd1f710930e779e56c871d908afe82a09935cc2ed1ea6593c0`），源码 `552bb650bb33ce3a0484ecb2471c4cd8934f4a12`。以下判定均针对这个提交；定位请用 `git show 552bb65:<路径>`，避免并行修复引起行号漂移。共 48 个编号（3 C、11 H、23 M、6 L、5 OPT）。本表是分流决定，不是新版发行验收或“全部修复”声明。

## 原包的只读基准

| 核对对象 | 实际结果 |
| --- | --- |
| 上版已验收的本机包（相对于本工作树根目录） | `releases/windows-x64-12fedbe5217a/JMComic-windows-x64.zip`，34,174,806 字节。保留原路径、原文件，不就地构建。 |
| 对 ZIP 字节执行 `Get-FileHash -Algorithm SHA256` | `6bba6a7f29f21fae091318c72c6fe3d854c537229b38c9da81073cea00b8bcf9`，与同目录 `SHA256SUMS.txt` 及[旧验收记录](WINDOWS_ACCEPTANCE.md)一致。 |
| 只读 `ZipFile.testzip()`；比较 ZIP 内 `JMComic/_internal/release.json` 与外部清单 | 全部成员 CRC 通过，两份 JSON 相同；源码输入 SHA-256 `12fedbe5217a7a628ca6605098a0fd621ac37eadd921e3d52d2b1bf5b2632be3`，对应旧产品提交 `d01bc0e`。构建指纹与 ZIP 校验值不可互换。 |
| 证据边界 | 历史 GitHub 内层 ZIP 为另一构建，不能用其哈希验证本机包；这次没重新运行旧 EXE，也没解压覆盖旧目录。新包须写入新目录，另记哈希、启停与恢复结果。 |

## 阶段 1 测试证据

用 `git archive 552bb650bb33ce3a0484ecb2471c4cd8934f4a12` 导出到独立临时目录，使用本项目 `.venv-build` 的 Windows x64 CPython 3.11.15 运行 `python -m pytest -q -rs --tb=short`：**2170 passed，2 skipped，294.29 秒**。两项跳过分别是 `test_audit.py` 和 `test_restore.py` 的文件符号链接权限。编译 `app.py launcher.py core routes`、17 个 JS 逐个 `node --check`、`pip check` 通过。实装发行约束为 31 项，不是原报告 §0.1 的 37 项。

单独增加 `tests/test_jobs_stream_routes.py::test_reconnecting_after_cancel_gets_one_terminal_event_without_a_stream_slot`：在临时库建排队记录再取消，不启动下载，实际 Flask 响应只有一次 `canceled` 和对应 job_id，未占流槽。该文件与 `tests/test_sse_client_capacity.py` 的阶段 1 专项合计 **11 passed**；新增用例不计入上面的原始快照计数。报告与表逐项比较为 48/48、无重复。此后工作区的并行修改不属于本基线。

## 处置表

“修复候选”指有具体代码依据，交相应模块先补失败用例；“实测”指现象、影响范围或收益尚待验证；“保留”指已有契约不能照原建议改；“延期”不等于消除风险。证据栏给出源码位置、已经存在的反证或下一步验收条件。

| ID | 处置（基线） | 源码依据与验证边界 |
| --- | --- | --- |
| C-01 | 修复候选：暂停书目的输出目录互斥 | `core/job_manager.py::_schedule_next` 排除在途专辑；`core/database.py::claim_next_queued_job` 没有覆盖已退出执行器的暂停行。`core/download_finalize.py::finalize_files` 按目录整理。测同书不同章节、打包删原图、恢复／取消及重启；必须仍让**其他书**占空槽。已有 `test_job_manager_contention.py` 的快速恢复只覆盖在途所有权。 |
| C-02 | 修复候选：首次 HTML 构造与域名发现 | `core/jm_service.py::get_client/new_image_client/new_check_client` 与 `core/sdk_adapter.py`；SDK 的 HTML 空列表发现路径不同于 API 初始化。`tests/test_sdk_adapter.py` 提供固定 HTML 域名，不能证明冷启动发现受代理、截止时间和关闭约束。用替身计调用与租约；不写死生产节点。 |
| C-03 | 阅读器专项实测，延期改架构 | `static/js/reader.js` 的 jump 循环、`preview.js` 缩略图和预读使用图片元素，不归 `utils.js` 的 fetch abort 管。六槽堵塞时长未经本轮浏览器测量。用慢图、跳页、离开和 bfcache 返回测请求与读页位置，再决定窗口化。 |
| H-01 | 修复候选：输出边界脱敏 | `core/logger.py::sanitize`、格式化 extra，`core/jm_service.py`/`job_manager.py` 的错误入库，`routes/api_jobs.py` 的列表和流共用错误内容。以假秘密检查日志、库、HTTP、SSE，勿把真实凭据当样本。 |
| H-02 | 修复候选：Windows 独占监听 | `app.py::_bind_preemptive` 探测后再用 `SO_REUSEADDR` 监听。按 Winsock 语义单次独占绑定；在临时进程验证忙端口、回退及释放后重启，不操作日常服务。 |
| H-03 | 修复候选：信号处理重入 | `app.py::main` 信号回调调用日志，`core/logger.py` 去重器用普通锁；`core/process_control.py` 通知主线程。测初始化期和正常服务期的协作停止；日志移出回调比只改成重入锁更直接。 |
| H-04 | 修复候选：Windows 名称与标识符 | `core/validation.py::safe_dirname/safe_filename/require_job_id` 的截断、清洗和保留名判断顺序不一致；与 `core/backup_paths.py` 对照。测试输出最终名称、控制字符、扩展名、非字符串及全串匹配。 |
| H-05 | 源码启动器升级专项 | `start.bat` 在 `launcher.py` 启动前执行 pip，源码服务可能仍加载原生扩展；不适用于已冻结的 EXE。复检繁忙/空闲身份、失败保留旧环境、日常无变化启动，不能按进程名停机。 |
| H-06 | 修复候选：按节点与总预算区分超时 | `core/update_checker.py` 的节点循环遇 timeout 结束，成功才更新 `_cursor`，多次失败有全局退避。用假时钟测试首节点故障、次节点成功及总预算耗尽；6 小时是退避上界，不是单次实测时长。 |
| H-07 | 修复候选：在线阅读报错分类 | `core/online_reader.py::_download`、`routes/api_online.py` 的 TimeoutError 分支；`core/upstream.py` 已处理多种 HTTP、curl 数字码和混合重试原因。按路由分别测坏正文、拒绝访问、TLS 与真超时；不能用搜索测试证明阅读已修。 |
| H-08 | Windows 文件分享冲突实测 | `core/storage.py` 发布用 `os.replace`；`core/download_finalize.py::finalize_files` 记录可选打包失败，已下载的散图不因此失败。区分旧包仍在、新包未发布与数据丢失；用临时旧包测短占用、长占用、原图和提示。 |
| H-09 | **保留零重试语义** | `core/jm_service.py` 计算图片尝试数，`core/image_download.py` 限制每趟节点数；`tests/check_download_queue.py` 和旧包验收已要求零重试时每页一次。元数据的独立故障转移策略另议，不把图片最少尝试数改成 2。 |
| H-10 | 本批小修：取消终态；其他项单列 | `routes/api_jobs.py::job_events` 在 tracker 消失后返回一次 canceled，`static/js/sse-client.js` 原先只关闭 completed/failed，`home.js` 的 running DOM 可在恢复时复活旧流。以真实脚本验证终态后错误、重试计时器和页面恢复；全局事件总线、下载页布局不属本批。 |
| H-11 | 阅读器与可访问性专项，延期 | `static/js/reading-nav.js` 错误探测、`reader.js`/`preview.js` 位图寿命、`static/css/style.css` 动画/语义色与设计约束是不同问题。分别测并发、长书内存、计算样式与实际背景的对比度；不以换框架作为通过条件。 |
| M-01 | 本地 API 来源边界候选 | `app.py` 已校验 Host、Origin、cross-site 和请求类型；same-site 跨端口及无元数据客户端为不同情形。需保留书签导航、旧客户端与合法 multipart 导入；协议变更须覆盖所有调用者。 |
| M-02 | 修复候选：后台工作所有权 | `routes/api_library.py` 单本执行器/批量线程、`api_wishlist.py` 收藏后线程不在 `core/runtime.py` 的停止列表。需观察超时返回后真实工作何时结束，停机后不得发布新结果。 |
| M-03 | 修复候选：标签空体返回码 | `routes/api_library.py` 标签路由强制解析 JSON 后广泛捕获异常；与 `app.py` 的解析错误处理分开。覆盖空体、非对象、非法 JSON、合法删除，确认不写库。 |
| M-04 | 调度延迟专项 | `routes/api_jobs.py` 创建与重试的唤醒不一致；`core/job_manager.py::_schedule_next` 每次领取一个。量测排队到领取，不破坏时间窗、并发上限及暂停公平性。 |
| M-05 | 修复候选：同步覆盖率 | `routes/api_library.py` sync-all 读取第一页再切前 50；成功时更新元数据。没有持久游标；连续两轮 51+ 部的实际选择应包含尾部，需兼顾失败条目。 |
| M-06 | 修复候选：诊断与客户端配置一致 | `routes/api_system.py` 从 SDK/设置取域再直连；裸 host:port、代理、TCP 成功与业务可用须分别表达。`tests/test_upstream_boundary.py` 已约束不可触发额外域名发现。 |
| M-07 | 拆分性能与设置契约 | `routes/page_routes.py` 下载页传完整列表，`templates/downloads.html` 由 JS 分页；`routes/api_settings.py` 导入逐项保存，`api_wishlist.py` 分项导入。保留逐项错误计数，以及“导出不含代理，手工导入可含代理”的实际行为。 |
| M-08 | 本批发行维护 | `routes/api_export.py` 直接用 pikepdf，`core/runtime_dependencies.py` 用 packaging；应核对全部直接 import。普通 CI 少 pip check；旧 ZIP 已有 img2pdf 的 LICENSE 及 qpdf DLL，但并非所有原生库的许可文本。检查实装 wheel、PyInstaller 收集路径及发布前的实际文件。 |
| M-09 | Windows 进程身份专项 | `launcher.py` 停机通知前核对记录，退出等待仍按 PID。需以句柄或等价证据区分原进程退出、PID 复用与访问被拒；不得把无法确认视为空闲。 |
| M-10 | CLI 副作用与产品契约分开 | `jmcomic_cli.py` 顶层导入日志，部分在线命令初始化数据库，下载走 SDK；`tests/test_sdk_adapter.py` 已覆盖参数共用。测试只读命令零写入；是否把 CLI 下载登记进 Web 队列需产品决定。 |
| M-11 | 数据库一致性候选 | `core/database.py::get_all_wishlist/get_library/get_library_stat` 的计数与条目来自多个查询；`get_jobs_page` 有显式读事务。用两连接验证同一快照下的总数与行。 |
| M-12 | 排队顺序与查询计划专项 | `core/database.py::insert_job_guarded/claim_next_queued_job`、`core/task_commands.py::_enqueue/queue_tail_time` 的时间戳来源不同，排序含表达式。先测批量/恢复/同秒顺序及 EXPLAIN，再决定索引与迁移。 |
| M-13 | Windows 路径专项，保留现有拒绝策略 | `core/storage.py` 祖先 reparse 检查、`core/backup.py` 路径边界、`core/restore.py` 源 ZIP 检查。云占位、挂载点、短名及 UNC 需要不同环境，不能由一般 junction 测试推导全部失败，不能直接放宽子树边界。 |
| M-14 | 停机预算候选 | `core/background_calls.py::stop` 重试清理，`app.py` 中 SSE 与 runtime 清理有顺序依赖。测 close 失败的返回时机、在途租约、等待线程和二次停止；调整 Waitress 线程数需另做容量实测。 |
| M-15 | 大备份性能候选 | `core/backup.py` 多次核对清单、回读 ZIP 并暂存数据库；`tests/test_backup.py` 要求检测备份中途内容变化、保住旧包。先量测字节数、临时盘和预算，不采信固定容量“必超时”。 |
| M-16 | 跨会话/关闭窗口另验 | `core/instance_lock.py` 使用 Windows `Local` 命名互斥；`launcher.py --wait` 的 Enter/Ctrl+C 协作退出不等于强关窗口。跨登录会话、关机时间窗、非 Windows 支持均未由本轮离线验证。 |
| M-17 | 长路径环境专项 | `core/validation.py` 截断组件而非总路径，`core/path_guard.py` 使用 realpath。应在长路径策略开/关、深目录和 UNC 上验证可写且不能越界，不能假设 realpath 恒返回扩展前缀。 |
| M-18 | 缓存正确性候选，性能待测 | `core/local_availability.py` 达上限时清表；散图证据复查类型、未复查正长度。批量状态分块已有容量保护。验证截空和换页的磁盘访问，不采信“每次都 100% 未命中”。 |
| M-19 | 连接复用基准，延期池化 | `core/jm_service.py` 临时元数据线程、逐图独占 client，`core/transfer.py` 的超时/关闭所有权不能打破。用回环测实际连接数及并发，不能将对象创建次数当 TLS 握手次数。 |
| M-20 | 按子项复核，不认定状态已被覆盖 | `core/database.py::update_job` 已禁止直接写 status，`finish_download` 是条件转换；仍需保证迟到进度不回写终态。`job_events` 无 tracker 的 queued/paused 与终态响应不同；单测暂停中写页、重试及 HTTP 窗口。 |
| M-21 | PDF 暂存基准，延期 | `routes/api_export.py` 归档源、转码和单页 PDF 可能同时占用预算；`tests/test_pdf_spooling.py` 验证逐页合并和清理。峰值与格式有关；量测实际占用再改清理时机，保持页数与 CRC。 |
| M-22 | 拆分错误归因与阅读资源上限 | `core/image_download.py::_hosts` 降低故障节点优先级，并非绝对禁用；异常处理未区分本地写盘错误。`core/online_reader.py` 缓存发布有代数保护，仍需单页大小/像素边界及读与清理交错测试。 |
| M-23 | 前端积压，非首页小修 | `detail.js` 等轮询寿命、`search.js` 双份快照/三种排序/星标、`wishlist.js` modal、`downloads.js` 无取消标签、各列表分页/状态键不同。逐页测试导航恢复与读屏状态；不能将取消 SSE 修复记作已解决这些项。 |
| L-01 | 缓存失效候选 | `routes/api_jobs.py::_check_cbz_path` 有独立 TTL；打包完成会清理其他索引。测先查无包再发布新包的首次列表徽章。 |
| L-02 | 构建临时目录与启动器清理分开 | `tools/build_windows.py` 发布后保留本次 work；`launcher.py` 的 RESTART_GLOBS、冻结指纹和日志辅助函数另审。只可清当次自建临时目录；不能清历史发行或改变服务复用语义。 |
| L-03 | 延期：桌面、恢复与日志分别验 | `core/desktop_platform.py::open_folder` 信任已授权调用方；`core/restore.py` 恢复时回队；`launcher.py` 主要在启动时轮转日志。补调用边界、收藏派生状态和持续写入用例。 |
| L-04 | 延期：四个文件/停机边界 | `core/download_layout.py` 章节标记、`download_checkpoint.py` 上次计数、`progress.py` 队列等待、`archive_pages.py` 索引打开路径。分别验证异常中断、部分检查点、主动唤醒和预算预检；不能只修其中一项就销项。 |
| L-05 | 前端安全加固，延期 | `library.js/search.js/detail.js` 设置封面源，`base.html/detail.html/404.html` 有内联脚本。先清点合法本地/CDN、转义和已有 CSP，再验严格脚本策略不破坏页面。 |
| L-06 | 页面参数校验候选 | `routes/page_routes.py` 的 album 和 preview-by-job 与 read/preview 数字校验不一致。保留不存在的合法任务返回 404，另测非法格式不触发下游查询。 |
| OPT-01 | 性能提案，先测量 | `core/database.py::get_db` 逐连接设 PRAGMA；详情缓存有有效期。采样连接开销、WAL 峰值、过期占用，验证停机及维护锁后再决定复用/checkpoint。 |
| OPT-02 | 性能提案，先测量 | `core/jm_service.py` 下载准备逐章 check_photo；详情有受限并发。比较首图时间，同时核对共享请求预算、暂停/取消及 SDK 所有权。 |
| OPT-03 | 性能提案，保留完整性检查 | `core/packer.py` 采用 DEFLATE、读旧包并校验新包。对真实尺寸的合成图比较 STORE 时间与大小；必须继续发现坏页，安全发布前不删原图。 |
| OPT-04 | 初始化/常量整理，延期 | `core/logger.py` 模块级初始化与 `jmcomic_cli.py` 只读入口相关；`validation.py`、`backup_paths.py` 等使用场景不同。先测导入/忙锁无副作用，勿靠合并常量放宽检查。 |
| OPT-05 | 验收工具优化，延期 | `core/release_lifecycle.py::run_worker` 调服务入口，`_request` 使用已发现的端口，并非选端口处。测临时 worker 在默认端口忙时的等待；保持普通启动器回退和实例身份检查。 |

## 无编号条目与范围

便于复检的四类索引（每个编号只列一次；一项内的子问题仍以原表为准）：

| 归类 | 编号 | 本次如何复核 |
| --- | --- | --- |
| 本轮修复 | C-01、C-02、H-01、H-02、H-03、H-04、H-05、H-07、H-08、M-02、M-03、M-05、M-06、M-08、M-11、L-06 | 直接对照任务发布、客户端生命周期、日志、启动器、Windows 整理、标签/书库与路由的回归用例。 |
| 合并处理 | H-06、H-10、M-07、M-14、M-17、M-18、M-22、L-01、L-02、L-04 | 分别随下载时限、取消事件、设置保存、停机、路径、缓存、取图、整理及发行工作处理；不能由其中一个通过的子项推断整条意见全部销项。 |
| 需要测量或后续专项 | C-03、H-11、M-01、M-04、M-09、M-10、M-12、M-13、M-15、M-16、M-19、M-21、M-23、L-03、L-05、OPT-01、OPT-02、OPT-03、OPT-04、OPT-05 | 其中备份、连接复用和 PDF 的受控实测见 [测量记录](WINDOWS_VNEXT_MEASUREMENTS.md)；阅读器、页面迁移、其他环境和优化另验。 |
| 依据不足，保留既有契约 | H-09、M-20 | 不把零重试改成两次请求，不根据迟到的进度事件就判定已覆盖数据库终态。 |

- §0：参数化查询/可信 schema、主机与来源校验、导出预算、文本转义及 fetch 退出清理是已有防线。批量预览已有 `status`；导航记忆是锁定的设计约束。全套离线绿灯不能证明每个新报告的场景已覆盖。
- §1.6：保留现有 HTTP 客户端依赖的 `data/item/job/result`、`reason/error_kind`、404/空项与分页键，先补契约再考虑版本化。核对文档的路由、schema、数据根和历史说法属于本批。`tests/conftest.py::collect_ignore` 的旧联网脚本不可单独作为离线回归运行；测试搬迁、前端组件测试改造另排。
- §2.1–2.6：Vue/Vite、SPA、合并书架、多标签总线、聚合 API、虚拟阅读器、重做主题组件均为待决提案；估算的包体、连接数与工期不是实测数据。此处阶段 1 是基线处置，阶段 3 是当前 MPA 的发行维护，**不是**原报告的 SPA Phase 1/3。保留 `eye-care`、显式页面路由、免 Node 运行、离线 socket/libcurl 边界。没有真实上游请求、下载任务或日常实例验收。
