# Cache and query optimization / 缓存与查询优化

## Measured checks / 可重复验证

- Eight simultaneous detail lookups for the same album result in one mocked upstream call.
  同一专辑 8 个并发详情请求只触发 1 次模拟上游调用。
- A cold 25-item downloaded-library page opens one database connection and runs four SQL statements.
  Before this change, file checks opened an additional connection/query for each uncached completed item.
  25 条下载记录的列表使用 1 个连接、4 条 SQL；此前冷缓存会对每条已完成记录额外查库。
- File-presence changes are visible on the next query, not after a five-minute negative cache expires.
  文件存在状态变化在下次查询生效，不再等待五分钟缓存过期。

These are request/query-count assertions, not claimed wall-clock speedups against the live upstream service.
以上是测试断言的请求数和查询数，并非真实上游服务的耗时提速承诺。

## Additional fixes / 其他修复

- TTL for both detail-cache tiers, defensive copies, corrupted-cache recovery, bounded fetch locks and LRU.
- Clear-cache invalidates memory/SQLite and prevents old in-flight results from repopulating them.
- Queued library filtering, deduplicated AND-tags, full-library tag autocomplete, deterministic pagination.
- The queue skips busy album IDs when claiming work instead of blocking unrelated albums behind them.
- Duplicate bulk tag-sync rejection, worker cleanup after startup errors, thread-safe archive-cache eviction.

详情缓存有效期、拷贝隔离、损坏恢复、有界锁及 LRU；清理期间旧请求不回填；排队及重复标签筛选；
全库标签补全与稳定分页；队列跳过被占用专辑；重复批量同步保护和后台资源清理。

## Validation / 验证

- `python -m pytest -q`: 137 passed, 1 skipped (Windows file-symlink permission).
- `python -m pip check` and Python syntax compilation passed.
- No UI styles, theme tokens, personal settings or download files changed.
- No live upstream downloads or EXE build were performed. The existing local-only usage limitation remains.
- npm lint/typecheck/build scripts are absent because this Python project has no `package.json`.

没有改动 UI 样式、主题、个人设置或下载文件；未执行真实上游下载和 EXE 打包。
