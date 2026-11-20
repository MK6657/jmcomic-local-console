"""Browser regressions for the frontend audit, with Flask and synthetic data."""

from urllib.parse import parse_qs, urlsplit

import pytest

from test_archive_reading import _job, comic, downloads
from test_atelier_gallery import chromium
from test_online_reader import online


ORIGIN = "http://frontend.test"
THEMES = ("sage", "mint", "paper", "linen", "apricot", "night", "pixel", "cyber", "anime")


@pytest.fixture
def ui_page(chromium, client):
    page = chromium.new_page(viewport={"width": 1280, "height": 900}, service_workers="block")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_default_timeout(6000)

    def dispatch(route):
        request = route.request
        url = urlsplit(request.url)
        if url.netloc != "frontend.test":
            route.abort("blockedbyclient")
            return
        path = url.path + ("?" + url.query if url.query else "")
        extra_headers = {k: v for k, v in request.headers.items() if k.lower().startswith("x-")}
        response = client.open(path, method=request.method, data=request.post_data_buffer,
                               content_type=request.headers.get("content-type"), headers=extra_headers)
        try:
            response.direct_passthrough = False
            route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.get_data())
        finally:
            response.close()

    page.route("**/*", dispatch)
    yield page
    page.close()
    assert errors == []


def job_result(*jobs):
    return {
        "status": "ok", "jobs": list(jobs), "page": 1, "pages": 1, "total": len(jobs),
        "counts": {"running": 0, "paused": 1, "active": 1, "queued": 0,
                   "completed": len(jobs), "failed": 0, "canceled": 0},
    }


@pytest.mark.parametrize("edition", ["atelier", "classic"])
def test_transfer_polling_settles_slow_requests_retries_and_rejects_old_filters(ui_page, client, edition):
    page = ui_page
    client.set_cookie("jmconsole_edition", edition)
    page.route("**/api/cover/**", lambda route: route.fulfill(status=404))
    page.clock.install()
    page.add_init_script("""{
      const nativeFetch = window.fetch;
      window.jobRequests = [];
      window.fetch = (url, options) => {
        if (!String(url).startsWith('/api/jobs?')) return nativeFetch(url, options);
        return new Promise((resolve, reject) => {
          const status = new URL(url, location.origin).searchParams.get('status');
          const reply = result => resolve(new Response(JSON.stringify(result), {status: 200}));
          jobRequests.push({status, signal: options.signal, reply});
          options.signal.addEventListener('abort', () => reject(new DOMException('Canceled', 'AbortError')));
        });
      };
    }""")
    page.goto(ORIGIN + "/downloads")
    page.wait_for_function("jobRequests.length === 1")
    page.clock.fast_forward(6200)
    assert page.evaluate("jobRequests.map(request => request.signal.aborted)") == [False]
    paused = {"job_id": "paused-fixture", "album_id": "100", "title": "Paused", "status": "paused"}
    page.evaluate("data => jobRequests[0].reply(data)", job_result(paused))
    page.locator('.job-card[data-job-id="paused-fixture"]').wait_for()

    page.clock.fast_forward(5100)
    page.wait_for_function("jobRequests.length === 2")
    page.clock.fast_forward(30010)
    page.get_by_text("请求超时，请稍后重试", exact=False).wait_for()
    assert page.evaluate("jobRequests[1].signal.aborted")
    page.clock.fast_forward(5100)
    page.wait_for_function("jobRequests.length === 3")

    for group in ("queued", "failed"):
        selector = f'#downloadTabs [data-filter="{group}"]' if edition == "atelier" else f"#{group}-tab"
        page.locator(selector).click()
        page.wait_for_function("group => jobRequests.at(-1).status === group", arg=group)
    assert page.evaluate("jobRequests.slice(2).map(request => request.signal.aborted)") == [True, True, False]
    failed = {"job_id": "failed-fixture", "album_id": "101", "title": "Failed", "status": "failed"}
    page.evaluate("data => jobRequests.at(-1).reply(data)", job_result(failed))
    page.locator('.job-card[data-job-id="failed-fixture"]').wait_for()
    assert page.locator('.job-card:visible[data-job-id="paused-fixture"]').count() == 0
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}))")
    page.clock.fast_forward(22000)
    assert page.evaluate("jobRequests.length") == 5
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
    page.wait_for_function("jobRequests.length === 6 && jobRequests.at(-1).status === 'failed'")


def test_transfer_export_picker_keeps_job_focus_and_format_when_readability_arrives(ui_page):
    page = ui_page
    jobs = [
        {"job_id": job_id, "album_id": "100", "title": job_id, "status": "completed",
         "output_path": "fixture", "selected_photo_ids": ["101"]}
        for job_id in ("completed-one", "completed-two")
    ]
    page.route("**/api/jobs?*", lambda route: route.fulfill(json=job_result(*jobs)))
    page.route("**/api/cover/**", lambda route: route.fulfill(status=404))
    page.route("**/api/export/completed-two/pdf", lambda route: route.fulfill(status=204))
    page.add_init_script("""{
      const nativeFetch = window.fetch;
      window.fetch = (url, options) => url === '/api/preview/available'
        ? new Promise(resolve => {
            window.releaseReadability = () => resolve(new Response(JSON.stringify({
              status: 'ok', readable: ['100'], local: {'100': {state: 'loose'}}
            }), {status: 200}));
          })
        : nativeFetch(url, options);
    }""")
    page.goto(ORIGIN + "/downloads")
    row = page.locator('[data-job-id="completed-two"].job-card')
    select = row.locator(".export-format-select")
    select.select_option("pdf")
    select.focus()
    page.evaluate("window.originalFormatSelect = document.activeElement; releaseReadability();")
    row.get_by_role("link", name="预览").wait_for()
    assert select.evaluate("""node => document.activeElement === node &&
        node !== originalFormatSelect && node.value === 'pdf'""")
    assert page.evaluate("localStorage.getItem('jm_export_format')") == "pdf"
    with page.expect_request("**/api/export/completed-two/pdf") as request:
        row.get_by_role("button", name="导出", exact=True).click()
    assert request.value.method == "POST"


@pytest.mark.parametrize("mode", ["read", "preview", "online"])
def test_reader_short_viewport_keeps_fab_actions_and_notice_reachable(ui_page, downloads, online, mode):
    page = ui_page
    if mode != "online":
        folder = downloads / "Short Viewport"
        comic(folder)
        _job("short-reader", "123", folder)
    page.set_viewport_size({"width": 620, "height": 300})
    page.emulate_media(reduced_motion="reduce")
    page.goto(f"{ORIGIN}/{mode}/123")
    tools = page.locator("#reader-tools")
    if mode != "preview":
        page.locator("#reader-page-1").wait_for()
        page.keyboard.press("m")
    tools.wait_for()
    page.wait_for_function("parseInt(document.documentElement.style.getPropertyValue('--atelier-reader-dock-h')) > 0")
    if mode != "online":
        page.evaluate("showToast('阅读提示 · 测试', 'warning')")
    toast = page.locator("#toast-container .toast.show")
    toast.wait_for()
    if mode == "online":
        assert "章节暂时无法获取" in toast.inner_text()
    toggle = page.locator(".reader-fab-toggle")
    toggle.click()
    panel = page.locator(".reader-fab-panel")
    panel.wait_for()
    bounds = panel.bounding_box()
    assert bounds["y"] >= 8 and bounds["y"] + bounds["height"] < 300
    assert panel.evaluate("node => node.scrollHeight > node.clientHeight")
    notice = toast.bounding_box()
    assert notice["x"] + notice["width"] <= bounds["x"]

    actions = panel.locator("button, a")
    assert actions.count() == 10
    toggle.focus()
    for action in actions.all():
        page.keyboard.press("Tab")
        assert action.evaluate("node => node === document.activeElement")
    assert panel.evaluate("node => node.scrollTop > 0")
    panel.hover()
    page.mouse.wheel(0, -600)
    page.wait_for_function("document.getElementById('reader-fab-panel').scrollTop === 0")
    assert tools.is_visible()
    panel.locator('[data-fab-action="top"]').click()
    assert toggle.get_attribute("aria-expanded") == "false"


def switch_edition(client, page, edition):
    response = client.get(f"/ui/{edition}")
    assert response.status_code == 303 and response.location == "/downloads"
    page.goto(ORIGIN + response.location)


def test_picture_sort_survives_switch_to_classic_and_next_page(ui_page, client):
    from routes.api_search import _VALID_SORTS

    page = ui_page
    searches = []

    def search(route):
        query = parse_qs(urlsplit(route.request.url).query)
        searches.append((query["sort"][0], query["page"][0]))
        route.fulfill(json={"status": "ok", "items": [{"album_id": "101", "title": "Fixture", "tags": []}],
                            "page": int(query["page"][0]), "page_size": 20, "total": 100, "has_more": True})

    page.route("**/api/search?*", search)
    page.route("**/api/cover/**", lambda route: route.fulfill(status=404))
    page.goto(ORIGIN + "/search?keyword=fixture&sort=pictures&page=3")
    page.locator(".album-card").wait_for()
    select = page.locator("#sort-select")
    assert set(select.locator("option").evaluate_all("options => options.map(option => option.value)")) == _VALID_SORTS
    switch_edition(client, page, "classic")
    page.locator('#mainNav a[data-nav-memory="/search"]').click()
    page.wait_for_url("**/search?**")
    assert select.input_value() == parse_qs(urlsplit(page.url).query)["sort"][0] == "pictures"
    assert set(select.locator("option").evaluate_all("options => options.map(option => option.value)")) == _VALID_SORTS
    with page.expect_request(lambda request: "/api/search?" in request.url and "page=4" in request.url):
        page.locator('#pagination button[data-page="4"]').first.click()
    page.wait_for_function("new URLSearchParams(location.search).get('page') === '4'")
    assert searches[-1] == ("pictures", "4")
    assert parse_qs(urlsplit(page.url).query)["sort"] == ["pictures"]


def save_settings(page):
    with page.expect_response(lambda response: response.url == ORIGIN + "/api/settings" and
                              response.request.method == "POST") as saved:
        page.locator("#save-settings-btn").click()
    assert saved.value.ok
    page.wait_for_function("!document.getElementById('save-settings-btn').disabled")
    return saved.value.request.post_data_json


def test_classic_format_choices_round_trip_to_atelier_with_auto_pack_dependency(ui_page, client):
    from core.settings import update_settings

    page = ui_page
    update_settings({"auto_pack": "true"})
    page.goto(ORIGIN + "/settings")
    page.locator("#image_format").select_option("png")
    page.locator("#pack_format").select_option("zip")
    assert save_settings(page) == {"image_format": "png", "pack_format": "zip"}

    switch_edition(client, page, "classic")
    page.goto(ORIGIN + "/settings")
    page.wait_for_function("document.getElementById('update-check-status')?.textContent && !document.getElementById('update-check-status').textContent.includes('正在读取')")
    assert page.locator("#toast-container .toast.bg-danger").count() == 0
    image_format = page.locator("#image_format")
    pack_format = page.locator("#pack_format")
    assert image_format.input_value() == "png" and pack_format.input_value() == "zip"
    page.set_viewport_size({"width": 390, "height": 844})
    bounds = pack_format.bounding_box()
    assert bounds["x"] + bounds["width"] <= 390
    page.locator("#auto_pack").uncheck()
    assert pack_format.is_disabled() and page.locator("#delete_originals").is_disabled()
    assert pack_format.input_value() == "zip"
    page.locator("#auto_pack").check()
    assert pack_format.is_enabled()
    image_format.select_option("jpg")
    pack_format.select_option("cbz")
    assert save_settings(page) == {"image_format": "jpg", "pack_format": "cbz"}
    switch_edition(client, page, "atelier")
    page.goto(ORIGIN + "/settings")
    assert image_format.input_value() == "jpg" and pack_format.input_value() == "cbz"


def test_classic_proxy_stays_out_of_html_and_unrelated_saves(ui_page, client):
    from core.settings import get_settings, update_settings

    proxy_value = "socks5h://fixture-user:fixture-secret@localhost:7890"
    update_settings({"proxy": proxy_value})
    client.set_cookie("jmconsole_edition", "classic")
    page = ui_page
    html = page.goto(ORIGIN + "/settings").text()
    assert "fixture-user" not in html and "fixture-secret" not in html
    proxy = page.locator("#proxy")
    assert proxy.get_attribute("type") == "password" and proxy.get_attribute("value") is None
    page.wait_for_function("document.getElementById('proxy').value !== ''")
    page.wait_for_function("document.getElementById('update-check-status')?.textContent && !document.getElementById('update-check-status').textContent.includes('正在读取')")
    assert page.locator("#toast-container .toast.bg-danger").count() == 0
    assert proxy.input_value() == proxy_value
    toggle = page.locator("#proxy-visibility")
    toggle.press("Enter")
    assert proxy.get_attribute("type") == "text" and toggle.get_attribute("aria-pressed") == "true"
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}))")
    assert proxy.get_attribute("type") == "password" and toggle.get_attribute("aria-pressed") == "false"
    page.locator("#retry_times").fill("0")
    assert save_settings(page) == {"retry_times": "0"}
    assert get_settings()["proxy"] == proxy_value
    proxy.fill("")
    assert save_settings(page) == {"proxy": ""}
    assert get_settings()["proxy"] == ""


PRESET_CONTRAST = """card => {
  const luminance = color => color.match(/[\\d.]+/g).slice(0, 3).map(Number)
    .map(channel => channel / 255)
    .map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4)
    .reduce((sum, value, index) => sum + value * [.2126, .7152, .0722][index], 0);
  const background = luminance(getComputedStyle(card).backgroundColor);
  const text = card.querySelectorAll('strong, i, :scope > span:last-child');
  const contrasts = Array.from(text, item => {
    const ink = luminance(getComputedStyle(item).color);
    return (Math.max(ink, background) + .05) / (Math.min(ink, background) + .05);
  });
  let opacity = 1;
  for (let node = card; node; node = node.parentElement) opacity *= Number(getComputedStyle(node).opacity);
  return {opacity, ratio: Math.min(...contrasts)};
}"""


def test_appearance_presets_keep_contrast_in_all_nine_themes_with_effects_off_and_on(ui_page):
    page = ui_page
    page.emulate_media(reduced_motion="reduce")
    page.goto(ORIGIN + "/settings")
    switch = page.locator("#ui-fx-enabled-toggle")
    presets = page.locator(".pref-fx-card")
    assert presets.count() == 5
    minimum = {}
    for enabled in (False, True):
        switch.set_checked(enabled)
        page.evaluate("window.atelierAppearance.whenSaved()")
        for theme in THEMES:
            page.locator(f'[data-pref-theme="{theme}"]').click()
            page.evaluate("window.atelierAppearance.whenSaved()")
            for preset in presets.all():
                page.mouse.move(0, 0)
                results = [preset.evaluate(PRESET_CONTRAST)]
                preset.hover()
                results.append(preset.evaluate(PRESET_CONTRAST))
                page.mouse.move(0, 0)
                page.keyboard.press("Tab")
                preset.focus()
                assert preset.evaluate("node => node.matches(':focus-visible')")
                results.append(preset.evaluate(PRESET_CONTRAST))
                assert all(result["opacity"] == 1 for result in results)
                ratio = min(result["ratio"] for result in results)
                assert ratio >= 4.5, (theme, enabled, preset.get_attribute("data-pref-fx"), ratio)
                minimum[enabled] = min(minimum.get(enabled, ratio), ratio)
    print(f"Preset contrast floor: off={minimum[False]:.2f}:1 on={minimum[True]:.2f}:1")
    switch.uncheck()
    page.locator('[data-pref-fx="pixel"]').click()
    page.evaluate("window.atelierAppearance.whenSaved()")
    assert switch.is_checked() and page.locator("html").get_attribute("data-fx") == "pixel"


def test_motion_keeps_documented_durations_and_honors_reduced_motion(ui_page):
    page = ui_page
    page.goto(ORIGIN + "/")
    root = page.locator("html")
    tile = page.locator(".desk-tile").first
    assert root.get_attribute("data-theme") == "sage"
    assert page.locator("[data-set-theme]").count() == len(THEMES)
    assert tile.evaluate("node => getComputedStyle(node).animationDuration") == "0.3s"
    assert tile.evaluate("node => getComputedStyle(node).animationIterationCount") == "1"
    assert "0.22s" in tile.evaluate("node => getComputedStyle(node, '::before').transitionDuration")
    page.evaluate("window.atelierAppearance.setFxPreset('anime')")
    assert "0.22s" in tile.evaluate("node => getComputedStyle(node).transitionDuration")
    assert "0.24s" in tile.evaluate("node => getComputedStyle(node, '::after').transitionDuration")
    page.emulate_media(reduced_motion="reduce")
    assert tile.evaluate("node => getComputedStyle(node).animationName") == "none"
    assert tile.evaluate("node => getComputedStyle(node).transitionDuration") == "0s"


def test_shelf_export_controls_remain_on_one_line_at_desktop_widths(ui_page, downloads):
    folder = downloads / "Shelf"
    comic(folder)
    _job("shelf-controls", "100", folder)
    page = ui_page
    page.goto(ORIGIN + "/library?status=readable")
    actions = page.locator(".library-card .card-actions").first
    actions.locator(".export-picker").wait_for()
    for width, height in ((1440, 900), (1280, 800)):
        page.set_viewport_size({"width": width, "height": height})
        assert actions.evaluate("""node => {
          const row = node.getBoundingClientRect();
          const controls = [...node.querySelectorAll('a, button, select')].map(control => control.getBoundingClientRect());
          const centers = controls.map(rect => (rect.top + rect.bottom) / 2);
          return controls.length === 5 && controls.every(rect => rect.left >= row.left && rect.right <= row.right + 1)
            && Math.max(...centers) - Math.min(...centers) < 2 && node.scrollWidth <= node.clientWidth + 1;
        }"""), (width, height)


def test_downloads_live_progress_updates_in_place_without_flicker_or_jitter(ui_page):
    page = ui_page
    snapshots = [
        [
            {"job_id": "run-1", "album_id": "100", "title": "Active Book", "status": "running",
             "done_pages": 0, "total_pages": 0, "selected_photo_ids": ["1001"]},
            {"job_id": "done-2", "album_id": "200", "title": "Shelf Book", "status": "completed",
             "output_path": "fixture", "total_pages": 12, "selected_photo_ids": ["2001"]},
        ],
        [
            {"job_id": "run-1", "album_id": "100", "title": "Active Book", "status": "running",
             "done_pages": 3, "total_pages": 20, "selected_photo_ids": ["1001"]},
            {"job_id": "done-2", "album_id": "200", "title": "Shelf Book", "status": "completed",
             "output_path": "fixture", "total_pages": 12, "selected_photo_ids": ["2001"]},
        ],
        [
            {"job_id": "run-1", "album_id": "100", "title": "Active Book", "status": "running",
             "done_pages": 18, "total_pages": 20, "selected_photo_ids": ["1001"]},
            {"job_id": "done-2", "album_id": "200", "title": "Shelf Book", "status": "completed",
             "output_path": "fixture", "total_pages": 12, "selected_photo_ids": ["2001"]},
        ],
        [
            {"job_id": "run-1", "album_id": "100", "title": "Active Book", "status": "completed",
             "output_path": "fixture", "done_pages": 20, "total_pages": 20, "selected_photo_ids": ["1001"]},
            {"job_id": "done-2", "album_id": "200", "title": "Shelf Book", "status": "completed",
             "output_path": "fixture", "total_pages": 12, "selected_photo_ids": ["2001"]},
        ],
    ]
    poll_index = {"value": 0}

    def fulfill_jobs(route):
        idx = min(poll_index["value"], len(snapshots) - 1)
        poll_index["value"] += 1
        jobs = snapshots[idx]
        running = sum(1 for j in jobs if j["status"] == "running")
        completed = sum(1 for j in jobs if j["status"] == "completed")
        route.fulfill(json={
            "status": "ok", "jobs": jobs, "page": 1, "pages": 1, "total": 2,
            "counts": {"running": running, "paused": 0, "active": running, "queued": 0,
                       "completed": completed, "failed": 0, "canceled": 0},
        })

    page.route("**/api/jobs?*", fulfill_jobs)
    page.route("**/api/jobs/*/events", lambda route: route.abort("blockedbyclient"))
    page.route("**/api/cover/**", lambda route: route.fulfill(status=404))
    page.route("**/api/preview/available", lambda route: route.fulfill(json={
        "status": "ok", "readable": ["100", "200"], "local": {"100": {"state": "loose"}, "200": {"state": "loose"}},
    }))
    page.route("**/api/updates/states", lambda route: route.fulfill(json={"status": "ok", "updates": {}}))
    page.clock.install()
    page.goto(ORIGIN + "/downloads")
    page.evaluate("window.atelierAppearance && window.atelierAppearance.setFxPreset('anime')")
    page.locator('.job-card[data-job-id="run-1"]').wait_for()
    page.locator('.folio-card[data-album-id="200"] .offline-badge').wait_for()
    page.clock.fast_forward(500)
    page.wait_for_function("""() => document.querySelectorAll('.folio-card[data-enter]').length === 0
        && getComputedStyle(document.querySelector('.folio-card[data-album-id="200"]')).animationName === 'none'""")
    page.evaluate("""() => {
        window.__refs = {
            card100: document.querySelector('.folio-card[data-album-id="100"]'),
            card200: document.querySelector('.folio-card[data-album-id="200"]'),
            img100: document.querySelector('.folio-card[data-album-id="100"] .folio-cover img'),
            img200: document.querySelector('.folio-card[data-album-id="200"] .folio-cover img'),
            row1: document.querySelector('.job-card[data-job-id="run-1"]'),
            top200: Math.round(document.querySelector('.folio-card[data-album-id="200"]').getBoundingClientRect().top),
        };
    }""")

    # Poll 2 (0/0 -> 3/20) and Poll 3 (3/20 -> 18/20): zero DOM replacement, zero vertical shift, zero animation replay
    for expected_info, expected_width in (("3 / 20 页", "15%"), ("18 / 20 页", "90%")):
        page.clock.fast_forward(5100)
        page.wait_for_function(
            "expected => document.getElementById('info-run-1')?.textContent === expected",
            arg=expected_info,
        )
        check = page.evaluate("""() => ({
            sameCard100: document.querySelector('.folio-card[data-album-id="100"]') === window.__refs.card100,
            sameCard200: document.querySelector('.folio-card[data-album-id="200"]') === window.__refs.card200,
            sameImg100: document.querySelector('.folio-card[data-album-id="100"] .folio-cover img') === window.__refs.img100,
            sameImg200: document.querySelector('.folio-card[data-album-id="200"] .folio-cover img') === window.__refs.img200,
            sameRow1: document.querySelector('.job-card[data-job-id="run-1"]') === window.__refs.row1,
            topShift200: Math.abs(Math.round(document.querySelector('.folio-card[data-album-id="200"]').getBoundingClientRect().top) - window.__refs.top200),
            width: document.getElementById('progress-run-1')?.style.width,
            anim100: getComputedStyle(document.querySelector('.folio-card[data-album-id="100"]')).animationName,
            anim200: getComputedStyle(document.querySelector('.folio-card[data-album-id="200"]')).animationName,
        })""")
        assert check == {
            "sameCard100": True, "sameCard200": True,
            "sameImg100": True, "sameImg200": True,
            "sameRow1": True, "topShift200": 0,
            "width": expected_width, "anim100": "none", "anim200": "none",
        }

    # Poll 4 (running -> completed): book cards & cover images remain mounted without replaying entrance animation
    page.clock.fast_forward(5100)
    page.locator('.job-card[data-job-id="run-1"][data-status="completed"]').wait_for()
    after_done = page.evaluate("""() => ({
        sameCard100: document.querySelector('.folio-card[data-album-id="100"]') === window.__refs.card100,
        sameCard200: document.querySelector('.folio-card[data-album-id="200"]') === window.__refs.card200,
        sameImg100: document.querySelector('.folio-card[data-album-id="100"] .folio-cover img') === window.__refs.img100,
        sameImg200: document.querySelector('.folio-card[data-album-id="200"] .folio-cover img') === window.__refs.img200,
        enterAttr: document.querySelectorAll('.folio-card[data-enter]').length,
    })""")
    assert after_done == {
        "sameCard100": True, "sameCard200": True,
        "sameImg100": True, "sameImg200": True, "enterAttr": 0,
    }


def test_downloads_export_saves_to_local_export_folder_without_browser_download(ui_page, downloads, tmp_path, monkeypatch):
    import zipfile
    from routes import api_export

    export_dir = tmp_path / "导出"
    monkeypatch.setattr(api_export, "get_export_dir", lambda: export_dir)
    folder = downloads / "LocalExportBook_123"
    comic(folder)
    _job("job-local-export", "123", folder)

    opened = []
    page = ui_page
    page.route("**/api/cover/**", lambda route: route.fulfill(status=404))
    page.route(
        "**/api/export/open-folder",
        lambda route: (
            opened.append(route.request.post_data_json),
            route.fulfill(json={"status": "ok", "message": "已打开导出文件夹", "folder": str(export_dir)}),
        )[-1],
    )
    browser_downloads = []
    page.on("download", lambda dl: browser_downloads.append(dl.suggested_filename))

    page.goto(ORIGIN + "/downloads")
    row = page.locator('.job-card[data-job-id="job-local-export"]')
    row.get_by_role("button", name="导出", exact=True).click()

    toast = page.locator("#toast-container .toast.bg-success").last
    toast.wait_for()
    assert "已保存到「导出」文件夹" in toast.inner_text()
    assert browser_downloads == []

    exported_files = list(export_dir.glob("*.cbz"))
    assert len(exported_files) == 1
    with zipfile.ZipFile(exported_files[0]) as zf:
        assert len(zf.namelist()) >= 1
        assert all(info.compress_type == zipfile.ZIP_STORED for info in zf.infolist())

    toast.get_by_role("button", name="打开目录").click()
    page.wait_for_function("() => true")
    assert opened == [{"filename": exported_files[0].name}]
