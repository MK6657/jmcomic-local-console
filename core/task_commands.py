"""Atomic download commands; this module never starts workers or contacts the upstream.

Matching active requests share one job. A chapter selection is a set for this purpose;
the empty set still means an entire album, never an arbitrary partial selection.
"""
from dataclasses import dataclass
from datetime import datetime
import json
import os

from . import database as db
from .validation import validate_numeric


RETRYABLE_STATUSES = ("failed", "canceled", "completed")


@dataclass(frozen=True)
class EnqueueResult:
    job_id: str
    created: bool


class TaskCommandError(ValueError):
    def __init__(self, reason: str, message: str, http_status: int):
        super().__init__(message)
        self.reason = reason
        self.http_status = http_status


def _selection(photo_ids) -> list[str]:
    if not isinstance(photo_ids, list) or len(photo_ids) > 1000:
        raise ValueError("photo_ids 必须是最多 1000 个数字章节 ID")
    normalized = [str(photo_id) for photo_id in photo_ids]
    if any(not validate_numeric(photo_id) for photo_id in normalized):
        raise ValueError("photo_ids 必须是最多 1000 个数字章节 ID")
    return list(dict.fromkeys(normalized))


def _matching_active(conn, album_id: str, photo_ids: list[str]):
    selected = set(photo_ids)
    rows = conn.execute(
        "SELECT job_id, selected_photo_ids FROM jobs WHERE album_id=? "
        "AND status IN ('queued', 'running', 'paused') ORDER BY created_at, id",
        (album_id,),
    )
    for row in rows:
        try:
            existing = _selection(json.loads(row["selected_photo_ids"] or "[]"))
        except (TypeError, ValueError):
            # A damaged legacy row is left for the scheduler to reject. It cannot
            # establish that this valid request is already in the queue.
            continue
        if set(existing) == selected:
            return row["job_id"]
    return None


def _sync_wishlist(conn, album_id: str) -> None:
    # A new chapter can queue behind a running chapter of the same album. Do not
    # turn the running album back into "queued", including on a duplicate click.
    downloading = conn.execute(
        "SELECT 1 FROM jobs WHERE album_id=? AND status IN ('running', 'paused') LIMIT 1",
        (album_id,),
    ).fetchone()
    db.update_wishlist_download_status(album_id, "downloading" if downloading else "queued", conn=conn)


def _enqueue(conn, album_id: str, title: str, photo_ids: list[str], output_path=None) -> EnqueueResult:
    existing = _matching_active(conn, album_id, photo_ids)
    if existing is not None:
        _sync_wishlist(conn, album_id)
        # Never modify a reused job's directory: it can already be writing pages.
        return EnqueueResult(existing, False)
    job_id = db.new_job_id()
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO jobs (job_id, album_id, title, selected_photo_ids, status, "
        "output_path, created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)",
        (job_id, album_id, title, json.dumps(photo_ids, ensure_ascii=False), output_path, now, now),
    )
    _sync_wishlist(conn, album_id)
    return EnqueueResult(job_id, True)


def enqueue_download(album_id: str, title: str, photo_ids: list[str]) -> EnqueueResult:
    """Create or reuse an equivalent active job in one BEGIN IMMEDIATE transaction."""
    album_id = str(album_id)
    if not validate_numeric(album_id):
        raise TaskCommandError("invalid_album", "album_id 必须是纯数字", 400)
    try:
        selected = _selection(photo_ids)
    except ValueError as error:
        raise TaskCommandError("invalid_selection", str(error), 400) from error
    with db.transaction() as conn:
        return _enqueue(conn, album_id, str(title or album_id)[:500], selected)


def retry_download(job_id: str) -> EnqueueResult:
    """Validate the source, enqueue, reuse its path, and update the bookmark atomically."""
    with db.transaction() as conn:
        source = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if source is None:
            raise TaskCommandError("not_found", "原任务不存在", 404)
        if source["status"] not in RETRYABLE_STATUSES:
            raise TaskCommandError("status", "该任务状态不能重试", 400)
        try:
            album_id = source["album_id"]
            if not validate_numeric(album_id):
                raise ValueError("invalid album")
            selected = _selection(json.loads(source["selected_photo_ids"] or "[]"))
        except (TypeError, ValueError) as error:
            raise TaskCommandError("invalid_source", "原任务的章节数据无效，无法重新下载", 409) from error
        output_path = source["output_path"]
        # Preserve the existing completed-job rule: a removed folder must not be
        # recreated merely by opening the new job's folder while it is queued.
        if output_path and source["status"] == "completed" and not os.path.isdir(output_path):
            output_path = None
        return _enqueue(conn, album_id, source["title"] or "", selected, output_path)


def recover_interrupted(excluded_job_ids=()) -> int:
    """Requeue interrupted executors atomically; a user's paused jobs stay paused.

    The single-process owner excludes jobs whose executors it still owns. Running
    recovery is never inferred from a timestamp or performed by a web request.
    """
    excluded = tuple(str(job_id) for job_id in excluded_job_ids)
    where = " AND job_id NOT IN (SELECT value FROM json_each(?))" if excluded else ""
    params = (json.dumps(excluded),) if excluded else ()
    with db.transaction() as conn:
        rows = conn.execute("SELECT job_id, album_id FROM jobs WHERE status='running'" + where, params).fetchall()
        if not rows:
            return 0
        now = datetime.now().isoformat()
        conn.executemany("UPDATE jobs SET status='queued', updated_at=? WHERE job_id=? AND status='running'",
                         [(now, row["job_id"]) for row in rows])
        for album_id in {row["album_id"] for row in rows}:
            _sync_wishlist(conn, album_id)
        return len(rows)


def requeue_interrupted(job_id: str) -> bool:
    """An exiting executor releases only its still-running task, never pause/cancel."""
    with db.transaction() as conn:
        row = conn.execute("SELECT album_id FROM jobs WHERE job_id=? AND status='running'", (job_id,)).fetchone()
        if row is None:
            return False
        changed = conn.execute("UPDATE jobs SET status='queued', updated_at=? WHERE job_id=? AND status='running'",
                               (datetime.now().isoformat(), job_id)).rowcount
        if changed:
            _sync_wishlist(conn, row["album_id"])
        return bool(changed)


def resume_download(job_id: str, *, has_executor: bool) -> tuple[bool, str]:
    """Resume an owned executor, or queue an interrupted paused job for scheduling."""
    with db.transaction() as conn:
        row = conn.execute("SELECT album_id, status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None or row["status"] != "paused":
            status = row["status"] if row else "不存在"
            return False, f"当前状态为「{status}」，无法恢复"
        status = "running" if has_executor else "queued"
        conn.execute("UPDATE jobs SET status=?, updated_at=? WHERE job_id=? AND status='paused'",
                     (status, datetime.now().isoformat(), job_id))
        _sync_wishlist(conn, row["album_id"])
        return True, "已恢复"
