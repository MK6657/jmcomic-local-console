"""Real SSE client code under deterministic EventSource/timer substitutes."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
SCRIPT = r"""
const fs=require('fs');const sources=[], timers=new Map();let next=1;
global.window=global;const lifecycle={};window.addEventListener=(k,f)=>lifecycle[k]=f;
global.document={querySelectorAll:()=>[],getElementById:()=>null};global.showToast=()=>{};
global.setTimeout=(f,ms)=>{const n=next++;timers.set(n,f);return n;};
global.clearTimeout=n=>timers.delete(n);
global.EventSource=class {constructor(url){this.url=url;this.handlers={};this.closed=false;sources.push(this);}
addEventListener(k,f){this.handlers[k]=f;}close(){this.closed=true;}};
eval(fs.readFileSync(process.argv[1],'utf8'));
const scenario=process.argv[2];
if(scenario==='sync'){
 syncSSE(['a','b']);syncSSE(['a','b']);const kept=sources.length;
 syncSSE(['b','c']);console.log(JSON.stringify({kept,total:sources.length,aClosed:sources[0].closed,bKept:!sources[1].closed}));
}else if(scenario==='leave'){
 syncSSE(['a']);sources[0].onerror();const queued=timers.size;
 disconnectAllSSE();for(const f of [...timers.values()])f();
 console.log(JSON.stringify({queued,pending:timers.size,total:sources.length}));
}else if(scenario==='replace'){
 syncSSE(['a']);sources[0].onerror();syncSSE(['b']);for(const f of [...timers.values()])f();
 console.log(JSON.stringify({pending:timers.size,urls:sources.map(s=>s.url)}));
}else if(scenario==='capacity'){
 syncSSE(Array.from({length:60},(_,i)=>'job'+i));console.log(JSON.stringify({total:sources.length}));
}else if(scenario==='backoff'){
 syncSSE(['a']);sources[0].onerror();for(let i=0;i<5;i++)syncSSE(['a']);
 console.log(JSON.stringify({pending:timers.size,total:sources.length}));
}
"""


def run(scenario):
    if NODE is None:
        pytest.skip("Node required")
    result = subprocess.run([NODE, "-e", SCRIPT, str(ROOT / "static/js/sse-client.js"), scenario],
                            capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_polling_reuses_healthy_streams_and_only_replaces_changed_jobs():
    assert run("sync") == {"kept": 2, "total": 3, "aClosed": True, "bKept": True}


def test_page_exit_cancels_reconnect_timer():
    assert run("leave") == {"queued": 1, "pending": 0, "total": 1}


def test_changing_page_does_not_revive_an_old_task_stream():
    assert run("replace") == {"pending": 0, "urls": ["/api/jobs/a/events", "/api/jobs/b/events"]}


def test_browser_limits_live_task_streams_to_five():
    assert run("capacity") == {"total": 5}


def test_polling_does_not_override_a_reconnect_backoff():
    assert run("backoff") == {"pending": 1, "total": 1}


@pytest.mark.skipif(NODE is None, reason="Node required")
def test_home_canceled_event_is_terminal_across_visibility_and_page_restore():
    subprocess.run([NODE, str(ROOT / "tests/js/home_sse.cjs")], check=True, timeout=15)
