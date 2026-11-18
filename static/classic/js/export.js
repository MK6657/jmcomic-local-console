// 下载管理和资源库共用的附件下载。离开页面时由 utils.js 中止尚在生成的附件。
window.downloadExport = async function (url, label, basename) {
  'use strict';
  var controller = window.registerAbortable(new AbortController());
  showToast('正在生成 ' + label + ' 文件…', 'info');
  try {
    var response = await fetch(url, {method: 'POST', signal: controller.signal});
    if (!response.ok) {
      var problem = await response.json();
      throw new Error(problem.message || '导出失败');
    }
    // 部分下载工具接管附件，对页面只返回 204。
    if (response.status === 204) {
      showToast(label + ' 已交给下载工具保存，请在下载工具里查看', 'info');
      return;
    }
    var blob = await response.blob();
    if (!blob.size) throw new Error('导出的文件是空的');
    var link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    try {
      link.download = (basename || 'download') + '.' + label.toLowerCase();
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
    window.unregisterAbortable(controller);
  }
};
