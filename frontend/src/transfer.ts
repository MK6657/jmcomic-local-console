import { createApp, h, onMounted, onUnmounted, shallowRef } from 'vue';
import './transfer.css';

// Counts belong to the same server snapshot as the visible queue, even when the user pages back.
// Missing/old API responses must not present unknown counts as zero; no second poll or SSE here.
const states = [ ['running', '下载中'], ['queued', '排队'], ['paused', '已暂停'], ['completed', '已完成'] ] as const;
type Counts = Record<typeof states[number][0], number>;
function init() {
  const container = document.getElementById('atelier-transfer-summary');
  if (!container) return;
  createApp({setup() {
    const counts = shallowRef<Counts | null>(null);
    function update(event: Event) {
      const payload = (event as CustomEvent).detail;
      if (payload?.status !== 'ok') return;
      const c = payload.counts;
      counts.value = c && states.every(([key]) => Number.isSafeInteger(c[key]) && c[key] >= 0) ? c : null;
    }
    onMounted(() => document.addEventListener('atelier-jobs-summary', update));
    onUnmounted(() => document.removeEventListener('atelier-jobs-summary', update));
    return () => counts.value ? h('nav', {class:'transfer-ledger', 'aria-label':'全部任务状态统计'}, states.map(([key,label]) => h('a', {key, href:'#'+key, 'aria-label':`${label} ${counts.value![key]} 项，查看任务`}, [h('strong',String(counts.value![key])),h('span',label)]))) : null;
  }}).mount(container);
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once:true}); else init();
