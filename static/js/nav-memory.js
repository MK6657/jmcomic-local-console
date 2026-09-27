/**
 * 顶部导航回到“上次离开时的样子”（只在本标签页：sessionStorage，关掉标签页即清除）。
 *
 *   搜索   → 上次的关键词、排序、每页数量和页码；结果用 search.js 的快照直接画出（不重新请求上游），回到看到的位置
 *   收藏   → 上次的搜索词、状态筛选、排序和页码，回到看到的位置
 *   资源库 → 上次的搜索词、状态、排序、标签、作者和页码，回到看到的位置
 *
 * 带 data-nav-memory="/search" 等属性的链接（顶部导航、阅读页和空资源库里通往搜索页的链接）
 * 在被指向、获得焦点、按下或点击时才换成记住的地址，所以总是最新的；当前就在该页面时用当前地址。
 * 记住的地址必须是同一路径（/search 或 /search?…），超过 MAX_AGE 或取不到时退回链接原本的地址。
 * 收藏 / 资源库只在经这些链接到达、后退/前进（含往返缓存 bfcache）或刷新时回到原位置；直接在地址栏输入地址仍然是初始页面。
 *
 * “看到哪里了”：导航栏不吸顶，要点它得先滚回页面最上方，所以离开时若在最上方（TOP_ZONE 内），
 * 记下的是之前最后停留（REST_MS 以上）或点击、输入过的位置；在最上方停留够久或在那里操作过，就记最上方。
 * 这个位置只属于当时的地址：换页、换筛选后作废。刷新（F5）不走这条规则，回到刷新前所在的位置。
 * 还原按列表（[data-nav-memory-list]：资源库卡片区、收藏表格、搜索结果区）的偏移计算：上方的内容后加载、
 * 高度变了也回到同一处。列表位置在记下“看到的位置”的那一刻量（手机上展开的导航菜单会把整页往下推）。
 *
 * 右下角快捷导航（quick-nav.js）里的 搜索 / 收藏 / 资源库 同样带 data-nav-memory；在它上面点击和点顶部导航一样
 * 不算“在这里做事”。它的“返回”经 expectReturn / takeReturn 回到跳走前的地址和那一刻所在的位置。
 */
(function () {
  'use strict';

  var MAX_AGE = 12 * 60 * 60 * 1000;     // 记住 12 小时
  var LIST_KEY = 'jm-nav-memory-v1';     // { '/library': {url, scrollY, listTop, rawScrollY, rawListTop, savedAt}, '/wishlist': {…} }
  var ARRIVAL_KEY = 'jm-nav-arrival-v1'; // 刚点了去收藏/资源库的记忆链接：{path, at}
  var SEARCH_KEY = 'jm-search-state-v1'; // search.js 的结果快照 {url, data, scrollY, savedAt, …}
  var LIST_PAGES = ['/library', '/wishlist'];
  var PATHS = ['/search'].concat(LIST_PAGES);
  var TOP_ZONE = 120;       // 顶部导航可见的范围（px）
  var REST_MS = 2000;       // 在一个位置停这么久才算“停下来看”
  var ARRIVAL_MS = 15000;   // 点链接到新页面画出列表之间最多这么久

  function readJson(key) {
    try {
      var value = JSON.parse(window.sessionStorage.getItem(key));
      return value && typeof value === 'object' ? value : null;
    } catch (_) { return null; }
  }

  function writeJson(key, value) {
    try { window.sessionStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* 不可用或存不下：不记 */ }
  }

  function removeKey(key) {
    try { window.sessionStorage.removeItem(key); } catch (_) { /* 不可用 */ }
  }

  function ageOk(at, limit) {
    var age = Date.now() - at;
    return typeof at === 'number' && age >= 0 && age < limit;
  }

  function isFresh(savedAt) { return ageOk(savedAt, MAX_AGE); }

  function currentUrl() { return window.location.pathname + window.location.search; }

  /** 本页是怎么打开的：'navigate' / 'reload' / 'back_forward'（取不到时为 ''） */
  function navigationType() {
    try {
      var entry = window.performance.getEntriesByType('navigation')[0];
      return (entry && entry.type) || '';
    } catch (_) { return ''; }
  }

  /** 元素顶部在整个页面中的位置 */
  function docTop(el) {
    return Math.round(el.getBoundingClientRect().top + window.scrollY);
  }

  /** 本页列表顶部现在的位置（没有列表时为 null） */
  function listTopNow() {
    var list = document.querySelector('[data-nav-memory-list]');
    return list ? docTop(list) : null;
  }

  /** 只接受同一路径的站内地址：path 本身或 path?…（不接受 //主机、反斜杠、空白、#） */
  function samePath(path, url) {
    return typeof url === 'string' && (url === path || url.indexOf(path + '?') === 0) && !/[\s\\#]/.test(url);
  }

  function listEntry(path) {
    var map = readJson(LIST_KEY);
    var entry = map && LIST_PAGES.indexOf(path) >= 0 ? map[path] : null;
    return entry && typeof entry === 'object' && isFresh(entry.savedAt) && samePath(path, entry.url) ? entry : null;
  }

  /** 去 path 时应打开的地址：当前就在该页 → 当前地址；否则本标签页记住的地址；都没有 → null */
  function lastUrl(path) {
    if (PATHS.indexOf(path) < 0) return null;
    if (window.location.pathname === path) return currentUrl();
    if (path === '/search') {
      var snapshot = readJson(SEARCH_KEY);
      return snapshot && isFresh(snapshot.savedAt) && samePath(path, snapshot.url) ? snapshot.url : null;
    }
    var entry = listEntry(path);
    return entry ? entry.url : null;
  }

  function retarget(link) {
    var path = link.getAttribute('data-nav-memory');
    if (PATHS.indexOf(path) < 0) return null;
    link.setAttribute('href', lastUrl(path) || path);
    return path;
  }

  // 捕获阶段：在页面自己的点击处理（如阅读页“返回”）之前换好地址；contextmenu 让“复制链接/新标签页打开”也拿到它
  function onLinkEvent(event) {
    var link = event.target && event.target.closest ? event.target.closest('a[data-nav-memory]') : null;
    if (!link) return;
    var path = retarget(link);
    // 到达标记只给在本标签页打开的普通左键点击（Ctrl/Shift/中键等在新标签页/窗口打开，本页不跳转）
    var plain = !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey && !event.button;
    if (event.type === 'click' && plain && LIST_PAGES.indexOf(path) >= 0) {
      writeJson(ARRIVAL_KEY, { path: path, at: Date.now() });
    }
  }
  ['pointerover', 'focusin', 'pointerdown', 'click', 'contextmenu'].forEach(function (type) {
    document.addEventListener(type, onLinkEvent, true);
  });
  Array.prototype.forEach.call(document.querySelectorAll('a[data-nav-memory]'), retarget);

  // ── 看到哪里了 ──
  var anchor = null; // { y, url, listTop }：最后停留或操作过的位置、当时的地址和列表位置
  var restTimer = null;

  function setAnchor(y) {
    anchor = { y: Math.max(0, Math.round(Number(y) || 0)), url: currentUrl(), listTop: listTopNow() };
  }

  /** 顶部导航和右下角快捷导航（quick-nav.js）：只是“去别处”的入口，在上面点击不算在这里做事 */
  function inNav(target) {
    return !!(target && target.closest && (target.closest('.navbar') || target.closest('.quick-nav')));
  }

  window.addEventListener('scroll', function () {
    clearTimeout(restTimer);
    restTimer = setTimeout(function () { setAnchor(window.scrollY); }, REST_MS);
  }, { passive: true });
  // 点击、在输入框里输入/选择 = 就在这里做事。滚动按键（PageUp、方向键、Home…）、触屏滑动不算；点导航（含快捷导航）本身也不算
  ['click', 'input', 'change'].forEach(function (type) {
    window.addEventListener(type, function (event) {
      if (!inNav(event.target)) setAnchor(window.scrollY);
    }, { capture: true, passive: true });
  });

  /** 离开时应记下的位置 { y, listTop }（见文件开头“看到哪里了”） */
  function place() {
    var y = Math.round(window.scrollY);
    if (y > TOP_ZONE || !anchor || anchor.url !== currentUrl()) return { y: y, listTop: listTopNow() };
    return { y: anchor.y, listTop: anchor.listTop };
  }

  // ── 收藏 / 资源库：离开时记下地址（筛选、排序、页码都在地址栏里）和看到的位置 ──
  function saveList() {
    var path = window.location.pathname;
    if (LIST_PAGES.indexOf(path) < 0) return;
    var map = readJson(LIST_KEY) || {};
    var old = map[path];
    if (!listDrawn && old && typeof old === 'object' && old.url === currentUrl()) {
      // 列表还没画出来（还没回到原位置）就离开或切走：页面还在最上方，不能拿它覆盖记住的位置
      old.savedAt = Date.now();
      writeJson(LIST_KEY, map);
      return;
    }
    var seen = place();
    map[path] = {
      url: currentUrl(), scrollY: seen.y, listTop: seen.listTop,
      rawScrollY: Math.round(window.scrollY), rawListTop: listTopNow(), savedAt: Date.now()
    };
    writeJson(LIST_KEY, map);
  }
  window.addEventListener('pagehide', saveList);
  // 进入往返缓存（bfcache）前停掉“停留”计时：否则回来后它接着走完，把回来时所在的最上方记成看到的位置
  window.addEventListener('pagehide', function () { clearTimeout(restTimer); });
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') saveList();
  });

  // 页面打开后用户已经自己滚动或操作过，就不再替他跳回原位置
  var userMoved = false;
  ['wheel', 'touchmove', 'keydown', 'pointerdown'].forEach(function (type) {
    window.addEventListener(type, function () { userMoved = true; }, { capture: true, passive: true });
  });
  var scrollRestored = false;
  var listDrawn = false; // 列表已经画出（restoreScroll 被调用过）

  // ── 快捷导航“返回”（quick-nav.js）：回到跳走前所在的地址和位置 ──
  // 点“返回”时记下目标 { url, y, listTop, at }，目标页画出内容后回到 y（只用一次，RETURN_MS 内有效）。
  // 搜索、收藏、资源库在画出结果后取（search.js / restoreScroll），它优先于离开时记下的位置；
  // 其他页面（首页、下载管理、详情、设置……）等内容长到够高再回去。
  var RETURN_KEY = 'jm-nav-return-v1';
  var RETURN_MS = 15000;       // 点“返回”到目标页打开之间最多这么久
  var RETURN_WAIT_MS = 8000;   // 其他页面最多等内容加载这么久（详情等靠请求画出）

  function expectReturn(url, y, listTop) {
    if (typeof url !== 'string') return;
    writeJson(RETURN_KEY, {
      url: url, y: Math.max(0, Math.round(Number(y) || 0)),
      listTop: typeof listTop === 'number' ? listTop : null, at: Date.now()
    });
  }

  /** 本页是不是“返回”的目标（没取走、没过期、地址相同） */
  function pendingReturn() {
    var pending = readJson(RETURN_KEY);
    if (!pending) return null;
    if (!ageOk(pending.at, RETURN_MS)) { removeKey(RETURN_KEY); return null; }
    return pending.url === currentUrl() ? pending : null;
  }

  /** 本页就是“返回”的目标：取出（只用一次）要回到的位置 { y, listTop }；不是或已过期 → null */
  function takeReturn() {
    var pending = pendingReturn();
    if (!pending) return null;
    removeKey(RETURN_KEY);
    return { y: pending.y, listTop: typeof pending.listTop === 'number' ? pending.listTop : null };
  }

  /** 带关键词的搜索页由 search.js 画出结果（含从往返缓存恢复时）后自己取“返回”的位置 */
  function searchRestoresItself() {
    return window.location.pathname === '/search' && /(^|[?&])keyword=[^&]/.test(window.location.search);
  }

  /** 其他页面：页面能滚到 y 了（或等够了）再回去；用户自己先动了就不再跳 */
  function returnWhenTall(back, startedAt) {
    if (userMoved) return;
    var doc = document.documentElement;
    var room = doc && typeof window.innerHeight === 'number' ? doc.scrollHeight - window.innerHeight : 0;
    if (room >= back.y || Date.now() - startedAt >= RETURN_WAIT_MS) {
      scrollBack(back.y, back.listTop);
      return;
    }
    setTimeout(function () { returnWhenTall(back, startedAt); }, 150);
  }

  /** 等列表画好（两帧）再回到 y，这期间用户自己动了就算了 */
  function scrollBackSoon(y, listTop) {
    window.requestAnimationFrame(function () {
      window.requestAnimationFrame(function () {
        if (!userMoved) scrollBack(y, listTop);
      });
    });
  }

  window.addEventListener('pageshow', function (event) {
    if (!event.persisted) return;
    // 快捷导航“返回”经浏览器后退回到这里（往返缓存）：内容都还在，直接回到跳走前的位置。
    // 带关键词的搜索页从快照重新画出结果后由 search.js 取
    if (!searchRestoresItself()) {
      var back = takeReturn();
      if (back) {
        window.requestAnimationFrame(function () { scrollBack(back.y, back.listTop); });
        return;
      }
    }
    // 从往返缓存回到收藏 / 资源库：页面停在离开时的位置——多半是为了点导航滚到的最上方，回到看到的位置
    if (LIST_PAGES.indexOf(window.location.pathname) < 0) return;
    var entry = listEntry(window.location.pathname);
    if (!entry || entry.url !== currentUrl() || !(entry.scrollY > TOP_ZONE) || window.scrollY > TOP_ZONE) return;
    window.requestAnimationFrame(function () {
      if (window.scrollY <= TOP_ZONE) scrollBack(entry.scrollY, entry.listTop);
    });
  });

  /**
   * 回到 y（离开时整页的位置）。listTop 是当时列表顶部的位置：y 在列表范围内时，按列表现在的位置换算。
   * instant：Bootstrap 的 :root { scroll-behavior: smooth } 会让它从顶部滑过去。
   */
  function scrollBack(y, listTop) {
    var target = y;
    var now = listTopNow();
    if (now !== null && typeof listTop === 'number' && y >= listTop) target = y + now - listTop;
    window.scrollTo({ top: Math.max(0, Math.round(target)), behavior: 'instant' });
    setAnchor(window.scrollY);
  }

  window.navMemory = {
    MAX_AGE: MAX_AGE,
    isFresh: isFresh,
    lastUrl: lastUrl,
    navigationType: navigationType,
    /** 离开时应记下的位置 { y, listTop }（见文件开头“看到哪里了”） */
    place: place,
    listTopNow: listTopNow,
    /** 页面把用户送回某个位置后调用：这就是当前“看到的地方”，马上滚回顶部点导航也不会丢 */
    markPlace: setAnchor,
    scrollBack: scrollBack,
    /** 快捷导航“返回”：记下目标地址和要回到的位置（quick-nav.js 在跳转前调用） */
    expectReturn: expectReturn,
    /** 本页是“返回”的目标时取出要回到的位置 { y, listTop }（只用一次），否则 null */
    takeReturn: takeReturn,
    /** 本页是否是“返回”的目标（不取走） */
    hasReturn: function () { return !!pendingReturn(); },
    /**
     * 收藏 / 资源库第一次画出列表后调用（只生效一次）：经快捷导航“返回”到这里 → 回到跳走前的位置；
     * 经记忆链接到达、后退/前进或刷新，且地址与离开时相同 → 回到离开时的位置（刷新回到刷新前所在的位置）。
     */
    restoreScroll: function () {
      if (scrollRestored) return;
      scrollRestored = true;
      listDrawn = true;
      var path = window.location.pathname;
      var arrival = readJson(ARRIVAL_KEY);
      if (arrival) removeKey(ARRIVAL_KEY);
      var back = takeReturn();
      if (back) {
        if (!userMoved) scrollBackSoon(back.y, back.listTop);
        return;
      }
      var type = navigationType();
      var arrived = !!arrival && arrival.path === path && ageOk(arrival.at, ARRIVAL_MS);
      if (!arrived && type !== 'back_forward' && type !== 'reload') return; // 直接输入地址等：初始页面
      var entry = listEntry(path);
      if (!entry || entry.url !== currentUrl() || userMoved) return;
      var reload = type === 'reload' && typeof entry.rawScrollY === 'number';
      var y = reload ? entry.rawScrollY : entry.scrollY;
      var listTop = reload ? entry.rawListTop : entry.listTop;
      if (!(y > 0)) return;
      scrollBackSoon(y, listTop);
    }
  };

  // 快捷导航“返回”到首页、下载管理、详情、设置、没有关键词的搜索页等：内容加载到够高后回到跳走前的位置
  // （收藏、资源库、带关键词的搜索页画出结果后自己取：restoreScroll / search.js）
  if (LIST_PAGES.indexOf(window.location.pathname) < 0 && !searchRestoresItself()) {
    var backHere = takeReturn();
    if (backHere && backHere.y > 0) returnWhenTall(backHere, Date.now());
  }
})();
