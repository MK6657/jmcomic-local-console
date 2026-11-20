# Chapter, bookmark and PDF fixes / 三项边界修复

1. Chapter directories now use a sanitized title plus numeric chapter ID and `.jm-chapter.json`.
   Matching owned directories are reused on retry. Existing unmarked or mismatched directories are
   preserved, using another destination instead. Duplicate upstream chapter IDs are filtered out.
   同名、清洗重名、大小写重名和截断重名章节不再共享图片；旧目录不覆盖、不迁移，重试复用新目录。
   Legacy and new directories may coexist after a re-download; inspect before manually removing either.
   重新下载后新旧目录可能同时存在，手动删除前应核对内容。

2. Single/bulk job deletion and bookmark status recalculation share a SQLite write transaction.
   Remaining completed jobs take precedence, followed by running/paused, queued, and the latest terminal
   job. With no remaining jobs the status becomes `none`. Errors roll back both deletion and status updates.
   清理后的状态优先级为：已完成、运行/暂停、排队、最近的其他终态；没有剩余任务时为未下载。
   更新失败会回滚任务删除及状态修改；不会删除下载文件。

3. PDF conversion validates pixel decoding. Any corrupt or unconvertible input aborts the export with
   HTTP 422 and an explicit error, including failures in the JPEG fallback. Temporary output is cleaned up.
   The existing download-page error handler displays the returned message; no UI styling changes are needed.
   PDF 遇到坏图明确失败，不返回缺页文件；现有下载页会显示错误消息，无需改动 UI 样式。

## Verification / 验证

- `python -m pytest -q`: 155 passed, 1 skipped (Windows file-symlink permission).
- Added tests cover concurrent chapter collisions, legacy preservation/retry reuse, mixed remaining job
  states, rollback, PDF corrupt input/fallback rejection, and valid multi-page PDF including alpha images.
- Dependency and Python syntax checks pass. npm lint/typecheck/build are unavailable without `package.json`.
- Personal data was not used; tests ran in temporary databases/directories. Live upstream downloads and
  EXE packaging remain outside this verification. The locked eye-care theme is unchanged.

使用临时数据库和目录验证，没有使用个人下载数据；外网上游下载及 EXE 打包仍未验收，护眼主题未变。
