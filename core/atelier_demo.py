"""Standalone synthetic library. Only loaded by the explicit desktop 'demo' command."""
import io
import os
from pathlib import Path
import socket


def install_catalog(app, books):
    from flask import jsonify, request, send_file
    from core.database import add_search_history
    from core.validation import validate_numeric
    from tools.seed_atelier_demo import render_cover_bytes, render_story_page_bytes

    by_id = {book['album_id']: book for book in books}
    by_chapter = {chapter['photo_id']: (book, chapter) for book in books for chapter in book['chapters']}
    original_cover = app.view_functions['api_album.cover_redirect']

    def search():
        keyword = request.args.get('q', '').strip()
        if not 0 < len(keyword) <= 200:
            return jsonify(status='error', message='请输入 1 至 200 个字符的检索词。'), 400
        rows = [dict(album_id=b['album_id'], title=b['title'], author=b['author'], tags=b['auto_tags'],
                     cover_url='/api/cover/'+b['album_id'], views=b['views'], likes=b['likes'],
                     pictures=sum(c['page_count'] for c in b['chapters'])) for b in books
                if keyword.casefold() in ' '.join([b['album_id'], b['title'], b['author'], *b['auto_tags']]).casefold()]
        sort = request.args.get('sort')
        rows.sort(key=lambda row: row[sort if sort in ('pictures', 'views', 'likes') else 'album_id'], reverse=True)
        size = max(1, min(100, request.args.get('page_size', 20, type=int)))
        page = max(1, request.args.get('page', 1, type=int))
        add_search_history(keyword)
        return jsonify(status='ok', items=rows[(page-1)*size:page*size], total=len(rows), page=page,
                       page_size=size, has_more=page*size < len(rows))

    def demo_album_detail(album_id):
        if not validate_numeric(album_id):
            return jsonify(status='error', message='album_id 必须是纯数字'), 400
        book = by_id.get(album_id)
        if not book:
            return jsonify(status='error', message='这本书不在离线样书目录中。'), 404
        return jsonify(status='ok', data={
            'album_id': album_id,
            'title': book['title'],
            'author': book['author'],
            'tags': list(book['auto_tags']),
            'actors': [],
            'works': ['藏书阁典藏画集'],
            'views': book['views'],
            'likes': book['likes'],
            'description': book['description'],
            'cover': '/api/cover/' + album_id,
            'photos': [
                {
                    'photo_id': ch['photo_id'],
                    'title': ch['title'],
                    'page_count': ch['page_count'],
                    'page_count_known': True,
                }
                for ch in book['chapters']
            ],
            'chapter_count': len(book['chapters']),
            'details_complete': True,
        })

    def demo_cover(album_id):
        if not validate_numeric(album_id):
            return jsonify(status='error', message='album_id 必须是纯数字'), 400
        book = by_id.get(album_id)
        if not book:
            return jsonify(status='error', message='这本书不在离线样书目录中。'), 404
        response = original_cover(album_id)
        if getattr(response, 'status_code', None) == 302:
            return send_file(io.BytesIO(render_cover_bytes(book)), mimetype='image/webp', max_age=86400)
        return response

    def demo_pages(album_id):
        if not validate_numeric(album_id):
            return jsonify(status='error', message='album_id 必须是纯数字'), 400
        book = by_id.get(album_id)
        if not book:
            return jsonify(status='error', message='这本书不在离线样书目录中。'), 404
        pages = []
        for chapter in book['chapters']:
            for ix in range(chapter['page_count']):
                pages.append(dict(page=len(pages)+1, chapter=chapter['title'], photo_id=chapter['photo_id'],
                                  url='/api/online-img/%s/%d' % (chapter['photo_id'], ix)))
        return jsonify(status='ok', album_id=album_id, title=book['title'], total_pages=len(pages),
                       pages=pages, skipped_chapters=0)

    def demo_image(photo_id, index):
        if not validate_numeric(photo_id):
            return jsonify(status='error', message='photo_id 必须是纯数字'), 400
        pair = by_chapter.get(photo_id)
        if not pair or not 0 <= index < pair[1]['page_count']:
            return jsonify(status='error', message='样书中没有这一页。'), 404
        book, chapter = pair
        data = render_story_page_bytes(book, chapter['title'], index+1, chapter['page_count'])
        return send_file(io.BytesIO(data), mimetype='image/webp', max_age=86400)

    def demo_retry_maintenance(album_id):
        import threading
        from core import album_maintenance
        from core.task_commands import TaskCommandError
        if not validate_numeric(album_id):
            return jsonify(status='error', message='album_id 必须是纯数字'), 400
        try:
            album_maintenance.retry(album_id)
        except TaskCommandError as exc:
            return jsonify(status='error', reason=exc.reason, message=str(exc)), exc.http_status
        work = album_maintenance.claim()
        if work:
            album_maintenance.run(work, threading.Event())
        return jsonify(status='ok', album_id=album_id, state='pending', message='已加入整册整理队列'), 202

    def demo_add_wishlist():
        from core import database as db
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(status='error', message='请求体需为 JSON 对象'), 400
        album_id = str(body.get('album_id', '')).strip()
        if not album_id:
            return jsonify(status='error', message='缺少 album_id'), 400
        if not validate_numeric(album_id):
            return jsonify(status='error', message='album_id 必须是纯数字'), 400
        book = by_id.get(album_id)
        title = str(body.get('title', '') or (book['title'] if book else ''))[:500]
        author = str(body.get('author', '') or (book['author'] if book else ''))[:200]
        cover_url = str(body.get('cover_url', '') or ('/api/cover/' + album_id if book else ''))[:2000]
        if not db.add_wishlist(album_id, title, author, cover_url):
            return jsonify(status='error', message='该漫画已在收藏中',
                           data={'album_id': album_id, 'title': title, 'author': author}), 409
        if book and book.get('auto_tags'):
            db.batch_sync_auto_tags(album_id, list(book['auto_tags']))
        return jsonify(status='ok', message='已添加到收藏', warnings=[],
                       data={'album_id': album_id, 'title': title, 'author': author}), 201

    # Only this dedicated Flask application serves the synthetic catalog. Real service uses the SDK.
    app.view_functions['api_search.search'] = search
    app.view_functions['api_album.album_detail'] = demo_album_detail
    app.view_functions['api_album.cover_redirect'] = demo_cover
    app.view_functions['api_online.online_album'] = demo_pages
    app.view_functions['api_online.online_image'] = demo_image
    app.view_functions['api_jobs.retry_maintenance'] = demo_retry_maintenance
    app.view_functions['api_wishlist.add_wishlist'] = demo_add_wishlist
    app.context_processor(lambda: {'atelier_demo': True})
    app.config['ATELIER_DEMO'] = True

    @app.before_request
    def keep_demo_offline():
        path = request.path
        blocked_post = request.method == 'POST' and (
            (path.startswith(('/api/jobs', '/api/batch', '/api/wishlist/download')) and not path.endswith('/open-folder'))
            or (path.startswith('/api/updates/') and path.endswith('/check'))
            or path == '/api/library/tags/sync-all'
            or (path.startswith('/api/library/') and path.endswith('/tags/sync'))
            or path == '/api/system/clear-cache'
        )
        if blocked_post or path == '/api/system/diagnose':
            return jsonify(status='error', message='此处是离线样书实例。实际下载或联网诊断请在正式实例使用。'), 409

def run_demo(*, open_browser):
    import secrets
    import threading
    import time
    import webbrowser
    from flask import request, abort
    from tools.seed_atelier_demo import _resolve_demo_home, seed_demo, demo_lock_name, DEMO_BOOKS
    base = Path(os.environ['LOCALAPPDATA'])
    if not base.is_absolute() or not base.is_dir():
        raise ValueError('没有找到当前用户的本地应用数据目录。')
    # No arbitrary --home, no caller-supplied instance paths, no reset/delete.
    # Resolve 8.3 short paths (e.g. RUNNER~1 on Windows CI) before binding core modules.
    home = _resolve_demo_home(base / 'JMComic-Atelier-Demo-v1')
    for key in list(os.environ):
        if key.upper().startswith('JMCONSOLE_'):
            del os.environ[key]
    os.environ['JMCONSOLE_HOME'] = str(home)
    from core.release_lifecycle import _guard_network
    from core.instance_lock import acquire_instance_lock, maintenance_lock
    _guard_network()
    lock_name = demo_lock_name(home)
    lease = acquire_instance_lock(lock_name)
    try:
        from core import database
        database._migration_lock = lambda: maintenance_lock(lock_name)
        seed_demo(home, keep_existing=True)
        from app import create_app
        from core.http_server import ManagedHTTPServer
        app = create_app()
        install_catalog(app, DEMO_BOOKS)
        finished = threading.Event()
        stop_token = secrets.token_urlsafe(32)
        app.context_processor(lambda: {'demo_stop_token': stop_token})

        @app.post('/demo/close')
        def close_demo():
            if not secrets.compare_digest(request.form.get('token', '').encode(), stop_token.encode()):
                abort(403)
            finished.set()
            return '<html lang="zh-CN"><meta charset="utf-8"><title>藏书阁</title><p>已退出离线样书预览，可以关闭此标签页。</p></html>'

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            server = ManagedHTTPServer(app, listener, threads=24)
            url = f'http://127.0.0.1:{listener.getsockname()[1]}/downloads'
            print('离线样书实例（Ctrl+C 关闭）：' + url, flush=True)
            if open_browser:
                webbrowser.open(url)
            try:
                from waitress import wasyncore
                # The serving thread owns teardown. Allow the last loopback response to flush.
                closing_at = None
                while closing_at is None or time.monotonic() < closing_at:
                    wasyncore.loop(timeout=.15, count=1, map=server.channels)
                    if finished.is_set() and closing_at is None:
                        closing_at = time.monotonic() + .6
            except KeyboardInterrupt:
                pass
            finally:
                server.stop()
                from core import background_calls, scheduler
                from core.tag_jobs import tag_jobs
                tag_jobs.stop(timeout=2)
                background_calls.stop(timeout=2)
                scheduler.stop(timeout=2)
    finally:
        lease.close()
    return 0
