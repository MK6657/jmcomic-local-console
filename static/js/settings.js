/**
 * JMComic 下载控制台 — 设置页 JavaScript
 * 加载/保存设置、敏感字段显示切换、表单验证
 * 依赖: utils.js (window.apiFetch / registerAbortable / unregisterAbortable)
 */

(function () {
  'use strict';

  var CONFIG = {
    formId: 'settings-form',
    saveBtnId: 'save-settings-btn',
    resetBtnId: 'settings-reset-btn',
    apiEndpoint: '/api/settings',
    toastContainerId: 'toast-container',
    // 敏感设置字段名（当前版本无敏感字段，预留）
    sensitiveFields: [],
  };

  // ── DOM 缓存 ──
  var els = {};
  var editVersion = 0;
  var fieldEdits = Object.create(null);
  var loadVersion = 0;
  var saving = false;
  var importing = false;
  var settingsReady = false;
  var initialLoad = null;

  function cacheElements() {
    els.form = document.getElementById(CONFIG.formId);
    els.saveBtn = document.getElementById(CONFIG.saveBtnId);
    els.resetBtn = document.getElementById(CONFIG.resetBtnId);
  }

  // ── API 调用（统一走 apiFetch，自带 AbortController/超时/pagehide 清理） ──

  async function fetchSettings() {
    var data = await window.apiFetch(CONFIG.apiEndpoint, { timeoutMs: 15000, cache: 'no-store' });
    if (!data || data.status !== 'ok' || !data.settings || typeof data.settings !== 'object' || Array.isArray(data.settings)) {
      throw new Error((data && data.message) || '服务器没有返回有效设置');
    }
    return data.settings;
  }

  async function saveSettings(settings) {
    // HTTP 错误时 apiFetch 会抛出带服务端 message 的 Error
    return window.apiFetch(CONFIG.apiEndpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settings),
      timeoutMs: 15000,
    });
  }

  // ── 表单填充 ──

  function syncDeleteOriginals() {
    var autoPack = document.getElementById('auto_pack');
    var deleteOriginals = document.getElementById('delete_originals');
    if (!autoPack || !deleteOriginals) return;
    deleteOriginals.disabled = !autoPack.checked;
    if (!autoPack.checked) deleteOriginals.checked = false;
  }

  function populateForm(settings, preserveEditsAfter) {
    if (!els.form) return;

    var keys = Object.keys(settings);
    for (var ki = 0; ki < keys.length; ki++) {
      var key = keys[ki];
      if (preserveEditsAfter !== undefined && fieldEdits[key] > preserveEditsAfter) continue;
      var value = settings[key];
      var input = els.form.querySelector('[name="' + key + '"]');
      if (!input) continue;

      if (input.type === 'checkbox') {
        input.checked = value === 'true' || value === true;
      } else if (input.type === 'radio') {
        var radio = els.form.querySelector('[name="' + key + '"][value="' + value + '"]');
        if (radio) radio.checked = true;
      } else {
        input.value = value;
      }
    }
    syncDeleteOriginals();
  }

  function collectFormValues() {
    if (!els.form) return {};

    var formData = new FormData(els.form);
    var values = {};

    for (var entry of formData.entries()) {
      values[entry[0]] = entry[1];
    }

    // 处理复选框：未勾选的不会出现在 FormData 中
    var checkboxes = els.form.querySelectorAll('input[type="checkbox"]');
    checkboxes.forEach(function (cb) {
      if (cb.name) values[cb.name] = !cb.disabled && cb.checked ? 'true' : 'false';
    });

    return values;
  }

  // ── 表单验证 ──

  var FIELD_RULES = [
    { name: 'timeout', label: '超时时间', min: 5, max: 120, defaultHint: '建议范围：10-60 秒' },
    { name: 'retry_times', label: '重试次数', min: 0, max: 20, defaultHint: '建议范围：1-5' },
    { name: 'max_running_jobs', label: '最大同时任务数', min: 1, max: 5, defaultHint: '建议范围：1-3' },
    { name: 'image_threads', label: '图片并发数', min: 1, max: 50, defaultHint: '建议范围 10-30' },
    { name: 'photo_threads', label: '章节并发数', min: 1, max: 10, defaultHint: '建议保持为 1' },
    { name: 'schedule_start', label: '开始时间', min: 0, max: 23, defaultHint: '取值范围：0-23' },
    { name: 'schedule_end', label: '结束时间', min: 0, max: 23, defaultHint: '取值范围：0-23' },
  ];

  function validateSettings(settings) {
    var errors = [];

    // 下载目录不能为空
    if (!settings.download_root || !settings.download_root.trim()) {
      errors.push({ field: 'download_root', message: '下载目录不能为空' });
    }

    // 代理地址格式检查（如果有的话）
    if (settings.proxy && settings.proxy.trim()) {
      var proxy = settings.proxy.trim();
      if (!/^(https?|socks5h?):\/\//i.test(proxy)) {
        errors.push({ field: 'proxy', message: '代理地址需以 http://、https://、socks5:// 或 socks5h:// 开头' });
      }
    }

    // 数值字段验证
    for (var ri = 0; ri < FIELD_RULES.length; ri++) {
      var rule = FIELD_RULES[ri];
      var val = settings[rule.name];
      if (val === undefined || val === '') continue;
      var num = Number(val);
      if (!Number.isInteger(num)) {
        errors.push({ field: rule.name, message: rule.label + ' 必须是有效数字' });
      } else if (rule.min !== undefined && num < rule.min) {
        errors.push({ field: rule.name, message: rule.label + ' 不能小于 ' + rule.min });
      } else if (rule.max !== undefined && num > rule.max) {
        errors.push({ field: rule.name, message: rule.label + ' 不能大于 ' + rule.max });
      }
    }

    return errors;
  }

  function showFieldError(fieldName, message) {
    var input = els.form.querySelector('[name="' + fieldName + '"]');
    if (!input) return;

    input.classList.add('is-invalid');

    // 从最近的父级查找 invalid-feedback 容器
    var parent = input.closest('.mb-3') || input.parentElement;
    var feedback = parent ? parent.querySelector('.invalid-feedback') : null;
    if (!feedback) {
      feedback = input.parentElement.querySelector('.invalid-feedback');
    }
    if (!feedback) {
      // 动态创建
      feedback = document.createElement('div');
      feedback.className = 'invalid-feedback';
      if (input.nextElementSibling) {
        input.parentElement.insertBefore(feedback, input.nextElementSibling);
      } else {
        input.parentElement.appendChild(feedback);
      }
    }
    feedback.textContent = message;
  }

  function clearFieldErrors() {
    if (!els.form) return;
    els.form.querySelectorAll('.is-invalid').forEach(function (el) {
      el.classList.remove('is-invalid');
    });
    els.form.querySelectorAll('.invalid-feedback').forEach(function (el) {
      // 只清空文本，不删除元素（保持 DOM 结构）
      if (!el.hasAttribute('data-dynamic')) {
        el.textContent = '';
      }
    });
  }



  // ── 导入 / 导出 ──

  async function handleExport() {
    // 需要原始 Response（blob），不走 apiFetch；手动注册 AbortController，
    // pagehide 时由 utils.js 统一 abort；30s 超时行为与原实现一致
    var controller = new AbortController();
    window.registerAbortable(controller);
    var timedOut = false;
    var timer = setTimeout(function () {
      timedOut = true;
      controller.abort();
    }, 30000);
    try {
      var resp = await fetch('/api/settings/export', { signal: controller.signal });
      if (!resp.ok) throw new Error('下载失败: ' + resp.status);
      var blob = await resp.blob();
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url;
      a.download = 'settings.json';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      showToast('设置已导出', 'success');
    } catch (err) {
      if (err && err.name === 'AbortError') {
        if (!timedOut) return; // pagehide 中止，静默
        err = new Error('请求超时，请稍后重试');
      }
      showToast('导出失败: ' + err.message, 'danger');
    } finally {
      clearTimeout(timer);
      window.unregisterAbortable(controller);
    }
  }

  async function handleImport(file) {
    if (!file || importing || saving) return;
    importing = true;
    loadVersion += 1; // An older initial GET must not overwrite imported settings.
    var importedVersion = editVersion;
    var formData = new FormData();
    formData.append('file', file);

    // 导入按钮加载状态
    var importBtn = document.getElementById('import-settings-btn');
    if (importBtn) {
      importBtn.disabled = true;
      importBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status"></span> 导入中...';
    }

    try {
      var data = await window.apiFetch('/api/settings/import', {
        method: 'POST',
        body: formData,
        timeoutMs: 60000,
      });
      if (data.status === 'ok') {
        var msg = '导入了 ' + data.imported + ' 项';
        if (data.skipped > 0) msg += '，跳过 ' + data.skipped + ' 项';
        if (Array.isArray(data.warnings) && data.warnings.length) {
          showToast(msg + '；' + data.warnings.join('；'), 'warning');
        } else {
          showToast(msg, 'success');
        }
        // 刷新页面显示最新设置
        await loadSettings(importedVersion);
      } else {
        showToast('导入失败: ' + (data.message || '未知错误'), 'danger');
      }
    } catch (err) {
      if (err && err.name === 'AbortError') return; // pagehide 中止，静默（finally 仍会恢复按钮）
      showToast('导入失败: ' + err.message, 'danger');
    } finally {
      importing = false;
      if (importBtn) {
        importBtn.disabled = false;
        importBtn.innerHTML = '<i class="bi bi-upload"></i> 导入设置';
      }
    }
  }

  // ── 恢复默认（暂未开放 UI 按钮，保留函数留待后续） ──

  async function handleReset() {
    if (!confirm('确定要恢复所有设置为默认值吗？')) return;

    try {
      await window.apiFetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ _reset: true }),
        timeoutMs: 15000,
      });
      showToast('已恢复默认设置', 'info');
      await loadSettings();
    } catch (err) {
      if (err && err.name === 'AbortError') return; // pagehide 中止，静默
      showToast('恢复默认失败: ' + err.message, 'danger');
    }
  }

  // ── 保存处理 ──

  async function handleSave(e) {
    if (e) e.preventDefault();

    if (!els.form || saving || importing) return;
    saving = true;
    // 禁用提交按钮 + 加载状态
    if (els.saveBtn) {
      els.saveBtn.disabled = true;
      els.saveBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span> 保存中...';
    }

    try {
      // Never submit a whole form based on placeholders while its persisted
      // baseline is still loading. The load merges only untouched fields.
      if (!settingsReady) {
        if (!initialLoad) initialLoad = loadSettings();
        await initialLoad;
        if (!settingsReady) throw new Error('尚未读取到已保存的设置，请稍后重试');
      }
      var settings = collectFormValues();
      if (settings.delete_originals === 'true') {
        if (!confirm('警告：已开启「打包后删除原始目录」！\n\n开启此选项后，每次自动打包完成后将永久删除原始图片目录。\n此操作不可撤销！\n\n确定要启用此功能吗？')) return;
      }
      clearFieldErrors();
      var errors = validateSettings(settings);
      if (errors.length > 0) {
        for (var ei = 0; ei < errors.length; ei++) {
          showFieldError(errors[ei].field, errors[ei].message);
        }
        var firstInput = els.form.querySelector('[name="' + errors[0].field + '"]');
        if (firstInput) firstInput.focus();
        return;
      }
      loadVersion += 1;
      var submittedVersion = editVersion;
      var result = await saveSettings(settings);
      if (!result || result.status !== 'ok' || !result.settings || typeof result.settings !== 'object' || Array.isArray(result.settings)) {
        throw new Error((result && result.message) || '服务器没有确认设置已保存');
      }
      // 保存成功后，用服务器返回的最新设置更新页面，无需手动刷新
      // A newer local edit is still an unsaved draft, not the submitted payload.
      if (editVersion === submittedVersion) {
        populateForm(result.settings);
      }
      if (Array.isArray(result.warnings) && result.warnings.length) {
        showToast('设置已保存；' + result.warnings.join('；'), 'warning');
      } else if (editVersion !== submittedVersion) {
        showToast('设置已保存；保存期间的新修改尚未保存', 'info');
      } else {
        showToast('设置已保存', 'success');
      }
      loadUpdateSummary(); // “自动检查新章节”开关可能刚改过
    } catch (err) {
      if (err && err.name === 'AbortError') return; // pagehide 中止，静默（finally 仍会恢复按钮）
      // A lost reply does not prove the transaction was rolled back. Keep the
      // draft so a transient failure can be retried without re-entering it.
      showToast('保存未确认: ' + err.message + '，当前修改已保留，请重试', 'danger');
    } finally {
      saving = false;
      if (els.saveBtn) {
        els.saveBtn.disabled = false;
        els.saveBtn.innerHTML = '<i class="bi bi-floppy me-1"></i> 保存设置';
      }
    }
  }



  // ── 检查新章节状态 ──
  // “检查新章节”一节下面的状态（GET /api/updates/summary，只读数据库和内存里的运行状态）：
  // 三行——后台在做什么 / 统计 / 最近一次检查。全部用 textContent（漫画标题来自上游）。
  // 页面打开、导入设置、保存设置后各读一次（开关保存后面板马上跟着变）。

  // 漫画的名字：标题，没有时用车号
  function updateAlbumName(item, quoted) {
    var title = String((item && item.title) || '').trim();
    if (title) return quoted ? '《' + title + '》' : title;
    return '车号 ' + String((item && item.album_id) || '?');
  }

  function updateOutcomeText(last) {
    switch (last.outcome) {
      case 'no_update': return '没有新章节';
      case 'new': return '发现 ' + (Number(last.new_count) || 0) + ' 话新章节';
      case 'changed': return '章节列表有变动';
      case 'baseline': return '已记下 ' + (Number(last.upstream_count) || 0) + ' 话';
      default: return '没检查成功（' + window.updateBadges.reasonText(last.error_kind) + '）';
    }
  }

  function updateSummaryLines(data, now) {
    var fmt = window.updateBadges;
    var runtime = data.runtime || {};
    var counts = data.counts || {};
    var phase;
    switch (runtime.phase) {
      case 'starting': phase = '程序刚启动，' + fmt.about(runtime.resume_at, now) + ' 开始检查'; break;
      case 'idle': phase = '后台检查中：一次只查一部，每部大约一天一次'; break;
      case 'checking': phase = '正在检查：' + updateAlbumName(runtime.current, false); break;
      case 'deferred': phase = '有下载任务在进行，检查等下载结束后继续'; break;
      case 'paused': phase = '连续几次连不上服务器，自动检查暂停，' + fmt.about(runtime.resume_at, now) + ' 再试'; break;
      case 'not_running': phase = '自动检查已开启，但后台检查没有在运行（重启程序后生效）'; break;
      default: phase = '自动检查已关闭。已有的检查结果仍会显示，可以在漫画详情页点「立即检查」。';
    }
    var parts = ['本地有内容的漫画 ' + (Number(counts.targets) || 0) + ' 部', '检查过 ' + (Number(counts.checked) || 0) + ' 部'];
    if (Number(counts.with_updates) > 0) parts.push(counts.with_updates + ' 部有新章节');
    if (Number(counts.failing) > 0) {
      parts.push(counts.failing + ' 部最近一次没检查成功' + (data.enabled ? '（会自动重试）' : '（不会自动重试）'));
    }
    parts.push('过去 24 小时检查 ' + (Number(data.checks_24h) || 0) + ' 次');
    var last = data.last
      ? '最近一次：' + fmt.formatTime(data.last.at, now) + ' · ' + updateAlbumName(data.last, true) + ' · ' + updateOutcomeText(data.last)
      : '还没有检查过';
    return [phase, parts.join(' · '), last];
  }

  function loadUpdateSummary() {
    var box = document.getElementById('update-check-status');
    if (!box || !window.updateBadges) return;
    window.apiFetch('/api/updates/summary', { timeoutMs: 15000, abortKey: 'settings-update-summary' })
      .then(function (data) {
        if (data.status !== 'ok') throw new Error(data.message || '读取检查状态失败');
        box.textContent = '';
        updateSummaryLines(data).forEach(function (text) {
          var line = document.createElement('div');
          line.textContent = text;
          box.appendChild(line);
        });
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return; // pagehide 中止，静默
        box.textContent = '暂时读不到检查状态，请稍后刷新页面';
      });
  }
  // ── 检查新章节状态结束 ──

  // ── 加载设置 ──

  async function loadSettings(expectedEditVersion) {
    var requestVersion = ++loadVersion;
    var requestedEditVersion = expectedEditVersion === undefined ? (settingsReady ? editVersion : 0) : expectedEditVersion;
    try {
      var settings = await fetchSettings();
      if (requestVersion === loadVersion) {
        populateForm(settings, requestedEditVersion);
        settingsReady = true;
        initSensitiveFields();
      }
    } catch (err) {
      initialLoad = null;
      if (err && err.name === 'AbortError') return; // pagehide 中止，静默
      showToast('加载设置失败: ' + err.message, 'danger');
    }
    loadUpdateSummary();
  }



  // ── 事件绑定 ──

  function bindEvents() {
    // 导出按钮
    var exportBtn = document.getElementById('export-settings-btn');
    if (exportBtn) {
      exportBtn.addEventListener('click', handleExport);
    }

    // 导入按钮 → 触发隐藏的 file input
    var importBtn = document.getElementById('import-settings-btn');
    var importFile = document.getElementById('import-settings-file');
    if (importBtn && importFile) {
      importBtn.addEventListener('click', function () {
        importFile.click();
      });
      importFile.addEventListener('change', function () {
        if (importFile.files && importFile.files[0]) {
          handleImport(importFile.files[0]);
          importFile.value = ''; // 重置以便重复选同一文件
        }
      });
    }

    // 保存按钮
    if (els.saveBtn) {
      els.saveBtn.addEventListener('click', handleSave);
    }

    // 自动打包开关联动
    var autoPackCheckbox = document.getElementById('auto_pack');
    var deleteOriginalsCheckbox = document.getElementById('delete_originals');
    if (autoPackCheckbox && deleteOriginalsCheckbox) {
      autoPackCheckbox.addEventListener('change', syncDeleteOriginals);
      syncDeleteOriginals();
    }

    // 表单提交拦截
    if (els.form) {
      els.form.addEventListener('submit', function (e) {
        e.preventDefault();
        handleSave();
      });
    }

    // 恢复默认按钮
    if (els.resetBtn) {
      els.resetBtn.addEventListener('click', handleReset);
    }

    // 输入时清除验证错误
    if (els.form) {
      function markEdited(e) {
        if (e.target && e.target.name) fieldEdits[e.target.name] = ++editVersion;
      }
      els.form.addEventListener('input', markEdited);
      els.form.addEventListener('change', markEdited);
      els.form.addEventListener('input', function (e) {
        var target = e.target;
        if (target.name && target.classList.contains('is-invalid')) {
          target.classList.remove('is-invalid');
          // 清空 invalid-feedback
          var parent = target.closest('.mb-3') || target.parentElement;
          var feedback = parent ? parent.querySelector('.invalid-feedback') : null;
          if (!feedback) {
            feedback = target.parentElement.querySelector('.invalid-feedback');
          }
          if (feedback) feedback.textContent = '';
        }
      });
    }
  }

  // ── 敏感字段 —— 显示切换（预留） ──

  function initSensitiveFields() {
    if (!CONFIG.sensitiveFields || CONFIG.sensitiveFields.length === 0) return;

    CONFIG.sensitiveFields.forEach(function (fieldName) {
      var input = els.form.querySelector('[name="' + fieldName + '"]');
      if (!input) return;

      // 确保 input 是 password 类型
      if (input.type !== 'password') {
        input.type = 'password';
      }

      // 查找或创建切换按钮
      var parent = input.closest('.input-group') || input.parentElement;
      var toggleBtn = parent.querySelector('.toggle-sensitive-btn');

      if (!toggleBtn) {
        toggleBtn = document.createElement('button');
        toggleBtn.type = 'button';
        toggleBtn.className = 'btn btn-outline-secondary toggle-sensitive-btn';
        toggleBtn.setAttribute('data-field', fieldName);
        toggleBtn.innerHTML = '<i class="bi bi-eye"></i>';
        toggleBtn.title = '显示/隐藏';

        if (parent.classList.contains('input-group')) {
          parent.appendChild(toggleBtn);
        } else {
          var wrapper = document.createElement('div');
          wrapper.className = 'input-group';
          input.parentNode.insertBefore(wrapper, input);
          wrapper.appendChild(input);
          wrapper.appendChild(toggleBtn);
        }
      }

      toggleBtn.addEventListener('click', function () {
        var isPassword = input.type === 'password';
        input.type = isPassword ? 'text' : 'password';
        this.innerHTML = isPassword ? '<i class="bi bi-eye-slash"></i>' : '<i class="bi bi-eye"></i>';
        this.title = isPassword ? '隐藏' : '显示';
      });
    });
  }

  // ── 初始化 ──

  function init() {
    cacheElements();

    // 仅在设置页面初始化
    if (!els.form) return;

    // 绑定事件
    bindEvents();

    // Bind edits before starting the asynchronous refresh of the server-rendered form.
    initialLoad = loadSettings();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
