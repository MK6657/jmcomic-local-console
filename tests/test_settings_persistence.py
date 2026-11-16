"""Settings drafts, response races and persistence across independent processes."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")

UI = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const pending=[], toasts=[], events={};
const controls={};
function element(name,type,value){
 const handlers={};const classes=new Set();
 return {name,type,value:value??'',checked:false,disabled:false,innerHTML:'',textContent:'',
  addEventListener:(n,f)=>(handlers[n]??=[]).push(f),
  fire:(n,e={})=>Promise.all((handlers[n]||[]).map(f=>f(e))),
  classList:{contains:c=>classes.has(c),add:c=>classes.add(c),remove:c=>classes.delete(c)},
  querySelector:()=>null,closest:()=>null,hasAttribute:()=>false,focus(){},
  parentElement:{querySelector:()=>null,appendChild(){}},setAttribute(){}};
}
for(const [name,type,value] of [
 ['download_root','text','C:/isolated/downloads'],['proxy','text',''],['timeout','number','30'],
 ['retry_times','number','2'],['max_running_jobs','number','1'],['image_threads','number','20'],
 ['photo_threads','number','1'],['schedule_start','select-one','23'],['schedule_end','select-one','7'],
 ['client_type','select-one','api'],['auto_pack','checkbox','true'],['delete_originals','checkbox','true'],
 ['skip_existing','checkbox','true'],['auto_update_check','checkbox','true']]) controls[name]=element(name,type,value);
controls.skip_existing.checked=controls.auto_update_check.checked=true;
const form=element('form');
form.querySelector=s=>{const m=s.match(/^\[name="([^"]+)"\]$/);return m?controls[m[1]]||null:null};
form.querySelectorAll=s=>s==='input[type="checkbox"]'?Object.values(controls).filter(c=>c.type==='checkbox'):[];
const save=element('save'), ids={'settings-form':form,'save-settings-btn':save,...controls};
global.document={readyState:'complete',getElementById:id=>ids[id]||null,
 addEventListener:(n,f)=>(events[n]??=[]).push(f),createElement:()=>element('created')};
global.window={apiFetch:(url,options={})=>new Promise((resolve,reject)=>pending.push({url,options,resolve,reject}))};
global.showToast=(message,type)=>toasts.push({message,type});
global.confirm=()=>true;
global.FormData=class {
 constructor(form){this.values=[];if(form)for(const c of Object.values(controls))
   if(!c.disabled&&(c.type!=='checkbox'||c.checked))this.values.push([c.name,c.value]);}
 entries(){return this.values[Symbol.iterator]()} has(name){return this.values.some(p=>p[0]===name)} append(k,v){this.values.push([k,v])}
};
new Function(source)();
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const get=()=>pending.filter(p=>(p.options.method||'GET')==='GET');
const posts=()=>pending.filter(p=>p.options.method==='POST');
const answer=p=>({status:'ok',settings:p});
const savedBaseline={timeout:'30',retry_times:'7',proxy:'http://127.0.0.1:7890',auto_update_check:'false'};
async function edit(name,value){const c=controls[name];if(c.type==='checkbox')c.checked=value;else c.value=value;
 await c.fire('change',{target:c});await form.fire('input',{target:c});await form.fire('change',{target:c});}
async function click(){return save.fire('click',{preventDefault(){}})}
const scenario=process.argv[2];
(async()=>{
 if(scenario==='late-load'){
   await edit('timeout','77');get()[0].resolve(answer({timeout:'30',skip_existing:'true'}));await flush();
   console.log(JSON.stringify({timeout:controls.timeout.value}));
 } else if(scenario==='late-load-after-save'){
   await edit('timeout','77');const savePromise=click();await flush();
   const beforeLoad=posts().length;get()[0].resolve(answer(savedBaseline));await flush();
   const body=JSON.parse(posts()[0].options.body);posts()[0].resolve(answer(body));await savePromise;
   console.log(JSON.stringify({timeout:controls.timeout.value,toasts,body,beforeLoad}));
 } else if(scenario==='late-baseline-then-save'){
   await edit('timeout','77');get()[0].resolve(answer(savedBaseline));await flush();
   const savePromise=click();await flush();const body=JSON.parse(posts()[0].options.body);
   posts()[0].resolve(answer(body));await savePromise;console.log(JSON.stringify({body}));
 } else if(scenario==='failed-baseline-retry'){
   get()[0].reject(new Error('fixture load failed'));await flush();await edit('timeout','77');
   const savePromise=click();await flush();const beforeLoad=posts().length;
   get()[1].resolve(answer(savedBaseline));await flush();const body=JSON.parse(posts()[0].options.body);
   posts()[0].resolve(answer(body));await savePromise;console.log(JSON.stringify({body,beforeLoad}));
 } else if(scenario==='double-submit'){
   get()[0].resolve(answer({timeout:'30'}));await flush();await edit('timeout','77');
   const a=click();const b=form.fire('submit',{preventDefault(){}});await flush();
   const count=posts().length;for(const p of posts())p.resolve(answer({timeout:'77'}));await Promise.all([a,b]);
   console.log(JSON.stringify({posts:count,toasts}));
 } else if(scenario==='edit-during-save'){
   get()[0].resolve(answer({timeout:'30'}));await flush();await edit('timeout','41');const p=click();await flush();
   await edit('timeout','77');posts()[0].resolve(answer({timeout:'41'}));await p;
   console.log(JSON.stringify({timeout:controls.timeout.value,toasts}));
 } else if(scenario==='business-error'){
   get()[0].resolve(answer({timeout:'30'}));await flush();await edit('timeout','77');const p=click();await flush();
   posts()[0].resolve({status:'error',message:'not saved'});await p;
   console.log(JSON.stringify({timeout:controls.timeout.value,toasts,gets:get().length}));
 } else if(scenario==='network-error'){
   get()[0].resolve(answer({timeout:'30'}));await flush();await edit('timeout','77');const p=click();await flush();
   posts()[0].reject(new Error('temporary network failure'));await flush();
   // Old code waits for a reload and discards the draft. Answer it to complete.
   for(const request of get().slice(1))request.resolve(answer({timeout:'30'}));await p;
   console.log(JSON.stringify({timeout:controls.timeout.value,toasts,gets:get().length}));
 } else if(scenario==='checkbox-zero'){
   get()[0].resolve(answer({auto_pack:'false',delete_originals:'false'}));await flush();
   await edit('auto_pack',true);await edit('delete_originals',true);await edit('skip_existing',false);
   await edit('retry_times','0');await edit('schedule_start','0');const p=click();await flush();
   const body=JSON.parse(posts()[0].options.body);posts()[0].resolve(answer(body));await p;
   console.log(JSON.stringify({body,deleteDisabled:controls.delete_originals.disabled,toasts}));
 } else if(scenario==='loaded-dependency'){
   get()[0].resolve(answer({auto_pack:'true',delete_originals:'true'}));await flush();
   console.log(JSON.stringify({checked:controls.delete_originals.checked,disabled:controls.delete_originals.disabled}));
 } else if(scenario==='disable-dependency'){
   get()[0].resolve(answer({auto_pack:'true',delete_originals:'true'}));await flush();
   await edit('auto_pack',false);const p=click();await flush();const body=JSON.parse(posts()[0].options.body);
   posts()[0].resolve(answer(body));await p;console.log(JSON.stringify({body}));
 } else if(scenario==='socks-warning'){
   get()[0].resolve(answer({proxy:'socks5h://127.0.0.1:7890'}));await flush();const p=click();await flush();
   if(!posts().length)throw Error('valid existing socks proxy blocked saving');
   const body=JSON.parse(posts()[0].options.body);posts()[0].resolve({...answer(body),warnings:['fixture runtime warning']});await p;
   console.log(JSON.stringify({body,toasts}));
 }
})().catch(error=>{console.error(error.stack);process.exitCode=1});
"""


def ui(scenario):
    if NODE is None:
        pytest.skip("requires Node.js")
    result = subprocess.run([NODE, "-e", UI, str(ROOT / "static/js/settings.js"), scenario],
                            cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("scenario", ["late-load", "late-load-after-save", "edit-during-save"])
def test_slow_settings_responses_never_overwrite_a_newer_draft(scenario):
    assert ui(scenario)["timeout"] == "77"


@pytest.mark.parametrize("scenario", ["late-load-after-save", "late-baseline-then-save", "failed-baseline-retry"])
def test_saving_an_early_edit_merges_persisted_untouched_fields_before_any_post(scenario):
    result = ui(scenario)
    assert result.get("beforeLoad", 0) == 0
    assert {key: result["body"][key] for key in ("timeout", "retry_times", "proxy", "auto_update_check")} == {
        "timeout": "77", "retry_times": "7", "proxy": "http://127.0.0.1:7890", "auto_update_check": "false"}


def test_enter_and_click_cannot_submit_two_settings_writes():
    assert ui("double-submit")["posts"] == 1


@pytest.mark.parametrize("scenario", ["business-error", "network-error"])
def test_failed_save_preserves_draft_and_does_not_claim_success_or_rollback(scenario):
    result = ui(scenario)
    assert result["timeout"] == "77"
    assert result["gets"] == 1
    assert not any(t["type"] == "success" or "已回滚" in t["message"] for t in result["toasts"])


def test_zero_values_and_checked_unchecked_switches_are_sent_explicitly():
    result = ui("checkbox-zero")
    assert {key: result["body"][key] for key in ("retry_times", "schedule_start", "auto_pack", "delete_originals", "skip_existing")} == {
        "retry_times": "0", "schedule_start": "0", "auto_pack": "true", "delete_originals": "true", "skip_existing": "false"}
    assert not result["deleteDisabled"]


def test_async_form_population_resynchronizes_dependent_checkbox():
    assert ui("loaded-dependency") == {"checked": True, "disabled": False}


def test_disabling_auto_pack_persists_delete_originals_false():
    body = ui("disable-dependency")["body"]
    assert body["auto_pack"] == body["delete_originals"] == "false"


def test_existing_socks_proxy_does_not_block_saving_and_apply_warning_is_visible():
    result = ui("socks-warning")
    assert result["body"]["proxy"] == "socks5h://127.0.0.1:7890"
    assert result["toasts"][-1]["type"] == "warning"


CHILD = r"""
import json, socket, sys
from pathlib import Path
def blocked(*args, **kwargs):
    raise AssertionError('network forbidden')
socket.socket.connect = blocked
from curl_cffi.curl import Curl
Curl.perform = blocked
import app
from core import database as db
from routes import api_settings
api_settings.sync_scheduler = lambda: None
application = app.create_app()
with application.test_client() as client:
    if sys.argv[1] == 'save':
        response = client.post('/api/settings', json=json.loads(sys.argv[2]))
    else:
        response = client.get('/api/settings')
    print(json.dumps({'http':response.status_code,'body':response.get_json(),'db_path':str(db.DB_PATH)}))
"""


def isolated_settings_process(home, mode, payload=None):
    environment = dict(os.environ)
    for name in ("JMCONSOLE_HOME", "JMCONSOLE_DOWNLOAD_DIR", "JMCONSOLE_CACHE_DIR", "JMCONSOLE_BACKUP_DIR"):
        environment.pop(name, None)
    environment["JMCONSOLE_HOME"] = str(home)
    environment["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run([sys.executable, "-c", CHILD, mode, json.dumps(payload)], cwd=ROOT,
                            env=environment, text=True, encoding="utf-8", capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def test_settings_survive_a_new_process_and_keep_instance_boundaries(tmp_path):
    home = tmp_path / "instance"
    patch = {"timeout": "47", "retry_times": "0", "schedule_start": "0", "schedule_end": "0",
             "skip_existing": False, "auto_pack": True, "delete_originals": True,
             "auto_update_check": False, "library_card_click": True, "client_type": "html"}
    saved = isolated_settings_process(home, "save", patch)
    reloaded = isolated_settings_process(home, "read")
    expected = {key: str(value).lower() if isinstance(value, bool) else value for key, value in patch.items()}
    assert saved["http"] == reloaded["http"] == 200
    assert {key: reloaded["body"]["settings"][key] for key in patch} == expected
    assert saved["db_path"] == reloaded["db_path"] == str(home / "runtime/data/app.db")
    other = isolated_settings_process(tmp_path / "different-instance", "read")
    assert other["body"]["settings"]["timeout"] == "30"


def test_postcommit_runtime_failure_reports_saved_state_and_warning(client, monkeypatch):
    from routes import api_settings
    def fail():
        raise RuntimeError("synthetic runtime application failure")
    monkeypatch.setattr(api_settings, "sync_scheduler", fail)
    response = client.post("/api/settings", json={"timeout": "47", "retry_times": "0"})
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "ok" and body["saved"] is True and body["warnings"]
    assert client.get("/api/settings").get_json()["settings"]["timeout"] == "47"


def test_failed_validation_is_not_committed_and_read_response_is_not_cached(client):
    response = client.get("/api/settings")
    before = response.get_json()["settings"]
    assert response.headers["Cache-Control"] == "no-store"
    assert client.post("/api/settings", json={"timeout": "47", "retry_times": "bad"}).status_code == 400
    assert client.get("/api/settings").get_json()["settings"] == before
