# Second audit / 第二轮整体检查

## Scope / 范围

Backend request boundaries, settings transactions, downloads and terminal states,
file export/preview, background-client lifetime, Windows startup and repository delivery.
后端输入边界、设置事务、下载终态、导出预览、后台连接生命周期、Windows 启动和仓库交付。

No UI styles or theme tokens were changed. The locked theme remains `eye-care`.
没有修改 UI 样式或主题 token，继续使用锁定的 `eye-care` 护眼主题。

## Fixed / 已修复

- Invalid JSON objects/settings, misleading settings-import counts and unsafe tag-delete fallbacks.
  无效 JSON/设置、设置导入计数不实、错误的标签删除条件触发清空。
- Cross-site browser access and untrusted Host headers against the local server.
  跨站浏览器访问及不可信 Host 对本地服务的请求。
- Link traversal during ZIP/PDF/CBZ enumeration; natural page ordering and escaped preview URLs.
  ZIP/PDF/CBZ 枚举时跟随链接；预览缺图、页序与特殊字符 URL。
- ZIP temporary-file cleanup on errors and concurrent archive publication.
  ZIP 失败时临时文件泄漏、并发归档临时文件及发布冲突。
- Partial/canceled downloads triggering success or destructive packaging; failed replacement of old images.
  不完整或取消下载被误判成功、误触发打包清理，以及重新下载失败破坏旧图片。
- Archive format/output location, safe original-image cleanup and existing organize-destination conflicts.
  归档格式/位置、原图清理范围，以及整理目录遇到已有目标时的嵌套/覆盖风险。
- Same-album worker overlap, leaked queue capacity after thread-start failure and paused-job deletion.
  同专辑任务并发写入、线程启动失败占用容量、暂停任务仍可删除。
- Shared-client creation/retirement races, including requests still executing after a timeout.
  共享连接创建/释放竞争，包括 HTTP 已超时但后台仍在运行的请求。
- Windows mutex startup race/handle typing, service-start ordering and incorrect diagnostic error counts.
  Windows 互斥锁启动竞争/句柄类型、服务启动顺序及诊断错误计数。

## Verification / 验证

- `python -m pytest -q`: **125 passed, 1 skipped**.
- The skipped case needs Windows file-symlink creation permission. The separate Windows directory-junction
  escape regression passed. 跳过项需要文件符号链接权限，另一个实际 Windows 目录联接越权测试通过。
- `python tests/check_startup.py`: normal startup and occupied-port fallback passed; temporary servers stopped.
  正常启动、端口占用回退通过，临时测试服务已停止。
- `python -m pip check`: no broken requirements.
- `python -m compileall -q app.py launcher.py launcher.pyw core routes tests`: passed.
- `node --check` for all files in `static/js`: passed.
- `git diff --check`: passed.
- npm lint/typecheck/build were attempted but are unavailable because there is no `package.json`.
  已尝试 npm 三项检查，因没有 package.json 不可用，没有新增复杂工程配置。

Personal databases, downloaded content and settings were not used for these tests.
本次测试使用隔离数据，不使用个人数据库、下载内容和设置。

## Limits / 边界

These results do not certify live upstream availability, real external downloads,
all possible concurrency schedules, browser visual layout, or EXE packaging.
验证结果不代表真实上游始终可用、外网下载全链路、所有并发时序、浏览器视觉布局或 EXE 打包已验收。
The service is still a local single-user application, not an authenticated hosted service.
服务仍是本地单用户工具，不是具备用户认证的托管服务。
