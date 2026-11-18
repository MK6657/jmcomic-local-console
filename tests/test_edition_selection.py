def test_classic_can_open_the_same_downloads_and_switch_back_without_migrating_data(client):
    switch = client.get('/ui/classic')
    assert switch.status_code == 303 and switch.location == '/downloads'
    html = client.get(switch.location).get_data(as_text=True)
    assert '/static/classic/js/downloads.js?' in html
    assert client.get('/static/classic/js/downloads.js').status_code == 200
    assert '/static/classic/css/style.css?' in html
    assert client.get('/api/jobs').get_json()['status'] == 'ok'
    html = client.get('/ui/atelier', follow_redirects=True).get_data(as_text=True)
    assert '/static/js/downloads.js?' in html and '/static/classic/' not in html
