"""Paginated downloads use real JS with the repository's small DOM substitute."""
from pathlib import Path

from test_update_checks_ui import FAKE_DOM, _node

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = FAKE_DOM + r"""
const [utilsPath,downloadsPath]=process.argv.slice(1);
for(const id of ['downloadTabs','downloadTabsContent','folio-list','folio-pagination']){
 const n=el('div');n.id=id;body.appendChild(n);
}
const tabs=document.getElementById('downloadTabs'),content=document.getElementById('downloadTabsContent');
const list=document.getElementById('folio-list'),pager=document.getElementById('folio-pagination');content.appendChild(pager);
global.location={hash:'#all'};
const summaries=[];document.addEventListener('atelier-jobs-summary',e=>summaries.push(e.detail));
let poll=null;const calls=[],held=[],synced=[];
global.setInterval=f=>{poll=f;return 1;};global.clearInterval=()=>{poll=null;};
global.showToast=()=>{};global.confirm=()=>true;window._jobTitleMap={};
window.setSSECallbacks=()=>{};window.disconnectAllSSE=()=>{};window.syncSSE=ids=>synced.push(ids);
eval(fs.readFileSync(utilsPath,'utf8'));
const counts={running:7,paused:1,active:8,queued:60,completed:120,failed:80,canceled:3};
const job=(id,status,latest)=>({job_id:id,album_id:'123',title:id,status,total_pages:1,done_pages:0,
 latest_completed_job_id:latest,maintenance:status==='completed'?{state:'failed',error:'archive <locked>'}:null});
function answer(url){const u=new URL(url,'http://fixture');const group=u.searchParams.get('status');const p=Number(u.searchParams.get('page'));
 const jobs=group==='all'?[job('A','paused'),{...job('B','completed'),selected_photo_ids:['12'],output_path:'test'},
 ...Array.from({length:7},(_,i)=>({...job('run'+i,'running'),album_id:String(901+i)}))]:
 group==='completed'?[job(p===1?'new':'old','completed','new')]:[job(group+p,group)];
 return {status:'ok',jobs,counts,total:group==='all'?8:counts[group],page:p,page_size:50,pages:group==='completed'?3:2};}
window.apiFetch=global.apiFetch=(url,opts={})=>{
 calls.push({url,method:opts.method||'GET'});
 if(url.startsWith('/api/jobs?'))return new Promise(resolve=>held.push({url,resolve}));
 if(url==='/api/preview/available')return Promise.resolve({status:'ok',readable:['123'],local:{'123':{state:'loose'}}});
 if(url==='/api/updates/states')return Promise.resolve({status:'ok',updates:{}});
 return Promise.resolve({status:'ok'});
};
eval(fs.readFileSync(downloadsPath,'utf8'));
function tab(group){location.hash='#'+group;winHandlers.hashchange.forEach(f=>f());}
function deliver(index){held[index].answer=answer(held[index].url);held[index].resolve(held[index].answer);}
function page(number){const button=pager.children.find(n=>n.getAttribute('data-page')===String(number));
 if(!button)throw Error('missing pager');(content.handlers.click||[]).forEach(f=>f({target:button}));}
(async()=>{
 (docHandlers.DOMContentLoaded||[]).forEach(f=>f());deliver(0);await flush();
 const initial={requests:calls.filter(c=>c.url.startsWith('/api/jobs?')).map(c=>c.url),runningCount:summaries[0].counts.running,
 sameResponse:summaries[0]===held[0].answer,html:list.innerHTML,synced:synced[0]};
 tab('completed');deliver(1);await flush();page(2);deliver(2);await flush();
 const second={url:held[2].url,html:list.innerHTML,pager:pager.textContent};
 poll();tab('failed');deliver(4);await flush();const beforeLate=list.innerHTML;deliver(3);await flush();
 const afterLate={failed:list.innerHTML,unchanged:beforeLate===list.innerHTML&&!summaries.includes(held[3].answer)};
 tab('completed');deliver(5);await flush();
 const mutations=calls.filter(c=>c.method!=='GET'&&/\/api\/(jobs|albums)\//.test(c.url));
 const retry=el('button');retry.setAttribute('data-action','retry-maintenance');retry.setAttribute('data-album-id','123');content.appendChild(retry);
 (content.handlers.click||[]).forEach(f=>f({target:retry}));await flush();
 console.log(JSON.stringify({initial,second,afterLate,returnedUrl:held[5].url,mutations,retried:calls.filter(c=>c.url.endsWith('/retry'))}));
})().catch(e=>{console.error(e);process.exit(1)});
"""


def test_downloads_page_counts_navigation_late_responses_and_read_only_polling():
    result = _node(SCRIPT, ROOT / "static/js/utils.js", ROOT / "static/js/downloads.js")
    assert result["initial"]["runningCount"] == 7 and result['initial']['sameResponse']
    assert result["initial"]["requests"] == ["/api/jobs?page=1&page_size=8&status=all&view=folios"]
    folio = result['initial']['html'].split('</article>')[0]
    assert 'data-job-id="A"' in folio and 'data-job-id="B"' in folio and 'data-job-id="run0"' not in folio
    assert all(f'data-action="{action}"' in folio for action in ('resume','cancel','read-chapter','export-zip','export-pdf'))
    assert 'data-job-id="run0"' in result['initial']['html'] and 'data-action="pause"' in result['initial']['html']
    assert len(result["initial"]["synced"]) == 5
    assert "page=2" in result["second"]["url"] and "status=completed" in result["second"]["url"]
    assert 'data-job-id="old"' in result["second"]["html"]
    assert "第 2/3 页" in result["second"]["pager"]
    assert 'data-job-id="failed1"' in result["afterLate"]["failed"]
    assert result["afterLate"]["unchanged"]
    assert "page=2" in result["returnedUrl"]
    assert result["mutations"] == []
    assert '整册整理失败' in result['second']['html'] and 'archive &lt;locked&gt;' in result['second']['html']
    assert 'data-action="retry-maintenance"' in result['second']['html']
    assert result['retried'] == [{'url': '/api/albums/123/maintenance/retry', 'method': 'POST'}]
