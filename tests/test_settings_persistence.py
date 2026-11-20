"""Settings drafts, response races and persistence across independent processes."""
import json
import os
import re
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
 ['download_root','text','C:/isolated/downloads'],['proxy','password',''],['timeout','number','30'],
 ['retry_times','number','2'],['max_running_jobs','number','1'],['image_threads','number','20'],
 ['photo_threads','number','1'],['schedule_start','select-one','23'],['schedule_end','select-one','7'],
 ['client_type','select-one','api'],['auto_pack','checkbox','true'],['delete_originals','checkbox','true'],
 ['skip_existing','checkbox','true'],['auto_update_check','checkbox','true'],['organize_mode','select-one','none'],
 ['schedule_enabled','checkbox','true'],['library_card_click','checkbox','true']]) controls[name]=element(name,type,value);
controls.download_root.readOnly=true;
controls.skip_existing.checked=controls.auto_update_check.checked=true;
const form=element('form');
form.elements=Object.values(controls);
form.querySelector=s=>{const m=s.match(/^\[name="([^"]+)"\]$/);return m?controls[m[1]]||null:null};
form.querySelectorAll=s=>s==='input[type="checkbox"]'?Object.values(controls).filter(c=>c.type==='checkbox'):[];
const save=element('save'), reset=element('reset'), importFile=element('','file'), importButton=element('');
const visibility=element('');
const ids={'settings-form':form,'save-settings-btn':save,'settings-reset-btn':reset,'import-settings-btn':importButton,'import-settings-file':importFile,'proxy-visibility':visibility,...controls};
global.document={readyState:'complete',getElementById:id=>ids[id]||null,
 addEventListener:(n,f)=>(events[n]??=[]).push(f),createElement:()=>element('created')};
global.window={addEventListener:(n,f)=>(events[n]??=[]).push(f),apiFetch:(url,options={})=>new Promise((resolve,reject)=>pending.push({url,options,resolve,reject}))};
global.showToast=(message,type)=>toasts.push({message,type});
const confirmations=[];
global.confirm=message=>{confirmations.push(message);return true};
global.FormData=class {
 constructor(form){this.values=[];if(form)for(const c of Object.values(controls))
   if(!c.disabled&&(c.type!=='checkbox'||c.checked))this.values.push([c.name,c.value]);}
 entries(){return this.values[Symbol.iterator]()} has(name){return this.values.some(p=>p[0]===name)} append(k,v){this.values.push([k,v])}
};
new Function(source)();
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const get=()=>pending.filter(p=>(p.options.method||'GET')==='GET');
const posts=()=>pending.filter(p=>p.url==='/api/settings'&&p.options.method==='POST');
const resets=()=>pending.filter(p=>p.url==='/api/settings/reset'&&p.options.method==='POST');
const writeButtons=()=>[save.disabled,reset.disabled,importButton.disabled];
const defaults=Object.fromEntries(form.elements.map(c=>[c.name,c.type==='checkbox'?String(c.checked):c.value]));
const answer=values=>({status:'ok',settings:{...defaults,...values}});
const savedBaseline={timeout:'30',retry_times:'7',proxy:'http://127.0.0.1:7890',auto_update_check:'false'};
async function edit(name,value){const c=controls[name];if(c.type==='checkbox')c.checked=value;else c.value=value;
 await c.fire('change',{target:c});await form.fire('input',{target:c});await form.fire('change',{target:c});}
async function click(){return save.fire('click',{preventDefault(){}})}
async function saveAndReply(server={},warnings=[]){
 const index=posts().length,done=click();await flush();
 const request=posts()[index];
 if(!request){await done;return null}
 const body=JSON.parse(request.options.body);
 request.resolve({...answer({...server,...body}),warnings});await done;return body;
}
function loadedFields(){return {retry_times:controls.retry_times.value,proxy:controls.proxy.value,
 auto_update_check:controls.auto_update_check.checked}}
async function startImport(){
 importFile.files=[{name:'settings.json'}];await importFile.fire('change');await flush();
 return pending.find(request=>request.url==='/api/settings/import');
}
const scenario=process.argv[2];
(async()=>{
 if(scenario==='tab-edits'){
   const {loaded,edits}=JSON.parse(process.argv[3]);get()[0].resolve(answer(loaded));await flush();
   for(const [name,value] of Object.entries(edits))await edit(name,value);
   console.log(JSON.stringify({body:await saveAndReply(loaded)}));
 } else if(scenario==='late-load'){
   await edit('timeout','77');get()[0].resolve(answer({timeout:'30',skip_existing:'true'}));await flush();
   console.log(JSON.stringify({timeout:controls.timeout.value}));
 } else if(scenario==='late-load-after-save'){
   await edit('timeout','77');const savePromise=click();await flush();
   const beforeLoad=posts().length;get()[0].resolve(answer(savedBaseline));await flush();
   const loaded=loadedFields(),body=JSON.parse(posts()[0].options.body);
   posts()[0].resolve(answer({...savedBaseline,...body}));await savePromise;
   console.log(JSON.stringify({timeout:controls.timeout.value,toasts,body,beforeLoad,loaded}));
 } else if(scenario==='late-baseline-then-save'){
   await edit('timeout','77');get()[0].resolve(answer(savedBaseline));await flush();
   console.log(JSON.stringify({loaded:loadedFields(),body:await saveAndReply(savedBaseline)}));
 } else if(scenario==='failed-baseline-retry'){
   get()[0].reject(new Error('fixture load failed'));await flush();await edit('timeout','77');
   const savePromise=click();await flush();const beforeLoad=posts().length;
   get()[1].resolve(answer(savedBaseline));await flush();const loaded=loadedFields(),body=JSON.parse(posts()[0].options.body);
   posts()[0].resolve(answer({...savedBaseline,...body}));await savePromise;console.log(JSON.stringify({body,beforeLoad,loaded}));
 } else if(scenario==='no-op'){
   const loaded={timeout:30,retry_times:0,skip_existing:false,auto_pack:true,delete_originals:true,pack_format:'zip'};
   const initial=answer(loaded);delete initial.settings.image_threads;
   await edit('retry_times','0');const saving=click();await flush();get()[0].resolve(initial);await flush();
   if(posts().length)posts()[0].resolve(initial);await saving;
   await edit('timeout','48');await edit('timeout','30.0');await edit('skip_existing',true);await edit('skip_existing',false);
   controls.download_root.value='';controls.photo_threads.disabled=true;controls.photo_threads.value='3';
   await edit('image_threads','31');
   await saveAndReply(loaded);const writes=posts().length,notices=toasts.slice();
   await edit('retry_times','1');const other=await saveAndReply(loaded);
   console.log(JSON.stringify({writes,notices,other,confirmations:confirmations.length}));
 } else if(scenario==='double-submit'){
   get()[0].resolve(answer({timeout:'30'}));await flush();await edit('timeout','77');
   const a=click();const b=form.fire('submit',{preventDefault(){}});await flush();
   const count=posts().length;for(const p of posts())p.resolve(answer({timeout:'77'}));await Promise.all([a,b]);
   console.log(JSON.stringify({posts:count,toasts}));
 } else if(scenario==='reset-overlap'){
   get()[0].resolve(answer(savedBaseline));await flush();
   await edit('retry_times','5');const saving=click();await flush();
   const duringSave=writeButtons();await reset.fire('click');await startImport();
   posts()[0].resolve(answer({retry_times:'5'}));await saving;
   const importing=await startImport();const duringImport=writeButtons();
   await reset.fire('click');await click();
   importing.resolve({status:'ok',imported:1,skipped:0});await flush();
   get()[1].resolve(answer({retry_times:'9'}));await flush();
   const resetting=reset.fire('click');await flush();
   const duringReset=writeButtons();await click();await startImport();
   resets()[0].resolve({...answer({retry_times:'2'}),warnings:['fixture runtime warning']});await flush();
   const duringRefresh=writeButtons();
   get()[2].resolve(answer({retry_times:'2'}));await resetting;
   console.log(JSON.stringify({duringSave,duringImport,duringReset,duringRefresh,after:writeButtons(),
      resetBodies:resets().map(p=>JSON.parse(p.options.body)),posts:posts().length,
      imports:pending.filter(p=>p.url==='/api/settings/import').length,
      confirms:confirmations.length,retry:controls.retry_times.value,notice:toasts.at(-1)}));
 } else if(scenario==='reset-failed'){
   get()[0].resolve(answer(savedBaseline));await flush();
   const resetting=reset.fire('click');await flush();
   resets()[0].resolve({status:'error',message:'fixture reset failed'});await resetting;
   console.log(JSON.stringify({after:writeButtons(),notice:toasts.at(-1),gets:get().length}));
 } else if(scenario==='edit-during-save'){
   get()[0].resolve(answer({timeout:'30'}));await flush();await edit('timeout','41');const p=click();await flush();
   await edit('timeout','77');await edit('retry_times','0');await edit('image_threads','21');await edit('image_threads','20');
   const confirmed={timeout:'41',retry_times:'4',image_threads:'32',skip_existing:'false'};
   posts()[0].resolve(answer(confirmed));await p;
   console.log(JSON.stringify({timeout:controls.timeout.value,image:controls.image_threads.value,skip:controls.skip_existing.checked,
     notice:toasts.at(-1),next:await saveAndReply(confirmed),again:await saveAndReply()}));
 } else if(scenario==='revert-during-save'){
   get()[0].resolve(answer({timeout:'30'}));await flush();await edit('timeout','47');const saving=click();await flush();
   await edit('timeout','30');posts()[0].resolve(answer({timeout:'47'}));await saving;
   console.log(JSON.stringify({timeout:controls.timeout.value,next:await saveAndReply({timeout:'47'})}));
 } else if(['business-error','network-error','invalid-ack','empty-ack'].includes(scenario)){
   get()[0].resolve(answer({timeout:'30'}));await flush();await edit('timeout','77');const p=click();await flush();
   if(scenario==='network-error')posts()[0].reject(new Error('temporary network failure'));
   else if(scenario==='empty-ack')posts()[0].resolve({status:'ok',settings:{}});
   else posts()[0].resolve(scenario==='invalid-ack'?{status:'ok'}:{status:'error',message:'not saved'});
   await flush();
   for(const request of get().slice(1))request.resolve(answer({timeout:'30'}));await p;
   console.log(JSON.stringify({timeout:controls.timeout.value,toasts:toasts.slice(),gets:get().length,
     retry:await saveAndReply({retry_times:'9'}),again:await saveAndReply()}));
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
 } else if(scenario==='late-pack-toggle'){
   await edit('auto_pack',true);await edit('delete_originals',true);await edit('auto_pack',false);await edit('auto_pack',true);
   const loaded={auto_pack:true,delete_originals:true};get()[0].resolve(answer(loaded));await flush();
   console.log(JSON.stringify({checked:controls.delete_originals.checked,body:await saveAndReply(loaded)}));
 } else if(scenario==='import-refresh'||scenario==='import-retry'){
   const retry=scenario==='import-retry';
   if(retry){get()[0].resolve(answer({timeout:'41'}));await flush()}
   await edit('timeout','61');const importing=await startImport();await edit('retry_times','0');
   await click();importing.resolve({status:'ok',imported:2,skipped:0});await flush();
   await edit('skip_existing',false);
   if(retry){get()[1].reject(new Error('refresh failed'));await flush()}
   const saving=retry?click():null;await flush();const beforeLoad=posts().length;
   const imported={timeout:'52',retry_times:'4',skip_existing:true};get().at(-1).resolve(answer(imported));await flush();
   if(!retry){get()[0].resolve(answer({timeout:'31'}));await flush()}
   const fields={timeout:controls.timeout.value,retry:controls.retry_times.value,skip:controls.skip_existing.checked};
   let body;
   if(saving){const request=posts()[0];body=JSON.parse(request.options.body);request.resolve(answer({...imported,...body}));await saving}
   else body=await saveAndReply(imported);
   console.log(JSON.stringify({fields,body,beforeLoad,gets:get().length,again:await saveAndReply()}));
 } else if(scenario==='masked-proxy'){
   const loaded={proxy:'socks5h://test-user:test-pass@localhost:7890'};
   get()[0].resolve(answer(loaded));await flush();
   await visibility.fire('click');const revealed=controls.proxy.type==='text'&&controls.proxy.value===loaded.proxy;
   for(const fn of events.pagehide)fn();
   await edit('retry_times','0');const body=await saveAndReply(loaded);
   await edit('proxy','');console.log(JSON.stringify({revealed,masked:controls.proxy.type==='password',body,direct:await saveAndReply()}));
 } else if(scenario==='socks-warning'){
   const loaded={proxy:'socks5h://127.0.0.1:7890'};get()[0].resolve(answer(loaded));await flush();
   await edit('retry_times','0');const other=await saveAndReply(loaded);
   await edit('proxy','socks5h://127.0.0.1:7900');const body=await saveAndReply({},['fixture runtime warning']);
   console.log(JSON.stringify({other,body,toasts}));
 }
})().catch(error=>{console.error(error.stack);process.exitCode=1});
"""


def ui(scenario, settings=None, *, edition="atelier"):
    if NODE is None:
        pytest.skip("requires Node.js")
    static = ROOT / ("static/classic" if edition == "classic" else "static")
    result = subprocess.run([NODE, "-e", UI, str(static / "js/settings.js"), scenario, json.dumps(settings)],
                            cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def tab_patch(loaded, **edits):
    return ui("tab-edits", {"loaded": loaded, "edits": edits})["body"]


@pytest.mark.parametrize("scenario", ["late-load", "late-load-after-save"])
def test_slow_settings_responses_never_overwrite_a_newer_draft(scenario):
    assert ui(scenario)["timeout"] == "77"


@pytest.mark.parametrize("scenario", ["late-load-after-save", "late-baseline-then-save", "failed-baseline-retry"])
def test_saving_an_early_edit_loads_untouched_fields_but_posts_only_the_draft(scenario):
    result = ui(scenario)
    assert result.get("beforeLoad", 0) == 0
    assert result["loaded"] == {"retry_times": "7", "proxy": "http://127.0.0.1:7890", "auto_update_check": False}
    assert result["body"] == {"timeout": "77"}


def test_enter_and_click_cannot_submit_two_settings_writes():
    assert ui("double-submit")["posts"] == 1


def test_save_import_and_reset_are_mutually_exclusive_and_reset_reports_warnings():
    result = ui("reset-overlap")
    assert all(result[phase] == [True] * 3 for phase in ("duringSave", "duringImport", "duringReset", "duringRefresh"))
    assert result["after"] == [False] * 3
    assert result["resetBodies"] == [{"scope": "all"}]
    assert result["posts"] == result["imports"] == result["confirms"] == 1
    assert result["retry"] == "2"
    assert result["notice"]["type"] == "warning" and "fixture runtime warning" in result["notice"]["message"]


def test_failed_reset_releases_write_controls_without_claiming_success():
    result = ui("reset-failed")
    assert result["after"] == [False] * 3 and result["gets"] == 1
    assert result["notice"]["type"] == "danger" and "fixture reset failed" in result["notice"]["message"]


def test_no_op_reverted_or_unwritable_fields_never_write_or_reconfirm_deletion():
    result = ui("no-op")
    assert result["writes"] == result["confirmations"] == 0
    assert [notice["type"] for notice in result["notices"]] == ["info", "info"]
    assert result["other"] == {"retry_times": "1"}


def test_save_response_merges_other_fields_and_rebases_only_the_remaining_draft():
    result = ui("edit-during-save")
    assert (result["timeout"], result["image"], result["skip"]) == ("77", "32", False)
    assert result["notice"]["type"] == "info" and "尚未保存" in result["notice"]["message"]
    assert result["next"] == {"timeout": "77", "retry_times": "0"}
    assert result["again"] is None


def test_edit_back_to_original_while_saving_is_a_draft_against_the_confirmed_value():
    assert ui("revert-during-save") == {"timeout": "30", "next": {"timeout": "30"}}


@pytest.mark.parametrize("scenario", ["business-error", "network-error", "invalid-ack", "empty-ack"])
def test_failed_save_preserves_draft_and_does_not_claim_success_or_rollback(scenario):
    result = ui(scenario)
    assert result["timeout"] == "77"
    assert result["gets"] == 1
    assert not any(t["type"] == "success" or "已回滚" in t["message"] for t in result["toasts"])
    assert result["retry"] == {"timeout": "77"} and result["again"] is None


def test_zero_values_and_checked_unchecked_switches_are_sent_explicitly():
    result = ui("checkbox-zero")
    assert result["body"] == {
        "retry_times": "0", "schedule_start": "0", "auto_pack": "true", "delete_originals": "true", "skip_existing": "false"}
    assert not result["deleteDisabled"]


def test_async_form_population_resynchronizes_dependent_checkbox():
    assert ui("loaded-dependency") == {"checked": True, "disabled": False}


def test_disabling_auto_pack_persists_delete_originals_false():
    body = ui("disable-dependency")["body"]
    assert body == {"auto_pack": "false", "delete_originals": "false"}


def test_late_load_preserves_the_implicit_uncheck_after_pack_off_then_on():
    assert ui("late-pack-toggle") == {"checked": False, "body": {"delete_originals": "false"}}


@pytest.mark.parametrize(("scenario", "gets"), [("import-refresh", 2), ("import-retry", 3)])
def test_import_reloads_baseline_and_keeps_only_edits_made_since_import(scenario, gets):
    result = ui(scenario)
    assert result["beforeLoad"] == 0 and result["gets"] == gets
    assert result["fields"] == {"timeout": "52", "retry": "0", "skip": False}
    assert result["body"] == {"retry_times": "0", "skip_existing": "false"}
    assert result["again"] is None


def test_existing_socks_proxy_does_not_block_saving_and_apply_warning_is_visible():
    result = ui("socks-warning")
    assert result["other"] == {"retry_times": "0"}
    assert result["body"] == {"proxy": "socks5h://127.0.0.1:7900"}
    assert result["toasts"][-1]["type"] == "warning"


@pytest.mark.parametrize("edition", ["atelier", "classic"])
def test_password_visibility_does_not_submit_a_mask_or_erase_the_saved_proxy(edition):
    assert ui('masked-proxy', edition=edition) == {
        'revealed': True, 'masked': True, 'body': {'retry_times': '0'}, 'direct': {'proxy': ''}
    }


@pytest.mark.parametrize('theme', ('sage', 'mint', 'paper', 'linen', 'apricot', 'night', 'pixel', 'cyber', 'anime'))
def test_preferences_render_saved_palette_and_zero_without_embedding_proxy(client, theme):
    from core.settings import update_settings
    update_settings({'ui_theme': theme, 'retry_times': 0, 'proxy': 'socks5h://fixture-user:fixture-secret@localhost'})
    html = client.get('/settings').get_data(as_text=True)
    assert 'fixture-secret' not in html and 'fixture-user' not in html
    assert 'type="password" id="proxy"' in html and re.search(r'id="retry_times"[^>]*value="0"', html)
    cards = dict(re.findall(r'data-pref-theme="(\w+)" aria-pressed="(true|false)"', html))
    assert cards == {key: str(key == theme).lower()
                     for key in ('sage', 'mint', 'paper', 'linen', 'apricot', 'night', 'pixel', 'cyber', 'anime')}
    assert 'id="ui-fx-enabled-toggle"' in html and 'id="pref-reset-appearance-btn"' in html
    for anchor in re.findall(r'href="#([\w-]+)"', html.split('<nav class="pref-index"')[1].split('</nav>')[0]):
        assert html.count(f'id="{anchor}"') == 1


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


def test_stale_settings_tab_preserves_other_tabs_change_after_restart(tmp_path):
    home = tmp_path / "two-tabs"
    loaded = isolated_settings_process(home, "read")["body"]["settings"]
    assert (loaded["timeout"], loaded["retry_times"]) == ("30", "2")
    first = tab_patch(loaded, timeout="47")
    stale = tab_patch(loaded, retry_times="0")
    for patch in (first, stale):
        assert isolated_settings_process(home, "save", patch)["http"] == 200
    restarted = isolated_settings_process(home, "read")["body"]["settings"]
    assert (restarted["timeout"], restarted["retry_times"]) == ("47", "0")
    assert first == {"timeout": "47"}
    assert stale == {"retry_times": "0"}


def test_stale_pack_off_also_clears_deletion_enabled_by_another_tab(client, monkeypatch):
    from routes import api_settings
    monkeypatch.setattr(api_settings, "sync_scheduler", lambda: None)

    def apply(patch):
        response = client.post("/api/settings", json=patch)
        assert response.status_code == 200
        return response.get_json()["settings"]

    loaded = apply({"auto_pack": True, "delete_originals": False})
    enable_deletion = tab_patch(loaded, delete_originals=True)
    assert enable_deletion == {"delete_originals": "true"}
    assert apply(enable_deletion)["delete_originals"] == "true"

    unrelated = tab_patch(loaded, retry_times="0")
    assert unrelated == {"retry_times": "0"}
    assert apply(unrelated)["delete_originals"] == "true"
    assert tab_patch(loaded) is None

    disable_pack = tab_patch(loaded, auto_pack=False)
    disabled = apply(disable_pack)
    reenable = tab_patch(disabled, auto_pack=True)
    final = apply(reenable)
    assert (final["auto_pack"], final["delete_originals"]) == ("true", "false")
    assert disable_pack == {"auto_pack": "false", "delete_originals": "false"}
    assert disabled["delete_originals"] == "false"
    assert reenable == {"auto_pack": "true"}


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
    response = client.post("/api/settings", json={"schedule_start": "6"})
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "ok" and body["saved"] is True
    assert body["warnings"] == ["定时调度未能立即应用，请重启程序后检查"]
    assert client.get("/api/settings").get_json()["settings"]["schedule_start"] == "6"


def test_failed_validation_is_not_committed_and_read_response_is_not_cached(client):
    response = client.get("/api/settings")
    before = response.get_json()["settings"]
    assert response.headers["Cache-Control"] == "no-store"
    assert client.post("/api/settings", json={"timeout": "47", "retry_times": "bad"}).status_code == 400
    assert client.get("/api/settings").get_json()["settings"] == before
