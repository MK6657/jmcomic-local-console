/**
 * 右下角快捷导航（base.html 的 #quick-nav；阅读页、单页预览有自己的底部工具，不显示）。
 * 顶部导航不吸顶，停在长列表中间时不用先滚回最上方就能去别处。
 *
 *   去处：首页 / 搜索 / 下载管理 / 收藏 / 资源库 / 设置，与顶部导航相同。搜索、收藏、资源库带 data-nav-memory，
 *     和顶部导航一样回到上次的搜索结果、筛选、页码和位置（nav-memory.js）。
 *   返回：回到本标签页里的上一页——和浏览器的后退一样，但只在本程序的页面之间——并回到离开那一页时所在的位置。
 *     经快捷导航、顶部导航还是页面里的链接去的都算；一直按就一页页往回走，不会在两页之间来回。
 *     本标签页里前面没有本程序的页面（直接打开、在新标签页打开），或浏览器太旧没有 Navigation API 时不可用。
 *     提示和读屏名称带上一页的名字。连点只算一次（第二下也不会落到退回去的那一页上）。
 *   到顶 / 到底：直接跳到本页最上 / 最下（不做滚动动画），不改地址和筛选；已在最上 / 最下时不可用。
 *
 * 每个页面离开时记下自己的名字和“看到的位置”（按浏览历史记录的标识存在本标签页，12 小时、最近 MAX_PAGES 条），
 * 下一页的“返回”据此显示“返回：…”，退回去时回到那个位置（nav-memory.js expectReturn / takeReturn）。
 *
 * 打开方式：鼠标移上去展开、移开收起；点按钮（触屏、键盘）展开并保持，再点、Esc、点别处或焦点离开时收起。
 * 在上面点击不算“在这里做事”（nav-memory.js inNav），打开它不会改掉记下的阅读位置。
 */
(function () {
  'use strict';

  var PAGES_KEY = 'jm-quick-nav-pages-v1'; // { 浏览历史记录的标识: {url, title, y, listTop, savedAt} }
  var MAX_PAGES = 50;
  var HOVER_CLOSE_MS = 300;               // 鼠标移开后稍等再收起，斜着移到面板上不会闪
  // “资源库 - JMComic 下载控制台”；阅读页、单页预览由脚本改成“标题 - JMComic”“标题 - JMComic 图片预览”
  var SITE_SUFFIX = /\s*-\s*JMComic(?:\s*(?:下载控制台|图片预览))?\s*$/;
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

  /** 这条浏览历史的标识（Navigation API；没有时为 null） */
  function historyKey() {
    try {
      var nav = window.navigation;
      return nav && nav.currentEntry ? nav.currentEntry.key || null : null;
    } catch (_) { return null; }
  }

  // 阅读页、单页预览：页头里的漫画标题（还没加载出来时是这些占位字样）
  var READER_PAGES = [
    { prefix: '/read/', heading: '#reader-title', label: '阅读', blank: '连续阅读' },
    { prefix: '/online/', heading: '#reader-title', label: '在线阅读', blank: '在线阅读' },
    { prefix: '/preview/', heading: '#album-title', label: '预览', blank: '图片预览' }
  ];

  function headingText(selector) {
    var heading = document.querySelector(selector);
    return heading ? String(heading.textContent || '').trim() : '';
  }

  /** “返回：…”里显示的页面名称：搜索带关键词，详情、阅读、预览带漫画标题 */
  function pageTitle() {
    var title = String(document.title || '').replace(SITE_SUFFIX, '').replace(SITE_PREFIX, '').trim()
      || window.location.pathname;
    var path = window.location.pathname;
    if (path === '/search') {
      var keyword = '';
      try { keyword = new URLSearchParams(window.location.search).get('keyword') || ''; } catch (_) { /* 忽略 */ }
      if (keyword) title = '搜索“' + keyword + '”';
    } else if (path.indexOf('/album/') === 0) {
      var name = headingText('#album-content .card-title');
      title = name ? '详情“' + name + '”' : '详情 ' + title;
    } else {
      READER_PAGES.forEach(function (reader) {
        if (path.indexOf(reader.prefix) !== 0) return;
        var album = headingText(reader.heading);
        title = album && album !== reader.blank && album !== '加载中...' ? reader.label + '“' + album + '”' : reader.blank;
      });
    }
    return title.length > TITLE_MAX ? title.slice(0, TITLE_MAX - 1) + '…' : title;
  }

  /** 本程序的页面（不是 /api/… 接口、/static/… 文件） */
  function isAppPage(pathname) {
    return !/^\/(api|static)(\/|$)/.test(pathname) && pathname !== '/favicon.ico';
  }

  /** 记下的各页（过期、格式不对的丢掉） */
  function readPages() {
    var map;
    try { map = JSON.parse(window.sessionStorage.getItem(PAGES_KEY)); } catch (_) { map = null; }
    var pages = {};
    if (!map || typeof map !== 'object' || Array.isArray(map)) return pages;
    var now = Date.now();
    var limit = maxAge();
    Object.keys(map).forEach(function (key) {
      var page = map[key];
      if (page && typeof page === 'object' && isLocalUrl(page.url) && typeof page.title === 'string'
          && typeof page.y === 'number' && typeof page.savedAt === 'number'
          && now - page.savedAt >= 0 && now - page.savedAt < limit) {
        pages[key] = page;
      }
    });
    return pages;
  }

  /** 只留最近的 MAX_PAGES 条 */
  function writePages(pages) {
    var keys = Object.keys(pages).sort(function (a, b) { return pages[b].savedAt - pages[a].savedAt; });
    var kept = {};
    keys.slice(0, MAX_PAGES).forEach(function (key) { kept[key] = pages[key]; });
    try { window.sessionStorage.setItem(PAGES_KEY, JSON.stringify(kept)); } catch (_) { /* 存不下：不记 */ }
  }

  /** 离开这一页时记下它的地址、名字和“看到的位置”（没有 Navigation API 时无从对应，不记） */
  function recordHere() {
    var key = historyKey();
    if (!key) return;
    var memory = nm();
    var seen = memory && memory.place ? memory.place() : { y: window.scrollY || 0, listTop: null };
    var pages = readPages();
    pages[key] = {
      url: currentUrl(),
      title: pageTitle(),
      y: Math.max(0, Math.round(Number(seen.y) || 0)),
      listTop: typeof seen.listTop === 'number' ? seen.listTop : null,
      savedAt: Date.now()
    };
    writePages(pages);
  }

  /** 浏览器有没有 Navigation API（没有时“返回”不可用：无法可靠判断上一条历史是不是本程序的页面） */
  function hasNavigationApi() {
    var nav = window.navigation;
    return !!(nav && nav.currentEntry && typeof nav.entries === 'function');
  }

  /**
   * 本标签页里的上一页（本程序的页面）：{ url, title, y, listTop }（名字和位置不知道时为 null）；没有 → null。
   * Navigation API 的历史记录只列出同源、连续的那一段：当前这条前面有、而且是本程序的页面（不是手动打开的
   * /api/… 接口等），就是上一页。没有 Navigation API 的浏览器一律当作没有（history.length 也数后面的记录、
   * referrer 在后退时不变，猜错了会点了没反应或退出本程序）。
   */
  function previousEntry() {
    if (!hasNavigationApi()) return null;
    try {
      var nav = window.navigation;
      var index = nav.currentEntry.index;
      var entry = index > 0 ? nav.entries()[index - 1] : null;
      if (!entry || !entry.url) return null;
      var url = new URL(entry.url, window.location.href);
      if (url.origin !== window.location.origin || !isAppPage(url.pathname)) return null;
      var saved = entry.key ? readPages()[entry.key] : null;
      return {
        url: url.pathname + url.search,
        title: saved ? saved.title : null,
        y: saved ? saved.y : null,
        listTop: saved ? saved.listTop : null
      };
    } catch (_) { return null; /* 取不到：当作没有 */ }
  }

  /** 返回：退回上一页，回到离开它时的位置；没有上一页 → false */
  function goBack() {
    var previous = previousEntry();
    if (!previous) return false;
    var memory = nm();
    if (previous.y !== null && memory && memory.expectReturn) {
      memory.expectReturn(previous.url, previous.y, previous.listTop);
    }
    // 双击“返回”时第二下会落在退回去的那一页上：让那一页在这一刻之后片刻内忽略双击的第二下（nav-memory.js）
    if (memory && memory.noteBackClick) memory.noteBackClick();
    window.history.back();
    return true;
  }

  // 每一页离开时都记下（阅读页、单页预览也记：从它们去别处后“返回”能显示它们的名字）
  window.addEventListener('pagehide', recordHere);

  // 供测试与控制台查看（页面本身不依赖）
  window.quickNav = {
    previousEntry: previousEntry, goBack: goBack, recordHere: recordHere, readPages: readPages, MAX_PAGES: MAX_PAGES
  };

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
  var leaving = false;   // 刚点了“返回”、页面正要跳走：连点 / 双击不再多退一页
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

  /** 按当前位置和上一页更新三个按钮 */
  function refresh() {
    var y = window.scrollY || 0;
    setDisabled(topBtn, y <= 1);
    setDisabled(bottomBtn, y >= maxScroll() - 1);
    var previous = previousEntry();
    if (backBtn) {
      setDisabled(backBtn, leaving || !previous);
      var label = previous ? (previous.title ? '返回：' + previous.title : '返回上一页')
        : (hasNavigationApi() ? '返回（这个标签页里前面没有本程序的页面）' : '返回（这个浏览器用不了，请用浏览器的后退）');
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
    // 去处是普通链接，照常打开（搜索/收藏/资源库的地址已由 nav-memory.js 换成记住的那个）
    if (event.target.closest('a[data-quick-dest]')) return;
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

  // 离开时收起（进入往返缓存的页面回来时是收起的）；回来时“返回”恢复可用并重新判断上一页
  window.addEventListener('pagehide', function () { setOpen(null); });
  window.addEventListener('pageshow', function () {
    leaving = false;
    clearTimeout(leavingTimer);
    refresh();
  });
  refresh();
})();
