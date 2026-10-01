(() => {
  'use strict';
  const active = new Set();
  async function request(url, options = {}, timeoutMs = 20000) {
    const controller = new AbortController();
    const upstream = options.signal;
    const abort = () => controller.abort();
    if (upstream?.aborted) abort();
    else upstream?.addEventListener('abort', abort, {once: true});
    let timedOut = false;
    const timer = window.setTimeout(() => { timedOut = true; abort(); }, timeoutMs);
    try {
      const response = await fetch(url, {...options, signal: controller.signal});
      let data;
      try { data = await response.json(); }
      catch (_) { throw new Error(`服务器未返回可读取的结果（HTTP ${response.status}）`); }
      if (!response.ok) {
        const detail = data.detail;
        const message = typeof detail === 'string' ? detail : detail?.message ||
          (Array.isArray(detail) ? detail.map(item => item.msg).join('；') : `请求失败（HTTP ${response.status}）`);
        const error = new Error(message); error.status = response.status; error.detail = detail;
        throw error;
      }
      return data;
    } catch (error) {
      if (timedOut) throw new Error('请求超时，后台操作可能仍在进行；请核对状态后重试。');
      throw error;
    } finally {
      window.clearTimeout(timer);
      upstream?.removeEventListener('abort', abort);
    }
  }
  function restore(storageKey) {
    try { const id = Number(sessionStorage.getItem(storageKey)); return Number.isInteger(id) && id > 0 ? id : null; }
    catch (_) { return null; }
  }
  function start({runId, storageKey, onUpdate = () => {}, onComplete = () => {}, onError = () => {}, intervalMs = 2000, maxWaitMs = 600000}) {
    const controller = new AbortController();
    let stopped = false, timer, wake;
    const stop = () => { stopped = true; controller.abort(); window.clearTimeout(timer); wake?.(); active.delete(stop); };
    active.add(stop);
    try { if (storageKey) sessionStorage.setItem(storageKey, String(runId)); } catch (_) { /* Optional recovery. */ }
    (async () => {
      const started = Date.now();
      try {
        while (!stopped) {
          const run = await request('/api/v1/runs/' + runId, {signal: controller.signal});
          if (stopped) return;
          onUpdate(run);
          const terminal = run.is_terminal ?? ['complete', 'completed', 'partial', 'failed'].includes(run.status);
          if (terminal) {
            try { if (storageKey) sessionStorage.removeItem(storageKey); } catch (_) { /* Optional recovery. */ }
            onComplete(run); return;
          }
          if (Date.now() - started >= maxWaitMs) throw new Error('后台仍在运行，可重新打开页面或到运行状态页继续查看。');
          await new Promise(resolve => { wake = resolve; timer = window.setTimeout(resolve, intervalMs); });
        }
      } catch (error) { if (!stopped) onError(error); }
      finally { active.delete(stop); }
    })();
    return {stop};
  }
  window.addEventListener('pagehide', () => { [...active].forEach(stop => stop()); });
  window.RunPoller = {request, start, restore};
})();
