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

  function cacheElements() {
    els.form = document.getElementById(CONFIG.formId);
    els.saveBtn = document.getElementById(CONFIG.saveBtnId);
    els.resetBtn = document.getElementById(CONFIG.resetBtnId);
  }

  // ── API 调用（统一走 apiFetch，自带 AbortController/超时/pagehide 清理） ──

  async function fetchSettings() {
    var data = await window.apiFetch(CONFIG.apiEndpoint, { timeoutMs: 15000 });
    return data.settings || {};
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

  function populateForm(settings) {
    if (!els.form) return;

    var keys = Object.keys(settings);
    for (var ki = 0; ki < keys.length; ki++) {
      var key = keys[ki];
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
      if (!formData.has(cb.name)) {
        values[cb.name] = 'false';
      }
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
      // 只检查 http/https 代理
      if (!/^https?:\/\//i.test(proxy)) {
        errors.push({ field: 'proxy', message: '代理地址需以 http:// 或 https:// 开头' });
      }
    }

    // 数值字段验证
    for (var ri = 0; ri < FIELD_RULES.length; ri++) {
      var rule = FIELD_RULES[ri];
      var val = settings[rule.name];
      if (val === undefined || val === '') continue;
      var num = Number(val);
      if (isNaN(num)) {
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
    if (!file) return;
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
        showToast(msg, 'success');
        // 刷新页面显示最新设置
        await loadSettings();
      } else {
        showToast('导入失败: ' + (data.message || '未知错误'), 'danger');
      }
    } catch (err) {
      if (err && err.name === 'AbortError') return; // pagehide 中止，静默（finally 仍会恢复按钮）
      showToast('导入失败: ' + err.message, 'danger');
    } finally {
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

    if (!els.form) return;

    var settings = collectFormValues();

    // 如果 delete_originals 开启，弹窗确认
    if (settings.delete_originals === 'true') {
      if (!confirm('警告：已开启「打包后删除原始目录」！\n\n开启此选项后，每次自动打包完成后将永久删除原始图片目录。\n此操作不可撤销！\n\n确定要启用此功能吗？')) {
        return;
      }
    }

    // 验证
    clearFieldErrors();
    var errors = validateSettings(settings);

    if (errors.length > 0) {
      for (var ei = 0; ei < errors.length; ei++) {
        showFieldError(errors[ei].field, errors[ei].message);
      }
      // 聚焦第一个错误字段
      var firstError = errors[0];
      var firstInput = els.form.querySelector('[name="' + firstError.field + '"]');
      if (firstInput) firstInput.focus();
      return;
    }

    // 禁用提交按钮 + 加载状态
    if (els.saveBtn) {
      els.saveBtn.disabled = true;
      els.saveBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span> 保存中...';
    }

    try {
      var result = await saveSettings(settings);
      // 保存成功后，用服务器返回的最新设置更新页面，无需手动刷新
      if (result && result.status === 'ok' && result.settings) {
        populateForm(result.settings);
        // 重新同步自动打包联动状态
        var autoPackCb = document.getElementById('auto_pack');
        var deleteOriginalsCb = document.getElementById('delete_originals');
        if (autoPackCb && deleteOriginalsCb) {
          if (autoPackCb.checked) {
            deleteOriginalsCb.disabled = false;
          } else {
            deleteOriginalsCb.checked = false;
            deleteOriginalsCb.disabled = true;
          }
        }
      }
      showToast('设置已保存', 'success');
    } catch (err) {
      if (err && err.name === 'AbortError') return; // pagehide 中止，静默（finally 仍会恢复按钮）
      // 保存失败时，回滚到服务端当前值
      showToast('保存失败: ' + err.message + '，已回滚', 'danger');
      await loadSettings();
    } finally {
      if (els.saveBtn) {
        els.saveBtn.disabled = false;
        els.saveBtn.innerHTML = '<i class="bi bi-floppy me-1"></i> 保存设置';
      }
    }
  }



  // ── 加载设置 ──

  async function loadSettings() {
    try {
      var settings = await fetchSettings();
      populateForm(settings);
      initSensitiveFields();
    } catch (err) {
      if (err && err.name === 'AbortError') return; // pagehide 中止，静默
      showToast('加载设置失败: ' + err.message, 'danger');
    }
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
      function syncDeleteOriginals() {
        if (autoPackCheckbox.checked) {
          deleteOriginalsCheckbox.disabled = false;
        } else {
          deleteOriginalsCheckbox.checked = false;
          deleteOriginalsCheckbox.disabled = true;
        }
      }
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

    // 加载设置
    loadSettings();

    // 绑定事件
    bindEvents();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
