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
  Undownloaded albums show an explanation instead of starting a download or streaming upstream content.
  The title scrolls away; return/jump controls stay hidden until you tap the picture/margin or press M.
  Press Escape to hide them again. A Reading tools button at the page top provides keyboard access.
  Page/chapter captions appear only on the first image; the hidden tools also include one-click top/bottom icons.
  Switch between paged and continuous reading while keeping the current page. Paged thumbnails show
  the full image with page numbers, horizontal browse arrows and automatic loading of later-page thumbnails.
  In paged mode the whole page and its controls fit one desktop screen (thumbnails follow below); page turns keep
  the current image until the next one is ready, and the following page is preloaded. Arrow keys with Alt/Ctrl
  are left to the browser, so Alt+← still goes back.
- The album detail page has a **Read online** button (and a per-chapter link) that opens the same continuous reader
  without downloading: this app fetches and unscrambles each page on demand through its own connection (same proxy
  and domains as downloads) and keeps recent pages in `runtime/cache/online/` (512 MB, least recently read pages
  evicted first; cleared by `POST /api/system/clear-cache`). Nothing is added to the library. A page that cannot
  be fetched gives up after the Settings timeout (at least 15 s), trying the other image hosts within that time; a
  slow page that is still arriving is not cut off early. The local reader stays local-only. On the detail page the cover is vertically centred against the information card; covers keep
  their proportions within the column width (max 300 px) and 400 px height, and small covers are not enlarged.
- Returning from details or the reader restores the search query, sort, result page, results and scroll position.
  Results are cached only in browser-session/history state and reused for up to 30 minutes.

- Search and detail pages; queued downloads, retry/cancel and SSE progress.
- Animated GIF pages are downloaded without unscrambling and saved as animated WebP (still `NNNNN.webp`), so the
  local reader keeps the animation; PDF export uses the first frame. Chapters downloaded by earlier versions (whose
  GIF pages may be sliced or reduced to one frame) get only their GIF pages fetched again the next time the album
  is downloaded, even with "skip existing" on.
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
