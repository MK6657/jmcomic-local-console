/**
 * 通用工具函数
 * 依赖: showToast (由 base.html 内联定义)
 * 被 sse-client.js, home.js, downloads.js 及各页面 JS 依赖
 */

/**
 * HTML 转义（覆盖 base.html 的内联版本，统一为 window 挂载）
 * 合并了 base.html 的 escapeHtml + jobs.js 的 esc()
 * @param {string|null|undefined} str
 * @returns {string}
 */
window.escapeHtml = function (str) {
  if (str === null || str === undefined) return '';
  var div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
};

/**
 * HTML 属性值转义（转义双引号和单引号，防止属性注入）
 * 只用于普通属性值：src="..." / value="..." / title="..." / data-*="..."。
 * 不能用于内联事件属性（on* 事件处理属性）：HTML 解析器会先把实体解码回原字符再执行其中的 JS，
 * 转义形同虚设。数据放进 data-* 属性，由 addEventListener 委托读取。
 * @param {string|null|undefined} str
 * @returns {string}
 */
window.escapeHtmlAttr = function (str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
};

/**
 * ── 请求中止基础设施 + 统一 fetch 封装 ──
 * 维护模块级 in-flight 请求集合，pagehide 时统一 abort，
 * 解决多次页面跳转后残留的 in-flight 请求占满浏览器连接池导致卡死的问题
 */
(function () {
  'use strict';

  /** @type {Set<AbortController>} 当前所有 in-flight 请求的 AbortController */
  var _inflight = new Set();

  /** @type {Object<string, AbortController>} abortKey → controller（同键新请求自动中止旧请求） */
  var _keyed = {};

  /**
   * 注册一个 AbortController 到 in-flight 集合（pagehide 时会被统一 abort）
   * 供需要拿到原始 Response 的裸 fetch（如导出文件下载）使用；
   * 请求结束后必须调用 unregisterAbortable 移除
   * @param {AbortController} controller
   * @returns {AbortController} 原样返回，便于链式使用
   */
  window.registerAbortable = function (controller) {
    if (controller) _inflight.add(controller);
    return controller;
  };

  /**
   * 请求结束（成功/失败）后从 in-flight 集合移除
   * @param {AbortController} controller
   */
  window.unregisterAbortable = function (controller) {
    if (controller) _inflight.delete(controller);
  };

  // 页面隐藏/卸载（含进入 bfcache）时 abort 所有 in-flight 请求，释放浏览器连接池
  window.addEventListener('pagehide', function () {
    _inflight.forEach(function (c) {
      try { c.abort(); } catch (e) { /* 忽略 */ }
    });
    _inflight.clear();
    _keyed = {};
  });

  /**
   * 统一 fetch 封装 — 自动 JSON 解析 + 错误处理 + AbortController + 超时
   *
   * 对调用方的行为约定：
   *   - 成功（2xx）→ resolve 解析后的 JSON 对象（与旧版一致）
   *   - HTTP 错误 → reject Error（err.message 为服务端 message 或通用文案，err.status 为状态码）
   *   - 主动超时 → reject Error('请求超时，请稍后重试')，且 err.isTimeout === true
   *   - pagehide / 调用方 abort → reject 原始 AbortError（err.name === 'AbortError'），
   *     调用方 .catch 应以 `if (err && err.name === 'AbortError') return;` 静默处理
   *
   * @param {string} url
   * @param {object} [options] 标准 fetch options，额外支持：
   *   - timeoutMs {number} 超时毫秒数，默认 30000（搜索接口服务端最长 25s、详情 20s，默认值须 >= 26s）；<= 0 表示不设超时
   *   - abortKey {string} 去重键：同键的上一个未完成请求会被自动 abort（防止快速翻页/刷新堆积请求）
   *   - signal 若调用方已传 signal 则完全尊重调用方（不创建 controller、不注册、不加超时），
   *     调用方可通过 registerAbortable 自行登记以获得 pagehide 清理
   * @returns {Promise<object>}
   */
  window.apiFetch = function (url, options) {
    options = options || {};

    // 复制一份 options 并剔除自定义字段，避免修改调用方对象 / 传给 fetch 多余字段
    var fetchOptions = {};
    Object.keys(options).forEach(function (k) {
      if (k !== 'timeoutMs' && k !== 'abortKey') fetchOptions[k] = options[k];
    });

    var controller = null;
    var timer = null;
    var timedOut = false; // 标志位：区分「超时触发的 abort」与「pagehide/去重触发的 abort」

    if (!fetchOptions.signal) {
      controller = new AbortController();
      fetchOptions.signal = controller.signal;
      window.registerAbortable(controller);

      // 同键请求去重：abort 上一个同键请求
      if (options.abortKey) {
        var prev = _keyed[options.abortKey];
        if (prev) {
          try { prev.abort(); } catch (e) { /* 忽略 */ }
        }
        _keyed[options.abortKey] = controller;
      }

      var timeoutMs = (typeof options.timeoutMs === 'number') ? options.timeoutMs : 30000;
      if (timeoutMs > 0) {
        timer = setTimeout(function () {
          timedOut = true;
          controller.abort();
        }, timeoutMs);
      }
    }

    // 请求结束（成功/失败）后的统一清理：清定时器 + 移出 in-flight 集合
    function cleanup() {
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
      if (controller) {
        window.unregisterAbortable(controller);
        if (options.abortKey && _keyed[options.abortKey] === controller) {
          delete _keyed[options.abortKey];
        }
      }
    }

    return fetch(url, fetchOptions)
      .then(function (r) {
        if (r.ok) return r.json();
        // 非 2xx → 尝试解析错误消息抛出（body 不是 JSON 时退化为通用消息）
        return r.json().then(function (data) {
          var e = new Error(data.message || '请求失败 (HTTP ' + r.status + ')');
          e.status = r.status;
          throw e;
        }, function () {
          var e = new Error('请求失败 (HTTP ' + r.status + ')');
          e.status = r.status;
          throw e;
        });
      })
      .then(function (data) {
        cleanup();
        return data;
      }, function (err) {
        cleanup();
        if (err && err.name === 'AbortError' && timedOut) {
          // 主动超时 → 转成普通错误并带标志位，调用方可提示「请求超时」
          var te = new Error('请求超时，请稍后重试');
          te.isTimeout = true;
          throw te;
        }
        // pagehide / 调用方 abort → 保持 AbortError 原样抛出，由调用方静默处理
        throw err;
      });
  };
})();

/**
 * 确认对话框包装器
 * @param {string} message
 * @returns {boolean}
 */
window.confirmAction = function (message) {
  return confirm(message);
};

/**
 * 打开任务的下载目录（被首页/downloads页共用）
 * @param {string} jobId
 */
window.openFolder = function (jobId) {
  apiFetch('/api/jobs/' + encodeURIComponent(jobId) + '/open-folder', { method: 'POST' })
    .catch(function (err) {
      if (err && err.name === 'AbortError') return; // pagehide 中止，静默
      showToast('打开文件夹失败: ' + err.message, 'danger');
    });
};

/**
 * URL 编码 job_id
 * @param {string} id
 * @returns {string}
 */
window.encodeJobId = function (id) {
  return encodeURIComponent(id);
};

/**
 * “阅读”按钮（搜索 / 资源库 / 收藏 / 下载管理共用），每部漫画都有。
 * 链接始终是 /read/<id>，由服务端在点击时决定：本地可读（core.local_availability）→ 打开本地文件，
 * 否则转到在线阅读 /online/<id>。readable 只决定按钮外观：
 *   true  → 实心 + 书本图标，“已下载内容：打开本地文件阅读”（本地可读不代表整部漫画都已下载）
 *   false → 描边 + 地球图标，“未下载：在线阅读”，读屏名称“阅读（在线）”（aria-label，以可见文字开头；
 *           不用 visually-hidden 文本：它绝对定位，会撑出收藏表格的横向滚动容器，整页可以左右滚动）
 *   'archive_problem'（本地只剩打不开的压缩包）→ 描边 + 书本图标，点开由阅读页说明原因、可改为在线阅读
 *   其他（还没判断）→ 描边 + 书本图标
 * album_id 不是纯数字（/read 会拒绝）时不生成按钮：create 返回 null，html 返回 ''。
 */
(function () {
  var STATES = {
    local: { variant: 'btn-primary', icon: 'bi-book', title: '已下载内容：打开本地文件阅读，无需联网', label: '' },
    online: { variant: 'btn-outline-primary', icon: 'bi-globe2', title: '未下载：在线阅读（从网络加载，不下载、不保存）', label: '阅读（在线）' },
    archive_problem: { variant: 'btn-outline-primary', icon: 'bi-book', title: '本地压缩包打不开：打开后说明原因，可以改为在线阅读', label: '' },
    unknown: { variant: 'btn-outline-primary', icon: 'bi-book', title: '已下载则打开本地文件，否则在线阅读', label: '' }
  };

  function stateName(readable) {
    if (readable === 'archive_problem') return readable;
    return readable === true ? 'local' : (readable === false ? 'online' : 'unknown');
  }

  function apply(link, readable) {
    var name = stateName(readable);
    var state = STATES[name];
    link.classList.remove('btn-primary', 'btn-outline-primary');
    link.classList.add(state.variant);
    link.title = state.title;
    link.setAttribute('data-read-state', name);
    link.textContent = '';
    var icon = document.createElement('i');
    icon.className = 'bi ' + state.icon;
    icon.setAttribute('aria-hidden', 'true');
    link.appendChild(icon);
    link.appendChild(document.createTextNode(' 阅读'));
    if (state.label) link.setAttribute('aria-label', state.label);
    else link.removeAttribute('aria-label');
    return link;
  }

  function create(albumId, readable, extraClass) {
    var id = String(albumId == null ? '' : albumId);
    if (!/^[0-9]{1,20}$/.test(id)) return null;
    var link = document.createElement('a');
    link.className = 'btn' + (extraClass ? ' ' + extraClass : '');
    link.href = '/read/' + encodeURIComponent(id);
    return apply(link, readable);
  }

  window.readLink = {
    /** 新建按钮；extraClass 是页面自己的尺寸/布局类（如 'btn-sm flex-fill'） */
    create: create,
    /** 同 create，返回 HTML 字符串（用字符串拼卡片的页面） */
    html: function (albumId, readable, extraClass) {
      var link = create(albumId, readable, extraClass);
      return link ? link.outerHTML : '';
    },
    /** 可读判断结果回来后更新已有按钮的外观（去向不变） */
    apply: apply,
    /** 列表条目的按钮外观：readable 为 true → 本地；本地只剩打不开的压缩包（problem 为 archive_corrupt /
     *  archive_empty，取自服务端的 archive_problem(s)：与 /read 的去向同一依据，不论下载状态分组）
     *  → 'archive_problem'（/read 打开阅读页说明原因，不说成“未下载：在线阅读”）；其他 → 在线 */
    stateFor: function (readable, problem) {
      if (readable === true) return true;
      return problem === 'archive_corrupt' || problem === 'archive_empty' ? 'archive_problem' : false;
    }
  };
})();

/**
 * 本地文件徽章（搜索 / 详情 / 下载管理 / 收藏 / 资源库共用，判定规则在服务端 core.local_availability）：
 *   problem(reason)       “下载过 · 本地文件不可用”的具体原因：
 *                         deleted 文件已删除 / archive_corrupt 压缩包损坏 / archive_empty 压缩包无可阅读图片
 *   archive(format, title) 压缩包标记（CBZ / ZIP）：只剩压缩包也能离线阅读时，跟在“可离线阅读”后面
 * 返回元素（未知原因 / 格式返回 null）；problemHtml / archiveHtml 返回 HTML 字符串（未知时为 ''）。
 */
(function () {
  var PROBLEMS = {
    deleted: { variant: 'status-badge-muted', icon: '', text: '文件已删除', title: '下载过，但本地文件已不在，需要重新下载' },
    archive_corrupt: { variant: 'status-badge-warning', icon: 'bi-exclamation-triangle', text: '压缩包损坏', title: '下载过，但本地压缩包已损坏、打不开；可以重新下载，或者在线阅读' },
    archive_empty: { variant: 'status-badge-warning', icon: 'bi-exclamation-triangle', text: '压缩包无可阅读图片', title: '下载过，但本地压缩包里没有能阅读的图片；可以重新下载，或者在线阅读' }
  };
  var FORMATS = { cbz: 'CBZ', zip: 'ZIP' };

  function badge(variant, iconName, text, title) {
    var b = document.createElement('span');
    b.className = 'badge ' + variant;
    b.title = title;
    if (iconName) {
      var i = document.createElement('i');
      i.className = 'bi ' + iconName;
      i.setAttribute('aria-hidden', 'true');
      b.appendChild(i);
      b.appendChild(document.createTextNode(' '));
    }
    b.appendChild(document.createTextNode(text));
    return b;
  }

  function problem(reason) {
    var spec = PROBLEMS[reason];
    return spec ? badge(spec.variant, spec.icon, spec.text, spec.title) : null;
  }

  function archive(format, title) {
    var label = FORMATS[format];
    if (!label) return null;
    return badge('status-badge-archive', 'bi-file-zip', label,
      title || '本地只保留了压缩包（' + label + '），直接从压缩包离线阅读，不解压到下载目录');
  }

  function html(node) { return node ? node.outerHTML : ''; }

  window.localBadges = {
    problem: problem,
    archive: archive,
    problemHtml: function (reason) { return html(problem(reason)); },
    archiveHtml: function (format, title) { return html(archive(format, title)); },
    /** 原因的文字（没有 / 未知 → ''） */
    problemText: function (reason) { return PROBLEMS[reason] ? PROBLEMS[reason].text : ''; }
  };
})();

/**
 * 检查新章节的标记与文案（详情 / 资源库 / 收藏 / 下载管理 / 设置共用，数据来自 /api/updates 与列表接口的 item.update）：
 *   chip(update, {compact, link, albumId}) 列表里的“有新章节 · N 话”（--primary 边框）/“章节有变动”（warning）；
 *                         只在检查已确认有新章节（new_count > 0）或章节有变动时返回元素，其余一律 null——列表只显示确认过的事实。
 *                         compact：收藏表格里的短写“新章节 · N”（“有”“话”只给读屏）；link：下载管理里可以点开详情的 <a>
 *   chipHtml(...)         同 chip，返回 HTML 字符串（没有时为 ''）
 *   formatTime(iso, now)  过去的时间：刚刚 / N 分钟前 / 今天 HH:mm / 昨天 HH:mm / M月D日 HH:mm / YYYY年M月D日 HH:mm
 *   formatAfter(iso, now) 将来的时间：稍后 / 约 N 分钟后 / 约 N 小时后 / 今天 HH:mm / 明天 HH:mm / M月D日 HH:mm
 *   about(iso, now)       拼句子用的“约 …”：formatAfter 已带“约”或是“稍后”时原样返回，否则前面加“约 ”
 *   reasonText(kind)      没检查成功的原因
 * 章节标题来自上游：只经 textContent / title 进入 DOM。时间是服务端的本地时间字符串（不带时区）。
 */
(function () {
  var REASONS = {
    network: '连不上服务器',
    timeout: '服务器响应太慢',
    not_found: '上游暂时找不到这部漫画（可能已下架）',
    upstream_error: '服务器返回的章节列表无法识别'
  };
  var MINUTE = 60000;
  var HOUR = 3600000;

  function toDate(value) {
    if (value instanceof Date) return isNaN(value.getTime()) ? null : value;
    if (typeof value === 'number') return new Date(value);
    if (!value) return null;
    var d = new Date(String(value));
    return isNaN(d.getTime()) ? null : d;
  }

  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function hm(d) { return pad(d.getHours()) + ':' + pad(d.getMinutes()); }
  function monthDay(d) { return (d.getMonth() + 1) + '月' + d.getDate() + '日 ' + hm(d); }
  // 相差几个日历日（按本地日期，不受夏令时影响）
  function dayDiff(a, b) {
    var da = new Date(a.getFullYear(), a.getMonth(), a.getDate());
    var db = new Date(b.getFullYear(), b.getMonth(), b.getDate());
    return Math.round((da - db) / 86400000);
  }

  function formatTime(iso, now) {
    var t = toDate(iso);
    if (!t) return '';
    var n = toDate(now) || new Date();
    var diff = n - t;
    if (diff > -MINUTE && diff < MINUTE) return '刚刚';
    if (diff >= MINUTE && diff < HOUR) return Math.floor(diff / MINUTE) + ' 分钟前';
    var days = dayDiff(n, t);
    if (days === 0) return '今天 ' + hm(t);
    if (days === 1) return '昨天 ' + hm(t);
    if (t.getFullYear() === n.getFullYear()) return monthDay(t);
    return t.getFullYear() + '年' + monthDay(t);
  }

  function formatAfter(iso, now) {
    var t = toDate(iso);
    var n = toDate(now) || new Date();
    if (!t) return '稍后';
    var diff = t - n;
    if (diff <= 0) return '稍后';
    if (diff < HOUR) return '约 ' + Math.max(1, Math.floor(diff / MINUTE)) + ' 分钟后';
    if (diff < 6 * HOUR) return '约 ' + Math.max(1, Math.round(diff / HOUR)) + ' 小时后';
    var days = dayDiff(t, n);
    if (days === 0) return '今天 ' + hm(t);
    if (days === 1) return '明天 ' + hm(t);
    return monthDay(t);
  }

  function about(iso, now) {
    var text = formatAfter(iso, now);
    return text === '稍后' || text.indexOf('约') === 0 ? text : '约 ' + text;
  }

  function reasonText(kind) {
    return REASONS[kind] || '原因未知';
  }

  function count(update) {
    var n = Number(update && update.new_count);
    return n > 0 ? n : 0;
  }

  // 新章节的名字（最多 5 个，上游顺序）：“第13话、第14话…”，没有名字时为 ''
  function names(update, n) {
    var titles = ((update && update.titles) || []).map(function (t) { return String(t || '').trim(); })
      .filter(Boolean).slice(0, 5);
    if (!titles.length) return '';
    return titles.join('、') + (n > titles.length ? '…' : '');
  }

  function newTitle(update, link, now) {
    var n = count(update);
    var when = formatTime(update.confirmed_at, now);
    var head = '上游在你下载之后新出了 ' + n + ' 话' + (when ? '（' + when + ' 确认）' : '');
    if (link) return head + '；打开详情选择下载';
    var list = names(update, n);
    return head + (list ? '：' + list : '') + '。检查不会自动下载，打开详情选择下载';
  }

  function changedTitle(update) {
    return '上游新出现 ' + count(update) + ' 话，另有 ' + (Number(update.removed_count) || 0)
      + ' 话已不在上游；打开详情核对';
  }

  function chip(update, opts) {
    opts = opts || {};
    if (!update) return null;
    var changed = update.state === 'changed';
    var n = count(update);
    if (!changed && !n) return null;
    var id = String(opts.albumId == null ? '' : opts.albumId);
    var link = opts.link && /^[0-9]{1,20}$/.test(id);
    var node = document.createElement(link ? 'a' : 'span');
    node.className = 'badge ' + (changed ? 'status-badge-warning' : 'status-badge-update');
    if (link) node.href = '/album/' + encodeURIComponent(id);
    node.title = changed ? changedTitle(update) : newTitle(update, link, opts.now);
    var icon = document.createElement('i');
    icon.className = 'bi ' + (changed ? 'bi-exclamation-triangle' : 'bi-bell');
    icon.setAttribute('aria-hidden', 'true');
    node.appendChild(icon);
    if (changed) {
      node.appendChild(document.createTextNode(' 章节有变动'));
    } else if (opts.compact) {
      // 收藏表格里的短写：看得见的是“新章节 · N”，读屏读“有新章节 · N 话”
      node.appendChild(document.createTextNode(' '));
      var pre = document.createElement('span');
      pre.className = 'visually-hidden';
      pre.textContent = '有';
      node.appendChild(pre);
      node.appendChild(document.createTextNode('新章节 · ' + n));
      var post = document.createElement('span');
      post.className = 'visually-hidden';
      post.textContent = ' 话';
      node.appendChild(post);
    } else {
      node.appendChild(document.createTextNode(' 有新章节 · ' + n + ' 话'));
    }
    return node;
  }

  window.updateBadges = {
    chip: chip,
    chipHtml: function (update, opts) {
      var node = chip(update, opts);
      return node ? node.outerHTML : '';
    },
    formatTime: formatTime,
    formatAfter: formatAfter,
    about: about,
    reasonText: reasonText
  };
})();
