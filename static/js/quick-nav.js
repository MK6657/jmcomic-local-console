/**
 * 右下角快捷导航（base.html 的 #quick-nav；阅读页、单页预览有自己的底部工具，不显示）。
 * 顶部导航不吸顶，停在长列表中间时不用先滚回最上方就能去别处。
 *
 *   去处：首页 / 搜索 / 下载管理 / 收藏 / 资源库 / 设置，与顶部导航相同。搜索、收藏、资源库带 data-nav-memory，
 *     和顶部导航一样回到上次的搜索结果、筛选、页码和位置（nav-memory.js）。
 *   返回：回到上一次用快捷导航跳走之前的页面和那一刻所在的位置。连续跳了几次就按相反顺序一步步退回
 *     （本标签页、12 小时内、最多 MAX_STACK 步），退完即不可用；“返回”本身不记一步，不会在两页之间来回。
 *     跳走前那条浏览历史还在身后时直接退回到它（浏览器后退 / Navigation API traverseTo：不多出历史记录，
 *     页面自己的“返回”和浏览器后退照常），否则打开那个地址。经浏览器后退（含历史菜单一次跳过几页）等其他方式
 *     已经回到那一步之前的，那一步和它之后的步都作废。连点“返回”只算一次。
 *   到顶 / 到底：直接跳到本页最上 / 最下（不做滚动动画），不改地址和筛选，也不影响“返回”；已在最上 / 最下时不可用。
 *
 * 打开方式：鼠标移上去展开、移开收起；点按钮（触屏、键盘）展开并保持，再点、Esc、点别处或焦点离开时收起。
 * 在上面点击不算“在这里做事”（nav-memory.js inNav），打开它不会改掉记下的阅读位置。
 */
(function () {
  'use strict';

  var STACK_KEY = 'jm-quick-nav-back-v1'; // [{url, y, listTop, title, key, savedAt}, …]，最后一个是最近一次跳走的地方
  var MAX_STACK = 10;
  var HOVER_CLOSE_MS = 300;               // 鼠标移开后稍等再收起，斜着移到面板上不会闪
  var SITE_SUFFIX = /\s*-\s*JMComic 下载控制台\s*$/;  // “资源库 - JMComic 下载控制台”
  var SITE_PREFIX = /^\s*JMComic 下载控制台\s*-\s*/;  // “JMComic 下载控制台 - 设置”
  var TITLE_MAX = 40;

  function nm() { return window.navMemory || null; }

  function maxAge() {
    var memory = nm();
    return (memory && memory.MAX_AGE) || 12 * 60 * 60 * 1000;
  }

  function currentUrl() { return window.location.pathname + window.location.search; }

  /** 只接受站内地址：以单个 / 开头，没有 //主机、反斜杠、空白 */
  function isLocalUrl(url) {
    return typeof url === 'string' && url.charAt(0) === '/' && url.charAt(1) !== '/' && !/[\s\\]/.test(url);
  }

  function readStack() {
    var list;
    try { list = JSON.parse(window.sessionStorage.getItem(STACK_KEY)); } catch (_) { list = null; }
    if (!Array.isArray(list)) return [];
    var now = Date.now();
    var limit = maxAge();
    return list.filter(function (entry) {
      return entry && typeof entry === 'object' && isLocalUrl(entry.url) && typeof entry.savedAt === 'number'
        && now - entry.savedAt >= 0 && now - entry.savedAt < limit;
    });
  }

  function writeStack(list) {
    try { window.sessionStorage.setItem(STACK_KEY, JSON.stringify(list.slice(-MAX_STACK))); } catch (_) { /* 存不下：不记 */ }
  }

  /** 这条浏览历史的标识（Navigation API；没有时为 null） */
  function historyKey() {
    try {
      var nav = window.navigation;
      return nav && nav.currentEntry ? nav.currentEntry.key || null : null;
    } catch (_) { return null; }
  }

  // 没有 Navigation API 的浏览器：给本标签页的每条浏览历史一个递增的序号，存在 history.state 里
  // （后退/前进时浏览器连同 state 一起恢复），用来判断一步是在当前这条之前还是之后
  var SEQ_KEY = 'jm-quick-nav-seq';

  function entrySeq() {
    try {
      var state = window.history.state;
      return state && typeof state.jmQuickNavSeq === 'number' ? state.jmQuickNavSeq : null;
    } catch (_) { return null; }
  }

  function ensureSeq() {
    if (entrySeq() !== null) return;
    try {
      var next = (Number(window.sessionStorage.getItem(SEQ_KEY)) || 0) + 1;
      window.sessionStorage.setItem(SEQ_KEY, String(next));
      window.history.replaceState(Object.assign({}, window.history.state, { jmQuickNavSeq: next }), '');
    } catch (_) { /* 存不下 / 不允许：只靠地址判断 */ }
  }
  ensureSeq();

  /** “返回：…”里显示的页面名称：搜索带关键词，详情带漫画标题 */
  function pageTitle() {
    var title = String(document.title || '').replace(SITE_SUFFIX, '').replace(SITE_PREFIX, '').trim()
      || window.location.pathname;
    var path = window.location.pathname;
    if (path === '/search') {
      var keyword = '';
      try { keyword = new URLSearchParams(window.location.search).get('keyword') || ''; } catch (_) { /* 忽略 */ }
      if (keyword) title = '搜索“' + keyword + '”';
    } else if (path.indexOf('/album/') === 0) {
      var heading = document.querySelector('#album-content .card-title');
      var name = heading ? heading.textContent.trim() : '';
      title = name ? '详情“' + name + '”' : '详情 ' + title;
    }
    return title.length > TITLE_MAX ? title.slice(0, TITLE_MAX - 1) + '…' : title;
  }

  /** 用快捷导航跳走前记下这里：地址、此刻所在的位置、列表位置、页面名称 */
  function remember() {
    var memory = nm();
    var stack = readStack();
    stack.push({
      url: currentUrl(),
      y: Math.max(0, Math.round(window.scrollY || 0)),
      listTop: memory ? memory.listTopNow() : null,
      title: pageTitle(),
      key: historyKey(),
      seq: entrySeq(),
      savedAt: Date.now()
    });
    writeStack(stack);
  }

  /** 本标签页的浏览历史（Navigation API）：{ nav, current: 当前这条的位置, at: 标识 → 位置 }；没有时为 null */
  function historyMap() {
    try {
      var nav = window.navigation;
      if (!nav || !nav.currentEntry || typeof nav.entries !== 'function') return null;
      var at = {};
      nav.entries().forEach(function (entry, index) { if (entry && entry.key) at[entry.key] = index; });
      return { nav: nav, current: nav.currentEntry.index, at: at };
    } catch (_) { return null; }
  }

  /**
   * 作废已经不在“身后”的步：
   *   1. 记下的那条历史就是当前这条或在它之后——用户已经经浏览器后退（含历史菜单一次跳过几页）回到了它之前，
   *      这一步和它之后记的步都不再是“跳走前”的地方（否则“返回”会往前跳到后来才去的页面，再在两页之间来回）；
   *   2. 最近一步就是当前这条历史或当前这个地址：已经回到那里了，“返回”到这里毫无意义。
   */
  function prune() {
    var stack = readStack();
    var before = stack.length;
    var history = historyMap();
    var seq = history ? null : entrySeq();
    for (var i = 0; i < stack.length; i++) {
      var step = stack[i];
      var ahead = history
        ? !!step.key && Object.prototype.hasOwnProperty.call(history.at, step.key) && history.at[step.key] >= history.current
        : seq !== null && typeof step.seq === 'number' && step.seq >= seq;  // 没有 Navigation API：按序号比
      if (ahead) {
        stack = stack.slice(0, i);
        break;
      }
    }
    var currentKey = historyKey();
    while (stack.length) {
      var top = stack[stack.length - 1];
      if (!((!!top.key && top.key === currentKey) || top.url === currentUrl())) break;
      stack.pop();
    }
    if (stack.length !== before) writeStack(stack);
    return stack;
  }

  /** entry 记下的那条历史还在、在当前这条之前、地址也没变：返回它的标识（可以直接退回去），否则 null */
  function earlierEntryKey(entry, history) {
    if (!entry.key || !history || !Object.prototype.hasOwnProperty.call(history.at, entry.key)) return null;
    var index = history.at[entry.key];
    if (index >= history.current) return null;
    try {
      var url = new URL(history.nav.entries()[index].url, window.location.href);
      return url.origin === window.location.origin && url.pathname + url.search === entry.url ? entry.key : null;
    } catch (_) { return null; }
  }

  /**
   * 返回：取出最近一步，回到那个地址和位置；没有可返回的 → false。
   * 那条历史还在身后就退回到它（浏览器后退 / traverseTo：不多出历史记录，页面恢复原样，页面自己的“返回”照常），
   * 否则打开那个地址。
   */
  function goBack() {
    var stack = prune();
    var entry = stack.pop();
    if (!entry) return false;
    writeStack(stack);
    var memory = nm();
    if (memory && memory.expectReturn) memory.expectReturn(entry.url, entry.y, entry.listTop);
    var history = historyMap();
    var key = earlierEntryKey(entry, history);
    try {
      if (key && history.at[key] === history.current - 1) {
        window.history.back();
        return true;
      }
      if (key && typeof history.nav.traverseTo === 'function') {
        var result = history.nav.traverseTo(key);
        // 被随后的另一次跳转取消等：不再补救（否则会盖掉用户新点的去处），也不留未处理的 Promise 拒绝
        if (result && result.committed) result.committed.catch(function () {});
        if (result && result.finished) result.finished.catch(function () {});
        return true;
      }
    } catch (_) { /* 退不回去：打开那个地址 */ }
    window.location.assign(entry.url);
    return true;
  }

  // 供测试与控制台查看（页面本身不依赖）
  window.quickNav = { remember: remember, goBack: goBack, prune: prune, readStack: readStack, MAX_STACK: MAX_STACK };

  // ── 界面 ──
  var root = document.getElementById('quick-nav');
  if (!root) {
    // 阅读页 / 在线阅读 / 单页预览没有界面：经历史菜单一次跳过几页回到这里时，同样作废已经越过的步
    prune();
    window.addEventListener('pageshow', function (event) { if (event && event.persisted) prune(); });
    return;
  }
  var toggle = root.querySelector('.quick-nav-toggle');
  var panel = root.querySelector('.quick-nav-panel');
  var backBtn = root.querySelector('[data-quick-action="back"]');
  var topBtn = root.querySelector('[data-quick-action="top"]');
  var bottomBtn = root.querySelector('[data-quick-action="bottom"]');
  if (!toggle || !panel) return;

  var openedBy = null;   // null 收起 / 'hover' 鼠标移上去展开 / 'click' 点按钮展开（保持）
  var closeTimer = null;
  var leaving = false;   // 刚点了“返回”、页面正要跳走：连点 / 双击不再取出下一步
  var leavingTimer = null;
  var LEAVING_RESET_MS = 4000; // 跳转被取消（停止加载等）时过这么久“返回”恢复可用
  var pressing = false;        // 正在快捷导航上按着（按下到这次点击结束）：焦点暂时落到 body 不算离开
  var pressTimer = null;
  var PRESS_GRACE_MS = 1000;   // 抬起后没有等来点击（拖出去了等）最多再算这么久
  // 面板展开时 Toast 让到它上方（style.css）：在 Toast 上按下、移上去、把焦点移过去都不算离开快捷导航，
  // 否则面板一收起 Toast 就落回原处，正要点的 × 从指针下面跑掉
  var toasts = document.getElementById('toast-container');

  function inToasts(node) { return !!(toasts && node && toasts.contains(node)); }

  function maxScroll() {
    var doc = document.documentElement;
    return Math.max(0, doc.scrollHeight - window.innerHeight);
  }

  // 不用 disabled：按下“到顶”后按钮变成不可用时键盘焦点会丢到页面开头；aria-disabled 保留焦点
  function setDisabled(btn, disabled) {
    if (!btn) return;
    btn.setAttribute('aria-disabled', disabled ? 'true' : 'false');
  }

  function isDisabled(btn) { return btn.getAttribute('aria-disabled') === 'true'; }

  /** 按当前位置和“返回”记录更新三个按钮 */
  function refresh() {
    var y = window.scrollY || 0;
    setDisabled(topBtn, y <= 1);
    setDisabled(bottomBtn, y >= maxScroll() - 1);
    var stack = prune();
    var entry = stack[stack.length - 1];
    root.classList.toggle('has-back', !!entry);
    if (backBtn) {
      setDisabled(backBtn, leaving || !entry);
      var label = entry ? '返回：' + entry.title : '返回（还没有用快捷导航跳转过）';
      backBtn.title = label;
      backBtn.setAttribute('aria-label', label);
    }
    // Toast 在展开时让到面板上方：style.css 用面板的实际高度（短窗口里面板被限高；窗口大小变了跟着更新）
    if (openedBy) document.documentElement.style.setProperty('--quick-nav-panel-h', panel.offsetHeight + 'px');
  }

  function setOpen(how) {
    clearTimeout(closeTimer);
    openedBy = how || null;
    root.classList.toggle('is-open', !!openedBy);
    toggle.setAttribute('aria-expanded', openedBy ? 'true' : 'false');
    if (openedBy) refresh();
  }

  /** 焦点是不是用键盘停在快捷导航里（不支持 :focus-visible 的浏览器当作不是） */
  function keyboardFocusInside() {
    try { return !!root.querySelector(':focus-visible'); } catch (_) { return false; }
  }

  toggle.addEventListener('click', function () {
    // 鼠标移上去已经展开：点一下是“固定住”，不是收起
    if (openedBy === 'hover') setOpen('click');
    else setOpen(openedBy ? null : 'click');
  });

  root.addEventListener('pointerenter', function (event) {
    if (event.pointerType !== 'mouse') return;
    clearTimeout(closeTimer);
    if (!openedBy) setOpen('hover');
  });
  function closeSoonAfterHover(event) {
    if (event.pointerType !== 'mouse' || openedBy !== 'hover') return;
    if (root.contains(event.relatedTarget) || inToasts(event.relatedTarget)) return; // 移到面板或 Toast 上：还在用
    clearTimeout(closeTimer);
    closeTimer = setTimeout(function () {
      // 正用键盘在面板里操作时不收起
      if (openedBy === 'hover' && !keyboardFocusInside()) setOpen(null);
    }, HOVER_CLOSE_MS);
  }
  root.addEventListener('pointerleave', closeSoonAfterHover);
  if (toasts) {
    toasts.addEventListener('pointerleave', closeSoonAfterHover);
    toasts.addEventListener('pointerenter', function (event) {
      if (event.pointerType === 'mouse' && openedBy === 'hover') clearTimeout(closeTimer);
    });
  }

  // Esc 收起；焦点在面板里时交还给按钮
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape' || !openedBy) return;
    var inside = root.contains(document.activeElement);
    setOpen(null);
    if (inside) toggle.focus();
  });
  // 点别处收起（点 Toast 不算：让它留在原处，关闭按钮点得到）
  document.addEventListener('pointerdown', function (event) {
    if (openedBy && !root.contains(event.target) && !inToasts(event.target)) setOpen(null);
  }, true);
  // 焦点离开快捷导航时收起：Tab 到页面别处（relatedTarget 在外面），或 Tab 出页面、切到别的窗口（relatedTarget 为空）。
  // 在面板空白处按下鼠标 / 手指时焦点也会落到 body（鼠标在按下时，触屏在抬起之后），这次按压到它的点击结束前不算离开
  function pressEnd() {
    pressing = false;
    clearTimeout(pressTimer);
  }
  root.addEventListener('pointerdown', function () {
    pressing = true;
    clearTimeout(pressTimer);
  });
  document.addEventListener('pointerup', function () {  // 可能在面板外抬起（拖出去了）
    if (!pressing) return;
    clearTimeout(pressTimer);
    pressTimer = setTimeout(pressEnd, PRESS_GRACE_MS);
  }, true);
  root.addEventListener('pointercancel', pressEnd);
  root.addEventListener('click', function () {  // 这次点击自己的焦点变化处理完再结束（新的按压会取消它）
    clearTimeout(pressTimer);
    pressTimer = setTimeout(pressEnd, 0);
  });
  root.addEventListener('focusout', function (event) {
    if (!openedBy) return;
    var next = event.relatedTarget;
    if (inToasts(next)) return;  // Tab 到 Toast 的关闭按钮
    if (next ? !root.contains(next) : !pressing) setOpen(null);
  });

  panel.addEventListener('click', function (event) {
    var link = event.target.closest('a[data-quick-dest]');
    if (link) {
      // 在本标签页打开的普通点击才记一步（Ctrl/Shift 等在新标签页/窗口打开，本页不跳走）；去当前页不记
      var plain = !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey && !event.button;
      if (plain && link.getAttribute('data-quick-dest') !== window.location.pathname) remember();
      return; // 链接照常打开（搜索/收藏/资源库的地址已由 nav-memory.js 换成记住的那个）
    }
    var btn = event.target.closest('button[data-quick-action]');
    if (!btn) return;
    var action = btn.getAttribute('data-quick-action');
    // 页面高度可能刚变过（结果刚画出、列表追加了内容）：按当前的位置重新判断能不能用
    if (action === 'top' || action === 'bottom') refresh();
    if (isDisabled(btn)) return;
    if (action === 'back') {
      if (goBack()) {
        leaving = true;
        setDisabled(backBtn, true);
        clearTimeout(leavingTimer);
        leavingTimer = setTimeout(function () { leaving = false; refresh(); }, LEAVING_RESET_MS);
      }
    } else if (action === 'top' || action === 'bottom') {
      // instant：Bootstrap 的 :root { scroll-behavior: smooth } 会让它慢慢滑过去
      window.scrollTo({ top: action === 'top' ? 0 : maxScroll(), behavior: 'instant' });
      refresh();
    }
  });

  // 展开时跟着滚动、窗口大小和页面内容高度的变化更新“到顶 / 到底”能不能用
  var framePending = false;
  function onViewportChange() {
    if (!openedBy || framePending) return;
    framePending = true;
    window.requestAnimationFrame(function () {
      framePending = false;
      refresh();
    });
  }
  window.addEventListener('scroll', onViewportChange, { passive: true });
  window.addEventListener('resize', onViewportChange);
  if (typeof window.ResizeObserver === 'function' && document.body) {
    new window.ResizeObserver(onViewportChange).observe(document.body);
  }

  // 离开时收起（进入往返缓存的页面回来时是收起的）；回来时“返回”恢复可用并重新判断
  window.addEventListener('pagehide', function () { setOpen(null); });
  window.addEventListener('pageshow', function () {
    leaving = false;
    clearTimeout(leavingTimer);
    refresh();
  });
  refresh();
})();
