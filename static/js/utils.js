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
 * 用于 src="..."  /  value="..."  /  onclick='...' 等属性上下文
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
