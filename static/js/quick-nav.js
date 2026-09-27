/**
 * 右下角快捷导航（base.html 的 #quick-nav；阅读页、单页预览有自己的底部工具，不显示）。
 * 顶部导航不吸顶，停在长列表中间时不用先滚回最上方就能去别处。
 *
 *   去处：首页 / 搜索 / 下载管理 / 收藏 / 资源库 / 设置，与顶部导航相同。搜索、收藏、资源库带 data-nav-memory，
 *     和顶部导航一样回到上次的搜索结果、筛选、页码和位置（nav-memory.js）。
 *   返回：回到上一次用快捷导航跳走之前的页面和那一刻所在的位置。连续跳了几次就按相反顺序一步步退回
 *     （本标签页、12 小时内、最多 MAX_STACK 步），退完即不可用；“返回”本身不记一步，不会在两页之间来回。
 *     上一条浏览历史正是跳走前那一页时用浏览器后退（不多出历史记录，页面自己的“返回”和浏览器后退照常），
 *     否则打开那个地址。经浏览器后退等其他方式已经回到那一页的，那一步作废。
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
      savedAt: Date.now()
    });
    writeStack(stack);
  }

  /** 已经经浏览器后退等方式回到了最近一步所在的那条历史或那个地址：那一步作废（“返回”到这里毫无意义） */
  function prune() {
    var stack = readStack();
    var key = historyKey();
    var changed = false;
    while (stack.length) {
      var top = stack[stack.length - 1];
      var same = (!!top.key && top.key === key) || top.url === currentUrl();
      if (!same) break;
      stack.pop();
      changed = true;
    }
    if (changed) writeStack(stack);
    return stack;
  }

  /** 上一条浏览历史就是 entry 记下的那一条（能直接后退过去） */
  function previousEntryIs(entry) {
    try {
      var nav = window.navigation;
      if (!entry.key || !nav || !nav.currentEntry || typeof nav.entries !== 'function') return false;
      var index = nav.currentEntry.index;
      var previous = index > 0 ? nav.entries()[index - 1] : null;
      if (!previous || previous.key !== entry.key) return false;
      var url = new URL(previous.url, window.location.href);
      return url.origin === window.location.origin && url.pathname + url.search === entry.url;
    } catch (_) { return false; }
  }

  /** 返回：取出最近一步，回到那个地址和位置；没有可返回的 → false */
  function goBack() {
    var stack = prune();
    var entry = stack.pop();
    if (!entry) return false;
    writeStack(stack);
    var memory = nm();
    if (memory && memory.expectReturn) memory.expectReturn(entry.url, entry.y, entry.listTop);
    if (previousEntryIs(entry)) window.history.back();
    else window.location.assign(entry.url);
    return true;
  }

  // 供测试与控制台查看（页面本身不依赖）
  window.quickNav = { remember: remember, goBack: goBack, prune: prune, readStack: readStack, MAX_STACK: MAX_STACK };

  // ── 界面 ──
  var root = document.getElementById('quick-nav');
  if (!root) return;
  var toggle = root.querySelector('.quick-nav-toggle');
  var panel = root.querySelector('.quick-nav-panel');
  var backBtn = root.querySelector('[data-quick-action="back"]');
  var topBtn = root.querySelector('[data-quick-action="top"]');
  var bottomBtn = root.querySelector('[data-quick-action="bottom"]');
  if (!toggle || !panel) return;

  var openedBy = null;   // null 收起 / 'hover' 鼠标移上去展开 / 'click' 点按钮展开（保持）
  var closeTimer = null;

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
      setDisabled(backBtn, !entry);
      var label = entry ? '返回：' + entry.title : '返回（还没有用快捷导航跳转过）';
      backBtn.title = label;
      backBtn.setAttribute('aria-label', label);
    }
  }

  function setOpen(how) {
    clearTimeout(closeTimer);
    openedBy = how || null;
    root.classList.toggle('is-open', !!openedBy);
    toggle.setAttribute('aria-expanded', openedBy ? 'true' : 'false');
    if (openedBy) refresh();
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
  root.addEventListener('pointerleave', function (event) {
    if (event.pointerType !== 'mouse' || openedBy !== 'hover') return;
    closeTimer = setTimeout(function () {
      // 正用键盘在面板里操作时不收起
      if (openedBy === 'hover' && !root.querySelector(':focus-visible')) setOpen(null);
    }, HOVER_CLOSE_MS);
  });

  // Esc 收起；焦点在面板里时交还给按钮
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape' || !openedBy) return;
    var inside = root.contains(document.activeElement);
    setOpen(null);
    if (inside) toggle.focus();
  });
  // 点别处收起
  document.addEventListener('pointerdown', function (event) {
    if (openedBy && !root.contains(event.target)) setOpen(null);
  }, true);
  // 键盘 Tab 离开面板时收起
  root.addEventListener('focusout', function (event) {
    if (openedBy && event.relatedTarget && !root.contains(event.relatedTarget)) setOpen(null);
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
    if (!btn || isDisabled(btn)) return;
    var action = btn.getAttribute('data-quick-action');
    if (action === 'back') {
      goBack();
    } else if (action === 'top' || action === 'bottom') {
      // instant：Bootstrap 的 :root { scroll-behavior: smooth } 会让它慢慢滑过去
      window.scrollTo({ top: action === 'top' ? 0 : maxScroll(), behavior: 'instant' });
      refresh();
    }
  });

  // 展开时跟着滚动更新“到顶 / 到底”能不能用
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

  // 离开时收起（进入往返缓存的页面回来时是收起的）；回来时重新判断“返回”
  window.addEventListener('pagehide', function () { setOpen(null); });
  window.addEventListener('pageshow', function () { refresh(); });
  refresh();
})();
