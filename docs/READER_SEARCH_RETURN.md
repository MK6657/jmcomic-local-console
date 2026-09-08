# Continuous local reading and search restoration

## Cause of the back-navigation bug / 返回丢失原因

Search results were only present in dynamically generated DOM, while the URL remained `/search`.
On non-bfcache back navigation, form values could be restored after the startup script had run;
the script therefore saw an empty keyword and never rebuilt results. The old bfcache handler
also cleared an otherwise valid result list to issue another search.

旧搜索结果只在页面 DOM 中，地址一直是 `/search`。非 bfcache 返回时，表单值可能在脚本运行后
才恢复，导致看得到关键词却没有结果；旧 bfcache 回调还会主动清空已有结果并重新请求。

## Behavior / 行为

- URL records query/sort/page/page-size. A session/history snapshot stores result data and scroll offset.
  Returning or reloading reuses a matching snapshot for at most 30 minutes; unavailable storage falls back to URL-based search.
- Search cards use separate semantic links/buttons, avoiding nested interactive elements, and add `Read` → `/read/<id>`.
- The new local-only reader reuses `/api/preview/<id>`. It provides vertical scrolling, batches of 20 images,
  native image lazy loading, explicit retries, page jumps, on-demand controls and session page memory.
- The title scrolls away instead of sticking over images. Return/page/jump controls start hidden;
  tap the picture or margin, press M, or use the inline Reading tools button to reveal them.
  Escape, Close, and jumping hide them again. No permanent floating button is added.
- Only the first image retains the page/chapter caption. Hidden reading tools include accessible
  top/bottom icons: top returns to the document start, bottom renders remaining batches and aligns
  with the last image's lower edge. Position is maintained while lazy images finish loading.
- No automatic download or remote image streaming. Missing/deleted local images produce a clear explanation.
- The paged preview now has a Continuous mode link; the continuous reader offers Paged mode in its hidden tools.
  Both share the current page through URL/session state and replace the mode's history entry, preserving Back navigation.
- Paged thumbnails constrain image dimensions with `object-fit: contain`, show page numbers and have horizontal arrows.
  The outer rail no longer clips vertically. Later-page jumps load the required thumbnail batch and resize keeps it visible.
- Colors, typography, spacing and radii follow the locked eye-care tokens; the paged canvas now uses the warm background too.

搜索地址、结果快照和滚动位置一起保存；阅读按钮打开独立的连续阅读页，复用本地预览接口。
每批 20 张图片并按需加载，支持跳页、重试与阅读位置记忆。保留原翻页预览，不自动下载或在线播放。
标题不吸顶，返回和跳页默认隐藏；轻触画面/空白处、按 M 或点击页首阅读工具可唤出，Esc/收起/跳页后隐藏。
只有第一页显示页码与章节说明；工具内新增一键到顶、到底小图标，到底会补齐剩余批次并定位末页图片底部。

## Validation / 验证

- Backend regression: 164 passed, 1 skipped for Windows file-symlink permission.
- Real-browser checks use only `tests/ui_reader_fixture.py` synthetic pages and temporary databases.
- Checked: search → detail → back, query/page/scroll restoration without an upstream re-search,
  reload restoration, reader → back, lazy batches, image-error retry, missing-download state,
  desktop 1329×912 and mobile 390×844 without horizontal overflow.
- Browser script: `tests/reader_browser_checks.js`, executed via Playwright CLI `run-code --filename`.
- Mode/thumbnail script: `tests/reading_modes_browser_checks.js`; synthetic 430-page check covers containment,
  page 25 round trips, last/previous page, refresh, mobile resizing, and Back returning to the original search results.
- Screenshots are local-only under ignored `output/playwright/`; no user comic images are used in tests.
- No target site is cloned: the existing project components and DESIGN.md define all new UI styling.

真实浏览器回归使用中性占位图片；不读取个人漫画内容。非克隆项目，复用现有组件并遵守 DESIGN.md。
