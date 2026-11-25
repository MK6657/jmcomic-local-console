"""Bounded, album-first views of the local transfer ledger."""
from contextlib import closing
import json
from pathlib import Path
import re

from . import database as db


STATES = ('running', 'paused', 'queued', 'completed', 'failed', 'canceled')


def _is_placeholder_title(title: object, album_id: object) -> bool:
    text = str(title or '').strip()
    aid = str(album_id or '').strip()
    if not text:
        return True
    if aid and (text == aid or re.fullmatch(rf'(?:JM[\s/_-]*)?{re.escape(aid)}', text, re.IGNORECASE)):
        return True
    return False


def _title_from_output_path(output_path: object, album_id: object) -> str:
    import os
    from . import path_guard
    from .publication import WORK
    from .storage import STAGING_DIRECTORY

    raw = str(output_path or '').strip()
    if not raw:
        return ''
    path = Path(raw)
    if any(part.casefold() in (WORK, STAGING_DIRECTORY) for part in path.parts):
        return ''
    if os.path.normcase(os.path.abspath(raw)) == os.path.normcase(os.path.abspath(str(path_guard.DOWNLOAD_ROOT))):
        return ''
    name = path.name.strip()
    if not name:
        return ''
    aid = str(album_id or '').strip()
    if aid:
        suffix = '_' + aid
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[:-len(suffix)].strip()
    return '' if _is_placeholder_title(name, aid) else name


def resolve_job_titles(jobs: list[dict], books: dict | None = None) -> list[dict]:
    """Backfill missing or numeric-ID job titles from album_meta, detail cache, or folder name."""
    missing_ids: list[str] = []
    for job in jobs:
        aid = str(job.get('album_id') or '').strip()
        if aid and _is_placeholder_title(job.get('title'), aid):
            book_title = (books.get(aid, {}).get('title') or '').strip() if books else ''
            if book_title and not _is_placeholder_title(book_title, aid):
                job['title'] = book_title
            else:
                missing_ids.append(aid)

    meta_titles: dict[str, str] = {}
    if missing_ids:
        unique_ids = tuple(dict.fromkeys(missing_ids))
        placeholders = ','.join('?' for _ in unique_ids)
        with closing(db.get_db()) as conn:
            for row in conn.execute(
                f'SELECT album_id, title FROM album_meta WHERE album_id IN ({placeholders})',
                unique_ids,
            ):
                candidate = (row['title'] or '').strip()
                if candidate and not _is_placeholder_title(candidate, row['album_id']):
                    meta_titles[row['album_id']] = candidate
            still_missing = [aid for aid in unique_ids if aid not in meta_titles]
            if still_missing:
                p2 = ','.join('?' for _ in still_missing)
                for aid, raw in conn.execute(
                    f'SELECT album_id, detail_json FROM album_detail_cache WHERE album_id IN ({p2}) '
                    'AND length(CAST(detail_json AS BLOB)) <= 262144',
                    tuple(still_missing),
                ):
                    try:
                        detail = json.loads(raw)
                    except (TypeError, ValueError, RecursionError):
                        continue
                    if isinstance(detail, dict):
                        candidate = str(detail.get('title') or '').strip()
                        if candidate and not _is_placeholder_title(candidate, aid):
                            meta_titles[aid] = candidate

    for job in jobs:
        aid = str(job.get('album_id') or '').strip()
        if not aid or not _is_placeholder_title(job.get('title'), aid):
            continue
        resolved = meta_titles.get(aid) or _title_from_output_path(job.get('output_path'), aid)
        if resolved:
            job['title'] = resolved
            if books is not None:
                entry = books.setdefault(aid, {})
                if _is_placeholder_title(entry.get('title'), aid):
                    entry['title'] = resolved
    return jobs


def transfer_page(page, page_size, album_id=None):
    with closing(db.get_db()) as conn:
        conn.execute('BEGIN')
        counts = dict.fromkeys(STATES, 0)
        counts.update(conn.execute('SELECT status, COUNT(*) FROM jobs GROUP BY status').fetchall())
        counts['all'] = sum(counts.values())
        counts['active'] = counts['running'] + counts['paused']
        size = min(page_size, 200 if album_id else 12)
        total = conn.execute('SELECT COUNT(*) FROM jobs WHERE album_id=?', (album_id,)).fetchone()[0] if album_id else (
            conn.execute('SELECT COUNT(DISTINCT album_id) FROM jobs').fetchone()[0])
        pages = max(1, (total + size - 1) // size)
        page = min(page, pages)
        offset = (page - 1) * size
        jobs = []
        album_totals = {}
        if album_id:
            latest = conn.execute("SELECT job_id FROM jobs WHERE album_id=? AND status='completed' "
                                  'ORDER BY created_at DESC,id DESC LIMIT 1', (album_id,)).fetchone()
            jobs = [dict(row, latest_completed_job_id=latest[0] if latest else None) for row in conn.execute(
                'SELECT * FROM jobs WHERE album_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?',
                (album_id, size, offset))]
            album_totals[album_id] = total
        else:
            books = conn.execute("SELECT album_id, COUNT(*) AS n FROM jobs GROUP BY album_id ORDER BY "
                                 "MAX(status IN ('running','paused')) DESC, MAX(status='queued') DESC, MAX(created_at) DESC, "
                                 'MAX(id) DESC LIMIT ? OFFSET ?', (size, offset)).fetchall()
            if books:
                ordered_ids = []
                for book in books:
                    key = book['album_id']
                    ordered_ids.append(key)
                    album_totals[key] = book['n']
                state_rank = {state: idx for idx, state in enumerate(STATES)}
                album_rank = {aid: idx for idx, aid in enumerate(ordered_ids)}
                rows = conn.execute(
                    """WITH ranked AS (
                           SELECT *,
                                  ROW_NUMBER() OVER (
                                      PARTITION BY album_id, status
                                      ORDER BY created_at DESC, id DESC
                                  ) AS _rn
                           FROM jobs
                           WHERE album_id IN (SELECT value FROM json_each(?))
                       )
                       SELECT * FROM ranked
                       WHERE (status IN ('running', 'paused', 'queued') AND _rn <= 2)
                          OR (status NOT IN ('running', 'paused', 'queued') AND _rn <= 1)""",
                    (json.dumps(ordered_ids),),
                ).fetchall()
                fetched = []
                for row in rows:
                    item = dict(row)
                    item.pop('_rn', None)
                    fetched.append(item)
                fetched.sort(key=lambda item: (
                    album_rank.get(item['album_id'], len(ordered_ids)),
                    state_rank.get(item['status'], len(STATES)),
                ))
                jobs.extend(fetched)
        return dict(jobs=jobs, album_totals=album_totals, counts=counts, page=page, pages=pages, page_size=size,
                    total=total, filter='all', view='album' if album_id else 'folios', album_id=album_id)


def book_metadata(jobs):
    wanted = {}
    for job in jobs:
        selected = job.get('selected_photo_ids')
        photo_ids = wanted.setdefault(job['album_id'], set())
        if isinstance(selected, list) and len(selected) == 1:
            photo_ids.add(str(selected[0]))
    if not wanted:
        return {}
    with closing(db.get_db()) as conn:
        ids = tuple(wanted)
        placeholders = ','.join('?' for _ in ids)
        rows = conn.execute(f'SELECT album_id,title,author FROM album_meta WHERE album_id IN ({placeholders})', ids)
        books = {row['album_id']: dict(row) for row in rows}
        cached = conn.execute(f'SELECT album_id,detail_json FROM album_detail_cache WHERE album_id IN ({placeholders}) '
                              'AND length(CAST(detail_json AS BLOB)) <= 262144', ids)
        for album_id, raw in cached:
            try:
                detail = json.loads(raw)
            except (TypeError, ValueError, RecursionError):
                continue
            if not isinstance(detail, dict):
                continue
            book = books.setdefault(album_id, {})
            for field in ('title', 'author'):
                if not book.get(field):
                    book[field] = detail.get(field) or ''
            photos = detail.get('photos')
            if isinstance(photos, list):
                book['chapters'] = {str(photo['photo_id']): {'index': index, 'title': photo.get('title')}
                                    for index, photo in enumerate(photos, 1) if isinstance(photo, dict)
                                    and str(photo.get('photo_id')) in wanted[album_id]}
        return books
