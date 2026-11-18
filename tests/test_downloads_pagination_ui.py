"""Paginated downloads use real JS with the repository's small DOM substitute."""
from pathlib import Path

from test_update_checks_ui import FAKE_DOM, _node

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = FAKE_DOM + r"""
const [utilsPath,downloadsPath]=process.argv.slice(1);
for(const id of ['downloadTabs','downloadTabsContent','running-section','queued-section','completed-section','failed-section',
 'running-count','queued-count','completed-count','failed-count','running-pagination','queued-pagination','completed-pagination','failed-pagination']){
 const n=el('div');n.id=id;body.appendChild(n);
}
const tabs=document.getElementById('downloadTabs'),content=document.getElementById('downloadTabsContent');
for(const g of ['running','queued','completed','failed'])content.appendChild(document.getElementById(g+'-pagination'));
let poll=null;const calls=[],held=[],synced=[];
global.setInterval=f=>{poll=f;return 1;};global.clearInterval=()=>{poll=null;};
global.showToast=()=>{};global.confirm=()=>true;window._jobTitleMap={};
window.setSSECallbacks=()=>{};window.disconnectAllSSE=()=>{};window.syncSSE=ids=>synced.push(ids);
eval(fs.readFileSync(utilsPath,'utf8'));
const counts={active:7,queued:60,completed:120,failed:80,canceled:3};
const job=(id,status,latest)=>({job_id:id,album_id:'123',title:id,status,total_pages:1,done_pages:0,
 latest_completed_job_id:latest,maintenance:status==='completed'?{state:'failed',error:'archive <locked>'}:null});
function answer(url){const u=new URL(url,'http://fixture');const group=u.searchParams.get('status');const p=Number(u.searchParams.get('page'));
 const jobs=group==='active'?Array.from({length:7},(_,i)=>job('run'+i,'running')):
 group==='completed'?[job(p===1?'new':'old','completed','new')]:[job(group+p,group)];
 return {status:'ok',jobs,counts,total:group==='active'?7:counts[group],page:p,page_size:50,pages:group==='completed'?3:2};}
window.apiFetch=global.apiFetch=(url,opts={})=>{
 calls.push({url,method:opts.method||'GET'});
 if(url.startsWith('/api/jobs?'))return new Promise(resolve=>held.push({url,resolve}));
 if(url==='/api/preview/available')return Promise.resolve({status:'ok',readable:[],local:{}});
 if(url==='/api/updates/states')return Promise.resolve({status:'ok',updates:{}});
 return Promise.resolve({status:'ok'});
};
eval(fs.readFileSync(downloadsPath,'utf8'));
function tab(group){const target=el('button');target.id=group+'-tab';(tabs.handlers['shown.bs.tab']||[]).forEach(f=>f({target}));}
function deliver(index){held[index].resolve(answer(held[index].url));}
function page(number){const button=document.getElementById('completed-pagination').children.find(n=>n.getAttribute('data-page')===String(number));
 if(!button)throw Error('missing pager');(content.handlers.click||[]).forEach(f=>f({target:button}));}
(async()=>{
 (docHandlers.DOMContentLoaded||[]).forEach(f=>f());deliver(0);await flush();
 const initial={requests:calls.filter(c=>c.url.startsWith('/api/jobs?')).map(c=>c.url),runningCount:document.getElementById('running-count').textContent,
 synced:synced[0]};
 tab('completed');deliver(1);await flush();page(2);deliver(2);await flush();
 const second={url:held[2].url,html:document.getElementById('completed-section').innerHTML,pager:document.getElementById('completed-pagination').textContent};
 poll();tab('failed');deliver(4);await flush();deliver(3);await flush();
 const afterLate={failed:document.getElementById('failed-section').innerHTML,completed:document.getElementById('completed-section').innerHTML};
 tab('completed');deliver(5);await flush();
 const mutations=calls.filter(c=>c.method!=='GET'&&/\/api\/(jobs|albums)\//.test(c.url));
 const retry=el('button');retry.setAttribute('data-action','retry-maintenance');retry.setAttribute('data-album-id','123');content.appendChild(retry);
 (content.handlers.click||[]).forEach(f=>f({target:retry}));await flush();
 console.log(JSON.stringify({initial,second,afterLate,returnedUrl:held[5].url,mutations,retried:calls.filter(c=>c.url.endsWith('/retry'))}));
})().catch(e=>{console.error(e);process.exit(1)});
"""


def test_downloads_page_counts_navigation_late_responses_and_read_only_polling():
    result = _node(SCRIPT, ROOT / "static/js/utils.js", ROOT / "static/js/downloads.js")
    assert result["initial"]["runningCount"] == "7"
    assert result["initial"]["requests"] == ["/api/jobs?page=1&page_size=50&status=active"]
    assert len(result["initial"]["synced"]) == 5
    assert "page=2" in result["second"]["url"] and "status=completed" in result["second"]["url"]
    assert 'data-job-id="old"' in result["second"]["html"]
    assert "第 2/3 页" in result["second"]["pager"]
    assert 'data-job-id="failed1"' in result["afterLate"]["failed"]
    assert result["afterLate"]["completed"] == result["second"]["html"]
    assert "page=2" in result["returnedUrl"]
    assert result["mutations"] == []
    assert '整册整理失败' in result['second']['html'] and 'archive &lt;locked&gt;' in result['second']['html']
    assert 'data-action="retry-maintenance"' in result['second']['html']
    assert result['retried'] == [{'url': '/api/albums/123/maintenance/retry', 'method': 'POST'}]
