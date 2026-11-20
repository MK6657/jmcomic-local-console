# Windows 便携版：长路径合成下载验收

在 `atelier-edition` 目录运行。驱动需要 Windows、Python 3.11+、Pillow 和 PATH 上的 `openssl`（Git for Windows 自带）。以下使用项目的构建环境；源码对照会核实子进程的 `jmcomic`、`curl-cffi` 和 Pillow 版本与发行清单一致。普通模式需要发行目录的 `JMComic-windows-x64.zip`、`SHA256SUMS.txt` 和 `release.json`。

```powershell
$python = '..\.venv-build\Scripts\python.exe'
$check = 'tests/check_atelier_download_paths.py'
$old = 'releases/windows-x64-a764df49df7b'
$candidate = 'releases/windows-x64-a786056510fb'
& $python tests/check_isolated_atelier_download.py "$candidate/JMComic/JMComic.exe"  # 日常实例运行时可用
& $python $check $candidate                          # 普通服务互斥锁空闲时可用
& $python $check $old --source-control "$old/JMComic-Atelier-source-50d2d38.zip"
& $python $check $candidate --source-control         # 当前源码，也可指定源码目录或发行的源码 ZIP
```

普通模式先核对发行 ZIP 的 SHA-256，再解压到新的临时 Unicode 目录。`JMComic.exe serve` 使用 EXE 同级默认下载目录，其绝对路径固定为 **162 个 UTF-16 单位**。驱动在向服务写入设置和创建任务前核实本次启动的 PID、随机令牌、实例标识和代码指纹。普通互斥锁被占用时直接退出，不连接或停止已有实例。

HTML SDK 的代理指向 `127.0.0.1` 上的固定应答夹具。夹具只在本机终止 TLS，不转发请求；临时证书只交给测试子进程，不加入系统信任库。专辑和章节均使用长中、日、英混排标题和非 BMP 字符。两章各两页的 PNG 固有尺寸均不相同。脚本通过 `/api/jobs` 实际入队；核验四张图各请求一次、WebP 转换、发布快照与公开目录字节一致、本地阅读页的章节和顺序，以及任务/整册 ZIP 的 CRC、章节标记和逐页 SHA-256。阅读验证走 HTTP，不包含浏览器交互。

`--source-control` 对源码使用独立互斥锁，沿用同一 SDK、HTTP 夹具和断言。JSON 中 `tested_source_sha256` 标识实际执行的源码，`source_matches_bundle` 标明它与给定发行包是否一致；此模式不算 EXE 通过。退出码 `0` 为所选模式通过，`1` 为执行或断言失败，`2` 为普通服务互斥锁占用。比较旧版时应核对失败阶段、原始错误和 `image_gets`，不能把任意非零退出都视为成功复现。

**与日常实例并存时验证最终 EXE。** `check_isolated_atelier_download.py` 接受解压后的 `JMComic.exe`；必须传这个参数，不传会运行源码。脚本启动发行 EXE 内置的 `_lifecycle-worker`，在带有随机令牌的临时实例和专用互斥锁下运行。它只为该进程把 HTML 和图片域名指向本地 HTTPS 夹具，禁止外部连接、代理和重定向；仍由发行包内的 SDK、真实队列和发布代码处理 `/api/jobs`。驱动核实进程身份与 EXE 指纹，并复用 `verify_pages` 检查阅读和导出。此项覆盖临时下载根的深度；不覆盖普通 `serve` 的启动/互斥锁、解压到深目录后默认同级存储的行为、真实上游或浏览器交互。

2026-11-18 本机实测（传统 Windows 路径限制生效）：

| 入口 | 下载根（UTF-16 单位） | 结果 |
| --- | ---: | --- |
| 旧包同指纹源码 `a764df49df7b` | 162 | 专辑和两章元数据各取一次，首张图片前报“下载路径过长”；图片 GET **0**。 |
| 最终包同指纹源码 `a786056510fb` | 162 | 图片 GET **4**；发布、阅读、任务 ZIP、整册 ZIP 均 **4/4**。 |
| 最终发行 EXE `a786056510fb`，隔离 worker | 164 | SDK 请求 **7** 次（1 个专辑、2 章、4 张图，各一次）；发布、阅读、两种 ZIP 均 **4/4**，最长公开页路径 205 单位。EXE SHA-256：`61b7d651105057b87290551909e55899e0e19a1a941ba3e8548022ea448ffe43`。 |
| 普通 `serve`：旧包 `a764df49df7b`、中间包 `57ca7125276c`、最终包 `a786056510fb` | 162 | 日常服务持有互斥锁，均 `blocked`（退出 `2`），未进入下载。 |
