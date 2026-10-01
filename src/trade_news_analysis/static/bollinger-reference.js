'use strict';

(() => {
  const root = document.getElementById('bollinger-reference');
  if (!root) return;
  const endpoint = '/api/v1/securities/' + root.dataset.securityId + '/bollinger-reference';
  const storageKey = 'trade-news:bollinger-reference:v1';
  const form = document.getElementById('bollinger-reference-form');
  const status = document.getElementById('bollinger-reference-status');
  const results = document.getElementById('bollinger-reference-results');
  const parameterMessage = document.getElementById('bollinger-reference-parameter-message');
  const fields = [
    {key: 'squeeze_lookback', label: '缩口分位观察期（日）', value: 120, min: 20, max: 500, step: 1},
    {key: 'squeeze_percentile', label: '缩口分位阈值（%）', value: 20, min: 0, max: 100, step: 'any'},
    {key: 'squeeze_days', label: '连续缩口天数', value: 3, min: 1, max: 20, step: 1},
    {key: 'breakout_min_pct', label: '突破日较前收盘涨幅（%）', value: 1, min: 0, max: 20, step: 'any'},
    {key: 'expansion_ratio', label: '带宽扩张倍数', value: 1.1, min: 1, max: 5, step: 'any'},
    {key: 'touch_tolerance_pct', label: '回踩中轨容差（%）', value: 0.5, min: 0.01, max: 5, step: 'any'},
    {key: 'pullback_max_bars', label: '突破及回踩各自等待上限（日）', value: 20, min: 1, max: 120, step: 1},
    {key: 'max_stop_pct', label: '初始止损距离上限（%）', value: 8, min: 0, exclusiveMin: true, max: 50, step: 'any'},
    {key: 'slope_lookback', label: '中轨斜率观察期（日）', value: 5, min: 1, max: 20, step: 1, advanced: true},
    {key: 'slope_min_pct', label: '中轨最小升幅（%）', value: 0.5, min: 0, max: 20, step: 'any', advanced: true},
    {key: 'lower_slope_lookback', label: '下轨斜率观察期（日）', value: 5, min: 1, max: 20, step: 1, advanced: true},
    {key: 'lower_slope_min_pct', label: '下轨方向变化门槛（%）', value: 0.5, min: 0.01, max: 20, step: 'any', advanced: true},
    {key: 'profit_min_pct', label: '追踪止损浮盈门槛（%）', value: 10, min: 0, max: 100, step: 'any', advanced: true},
    {key: 'gap_min_pct', label: '跳空最小幅度（%）', value: 0.5, min: 0, max: 20, step: 'any', advanced: true},
    {key: 'volume_max_ratio', label: '缩量上限（相对均量）', value: 0.8, min: 0, exclusiveMin: true, max: 1, step: 'any', advanced: true},
    {key: 'contraction_max_ratio', label: '振幅收缩上限倍数', value: 0.7, min: 0, exclusiveMin: true, max: 2, step: 'any', advanced: true},
    {key: 'stop_atr_buffer', label: '止损 ATR 缓冲倍数', value: 0.25, min: 0, max: 3, step: 'any', advanced: true},
    {key: 'support_cluster_pct', label: '支撑聚合容差（%）', value: 0.5, min: 0, max: 5, step: 'any', advanced: true},
  ];
  const basisLabels = {total_return_adjusted: '含分红调整价格', split_adjusted: '拆股调整价格'};
  let adjustment = 'total_return_adjusted';
  let sequence = 0;
  let controller = null;
  let scheduled = null;
  let disposed = false;
  let parameters = Object.fromEntries(fields.map(field => [field.key, field.value]));

  const number = (value, digits = 2) => Number.isFinite(value)
    ? value.toLocaleString('zh-CN', {minimumFractionDigits: digits, maximumFractionDigits: digits}) : '—';
  const time = value => {
    if (!value) return '—';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN');
  };
  const text = (id, value) => { document.getElementById(id).textContent = value; };
  function parameterNotice(message, error = false) {
    parameterMessage.textContent = message;
    parameterMessage.dataset.state = error ? 'error' : 'ready';
  }
  function valid(field, value) {
    return Number.isFinite(value) && (field.exclusiveMin ? value > field.min : value >= field.min) && value <= field.max &&
      (field.step !== 1 || Number.isInteger(value));
  }
  try {
    const stored = localStorage.getItem(storageKey);
    if (stored) {
      const saved = JSON.parse(stored);
      let reset = false;
      fields.forEach(field => {
        if (saved && valid(field, saved[field.key])) parameters[field.key] = saved[field.key];
        else reset = true;
      });
      if (reset) parameterNotice('部分已保存参数无效，已恢复对应默认值。');
    }
  } catch (_) { parameterNotice('已保存参数无法读取，已使用默认值。'); }
  fields.forEach(field => {
    const label = document.createElement('label');
    label.append(document.createTextNode(field.label));
    const input = document.createElement('input');
    input.type = 'number'; input.name = field.key; input.required = true;
    input.min = String(field.min); input.max = String(field.max); input.step = String(field.step);
    input.value = String(parameters[field.key]);
    const hint = document.createElement('small');
    hint.textContent = '默认 ' + field.value + (field.exclusiveMin ? '；必须大于 0' : '');
    label.append(input, hint);
    document.getElementById(field.advanced
      ? 'bollinger-reference-advanced-parameters' : 'bollinger-reference-primary-parameters').append(label);
  });
  function fillList(id, items, format = value => String(value)) {
    const list = document.getElementById(id);
    list.replaceChildren();
    (Array.isArray(items) && items.length ? items : [null]).forEach(item => {
      const entry = document.createElement('li');
      entry.textContent = item === null ? '—' : format(item);
      list.append(entry);
    });
  }
  function noticeList(id, items) {
    const container = document.getElementById(id);
    const values = Array.isArray(items) ? items.filter(Boolean) : [];
    container.classList.toggle('hidden', !values.length);
    const list = container.querySelector('ul');
    list.replaceChildren();
    values.forEach(value => {
      const item = document.createElement('li'); item.textContent = String(value); list.append(item);
    });
  }
  function render(data) {
    const market = data.data || {};
    const action = data.action || {};
    const technical = data.technical_action;
    const stopPriority = action.code === 'stop_triggered';
    const blocked = !stopPriority && Boolean(data.guardrails?.blocked || market.stale || data.status !== 'ready');
    const priceBasis = basisLabels[data.price_basis] || basisLabels[adjustment];
    text('bollinger-reference-meta', '条件日期：' + (data.as_of || '—') + ' · ' + priceBasis +
      ' · 来源：' + (market.source || '—') + ' · 币种：' + (market.currency || '—') +
      ' · 最新交易日：' + (market.latest_trade_date || '—') +
      ' · 行情状态：' + (market.stale ? '已过期' : '本地已保存') +
      ' · 最近成功采集：' + time(market.last_success_at));
    text('bollinger-reference-label', blocked && action.code !== 'blocked'
      ? '参考暂停' : action.label || '等待条件');
    text('bollinger-reference-reason', action.reason || '尚无可用条件说明。');
    document.getElementById('bollinger-reference-action').dataset.code = blocked ? 'blocked' : action.code || 'wait';
    const technicalNode = document.getElementById('bollinger-reference-technical');
    technicalNode.classList.toggle('hidden', !technical || technical.label === action.label);
    technicalNode.textContent = technical ? '形态结论：' + (technical.label || '—') + '。' + (technical.reason || '') : '';
    const reasons = [...(data.guardrails?.reasons || [])];
    if (market.stale) reasons.push('行情已过期，请先刷新上方行情，再核对本参考。');
    if (data.status === 'insufficient') reasons.push('已收盘历史不足，暂不能完成全部条件核对。');
    if (data.status === 'invalid_data') reasons.push('行情数据未通过检查，暂不生成操作参考。');
    noticeList('bollinger-reference-guardrails', reasons);
    const setup = data.setup || {};
    text('bollinger-reference-setup', '形态阶段：' + (setup.label || '—') +
      ' · 突破日期：' + (setup.breakout_date || '—') + ' · 首次回踩：' + (setup.first_pullback_date || '—'));
    const lower = data.lower_band || {};
    text('bollinger-reference-lower', '下轨方向：' + (lower.label || '待确认') +
      ' · ' + (lower.lookback || '—') + ' 日斜率：' +
      (Number.isFinite(lower.slope_pct) ? number(lower.slope_pct) + '%' : '—') +
      '。' + (lower.reason || '下轨方向尚无法计算。'));
    const levels = data.levels || {};
    const grid = document.getElementById('bollinger-reference-levels');
    grid.replaceChildren();
    [
      ['entry_reference', '当前中轨观察位'], ['initial_stop', '初始止损参考'],
      ['initial_risk_pct', '初始止损距离'], ['current_stop', '已记录止损（同口径）'],
      ['upper', '布林上轨'], ['middle', '布林中轨'], ['lower', '布林下轨'],
      ['trailing_stop', '移动止损参考'],
    ].forEach(([key, label]) => {
      const item = document.createElement('div');
      const name = document.createElement('dt'); name.textContent = label;
      const value = document.createElement('dd'); value.dataset.level = key;
      value.textContent = number(levels[key]) + (Number.isFinite(levels[key]) && key === 'initial_risk_pct' ? '%' : '');
      item.append(name, value); grid.append(item);
    });
    const checks = document.getElementById('bollinger-reference-checks');
    checks.replaceChildren();
    (data.checks || []).forEach(check => {
      const item = document.createElement('li');
      const state = document.createElement('span'); state.className = 'bollinger-reference-check-state';
      state.dataset.passed = String(check.passed);
      state.textContent = check.passed === true ? '已满足' : check.passed === false ? '未满足' : '待确认';
      const label = document.createElement('strong'); label.textContent = check.label || '待核对条件';
      const detail = document.createElement('p'); detail.textContent = check.detail || '—';
      item.append(state, label, detail); checks.append(item);
    });
    if (!checks.children.length) checks.textContent = '—';
    const position = data.position || {};
    const positionLabels = {long: '多头持仓', short: '空头持仓', flat: '未持仓', unknown: '持仓未知'};
    text('bollinger-reference-position', '持仓：' + (positionLabels[position.status] || position.status || '—') +
      ' · 输入状态：' + (position.reliable ? '可比较' : '待核对') +
      ' · 浮盈：' + (position.reliable && Number.isFinite(position.profit_pct) ? number(position.profit_pct) + '%' : '—') +
      ' · 来源：' + (data.holding_source || '—'));
    const holdingsNote = data.holdings_source?.note;
    text('bollinger-reference-holdings-note', holdingsNote || '');
    document.getElementById('bollinger-reference-holdings-note').hidden = !holdingsNote;
    const savedStop = data.recorded_stop || {};
    if (Number.isFinite(savedStop.price) && savedStop.basis_verified === false) {
      document.getElementById('bollinger-reference-position').append(document.createTextNode(
        ' · 已保存止损输入：' + number(savedStop.price) + ' ' + (savedStop.currency || '') + '（拆股口径待核对）'));
    }
    const supports = Array.isArray(levels.supports) ? levels.supports : [];
    text('bollinger-reference-supports-summary', '已确认的支撑点（共 ' + supports.length + ' 个）');
    fillList('bollinger-reference-supports', supports, support => number(support.price) +
      ' · 低点日期 ' + (support.date || '—') + ' · 确认日期 ' + (support.confirmed_at || '—'));
    const addOn = data.add_on || {};
    const tranches = Array.isArray(addOn.tranches) ? addOn.tranches : [20, 20, 20, 40];
    text('bollinger-reference-tranches', tranches.map(value => number(value, 0) + '%').join(' / ') +
      ' 为计划仓位比例，不表示已经执行。');
    fillList('bollinger-reference-add-on-conditions', addOn.conditions);
    noticeList('bollinger-reference-warnings', data.warnings);
    fillList('bollinger-reference-definitions', data.definitions);
    const eventLabels = {breakout: '突破', first_pullback: '首次回踩', entry_reference: '中轨参考条件',
      stop_triggered: '止损条件触发', trailing_reference: '移动止损参考'};
    fillList('bollinger-reference-events', (data.events || []).slice(-8), event =>
      (event.date || '—') + ' · ' + (event.label || eventLabels[event.code] || '条件更新') +
      (event.reason ? '：' + event.reason : ''));
    status.dataset.state = blocked ? 'blocked' : 'ready';
    status.textContent = blocked ? '当前条件限制已列出；形态和价格仅供核对。' : '已按本地已收盘日线核对条件。';
    results.classList.remove('hidden');
  }
  async function recalculate() {
    if (disposed) return;
    const requestId = ++sequence;
    if (controller) controller.abort();
    const current = new AbortController(); controller = current;
    let timedOut = false;
    const timeout = window.setTimeout(() => { timedOut = true; current.abort(); }, 20000);
    status.dataset.state = 'loading'; status.textContent = '正在读取本地行情并计算条件…';
    results.classList.add('hidden');
    try {
      const query = new URLSearchParams({adjustment});
      Object.entries(parameters).forEach(([key, value]) => query.set(key, String(value)));
      const response = await fetch(endpoint + '?' + query.toString(), {signal: current.signal});
      let data;
      try { data = await response.json(); } catch (_) { throw new Error('服务器返回 HTTP ' + response.status); }
      if (!response.ok) {
        const detail = Array.isArray(data.detail) ? data.detail.map(item => item.msg).join('；') : data.detail;
        throw new Error(typeof detail === 'string' ? detail : '服务器返回 HTTP ' + response.status);
      }
      if (!disposed && requestId === sequence) render(data);
    } catch (error) {
      if (disposed || requestId !== sequence || (error.name === 'AbortError' && !timedOut)) return;
      status.dataset.state = 'error';
      status.textContent = '本面板暂无法计算：' + (timedOut ? '请求超时' : error.message) + '。可点击重新计算重试。';
    } finally { window.clearTimeout(timeout); }
  }
  function schedule() {
    window.clearTimeout(scheduled);
    scheduled = window.setTimeout(recalculate, 0);
  }
  form.addEventListener('input', () => parameterNotice('参数已修改，点击“重新计算”后生效。'));
  form.addEventListener('submit', event => {
    event.preventDefault();
    const next = {};
    for (const field of fields) {
      const input = form.elements.namedItem(field.key);
      const value = input.valueAsNumber;
      if (!valid(field, value)) {
        document.getElementById('bollinger-reference-parameters').open = true;
        if (field.advanced) root.querySelector('.bollinger-reference-advanced').open = true;
        parameterNotice(field.label + '无效，请检查允许范围。', true); input.focus(); return;
      }
      next[field.key] = value;
    }
    parameters = next;
    try { localStorage.setItem(storageKey, JSON.stringify(parameters)); parameterNotice('参数已保存至本浏览器。'); }
    catch (_) { parameterNotice('浏览器无法保存参数，本次仍可计算。'); }
    schedule();
  });
  document.getElementById('bollinger-reference-reset').addEventListener('click', () => {
    parameters = Object.fromEntries(fields.map(field => [field.key, field.value]));
    fields.forEach(field => { form.elements.namedItem(field.key).value = String(field.value); });
    try { localStorage.removeItem(storageKey); } catch (_) { /* Current-page defaults still apply. */ }
    parameterNotice('已恢复默认参数。'); schedule();
  });
  document.addEventListener('daily-bars:updated', event => {
    if (!Object.hasOwn(basisLabels, event.detail?.adjustment)) return;
    adjustment = event.detail.adjustment; schedule();
  });
  window.addEventListener('pagehide', event => {
    if (!event.persisted) { disposed = true; window.clearTimeout(scheduled); if (controller) controller.abort(); }
  });
  schedule();
})();
