import json


def test_folios_keep_mixed_chapters_visible_and_page_each_books_history(client):
    from core import database as db

    with db.transaction() as conn:
        conn.executemany('INSERT INTO jobs(job_id,album_id,status,selected_photo_ids,created_at) VALUES(?,?,?,?,?)', [
            ('A', '101', 'paused', '["1011"]', '2026-01-01'), ('B', '101', 'completed', '["1012"]', '2026-01-01'),
            ('C', '102', 'running', '[]', '2026-01-02'), ('D', '103', 'queued', '[]', '2026-02-01'),
            *((f'past{index}', '101', 'canceled', '[]', '2026-03-01') for index in range(75)),
        ])
        detail = {'title': '同册', 'author': '作者', 'photos': [{'photo_id': '1012', 'title': '次章'}]}
        conn.execute('INSERT INTO album_detail_cache(album_id,detail_json,cached_at) VALUES(?,?,?)',
                     ('101', json.dumps(detail), '2026-01-01'))

    overview = client.get('/api/jobs?view=folios&page_size=2').get_json()
    assert (overview['total'], overview['pages']) == (3, 2)
    assert overview['album_totals'] == {'101': 77, '102': 1}
    assert {job['job_id'] for job in overview['jobs']} == {'A', 'B', 'C', 'past74'}
    assert overview['books']['101'] == {**{key: detail[key] for key in ('title', 'author')},
                                       'chapters': {'1012': {'index': 1, 'title': '次章'}}}
    assert client.get('/api/jobs?view=folios&page_size=2&page=2').json['jobs'][0]['job_id'] == 'D'
    rows = []
    for page in (1, 2):
        history = client.get(f'/api/jobs?view=album&album_id=101&page={page}').get_json()
        assert history['counts'] == overview['counts']
        assert (history['total'], history['pages']) == (77, 2)
        rows.extend(history['jobs'])
    assert len({job['job_id'] for job in rows}) == len(rows) == 77
    assert all(job['album_id'] == '101' for job in rows)
    assert next(job for job in rows if job['job_id'] == 'B')['latest_completed_job_id'] == 'B'
    assert client.get('/api/jobs?status=canceled&page=2&view=jobs').json['total'] == 75
    assert client.get('/api/jobs?view=album&album_id=bad').status_code == 400


def test_atelier_download_page_loads_summary_before_list_and_css_in_head(client):
    html = client.get('/downloads').get_data(as_text=True)
    head = html.split('</head>')[0]
    assert 'css/atelier-downloads.css' in head and 'atelier-ui/transfer.css' in head
    assert html.index('id="atelier-transfer-summary"') < html.index('atelier-ui/transfer.js') < html.index('js/downloads.js')
    assert all(f'href="#{state}"' in html for state in ('all','running','paused','queued','completed','failed','canceled'))
