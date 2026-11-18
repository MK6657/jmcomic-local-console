# 藏书阁 Windows x64：本地交付与验收（2026-11-18）

本版以 `codex/windows-vnext@2903b8c` 为基线。交付可运行的 Windows 程序、离线样书预览和源码；不需要用户安装 Node 或 Python。使用方法见 [Windows 藏书阁手册](ATELIER_WINDOWS.md)。这是新界面的首次交付，尚未在用户日常书库上试用。

| 构件 | 标识 |
| --- | --- |
| 本地 Windows 便携包 | `releases/windows-x64-a764df49df7b/JMComic-windows-x64.zip`（35,893,123 字节） |
| ZIP SHA-256 | `19c4ff920121ad96fe278efebb278a065fd03e9b46135977d81f2e874edd8089` |
| 构建输入 SHA-256 | `a764df49df7b74a53794af2783d7deed0124db366f62827d5f7bafb92ce1bf8d`；发行目录含 `release.json` 和 `SHA256SUMS.txt` |
| 运行环境 | CPython 3.11.15、官方 jmcomic 2.7.10、原有 Flask / Waitress / SQLite；正式模式继续配置真实上游 |

## 用户能看到什么

- 四套矿物纸墨主题由服务端首屏输出；侧栏、偏好设置和阅读器使用同一选择。首页、搜索、书阁、收藏、下载、详情、设置、日志、在线/本地阅读、单页预览和错误页都在真实应用里。
- 书库和搜索支持书架/名录；检索保留“最多图片”及返回位置。传输中心按册展示各任务，分别处理续传、整理、章节阅读与 ZIP/PDF 导出，可查看已取消任务。连续阅读和单页预览按视口加载。
- 新版没有整站强行换框架：应用继续使用 Flask、Jinja 和现有 JS；传输统计已经采用 Vue 3 + TypeScript + Vite，在本地打包。`/ui/classic` 可以在同一个实例恢复旧页面，`/ui/atelier` 切回来。
- `Preview-Atelier.cmd` 运行隔离的合成书库；搜索和在线阅读模式在这个入口使用合成目录与页图，正式下载在预览中禁用。请用普通实例进行真实上游试用。

## 实际执行的验证

| 层级 | 结果与边界 |
| --- | --- |
| 离线全量 | 构建环境 `python -m pytest -q -rs`：**2,393 通过、6 跳过、0 失败**。其中 4 条需要 Playwright，使用本机 Chromium 另跑 **4/4 通过**；另 2 条需要本机 Windows 文件符号链接权限。Python 编译、36 个 JS 文件分别做语法检查、`pip check`、TypeScript 检查与 Vite 构建均通过。 |
| 真实队列 + 本机 HTTP | `tests/check_download_queue.py` 的 `--two-stalls`、`--pause`、`--quick-resume` 三种场景通过。两张卡页、零重试时坏任务失败，后续任务完成，4 张测试图各请求一次、执行器归零；这些请求没有发送到真实上游。 |
| 解压后的 EXE（普通模式） | `tests/check_windows_release.py --check-launcher --check-restore --check-vnext` 通过：ZIP 和 Unicode 路径、去掉系统 Python、自检与重启、新旧实例识别、遇忙暂缓、旧库完整恢复；A 的未发布页隔离，B 的阅读、任务和整册 ZIP/PDF 正确。 |
| 解压后的 EXE（样书模式） | `tests/check_atelier_release.py` 通过。11 个页面入口、Vue 产物和许可证均可访问；第 2 话导出的 12 张图与已发布章节按页校验相同，章节标记属于 B，PDF 为 12 页。CBZ 单独可读，经典版可切回；退出并重开 EXE 后，玄墨、零重试、49 秒超时均保留。 |
| Chromium 实际 EXE 页面 | 下载页显示真实任务统计，同册 A 保留 8/20 页、B 已发布 12/12 页；390px 检查取消筛选及任务行，无水平溢出。阅读器从本地 CBZ 请求并解码真实页面。 |

## 430 页专项（源码，非 EXE 性能基准）

Chromium 148，1280×800，真实回环 Waitress 和阅读路由；书页和网络上游为夹具。三种模式均到达第 430 页，末页图像按固有尺寸核对。返回搜索第 3 页第 47 项，三种模式的滚动位置差均为 0，没有重发搜索。耗时只代表这台机器本次样例。

| 模式 | 初始第 16 页：图片请求 / 峰值图片节点 | 远跳第 429 页：新增请求 / 峰值图片节点 | 返回后首个本地 API 请求耗时 |
| --- | --- | --- | --- |
| 本地连续，CBZ | 6 / 6 | 4 / 6 | 21.4 ms |
| 本地单页，CBZ | 10 / 10 | 10 / 10 | 8.4 ms |
| 在线连续，合成上游 | 6 / 6 | 4 / 6 | 10.7 ms |

复跑：安装了 Playwright/Chromium 的测试环境运行 `python -m pytest -qs tests/check_atelier_gallery_430.py`。本次不包含真实上游可用性、公网部署、Docker 或用户个人库验收；旧便携包和原有实例均保留。
