"""Drive the file picker and retry button with real export JSON and import receipts from a temporary database."""
import io
import json

from test_update_checks_ui import FAKE_DOM, ROOT, STATIC_JS, _node


IMPORT = "/api/wishlist/import-file"
SCRIPT = FAKE_DOM + r"""
const [utilsPath, wishlistPath, templatePath] = process.argv.slice(1);
const scenario = JSON.parse(fs.readFileSync(0, 'utf8'));
const toolbar = el('div'); toolbar.className = 'd-flex'; body.appendChild(toolbar);
toolbar.after = node => body.insertBefore(node, body.children[1]);
for (const [, tag, id] of fs.readFileSync(templatePath, 'utf8').matchAll(/<(\w+)\b[^>]*?\sid="([\w-]+)"/g)) {
  const node = el(tag); node.id = id;
  (id === 'wishlist-import-file-btn' ? toolbar : body).appendChild(node);
}
const picker = document.getElementById('wishlist-import-file-btn');
const input = document.getElementById('import-file-input');
const query = document.querySelectorAll;
document.querySelectorAll = selector => selector === '.item-checkbox:checked' ? [] : query(selector);
global.location = {pathname: '/wishlist', search: '', hash: ''};
global.setInterval = () => 1; global.clearInterval = () => {};
eval(fs.readFileSync(utilsPath, 'utf8'));
const files = [], pending = [], steps = [];
let peak = 0, picked = 0;
input.addEventListener('click', () => picked++);
window.apiFetch = (url, opts) => {
  if (url.startsWith('/api/wishlist?')) return Promise.resolve({status: 'ok', items: [], total: 0});
  if (url !== '/api/wishlist/import-file' || opts.method !== 'POST') throw Error('unexpected request: ' + url);
  files.push(opts.body.get('file'));
  return new Promise((resolve, reject) => { pending.push({resolve, reject}); peak = Math.max(peak, pending.length); });
};
eval(fs.readFileSync(wishlistPath, 'utf8'));
(async () => {
  for (const action of scenario.actions) {
    if ('select' in action) {
      picker.click(); input.value = '收藏.json'; input.files = [new Blob([scenario.sources[action.select]])];
      input.handlers.change.forEach(handler => handler.call(input));
    } else if (action.retry) {
      const button = body.querySelector('[role="status"]').parentNode.querySelector('button');
      if (button.hidden) throw Error('retry is not available');
      button.click(); if (action.twice) button.click();
    } else if (action.leave) {
      winHandlers.pagehide.forEach(handler => handler({}));
    } else if (action.back) {
      winHandlers.pageshow.forEach(handler => handler({persisted: true}));
    } else {
      const request = pending.shift();
      if (action.error) request.reject(makeError(action.error)); else request.resolve(action.reply);
    }
    await flush();
    const status = body.querySelector('[role="status"]');
    steps.push({text: status.textContent, kind: status.parentNode.className,
      retry: !status.parentNode.querySelector('button').hidden, busy: picker.disabled, requests: files.length});
  }
  const batches = await Promise.all(files.map(async file => ({name: file.name, rows: JSON.parse(await file.text())})));
  process.stdout.write(JSON.stringify({steps, batches, peak, picked, inputValue: input.value}));
})().catch(err => { console.error(err); process.exit(1); });
"""


def _run(sources, actions):
    return _node(SCRIPT, STATIC_JS / "utils.js", STATIC_JS / "wishlist.js", ROOT / "templates/wishlist.html",
                 data={"sources": sources, "actions": actions})


def _post(client, rows):
    payload = json.dumps(rows, ensure_ascii=False).encode()
    return client.post(IMPORT, data={"file": (io.BytesIO(payload), "wishlist.json")})


def test_export_over_200_round_trips_and_resumes_after_lost_receipt(client):
    from core import database as db

    with db.transaction() as conn:
        conn.executemany("INSERT INTO wishlist(album_id,title,author,cover_url) VALUES (?,?,?,?)", [
            (str(90000 + index), f"合成《{index}》", "示例作者", f"/test-cover/{index}") for index in range(401)])
    exported = client.get("/api/wishlist/export")
    rows = exported.get_json()
    assert len(rows) == 401
    with db.transaction() as conn:
        conn.execute("DELETE FROM wishlist")
    assert _post(client, rows).status_code == 400
    first, middle, last = rows[:200], rows[200:400], rows[400:]
    first_ack = _post(client, first).json
    assert _post(client, middle).json["added"] == 200
    retry_ack = _post(client, middle).json
    last_ack = _post(client, last).json
    replay = [_post(client, batch).json for batch in (first, middle, last)]
    out = _run([exported.get_data(as_text=True)], [
        {"select": 0}, {"reply": first_ack}, {"error": {"isTimeout": True, "message": "请求超时"}},
        {"retry": True, "twice": True}, {"reply": retry_ack}, {"reply": last_ack},
        {"select": 0}, *({"reply": receipt} for receipt in replay),
    ])

    assert [batch["rows"] for batch in out["batches"]] == [first, middle, middle, last, first, middle, last]
    assert {batch["name"] for batch in out["batches"]} == {"wishlist.json"}
    assert out["peak"] == 1 and out["picked"] == 2 and out["inputValue"] == ""
    paused = out["steps"][2]
    assert paused["requests"] == 2 and paused["retry"] and not paused["busy"]
    assert "已确认 200/401 条：新增 200，已存在 0，失败 0" in paused["text"]
    assert "从第 201 条重试" in paused["text"] and "请求超时" in paused["text"]
    assert "已确认 401/401 条：新增 201，已存在 200，失败 0" in out["steps"][5]["text"]
    assert "重试前已写入的收藏按已存在计数" in out["steps"][5]["text"]
    assert "新增 0，已存在 401，失败 0" in out["steps"][-1]["text"] and not out["steps"][-1]["retry"]
    fields = ("album_id", "title", "author", "cover_url")
    assert sorted(tuple(row[key] for key in fields) for row in db.export_all_wishlist()) == sorted(
        tuple(row[key] for key in fields) for row in rows)


def test_import_validates_file_and_counts_row_errors_at_their_original_positions(client):
    first = [{"album_id": str(index)} for index in range(1, 201)]
    tail = [first[0], None, {"album_id": "201"}]
    out = _run(["{", "{}", "[]", json.dumps(first + tail)], [
        {"select": 0}, {"select": 1}, {"select": 2}, {"select": 3}, {"reply": _post(client, first).json},
        {"reply": {"status": "ok", "added": 0, "skipped_existing": 0, "errors": []}},
        {"retry": True}, {"reply": _post(client, tail).json},
    ])
    malformed, wrong_shape, empty = out["steps"][:3]
    assert "JSON 解析失败" in malformed["text"] and "JSON 数组" in wrong_shape["text"] and "没有收藏" in empty["text"]
    assert [step["requests"] for step in out["steps"][:3]] == [0, 0, 0]
    assert out["steps"][5]["retry"] and "计数与本批条数不符" in out["steps"][5]["text"]
    assert [batch["rows"] for batch in out["batches"]] == [first, tail, tail]
    final = out["steps"][-1]
    assert "新增 201，已存在 1，失败 1" in final["text"] and "第 202 条：条目不是对象" in final["text"]
    assert "alert-warning" in final["kind"] and not final["retry"] and not final["busy"]


def test_navigating_away_does_not_start_the_next_batch_without_retry():
    rows = [{"album_id": str(index)} for index in range(1, 202)]
    receipt = {"status": "ok", "added": 200, "skipped_existing": 0, "errors": []}
    out = _run([json.dumps(rows)], [{"select": 0}, {"leave": True}, {"reply": receipt}, {"back": True},
                                  {"retry": True}, {"reply": dict(receipt, added=1)}])
    for paused in out["steps"][2:4]:
        assert paused["requests"] == 1 and paused["retry"] and "从第 201 条重试" in paused["text"]
    assert [batch["rows"] for batch in out["batches"]] == [rows[:200], rows[200:]]
    assert "已确认 201/201 条：新增 201，已存在 0，失败 0" in out["steps"][-1]["text"]
