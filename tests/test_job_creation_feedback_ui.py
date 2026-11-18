"""Exercise search, detail and download buttons with the actual create/retry API's responses."""
import pytest

from test_update_checks_ui import FAKE_DOM, STATIC_JS, _js_block, _node


SCRIPT = FAKE_DOM + r"""
const [utilsPath, pagePath] = process.argv.slice(1);
const scenario = JSON.parse(fs.readFileSync(0, 'utf8'));
const toasts = [], posts = [], steps = [];
let active = 0, peak = 0;
function mount(tag, id, parent = body) {
  const node = el(tag); node.id = id; parent.appendChild(node); return node;
}
global.location = {search: ''}; global.confirm = () => true;
global.showToast = (message, type) => toasts.push({message, type});
eval(fs.readFileSync(utilsPath, 'utf8'));
window.apiFetch = (url, opts = {}) => {
  if (url.startsWith('/api/jobs?')) return new Promise(() => {});
  if (opts.method !== 'POST' || !scenario.responses.length) throw Error('unexpected request: ' + url);
  posts.push({url, body: opts.body ? JSON.parse(opts.body) : null});
  peak = Math.max(peak, ++active);
  const reply = scenario.responses.shift();
  return new Promise((resolve, reject) => setImmediate(() => {
    active--; if (reply.error) reject(makeError(reply.error)); else resolve(reply);
  }));
};
let click, batchButton;
if (scenario.page === 'downloads') {
  mount('div', 'downloadTabs');
  const content = mount('div', 'downloadTabsContent'), failed = mount('div', 'folio-list', content);
  const single = mount('button', 'single', content); single.setAttribute('data-action', 'retry');
  batchButton = mount('button', 'batch', content); batchButton.setAttribute('data-action', 'batch-retry');
  for (const id of scenario.cards) {
    const card = mount('div', id, failed); card.className = 'job-card';
    card.setAttribute('data-job-id', id); card.setAttribute('data-status', 'failed');
  }
  window.setSSECallbacks = () => {}; window.syncSSE = () => {};
  eval(fs.readFileSync(pagePath, 'utf8'));
  click = action => {
    single.setAttribute('data-job-id', action.id || '');
    content.handlers.click.forEach(handler => handler({target: action.batch ? batchButton : single}));
  };
} else if (scenario.page === 'search') {
  for (const id of ['search-form', 'search-input', 'sort-select', 'page-size-select', 'pagination']) mount('div', id);
  const results = mount('div', 'search-results');
  const button = mount('button', 'download', results);
  button.setAttribute('data-album-id', '601'); button.setAttribute('data-action', 'download');
  eval(fs.readFileSync(pagePath, 'utf8'));
  click = () => results.handlers.click.forEach(handler => handler({target: button}));
} else {
  const selected = mount('button', 'download-selected-btn'), all = mount('button', 'download-all-btn');
  const checked = mount('input', 'chapter'); checked.value = '11'; checked.className = 'chapter-checkbox-item';
  const query = document.querySelectorAll;
  document.querySelectorAll = selector => selector === '.chapter-checkbox-item:checked' ? [checked] : query(selector);
  eval(scenario.detailBindings + '\nbindEvents({album_id: "601"});');
  click = action => (action.all ? all : selected).click();
}
(async () => {
  for (const action of scenario.actions) {
    click(action); if (action.twice) click(action);
    await flush();
    steps.push({posts: posts.length, toasts: toasts.splice(0), disabled: batchButton && batchButton.disabled});
  }
  process.stdout.write(JSON.stringify({steps, posts, peak, receiptsLeft: scenario.responses.length}));
})().catch(err => { console.error(err); process.exit(1); });
"""


def _run(page, actions, responses, cards=()):
    page_path = STATIC_JS / f"{page}.js"
    bindings = ""
    if page == "detail":
        source = page_path.read_text(encoding="utf-8")
        bindings = "\n".join(_js_block(source, marker) for marker in
                             ("function toastErr(", "function bindEvents(", "function createDownloadJob("))
    return _node(SCRIPT, STATIC_JS / "utils.js", page_path, data={
        "page": page, "cards": cards, "responses": responses, "actions": actions, "detailBindings": bindings})


def _receipt(response):
    if response.status_code >= 400:
        return {"error": {"status": response.status_code, "message": response.json["message"]}}
    return response.json


def _failed(job_id):
    from core import database as db
    db.insert_job(job_id, "601", "合成任务", ["11"])
    db.transition_job_status(job_id, ["queued"], "failed")


@pytest.mark.parametrize("page", ["search", "detail", "downloads"])
def test_one_created_job_then_its_existing_id_is_shown(page, client, monkeypatch):
    from core import database as db
    from core.job_manager import job_manager
    monkeypatch.setattr(job_manager, "schedule_next", lambda: None)
    action = {"id": "job_source"}
    if page == "downloads":
        _failed("job_source")
        path, body = "/api/jobs/job_source/retry", None
    else:
        path = "/api/jobs"
        body = {"album_id": "601", "photo_ids": [] if page == "search" else ["11"]}
        if page == "search":
            body["title"] = ""
    first, again = (client.post(path, json=body) for _ in range(2))
    assert (first.status_code, again.status_code) == (201, 200)
    assert len([job for job in db.get_all_jobs() if job["status"] == "queued"]) == 1
    out = _run(page, [action, dict(action, all=True), action],
               [first.json, again.json, {"status": "ok", "job_id": "job_uncertain"}])
    assert out["posts"] == [{"url": path, "body": body}] * 3
    created, reused, unknown = (step["toasts"] for step in out["steps"])
    assert created == [{"message": f"已创建下载任务（ID：{first.json['job_id']}）", "type": "success"}]
    assert reused == [{"message": f"已有相同的下载任务，未重复创建（ID：{first.json['job_id']}）", "type": "info"}]
    assert unknown[0]["type"] == "warning" and "未能确认" in unknown[0]["message"]


def test_batch_retry_counts_real_outcomes_and_ignores_a_second_click(client):
    _failed("job_first")
    _failed("job_equivalent")
    cards = ["job_first", "job_equivalent", "job_missing"]
    paths = [f"/api/jobs/{job_id}/retry" for job_id in cards]
    replies = [client.post(path) for path in paths]
    assert [response.status_code for response in replies] == [201, 200, 404]
    out = _run("downloads", [{"batch": True, "twice": True}], [_receipt(response) for response in replies], cards)
    assert [post["url"] for post in out["posts"]] == paths
    assert out["peak"] == 1 and out["receiptsLeft"] == 0
    assert out["steps"] == [{"posts": 3, "disabled": False, "toasts": [
        {"message": "本页重试：新建 1，复用 1，失败 1", "type": "warning"}]}]


def test_batch_retry_all_reused_rejected_or_interrupted(client):
    _failed("job_source")
    path = "/api/jobs/job_source/retry"
    assert client.post(path).status_code == 201
    reused = _receipt(client.post(path))
    failed = _receipt(client.post("/api/jobs/job_missing/retry"))
    replies = [reused, reused, failed, failed, {"error": {"isTimeout": True}}, reused,
               {"error": {"name": "AbortError"}}]
    out = _run("downloads", [{"batch": True}] * 4, replies, ["job_a", "job_b"])
    info, error, uncertain, aborted = out["steps"]
    assert info["toasts"] == [{"message": "本页重试：新建 0，复用 2，失败 0", "type": "info"}]
    assert error["toasts"] == [{"message": "本页重试：新建 0，复用 0，失败 2", "type": "danger"}]
    assert "结果未确认 1" in uncertain["toasts"][0]["message"] and uncertain["toasts"][0]["type"] == "warning"
    assert aborted == {"posts": 7, "disabled": False, "toasts": []}
