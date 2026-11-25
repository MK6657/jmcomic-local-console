// 下载管理和资源库共用的本地统一导出（优先直接保存到项目「导出」目录，不占用浏览器下载目录）。
(function () {
  'use strict';

  var _activeExports = Object.create(null);

  window.openExportFolder = function (filename) {
    var options = { method: 'POST' };
    if (filename) {
      options.headers = { 'Content-Type': 'application/json' };
      options.body = JSON.stringify({ filename: String(filename) });
    }
    var req = window.apiFetch
      ? window.apiFetch('/api/export/open-folder', options)
      : fetch('/api/export/open-folder', options).then(function (r) {
        if (!r.ok) return r.json().then(function (d) { throw new Error((d && d.message) || '打开失败'); });
        return r.json();
      });
    return req.catch(function (err) {
      if (err && err.name === 'AbortError') return;
      showToast('打开导出文件夹失败: ' + err.message, 'danger');
    });
  };

  window.downloadExport = async function (url, label, basename) {
    var key = String(url || '');
    if (_activeExports[key]) return;
    _activeExports[key] = true;
    var controller = window.registerAbortable(new AbortController());
    showToast('正在导出 ' + label + ' 到「导出」文件夹…', 'info');
    try {
      var response = await fetch(url, {
        method: 'POST',
        headers: { 'X-Export-Target': 'local' },
        signal: controller.signal,
      });
      if (!response.ok) {
        var problem = null;
        try { problem = await response.json(); } catch (_) {}
        throw new Error((problem && problem.message) || ('导出失败 (HTTP ' + response.status + ')'));
      }
      // 部分下载工具接管附件，对页面只返回 204。
      if (response.status === 204) {
        showToast(label + ' 已交给下载工具保存，请在下载工具里查看', 'info');
        return;
      }
      var contentType = response.headers && response.headers.get ? String(response.headers.get('Content-Type') || '') : '';
      if (contentType.toLowerCase().indexOf('application/json') !== -1) {
        var saved = await response.json();
        if (!saved || saved.status !== 'ok' || !saved.saved || !saved.filename) {
          throw new Error((saved && saved.message) || '导出结果异常');
        }
        showToast(label + ' 已保存到「导出」文件夹：' + saved.filename, 'success', {
          label: '打开目录',
          onClick: function () { window.openExportFolder(saved.filename); },
        });
        return;
      }
      var blob = await response.blob();
      if (!blob.size) throw new Error('导出的文件是空的');
      var disposition = response.headers && response.headers.get ? response.headers.get('Content-Disposition') : '';
      var filename = '';
      if (disposition) {
        var starMatch = disposition.match(/filename\*\s*=\s*(?:UTF-8''|utf-8'')([^;]+)/);
        if (starMatch) {
          try { filename = decodeURIComponent(starMatch[1].trim().replace(/^["']|["']$/g, '')); } catch (_) {}
        }
        if (!filename) {
          var plainMatch = disposition.match(/filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;]+)/);
          if (plainMatch) filename = (plainMatch[1] || plainMatch[2] || '').trim();
        }
        filename = filename.replace(/[\\/\r\n]+/g, '_').trim();
      }
      var link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      try {
        link.download = filename || ((basename || 'download') + '.' + label.toLowerCase());
        document.body.appendChild(link);
        link.click();
      } finally {
        link.remove();
        URL.revokeObjectURL(link.href);
      }
      showToast(label + ' 导出完成', 'success');
    } catch (error) {
      if (error.name !== 'AbortError') showToast(label + ' 导出失败：' + error.message, 'danger');
    } finally {
      delete _activeExports[key];
      window.unregisterAbortable(controller);
    }
  };
})();
