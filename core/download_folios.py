"""Bounded, album-first views of the local transfer ledger."""
from contextlib import closing
import json

from . import database as db


STATES = ('running', 'paused', 'queued', 'completed', 'failed', 'canceled')


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
            for book in books:
                key = book['album_id']
                album_totals[key] = book['n']
                for state in STATES:
                    rows = conn.execute('SELECT * FROM jobs WHERE album_id=? AND status=? '
                                        'ORDER BY created_at DESC,id DESC LIMIT ?',
                                        (key, state, 2 if state in STATES[:3] else 1))
                    jobs.extend(dict(row) for row in rows)
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
