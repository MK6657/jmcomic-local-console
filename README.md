# JMComic Local Console

[English](README.md) | [简体中文](README.zh-CN.md)

A Windows-first local browser interface for jmcomic: search, download queues,
live progress, bookmarks, a downloaded library, image previews, and archive/PDF export.
The application interface is in Chinese; documentation is bilingual.

This is a **local Python application**, not a browser extension or a hosted service.
Only use it for content you are authorized to access and download. This repository
does not include downloaded content, credentials, or the upstream reference project.

## Requirements

- Windows 10/11 and Python 3.10–3.12; this maintenance pass was tested with Python 3.11.
- Internet access for dependency installation; search/download also depend on upstream availability.
- A separate project virtual environment, not a shared Hermes/system environment.

## Quick start

Clone or extract the repository, then double-click **start.bat**. It changes to
the project directory, creates `.venv` if missing, installs/checks all declared
runtime dependencies, starts the server, and opens your browser.
The first run needs Python available as `python` on PATH.

Keep the launcher window open. Press **Enter** or **Ctrl+C** there to stop the
server it started. If an existing matching server is found, it simply opens it
without taking ownership or stopping it.

Alternatively, run in PowerShell from the repository directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe launcher.py --wait
```

For foreground operation without automatically opening a browser:

```powershell
.\.venv\Scripts\python.exe app.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000). If occupied, the server tries
5001–5003; use the URL printed in the terminal. Other programs are never killed
to free a port. The launcher verifies application identity and PID before opening
an existing service. Running start.bat again reuses a running server, unless `app.py`, `core/`, `routes/` or
`templates/` differ from the files it started with (compared by content, so an update copied over the old
folder counts too): then it stops only that verified server and starts the updated one
(interrupted downloads are re-queued at startup). Static CSS/JS changes need no restart.

After installing dependencies, `launcher.pyw` offers a windowless launch.
`launcher.py` without `--wait` also leaves the server in the background; to stop it,
verify and terminate only the app's PID recorded in `runtime/data/flask.json`.

## Features and settings

- Search cards now have a **Read** button opening a continuous scrolling local reader.
  It loads images in batches, supports page jumps/retry and remembers the page within the browser session.
  It never starts a download; for an album that isn't downloaded, **Read** opens online reading instead (see below).
  The title scrolls away; return/jump controls stay hidden until you tap the picture/margin or press M.
  Press Escape to hide them again. A Reading tools button at the page top provides keyboard access.
  Page/chapter captions appear only on the first image; the hidden tools also include one-click top/bottom icons.
  Switch between paged and continuous reading while keeping the current page. Paged thumbnails show
  the full image with page numbers, horizontal browse arrows and automatic loading of later-page thumbnails.
  In paged mode the whole page and its controls fit one desktop screen (thumbnails follow below); page turns keep
  the current image until the next one is ready, and the following page is preloaded. Arrow keys with Alt/Ctrl
  are left to the browser, so Alt+← still goes back.
- The **搜索 / 收藏 / 资源库** links in the top bar return to where you left them in this tab (for 12 hours): the same
  search results and page (shown from memory, without searching the site again), the same filters, sort and page, and
  the same place in the list — even if you scrolled back up to reach the menu. Typing `/search` directly still opens a
  fresh search page.
- Comics you can read offline are marked "✓ 已下载内容 · 可离线阅读" everywhere: on search result covers, the detail
  page, library cards, favourites and 下载管理 (which also offers 预览 for them). "Readable" means the
  latest download's folder still exists and contains page images (or a readable CBZ/ZIP); it does not mean the whole
  comic is downloaded. When the local chapter folders and a freshly fetched chapter list prove that only some chapters
  are downloaded, the detail page adds "部分章节已下载 · M/N 话". Deleted or emptied folders are shown as 文件已删除.
- **New chapter checks** (Settings → 检查新章节, on by default): comics with downloaded content ("已下载内容 · 可离线阅读",
  including partial and CBZ/ZIP-only downloads) are checked slowly in the background — one comic at a time, each about
  once a day, at least 2 minutes apart, waiting while a download runs. A check fetches the chapter list once and
  **never downloads anything**. "New" means chapters published upstream after your download; chapters you did not
  select are never new, and comics downloaded before this feature only get their current list recorded on the first
  check. Confirmed new chapters show as "有新章节 · N 话" in the library, favourites and 下载管理; the detail page shows
  the status, marks the new chapters, has **立即检查** (works even with the setting off) and **选中这些章节**, which only
  ticks those chapters — downloading still needs **下载选中章节**.
- **Batch downloads**, always user-started and confirmed first: **下载新章节** (library) queues only the confirmed new
  chapters of comics that already have downloaded content, favourites or not — never the whole comic; **下载未下载的收藏**
  (favourites) queues whole comics for favourites never downloaded or whose download was cancelled. Failed downloads
  stay in 下载管理 → 失败 for retry, and comics with downloaded content (including partial ones) are never included.
  Both open a list of the exact comics, chapters and scope, with the reason for every comic left out; nothing is queued
  until you confirm. The server re-checks the list when you confirm, never adds a second job for a comic that is
  already queued, and, when 定时下载 is on, queued jobs still start only inside its window. The favourites
  selection bar's **下载选中的收藏** applies the same rule to the selected rows. On the detail page, when **立即检查**
  confirms new chapters that the chapter table does not show yet, the table is refreshed in place, keeping ticks and
  the scroll position.
- Every comic in search results, the library, favourites and 下载管理 (completed and failed tasks) has a **阅读**
  button: a downloaded comic opens its local files (filled button, book icon), anything else opens online reading
  (outlined button, globe icon). The choice is made when you click, so it is right even if the files changed after
  the page was opened. The detail page keeps its **在线观看** button and adds **阅读** once the comic is downloaded.
- Favourites can be filtered by download status and sorted by newest/oldest, title, author or status; the library
  has matching status groups, sorting by author and a click-an-author filter. Choices are kept in the address bar.
- The album detail page has a **Read online** button (and a per-chapter link) that opens the same continuous reader
  without downloading: this app fetches and unscrambles each page on demand through its own connection (same proxy
  and domains as downloads) and keeps recent pages in `runtime/cache/online/` (512 MB, least recently read pages
  evicted first; cleared by `POST /api/system/clear-cache`). Nothing is added to the library. A page that cannot
  be fetched gives up after the Settings timeout (at least 15 s), trying the other image hosts within that time; a
  slow page that is still arriving is not cut off early. The local reader stays local-only. On the detail page the cover is vertically centred against the information card; covers keep
  their proportions within the column width (max 300 px) and 400 px height, and small covers are not enlarged.
- Returning from details or the reader restores the search query, sort, result page, results and scroll position.
  Results are cached only in browser-session/history state: reused for up to 12 hours when you come back through the
  menu or Back, and for up to 30 minutes on a reload (F5), after which the page searches again.

- Search and detail pages; queued downloads, retry/cancel and SSE progress.
- Animated GIF pages are downloaded without unscrambling and saved as animated WebP (still `NNNNN.webp`), so the
  local reader keeps the animation; PDF export uses the first frame. Chapters downloaded by earlier versions (whose
  GIF pages may be sliced or reduced to one frame) get only their GIF pages fetched again the next time the album
  is downloaded, even with "skip existing" on.
- Organizing downloads never copies then deletes: 按作者 moves the whole comic folder in one rename, retried for a
  few seconds while another program (antivirus, Explorer preview, an image viewer) holds a file in it; if it still
  fails, the complete folder stays where it is and is organized after the comic's next download.
  扁平化 names every page `<chapter folder>_p<page number>` (e.g. `第1话__71_p00001.webp`), a name earlier versions
  never used, so a re-downloaded page replaces its own flattened copy and never another page; "skip existing"
  recognises these pages, loose or inside the comic's archive, whichever organize mode is selected now (a loose copy
  always stands in for the archived one, so a damaged loose copy does not count: the page is fetched again and still
  has a good copy after automatic packing). Chapters flattened by earlier versions (counter names) are recognised
  only as whole, complete chapters. Otherwise they are downloaded again: a downloaded page that is byte for byte one
  of the chapter's old pages is dropped (each old page stands in for one downloaded page only; the old file stays;
  the page may be fetched again on later downloads, not shown twice while the old copy can be read; a page whose own
  new-named copy is damaged replaces that copy instead); if the old page cannot be read while organizing (the
  archive or file is held by another program, or damaged), the downloaded page is kept under its new name instead,
  so that page shows twice (after automatic packing, for good). Pages that differ (such as old sliced GIF pages) are
  kept under the new names next to the old files, so those show twice. Organizing never renames, overwrites or
  deletes those old files or the archive. A page that stays locked is left in its chapter folder until the comic's
  next download: meanwhile it is listed before its chapter's flattened pages (and shows twice while the comic folder
  already has a copy of it: the flattened copy it could not replace, or the old page it duplicates), and automatic
  packing waits until it is flattened. A damaged page (`NNNNN.webp`) in a chapter folder is fetched again whenever
  its chapter is downloaded. Known narrow limits: old duplicate counter names that happen to add up to a chapter's
  current page count make it count as complete; archived pages count as present from the archive's index without
  re-reading each one; if another program holds the archive while the folder is organized (and, for complete old
  chapters with "skip existing" on, also when the chapters are checked), old pages are downloaded again and packed
  next to their old copies; in a chapter that still has old counter-named pages, those are listed before its
  new-named pages, so a page kept only under its old name can come before an earlier page that got a new name (an
  old page missing, a page inserted upstream, or a re-fetched GIF page that is not the chapter's last).
- Bookmarks, a downloaded library and local image reader.
- ZIP/PDF export and optional automatic CBZ/ZIP packaging.
- Scheduling, concurrency, timeout, retries and proxy configuration.
- Settings import/export. **Proxy configuration is omitted from exports**, so
  importing an exported file preserves the destination's proxy. A manually prepared
  import can still contain a proxy; treat such files as private.

Configure a proxy on the Settings page if needed. Its credentials remain in the
local SQLite database; never share that database. Download output is fixed to
`downloads/`. Optional "delete originals after packing" removes source images;
keep it disabled unless intended.

## Local data and privacy

| Path | Purpose | Committed? |
| --- | --- | --- |
| `runtime/data/app.db` | Settings, jobs and history | No |
| `runtime/logs/` | App/error logs and launcher output | No |
| `downloads/` | Downloaded files and exports | No |
| `.venv/`, `.worktrees/` | Local environment/worktrees | No |
| `参考项目/`, `docs/reports/` | Reference checkout/historical local audit notes | No |

Keep the service on `127.0.0.1`. There is no user authentication: do not expose it
through port forwarding, a public proxy, or a shared server. Logs may contain
searches, titles and local paths even when authentication details are masked.
`runtime/logs/launcher.log` captures startup/output errors. The launcher archives it before each start,
and like the app logs it is kept for 7 days.

### Logs and troubleshooting

- **Settings → 日志与诊断** (`/settings#logs`) shows recent problems (warnings and errors) or all entries,
  with search, a per-request filter (request id) and a button that opens the log folder.
  A page that fails in the online reader links straight there.
- Files in `runtime/logs/`: `app.log` (everything from INFO), `error.log` (warnings and errors),
  `launcher.log` (startup output). `app.log` and `error.log` roll over daily or at 20 MB into
  `<name>.YYYY-MM-DD[.N]` (a higher N is newer). `launcher.log` is archived by the launcher before each start
  when it is over 5 MB or was last written on an earlier day, so a server left running keeps writing to it.
- Repeated messages are merged: the first one is written in full, repeats of the same kind (messages that differ
  only in numbers such as page numbers or durations) are counted and written as one line
  `↑ 同类消息 N 次已合并（…）`. Ids written as `job_id=` / `album_id=` / `photo_id=` (any `…_id=`) are kept apart;
  other numbers, including ids inside request paths such as `/api/online-img/<photo_id>/<n>`, are merged like page
  numbers. A merged line that covers several requests lists their request ids, so the per-request filter finds it.
- Log files older than 7 days are deleted automatically at startup and daily, with a 100 MB total cap.
  Routine static-file and health-check requests are not logged.

## Development and checks

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe tests/check_startup.py
.\.venv\Scripts\python.exe -m compileall -q app.py launcher.py launcher.pyw core routes tests
```

Default regressions use temporary runtime directories and databases, without live
searches/downloads. Legacy scripts listed in `tests/conftest.py` are excluded from
collection because they perform network requests or mutate application data at
import time. Do not run them against your personal instance; use a disposable copy.

`check_startup.py` is a separate local HTTP check: it starts/stops disposable
server instances and tests port fallback without contacting upstream services.

There is no `package.json`, so npm lint/typecheck/build scripts are not provided.
`build.spec` is a historical PyInstaller recipe, not a verified release binary.

## Troubleshooting and limitations

- Startup failure: inspect `runtime/logs/launcher.log`, or run `app.py` directly.
- Missing dependency: install the complete requirements with the same interpreter.
- Failed search: check connectivity, proxy settings and app logs. Upstream domains
  in `core/settings.py` may change over time.
- Offline tests do not prove upstream availability, real downloads or EXE packaging.
- Windows-specific folder opening and the single-instance mutex are not portable.

## Maintenance changes

### Chapter, cleanup and PDF correctness

- Chapter folders include the chapter ID and an ownership marker, preventing identical,
  sanitized or case-insensitive titles from sharing images. Retries reuse marked folders.
  Legacy folders remain untouched and are not automatically migrated; re-downloading can
  leave both legacy and new folders, which should be reviewed before manual cleanup.
- Clearing or deleting job records recomputes bookmark status from remaining jobs in the
  same transaction. Existing downloads/queued tasks are not reset to "not downloaded".
- PDF export is all-or-nothing: corrupt/unconvertible images produce an explicit error
  and no partial PDF. Original images remain untouched and temporary output is removed.

Validation: 155 tests passed, one Windows file-symlink permission case skipped.

### Cache and query optimization

- Concurrent requests for the same detail share one fetch; cache TTL is honored,
  returned objects cannot mutate cached data, and corrupt entries are refetched.
- Clear-cache removes both detail-cache tiers without erasing personal records;
  an older in-flight fetch cannot repopulate the cleared cache.
- A 25-item downloaded-library page is regression-tested at one DB connection and
  four SQL statements. File availability updates immediately rather than staying stale for five minutes.
- Queued filtering, duplicate tag filters, rare-tag autocomplete and stable pagination are fixed.
- Busy-album jobs no longer block unrelated queued jobs. Duplicate bulk tag-sync requests
  return a busy response; archive-cache eviction is safe under concurrent requests.

Validation: 137 tests passed, one Windows file-symlink permission case skipped.

### Second audit

- Malformed API JSON and invalid settings return explicit errors; settings save atomically.
- Browser-origin/Host checks protect the loopback service from cross-site requests.
- ZIP/PDF/CBZ file discovery skips symbolic links and Windows junctions; previews
  include nested chapters, natural page order and escaped filenames.
- Incomplete/canceled downloads do not trigger automatic packaging or original-image
  deletion. Failed image replacement preserves the previous valid image.
- Automatic ZIP/CBZ archives stay in the recorded output directory. Original-image
  cleanup preserves archives, notes and other files; readers need original images.
- Same-album workers cannot write concurrently; failed thread startup releases capacity.
- Shared client retirement waits for in-flight and timed-out background calls to finish.
- Windows mutex protection applies even before startup binds a port. Missing Waitress
  now fails explicitly rather than starting an untracked development-server fallback.

### Initial maintenance

- Fixed proxy placement in jmcomic HTTP client metadata.
- Removed proxy/private fields from settings exports.
- Replaced forced port cleanup with verified service discovery and safe fallback.
- Shared launcher logic, file-based child output and failed-startup cleanup.
- Added isolated offline regressions and local/private-file exclusions.
- Preserved the locked `eye-care` theme; no UI styles changed.

## Third-party components

Runtime dependencies are listed in `requirements.txt`. Vendored frontend assets
retain their notices; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
No license has been selected for the project's original code yet. This is not an
open-source license grant; select a license before distributing it as open source.
