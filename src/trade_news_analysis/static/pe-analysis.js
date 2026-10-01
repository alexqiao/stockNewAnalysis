(() => {
  'use strict';
  let root = document.getElementById('pe-analysis');
  if (!root?.dataset.endpoint) return;
  let busy = false, dirty = false;
  const endpoint = root.dataset.endpoint;
  function message(text) {
    const target = root.querySelector('#pe-message'); target.textContent = text; target.classList.remove('hidden');
  }
  function payload() {
    const number = input => input.value.trim() === '' ? null : Number(input.value);
    const overrides = {};
    root.querySelectorAll('[data-override]').forEach(input => { overrides[input.dataset.override] = number(input); });
    const assumptions = [...root.querySelectorAll('#pe-table tbody tr')].map(row => {
      const value = name => number(row.querySelector(`[data-assumption="${name}"]`));
      const revenueGrowth = value('revenue_growth'), incomeGrowth = value('net_income_growth');
      return {year_offset: Number(row.dataset.yearOffset), revenue_growth: revenueGrowth === null ? null : revenueGrowth / 100,
        net_income_growth: incomeGrowth === null ? null : incomeGrowth / 100, pe_low: value('pe_low'), pe_high: value('pe_high')};
    });
    return {overrides, assumptions};
  }
  function setBusy(value, freeze) {
    busy = value; root.inert = value && freeze;
    root.querySelectorAll('button').forEach(button => { button.disabled = value; });
  }
  async function renderLatest() {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(window.location.href, {signal: controller.signal});
      if (!response.ok) throw new Error('无法更新估值摘要');
      const updated = new DOMParser().parseFromString(await response.text(), 'text/html').getElementById('pe-analysis');
      if (!updated) throw new Error('缺少估值摘要');
      if (dirty) {
        root.querySelectorAll('[data-override], [data-assumption]').forEach(input => {
          const attribute = input.hasAttribute('data-override') ? 'data-override' : 'data-assumption';
          const row = input.closest('[data-year-offset]');
          const selector = (row ? `[data-year-offset="${row.dataset.yearOffset}"] ` : '') + `[${attribute}="${input.getAttribute(attribute)}"]`;
          const replacement = updated.querySelector(selector); if (replacement) replacement.value = input.value;
        });
      }
      root.replaceWith(updated); root = updated;
    } finally { window.clearTimeout(timer); }
  }
  async function operate(refresh, automatic = false) {
    if (busy) return;
    if (!refresh && [...root.querySelectorAll('input')].some(input => !input.reportValidity())) return;
    const body = refresh ? undefined : JSON.stringify(payload());
    setBusy(true, !automatic); message(refresh ? '正在刷新基础数据…' : '正在保存估值假设…');
    try {
      await window.RunPoller.request(endpoint + (refresh ? '/refresh' : ''), {
        method: refresh ? 'POST' : 'PUT', headers: {'Content-Type': 'application/json'}, body,
      });
      if (!refresh) dirty = false;
      try { await renderLatest(); message(refresh ? (dirty ? '基础数据已刷新；未保存的估值草稿已保留，请保存后重算。' : '基础数据已刷新。') : '估值假设已保存并重算。'); }
      catch (_) { message('操作已保存；暂时无法更新摘要，当前输入保留，可稍后重新查看。'); }
    } catch (error) { message('操作失败：' + error.message); }
    finally { setBusy(false, false); }
  }
  document.addEventListener('input', event => { if (root.contains(event.target)) dirty = true; });
  document.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button || !root.contains(button) || button.disabled || busy) return;
    if (button.id === 'save-pe') operate(false);
    else if (button.id === 'refresh-pe') operate(true);
    else if (button.id === 'clear-pe-overrides') {
      root.querySelectorAll('[data-override]').forEach(input => { input.value = ''; });
      dirty = true; message('人工覆盖已在草稿中清除，点击“保存并重算”后生效。');
    }
  });
  if (root.dataset.refreshRecommended === 'true') operate(true, true);
})();
