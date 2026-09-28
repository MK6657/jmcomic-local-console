/**
 * 批量下载的确认窗口：资源库「下载新章节」、收藏「下载未下载的收藏」「下载选中的收藏」共用
 * 依赖: utils.js (window.apiFetch, window.updateBadges)、Bootstrap 5 的 Modal、base.html 的 showToast
 *
 *   window.batchDownloadDialog.open(kind, { albumIds, opener, onDone })
 *     kind：new_chapters / undownloaded_favourites / selected_favourites（albumIds 只用于最后一种）
 *     opener：关闭后焦点回到它；onDone：加入下载队列之后调一次（页面按原来的页码、筛选和位置刷新列表）
 *   window.batchDownloadDialog.isOpen()：窗口开着时，列表页暂停自动刷新
 *
 * 清单由服务端算（POST /api/batch-downloads/preview：只读本地记录，不联网，不建任务），这里只显示；用户只能取消勾选。
 * “加入下载队列”POST /api/batch-downloads/confirm {kind, token, exclude}：服务端在一个事务里重新算清单，
 * 和预览时不一样就什么都不建、返回 409 和最新的清单——这里按最新清单重新列出、请用户再看一下，从不自动再确认。
 * 加入的任务照常排队，受定时下载时间段和同时下载数限制；窗口如实说明什么时候开始。
 * 标题等远程数据只经 textContent 进入 DOM。弹窗没有 fade 类：打开、关闭都没有动画。
 */
(function () {
  'use strict';

  var PREVIEW_URL = '/api/batch-downloads/preview';
  var CONFIRM_URL = '/api/batch-downloads/confirm';

  var TITLES = {
    new_chapters: '下载新章节',
    undownloaded_favourites: '下载未下载的收藏',
    selected_favourites: '下载选中的收藏'
  };

  var SCOPES = {
    new_chapters: '只包含本地有已下载内容、检查确认过有新章节的漫画（收藏和没收藏的都算）；每部只下载列出的新章节，'
      + '已下载的章节和当初没选的章节都不会下载。',
    undownloaded_favourites: '只包含收藏页“未下载”里的：从未下载过的，和下载被取消的；每部下载整部漫画（开始下载时上游的全部章节）。'
      + '下载失败的请在「下载管理」的「失败」里重试；已有已下载内容的（包括只下载了部分章节的）不会包含。',
    selected_favourites: '只下载选中的收藏里“未下载”的（从未下载过的、下载被取消的），每部下载整部漫画；其余的不会下载，原因列在下面。'
  };

  var EMPTY = {
    new_chapters: '现在没有要下载的新章节。只有检查确认过的新章节会出现在这里；可以在漫画详情页点「立即检查」。'
      + '检查只核对章节列表，从不自动下载。',
    undownloaded_favourites: '“未下载”里没有要下载的收藏。',
    selected_favourites: '选中的收藏里没有这次可以整部下载的“未下载”收藏（原因见下）。'
  };

  var REASONS = {
    active: '已在下载队列中（排队中 / 下载中 / 已暂停），不重复创建任务',
    too_many: '新章节超过 1000 话，请到详情页选择要下载的章节',
    records_cleared: '下载记录被清理过（例如在「下载管理」里清空了已结束的任务），下载目录里可能还有这部漫画的文件，'
      + '这次不整部下载；确定要整部下载请点收藏里这一行的「下载」，或到详情页选择章节下载',
    readable: '已有已下载内容（可离线阅读），不整部重新下载；新章节请用资源库的「下载新章节」',
    failed: '上次下载失败，请在「下载管理」的「失败」里重试',
    missing: '下载过，本地文件不可用，请在「下载管理」里重新下载',
    not_favourite: '已不在收藏里'
  };

  var STALE_NOTICE = '清单刚刚有变化（有任务开始或结束，或刚确认了新章节），已按最新情况重新列出，请再看一下后确认。';
  var STALE_EMPTY = '清单刚刚有变化：现在没有要下载的了，原因列在下面。';
  var STALE_EMPTY_BARE = '清单刚刚有变化：现在没有要下载的了。';   // 下面没有列出任何原因时
  // 确认还没有回音时窗口被关掉（或又打开了别的清单），之后才知道清单变了：什么都没建
  var STALE_CLOSED = '清单有变化，这次没有加入下载队列；请重新打开清单再确认';

  var root = null;      // div.modal#batch-dialog：第一次打开时建
  var parts = {};       // 窗口里的节点
  var current = null;   // 这一次打开：{ kind, albumIds, opener, onDone, preview, unticked, boxes, busy, done }
  var opened = false;

  function toast(message, type) {
    if (typeof showToast === 'function') showToast(message, type);
  }

  function node(tag, className, text) {
    var n = document.createElement(tag);
    if (className) n.className = className;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function icon(name) {
    var i = node('i', 'bi ' + name);
    i.setAttribute('aria-hidden', 'true');
    return i;
  }

  function part(key, parent, tag, id, className) {
    var n = node(tag, className);
    if (id) n.id = id;
    parts[key] = n;
    parent.appendChild(n);
    return n;
  }

  // 定时下载的开始 / 结束时：HH:00
  function hour(h) {
    var n = Number(h);
    return (n < 10 ? '0' : '') + n + ':00';
  }

  // 显示用的标题：没有标题（服务端用车号代替）时写“车号 N”
  function displayTitle(title, id) {
    var t = String(title === undefined || title === null ? '' : title).trim();
    return t && t !== id ? t : '车号 ' + id;
  }

  function chapterName(c) {
    var title = String((c && c.title) || '').trim();
    return title || ('第' + (c && c.index != null ? c.index : '?') + '话');
  }

  // ── 窗口（只建一次） ──
  function build() {
    if (root) return;
    root = node('div', 'modal');   // 没有 fade：不做淡入淡出
    root.id = 'batch-dialog';
    root.setAttribute('tabindex', '-1');
    root.setAttribute('role', 'dialog');
    root.setAttribute('aria-modal', 'true');
    root.setAttribute('aria-labelledby', 'batch-dialog-title');
    root.setAttribute('aria-describedby', 'batch-dialog-summary');
    var dialog = node('div', 'modal-dialog modal-lg modal-dialog-scrollable');
    var content = node('div', 'modal-content');

    var header = node('div', 'modal-header');
    part('title', header, 'h5', 'batch-dialog-title', 'modal-title');
    var close = node('button', 'btn-close');
    close.type = 'button';
    close.setAttribute('data-bs-dismiss', 'modal');
    close.setAttribute('aria-label', '关闭');
    header.appendChild(close);

    var body = node('div', 'modal-body');
    var status = part('status', body, 'p', 'batch-dialog-status', 'batch-dialog-status');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    var notice = part('notice', body, 'div', 'batch-dialog-notice', 'batch-dialog-alert batch-dialog-alert--wait');
    notice.setAttribute('role', 'alert');
    notice.hidden = true;
    notice.appendChild(icon('bi-exclamation-triangle'));
    parts.noticeText = node('span', null, STALE_NOTICE);
    notice.appendChild(parts.noticeText);
    var summary = part('summary', body, 'p', 'batch-dialog-summary', 'batch-dialog-summary');
    summary.setAttribute('aria-live', 'polite');
    summary.setAttribute('tabindex', '-1');
    part('scope', body, 'p', 'batch-dialog-scope', 'batch-dialog-meta');
    part('extra', body, 'p', 'batch-dialog-extra', 'batch-dialog-meta');
    part('window', body, 'div', 'batch-dialog-window', 'batch-dialog-alert');
    part('list', body, 'ul', 'batch-dialog-list', 'batch-dialog-list').setAttribute('aria-label', '这次会下载的漫画');
    part('skippedTitle', body, 'p', 'batch-dialog-skipped-title', 'batch-dialog-sub');
    part('skipped', body, 'ul', 'batch-dialog-skipped', 'batch-dialog-skipped')
      .setAttribute('aria-labelledby', 'batch-dialog-skipped-title');
    part('reviewTitle', body, 'p', 'batch-dialog-review-title', 'batch-dialog-sub');
    part('review', body, 'ul', 'batch-dialog-review', 'batch-dialog-skipped')
      .setAttribute('aria-labelledby', 'batch-dialog-review-title');
    part('outside', body, 'p', 'batch-dialog-outside', 'batch-dialog-meta');

    var footer = node('div', 'modal-footer');
    var cancel = part('cancel', footer, 'button', 'batch-dialog-cancel', 'btn btn-outline-secondary');
    cancel.type = 'button';
    cancel.setAttribute('data-bs-dismiss', 'modal');
    var confirm = part('confirm', footer, 'button', 'batch-dialog-confirm', 'btn btn-primary');
    confirm.type = 'button';
    confirm.addEventListener('click', confirmNow);

    content.appendChild(header);
    content.appendChild(body);
    content.appendChild(footer);
    dialog.appendChild(content);
    root.appendChild(dialog);

    // 打开后焦点在“取消”上：回车不会误确认
    root.addEventListener('shown.bs.modal', function () {
      if (typeof parts.cancel.focus === 'function') parts.cancel.focus();
    });
    root.addEventListener('hidden.bs.modal', function () {
      opened = false;
      parts.notice.hidden = true;
      var opener = current && current.opener;
      if (opener && typeof opener.focus === 'function') opener.focus();
    });
    // 和页面模板里的弹窗一样放在快捷导航之前：后面还有能获得焦点的元素，按 Tab 不会跳出窗口
    //（Bootstrap 的焦点限制只在焦点落到窗口外的元素上时把它拉回来）
    var anchor = document.getElementById('quick-nav');
    if (anchor && anchor.parentNode === document.body) document.body.insertBefore(root, anchor);
    else (document.querySelector('.main-container') || document.body).appendChild(root);
  }

  function modal() {
    return bootstrap.Modal.getOrCreateInstance(root);
  }

  function setContentHidden(hidden) {
    ['summary', 'scope', 'extra', 'window', 'list', 'skippedTitle', 'skipped', 'reviewTitle', 'review', 'outside']
      .forEach(function (key) { parts[key].hidden = hidden; });
  }

  function showLoading() {
    setContentHidden(true);
    parts.summary.textContent = '';
    parts.status.textContent = '';
    parts.status.appendChild(icon('bi-hourglass-split'));
    parts.status.appendChild(document.createTextNode(' 正在列出要下载的内容…（只读本地记录，不联网）'));
    parts.cancel.textContent = '取消';
    parts.confirm.hidden = false;
    parts.confirm.disabled = true;
    parts.confirm.removeAttribute('aria-busy');
    parts.confirm.textContent = '加入下载队列';
  }

  function showFailure(s, message) {
    if (current !== s) return;
    setContentHidden(true);
    var text = message || '网络错误';
    parts.status.textContent = '';
    parts.status.appendChild(document.createTextNode(
      (text.indexOf('没能列出要下载的内容') === 0 ? text : '没能列出要下载的内容：' + text) + ' '));
    var retry = node('button', 'btn btn-sm btn-outline-primary');
    retry.type = 'button';
    retry.appendChild(icon('bi-arrow-clockwise'));
    retry.appendChild(document.createTextNode(' 重试'));
    retry.addEventListener('click', function () {
      loadPreview(s);
      if (typeof parts.cancel.focus === 'function') parts.cancel.focus();   // 重试按钮随之消失
    });
    parts.status.appendChild(retry);
    parts.confirm.hidden = true;
    parts.cancel.textContent = '关闭';
  }

  // ── 清单 ──
  function loadPreview(s) {
    s.preview = null;
    showLoading();
    var body = { kind: s.kind };
    if (s.albumIds) body.album_ids = s.albumIds;
    window.apiFetch(PREVIEW_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      timeoutMs: 60000,
      abortKey: 'batch-dialog-preview'
    })
    .then(function (data) {
      if (current !== s) return;
      if (!data || data.status !== 'ok') { showFailure(s, data && data.message); return; }
      s.preview = data;
      render(s);
    })
    .catch(function (err) {
      if (current !== s || (err && err.name === 'AbortError')) return;   // 离开页面 / 又打开了一次
      showFailure(s, err && (err.status || err.isTimeout) ? err.message : '');
    });
  }

  function listed(s) {
    return (s.preview && s.preview.items) || [];
  }

  function chosen(s) {
    return listed(s).filter(function (item) { return !s.unticked[String(item.album_id)]; });
  }

  // 概要（随勾选变化）和“加入下载队列（N 部）”
  function refreshSummary(s) {
    var items = listed(s);
    if (!items.length) {
      parts.summary.textContent = EMPTY[s.kind];
      return;
    }
    var picked = chosen(s);
    var a = picked.length;
    var text;
    if (s.kind === 'new_chapters') {
      var c = 0;
      picked.forEach(function (item) { c += (item.photo_ids || []).length; });
      text = '将下载 ' + a + ' 部漫画的 ' + c + ' 话新章节';
    } else if (s.kind === 'undownloaded_favourites') {
      text = '将下载 ' + a + ' 部收藏，每部下载整部漫画';
    } else {
      text = '选中的 ' + s.albumIds.length + ' 部里，将下载 ' + a + ' 部“未下载”的收藏，每部下载整部漫画';
    }
    if (items.length > a) text += '（已取消勾选 ' + (items.length - a) + ' 部）';
    parts.summary.textContent = text;
    if (!s.busy) {
      parts.confirm.textContent = '加入下载队列（' + a + ' 部）';
      parts.confirm.disabled = a === 0;
    }
  }

  function itemMeta(kind, item) {
    if (kind !== 'new_chapters') return item.state === 'canceled' ? '整部漫画 · 上次下载已取消' : '整部漫画 · 从未下载';
    var n = (item.photo_ids || []).length;
    var names = (item.chapters || []).slice(0, 5).map(chapterName).join(' · ');
    var text = '新章节 ' + n + ' 话' + (names ? '：' + names + (n > 5 ? ' … 等 ' + n + ' 话' : '') : '');
    var when = window.updateBadges ? window.updateBadges.formatTime(item.confirmed_at) : '';
    return text + (when ? ' · ' + when + ' 检查确认' : '');
  }

  function itemRow(s, item) {
    var id = String(item.album_id);
    var name = displayTitle(item.title, id);
    var li = node('li', 'batch-dialog-item');
    var box = node('input', 'form-check-input');
    box.type = 'checkbox';
    box.id = 'batch-item-' + id;
    box.checked = !s.unticked[id];
    box.setAttribute('aria-label', '下载 ' + name);
    box.addEventListener('change', function () {
      if (box.checked) delete s.unticked[id];
      else s.unticked[id] = true;
      refreshSummary(s);
    });
    s.boxes.push(box);
    var text = node('div');
    var label = node('label');
    label.setAttribute('for', box.id);
    label.appendChild(node('span', 'batch-dialog-title', name));
    label.appendChild(node('span', 'batch-dialog-id', '#' + id));
    text.appendChild(label);
    text.appendChild(node('div', 'batch-dialog-meta', itemMeta(s.kind, item)));
    li.appendChild(box);
    li.appendChild(text);
    return li;
  }

  function windowCopy(w, q) {
    w = w || {};
    q = q || {};
    var ahead = Number(q.ahead) || 0;
    var queue = '同时最多 ' + (Number(q.max_running) || 1) + ' 个任务' + (ahead > 0 ? '；前面还有 ' + ahead + ' 个任务' : '');
    if (!w.enabled) return { wait: false, icon: 'bi-info-circle', text: '确认后加入下载队列，按顺序开始（' + queue + '）。' };
    if (w.invalid) {
      return { wait: true, icon: 'bi-exclamation-triangle',
        text: '已开启定时下载，但时间段设置无效：排队的任务不会自动开始。可以到「设置 → 定时下载」修改。' };
    }
    var start = hour(w.start), end = hour(w.end);
    if (w.never) {
      return { wait: true, icon: 'bi-exclamation-triangle',
        text: '已开启定时下载，但开始和结束时间都是 ' + start + '：排队的任务不会自动开始。可以到「设置 → 定时下载」修改时间段。' };
    }
    if (w.open) {
      return { wait: false, icon: 'bi-info-circle',
        text: '现在在定时下载时间段内（' + start + '–' + end + '）：确认后按顺序开始（' + queue + '）；到 ' + end
          + ' 还没开始的任务会等下一次时间段。' };
    }
    return { wait: true, icon: 'bi-hourglass-split',
      text: '已开启定时下载（' + start + '–' + end + '），现在不在时间段内：这些任务先排队，'
        + (w.opens_tomorrow ? '明天' : '今天') + ' ' + start + ' 起才开始下载（到时程序需要开着）。' };
  }

  function render(s) {
    var p = s.preview;
    var items = listed(s);
    var empty = items.length === 0;
    // 重新列出（409 stale）后：还在清单里、之前取消勾选的保持不勾选
    var still = {};
    items.forEach(function (item) { still[String(item.album_id)] = true; });
    Object.keys(s.unticked).forEach(function (id) { if (!still[id]) delete s.unticked[id]; });

    parts.status.textContent = '';
    setContentHidden(false);

    parts.scope.textContent = SCOPES[s.kind];
    parts.scope.hidden = empty;

    parts.extra.textContent = '';
    var lines = [];
    if (p.settings && p.settings.skip_existing === false) lines.push('设置里关闭了「跳过已存在的文件」：本地已有的页也会重新下载。');
    if (p.more > 0) lines.push('另有 ' + p.more + ' 部这次不下载（一次最多 ' + (p.limit || 50) + ' 部）；加入队列后可以再点一次。');
    lines.forEach(function (line, n) {
      if (n) parts.extra.appendChild(node('br'));
      parts.extra.appendChild(document.createTextNode(line));
    });
    parts.extra.hidden = empty || !lines.length;

    var w = windowCopy(p.window, p.queue);
    parts.window.textContent = '';
    parts.window.className = 'batch-dialog-alert' + (w.wait ? ' batch-dialog-alert--wait' : '');
    parts.window.appendChild(icon(w.icon));
    parts.window.appendChild(node('span', null, w.text));
    parts.window.hidden = empty;

    s.boxes = [];
    parts.list.textContent = '';
    items.forEach(function (item) { parts.list.appendChild(itemRow(s, item)); });
    parts.list.hidden = empty;

    var skipped = p.skipped || [];
    parts.skippedTitle.textContent = '这些不会下载（' + skipped.length + ' 部）';
    parts.skipped.textContent = '';
    skipped.forEach(function (skip) {
      var id = String(skip.album_id);
      var title = displayTitle(skip.title, id);
      var who = title === '车号 ' + id ? title : title + '（' + id + '）';
      parts.skipped.appendChild(node('li', null, who + '：' + (REASONS[skip.reason] || '这次不下载')));
    });
    parts.skippedTitle.hidden = parts.skipped.hidden = !skipped.length;

    var review = p.review || [];
    var changed = Number(p.out_of_scope && p.out_of_scope.changed) || review.length;
    parts.reviewTitle.textContent = '章节列表有变动的 ' + changed + ' 部不在这里下载，请先到详情页核对：';
    parts.review.textContent = '';
    review.forEach(function (row) {
      var id = String(row.album_id);
      var li = node('li', null, displayTitle(row.title, id) + '：新出现 ' + (Number(row.new_count) || 0) + ' 话，另有 '
        + (Number(row.removed_count) || 0) + ' 话已不在上游 · ');
      var link = node('a', null, '去核对');
      link.href = '/album/' + encodeURIComponent(id);
      link.setAttribute('target', '_blank');
      link.setAttribute('rel', 'noopener');
      li.appendChild(link);
      parts.review.appendChild(li);
    });
    parts.reviewTitle.hidden = parts.review.hidden = !review.length;

    var outside = [];
    if (s.kind === 'undownloaded_favourites') {
      var o = p.out_of_scope || {};
      if (o.failed > 0) outside.push('失败 ' + o.failed + ' 部（请在「下载管理」的「失败」里重试）');
      if (o.readable > 0) outside.push('已有已下载内容 ' + o.readable + ' 部');
      if (o.active > 0) outside.push('排队中 / 下载中 ' + o.active + ' 部');
      if (o.missing > 0) outside.push('下载过 · 本地文件不可用 ' + o.missing + ' 部');
    }
    parts.outside.textContent = outside.length ? '不包含：' + outside.join(' · ') : '';
    parts.outside.hidden = !outside.length;

    parts.cancel.textContent = empty ? '关闭' : '取消';
    parts.confirm.hidden = empty;
    parts.confirm.removeAttribute('aria-busy');
    refreshSummary(s);
  }

  // ── 加入下载队列 ──
  function setBusy(s, on) {
    s.busy = on;
    if (current !== s) return;
    var btn = parts.confirm;
    // 有焦点的按钮 / 复选框被禁用时焦点会掉到页面上（Esc 也关不了窗口）：先移到窗口本身
    var active = document.activeElement;
    if (on && active && (active === btn || s.boxes.indexOf(active) >= 0) && typeof root.focus === 'function') {
      root.focus();
    }
    s.boxes.forEach(function (box) { box.disabled = on; });
    // 请求已发出：这时关窗口撤不回，按钮不写「取消」
    parts.cancel.textContent = on ? '关闭' : '取消';
    if (on) {
      btn.disabled = true;
      btn.setAttribute('aria-busy', 'true');
      btn.textContent = '';
      btn.appendChild(icon('bi-hourglass-split'));
      btn.appendChild(document.createTextNode(' 正在加入下载队列…'));
    } else {
      btn.removeAttribute('aria-busy');
      refreshSummary(s);
    }
  }

  // 出错后「加入下载队列」又能点了：焦点还在窗口本身（或掉到了页面上）时回到这个按钮，键盘不用再找
  function refocus(s) {
    var active = document.activeElement;
    if (current !== s || !opened || (active && active !== root && active !== document.body)) return;
    if (!parts.confirm.hidden && !parts.confirm.disabled && typeof parts.confirm.focus === 'function') {
      parts.confirm.focus();
    }
  }

  // 定时下载时间段 → Toast 的后半句和类型
  function windowNote(w) {
    w = w || {};
    if (w.enabled && (w.never || w.invalid)) return { suffix: '；定时下载的时间段设置不对，任务不会自动开始', type: 'warning' };
    if (w.enabled && !w.open) {
      return { suffix: '，' + (w.opens_tomorrow ? '明天' : '今天') + ' ' + hour(w.start) + ' 起开始下载（定时下载）', type: 'info' };
    }
    return { suffix: '', type: 'success' };
  }

  function finish(s, data) {
    s.busy = false;
    var counts = data.counts || {};
    var albums = counts.albums != null ? counts.albums : (data.created || []).length;
    var text = s.kind === 'new_chapters'
      ? '已加入下载队列：' + albums + ' 部漫画的 ' + (counts.chapters || 0) + ' 话新章节'
      : '已加入下载队列：' + albums + ' 部收藏（整部）';
    var note = windowNote(data.window);
    toast(text + note.suffix, note.type);
    s.done = true;   // 这一次打开已经加入过队列：不会再确认
    if (current === s && opened) modal().hide();
    if (typeof s.onDone === 'function') s.onDone(data);
  }

  function confirmFailed(s, err) {
    var data = err && err.data;
    if (err && err.status === 409 && data && data.reason === 'stale' && data.preview) {
      s.busy = false;
      if (current !== s || !opened) { toast(STALE_CLOSED, 'warning'); return; }
      // 清单变了，什么都没建：按最新情况重新列出，请用户再看一下（从不自动再确认）
      s.preview = data.preview;
      render(s);
      var reasons = !parts.skipped.hidden || !parts.review.hidden || !parts.outside.hidden;
      parts.noticeText.textContent = listed(s).length ? STALE_NOTICE : (reasons ? STALE_EMPTY : STALE_EMPTY_BARE);
      parts.notice.hidden = false;
      if (typeof parts.summary.focus === 'function') parts.summary.focus();
      return;
    }
    setBusy(s, false);
    if (err && err.name === 'AbortError') return;   // 离开页面：不提示
    refocus(s);
    if (err && err.status === 400 && data && data.reason === 'nothing_selected') {
      toast('没有勾选要下载的漫画', 'warning');
    } else if (err && err.status === 409) {
      toast(err.message || '上一次确认还在处理，请稍后再试', 'warning');   // busy：窗口留着，可以再点
    } else {
      toast(err && (err.status || err.isTimeout) ? (err.message || '网络错误') : '网络错误', 'danger');
    }
  }

  function confirmNow() {
    var s = current;
    if (!s || !s.preview || s.busy || s.done) return;   // 连点只发一次；加入过队列后不再发
    var exclude = [];
    listed(s).forEach(function (item) {
      var id = String(item.album_id);
      if (s.unticked[id]) exclude.push(id);
    });
    if (!chosen(s).length) return;
    setBusy(s, true);
    var body = { kind: s.kind, token: s.preview.token, exclude: exclude };
    if (s.albumIds) body.album_ids = s.albumIds;
    window.apiFetch(CONFIRM_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      timeoutMs: 60000
    })
    .then(function (data) {
      if (!data || data.status !== 'ok') {
        setBusy(s, false);
        refocus(s);
        toast((data && data.message) || '网络错误', 'danger');
        return;
      }
      finish(s, data);
    })
    .catch(function (err) { confirmFailed(s, err); });
  }

  // ── 对外 ──
  function open(kind, options) {
    options = options || {};
    if (!TITLES[kind] || opened) return false;
    if (typeof bootstrap === 'undefined' || !bootstrap.Modal) return false;
    var ids = null;
    if (kind === 'selected_favourites') {
      ids = [];
      (options.albumIds || []).forEach(function (id) {
        id = String(id);
        if (ids.indexOf(id) < 0) ids.push(id);
      });
      if (!ids.length) return false;
    }
    build();
    current = { kind: kind, albumIds: ids, opener: options.opener || document.activeElement || null,
      onDone: options.onDone, preview: null, unticked: {}, boxes: [], busy: false, done: false };
    parts.title.textContent = TITLES[kind];
    parts.notice.hidden = true;
    opened = true;
    modal().show();
    loadPreview(current);
    return true;
  }

  // 列清单时离开了页面（请求被中止）、又从往返缓存回来：重新列一次
  window.addEventListener('pageshow', function (event) {
    if (event.persisted && opened && current && !current.preview && !current.busy) loadPreview(current);
  });

  window.batchDownloadDialog = {
    open: open,
    isOpen: function () { return opened; }
  };
})();
