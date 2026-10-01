'use strict';

(() => {
  const root = document.getElementById('daily-bars');
  if (!root) return;
  const endpoint = '/api/v1/securities/' + root.dataset.securityId + '/daily-bars';
  const host = document.getElementById('daily-bars-chart');
  const status = document.getElementById('daily-bars-status');
  const meta = document.getElementById('daily-bars-meta');
  const quote = document.getElementById('daily-bars-quote');
  const empty = document.getElementById('daily-bars-empty');
  const refreshButton = document.getElementById('daily-bars-refresh');
  const adjustment = document.getElementById('daily-bars-adjustment');
  const adjustmentNote = document.getElementById('daily-bars-adjustment-note');
  const bollingerToggle = document.getElementById('daily-bars-bollinger');
  const periods = [5, 10, 20, 60];
  const colors = {5: '#b37610', 10: '#356bc4', 20: '#9144a4', 60: '#527454'};
  const bollingerPeriod = 20;
  const bollingerWidth = 2;
  const bollingerLabels = {upper: '上轨', middle: '中轨', lower: '下轨'};
  const up = '#ba443b';
  const down = '#16825b';
  let payload = null;
  let chart = null;
  let candles = null;
  let volume = null;
  let busy = false;
  let disposed = false;
  let months = 12;
  let preferredAdjustment = 'total_return_adjusted';
  let plotted = [];
  let byDate = new Map();
  const movingAverages = new Map();
  const lines = new Map();
  const bollingerLines = new Map();
  const bollingerValues = new Map();

  const formatNumber = (value, digits = 2) => Number.isFinite(value)
    ? value.toLocaleString('zh-CN', {minimumFractionDigits: digits, maximumFractionDigits: digits}) : '—';
  const formatTime = value => {
    if (!value) return '尚无';
    const time = new Date(value);
    return Number.isNaN(time.getTime()) ? value : time.toLocaleString('zh-CN');
  };
  function showStatus(message, state) {
    status.textContent = message;
    status.dataset.state = state;
  }
  async function request(url, method = 'GET') {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(url, {method, signal: controller.signal});
      let data;
      try { data = await response.json(); }
      catch (_) { throw new Error('服务器返回 HTTP ' + response.status + '，无法读取行情响应'); }
      if (!response.ok) {
        const detail = data.detail || data.error;
        throw new Error(typeof detail === 'string' ? detail : '服务器返回 HTTP ' + response.status);
      }
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('行情请求超时，请稍后重试');
      throw error;
    } finally { window.clearTimeout(timeout); }
  }
  function metadata() {
    const coverage = payload.coverage || {};
    meta.textContent = '来源：' + (payload.source || 'Yahoo Finance') +
      ' · 币种：' + (payload.currency || '未知') + ' · 时区：' + (payload.timezone || '未知') +
      ' · 最新交易日：' + (payload.latest_trade_date || '尚无') +
      ' · 历史：' + (coverage.start || '—') + ' 至 ' + (coverage.end || '—') +
      ' · 最近成功采集：' + formatTime(payload.last_success_at) +
      ' · 最近尝试：' + formatTime(payload.last_attempt_at);
    const retry = payload.next_retry_at ? '；下次重试：' + formatTime(payload.next_retry_at) : '';
    if (payload.sync_status === 'partial') {
      showStatus('行情待补：' + (payload.error || '来源数据尚未完整返回') +
        '。当前显示已保存行情。' + retry, 'stale');
    } else if (payload.error || payload.sync_status === 'failed') {
      showStatus('最近采集失败：' + (payload.error || '请稍后重试') +
        (payload.bars.length ? '。仍显示已保存行情。' : '') + retry, 'error');
    } else if (['queued', 'running'].includes(payload.sync_status)) {
      showStatus('行情正在后台更新' + (payload.bars.length ? '，当前显示已保存行情。' : '。'), 'loading');
    } else if (payload.stale || payload.needs_refresh) {
      showStatus('行情待更新' + (payload.bars.length ? '，当前显示已保存行情。' : '。') + retry, 'stale');
    } else {
      showStatus(payload.bars.length ? '已读取本地已收盘日线。' : '尚无可用的已收盘日线。',
        payload.bars.length ? 'ready' : 'empty');
    }
  }
  function showQuote(bar) {
    if (!bar) {
      quote.textContent = '日线仅包含已收盘交易日。';
    } else {
      quote.textContent = bar.time + ' · 开 ' + formatNumber(bar.open) + ' · 高 ' +
        formatNumber(bar.high) + ' · 低 ' + formatNumber(bar.low) + ' · 收 ' +
        formatNumber(bar.close) + ' ' + (payload.currency || '') + ' · 成交量 ' +
        (Number.isFinite(bar.volume) ? formatNumber(bar.volume, 0) + ' 股' : '缺失');
    }
    periods.forEach(period => {
      root.querySelector('[data-ma-value="' + period + '"]').textContent =
        formatNumber(bar ? movingAverages.get(period)?.get(bar.time) : null);
    });
    Object.keys(bollingerLabels).forEach(key => {
      root.querySelector('[data-bollinger-value="' + key + '"]').textContent =
        formatNumber(bar ? bollingerValues.get(bar.time)?.[key] : null);
    });
  }
  function ensureChart() {
    if (chart) return;
    if (!window.LightweightCharts) throw new Error('本地图表组件加载失败，请刷新页面');
    // Old cached CSS can omit the chart height even though the current script loaded.
    if (host.getBoundingClientRect().height === 0) host.style.minHeight = '360px';
    const library = window.LightweightCharts;
    chart = library.createChart(host, {
      autoSize: true,
      layout: {background: {type: 'solid', color: '#fffef8'}, textColor: '#667169',
        panes: {separatorColor: '#d8d8ce', separatorHoverColor: '#a7b7ab', enableResize: true}},
      grid: {vertLines: {color: '#eeeee5'}, horzLines: {color: '#eeeee5'}},
      rightPriceScale: {borderColor: '#d8d8ce'},
      timeScale: {borderColor: '#d8d8ce', timeVisible: false, rightOffset: 3},
      localization: {locale: 'zh-CN'},
      crosshair: {mode: library.CrosshairMode.Normal},
      handleScroll: {vertTouchDrag: false},
    });
    candles = chart.addSeries(library.CandlestickSeries, {
      upColor: up, downColor: down, borderVisible: false, wickUpColor: up, wickDownColor: down,
      priceLineVisible: false,
    }, 0);
    volume = chart.addSeries(library.HistogramSeries, {
      priceFormat: {type: 'volume'}, priceLineVisible: false, lastValueVisible: false,
    }, 1);
    chart.panes()[1].setHeight(110);
    periods.forEach(period => {
      lines.set(period, chart.addSeries(library.LineSeries, {
        color: colors[period], lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false,
      }, 0));
    });
    Object.keys(bollingerLabels).forEach(key => {
      bollingerLines.set(key, chart.addSeries(library.LineSeries, {
        color: '#167985', lineWidth: 1,
        lineStyle: key === 'middle' ? library.LineStyle.Dashed : library.LineStyle.Solid,
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        visible: bollingerToggle.checked,
      }, 0));
    });
    chart.subscribeCrosshairMove(event => {
      const time = event.time;
      const key = typeof time === 'string' ? time : time && typeof time === 'object'
        ? time.year + '-' + String(time.month).padStart(2, '0') + '-' + String(time.day).padStart(2, '0') : '';
      showQuote(byDate.get(key) || plotted[plotted.length - 1]);
    });
  }
  function showRange() {
    if (!chart || !plotted.length) return;
    const latest = plotted[plotted.length - 1].time;
    const start = new Date(latest + 'T00:00:00Z');
    const day = start.getUTCDate();
    start.setUTCDate(1);
    start.setUTCMonth(start.getUTCMonth() - months);
    const lastDay = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth() + 1, 0)).getUTCDate();
    start.setUTCDate(Math.min(day, lastDay));
    const cutoff = start.toISOString().slice(0, 10);
    const index = plotted.findIndex(bar => bar.time >= cutoff);
    chart.timeScale().setVisibleLogicalRange({from: Math.max(0, index) - 0.5, to: plotted.length - 0.5});
    root.querySelectorAll('[data-months]').forEach(button => {
      button.setAttribute('aria-pressed', String(Number(button.dataset.months) === months));
    });
  }
  function plot() {
    if (!payload) return;
    const adjustedAvailable = (payload.available_adjustments || []).includes('total_return_adjusted') &&
      payload.bars.length > 0 && payload.bars.every(bar =>
        ['adj_open', 'adj_high', 'adj_low', 'adj_close'].every(field => Number.isFinite(bar[field])));
    adjustment.querySelector('[value="total_return_adjusted"]').disabled = !adjustedAvailable;
    adjustment.value = adjustedAvailable ? preferredAdjustment : 'split_adjusted';
    adjustmentNote.classList.toggle('hidden', adjustedAvailable || !payload.bars.length);
    adjustmentNote.textContent = '含分红调整数据不完整，当前仅显示拆股调整价格；复权切换暂不可用。';
    if (!payload.bars.length) {
      host.classList.add('hidden');
      empty.classList.remove('hidden');
      plotted = [];
      byDate.clear();
      movingAverages.clear();
      bollingerValues.clear();
      showQuote(null);
      return;
    }
    host.classList.remove('hidden');
    empty.classList.add('hidden');
    ensureChart();
    const adjusted = adjustment.value === 'total_return_adjusted';
    plotted = payload.bars.map(bar => ({
      time: bar.date, open: adjusted ? bar.adj_open : bar.open,
      high: adjusted ? bar.adj_high : bar.high, low: adjusted ? bar.adj_low : bar.low,
      close: adjusted ? bar.adj_close : bar.close, volume: bar.volume,
    }));
    byDate = new Map(plotted.map(bar => [bar.time, bar]));
    candles.setData(plotted.map(({time, open, high, low, close}) => ({time, open, high, low, close})));
    volume.setData(plotted.map(bar => Number.isFinite(bar.volume)
      ? {time: bar.time, value: bar.volume, color: bar.close >= bar.open ? up : down}
      : {time: bar.time}));
    periods.forEach(period => {
      let sum = 0;
      const data = [];
      const values = new Map();
      plotted.forEach((bar, index) => {
        sum += bar.close;
        if (index >= period) sum -= plotted[index - period].close;
        if (index >= period - 1) {
          const value = sum / period;
          data.push({time: bar.time, value});
          values.set(bar.time, value);
        }
      });
      movingAverages.set(period, values);
      lines.get(period).setData(data);
      lines.get(period).applyOptions({visible: root.querySelector('[data-ma="' + period + '"]').checked});
    });
    bollingerValues.clear();
    for (let index = bollingerPeriod - 1; index < plotted.length; index += 1) {
      const windowBars = plotted.slice(index - bollingerPeriod + 1, index + 1);
      const middle = windowBars.reduce((sum, bar) => sum + bar.close, 0) / bollingerPeriod;
      // Population variance; centered differences avoid cancellation on nearly flat prices.
      const variance = windowBars.reduce((sum, bar) => sum + (bar.close - middle) ** 2, 0) / bollingerPeriod;
      const offset = bollingerWidth * Math.sqrt(variance);
      bollingerValues.set(plotted[index].time, {upper: middle + offset, middle, lower: middle - offset});
    }
    bollingerLines.forEach((series, key) => {
      series.setData(Array.from(bollingerValues, ([time, values]) => ({time, value: values[key]})));
      series.applyOptions({visible: bollingerToggle.checked});
    });
    showRange();
    showQuote(plotted[plotted.length - 1]);
  }
  async function loadCache() {
    const data = await request(endpoint);
    if (!Array.isArray(data.bars)) throw new Error('行情响应缺少日线数据');
    if (disposed) return data;
    const previous = payload;
    // A transient empty response must not erase a chart already visible in this page.
    if (payload?.bars.length && !data.bars.length) {
      payload = {...data, bars: payload.bars, coverage: payload.coverage,
        latest_trade_date: payload.latest_trade_date, last_success_at: payload.last_success_at,
        available_adjustments: payload.available_adjustments, currency: payload.currency,
        timezone: payload.timezone, source: payload.source, stale: true};
    } else { payload = data; }
    metadata();
    if (!previous || previous.last_success_at !== payload.last_success_at ||
        previous.bars.length !== payload.bars.length ||
        previous.latest_trade_date !== payload.latest_trade_date ||
        previous.currency !== payload.currency ||
        previous.available_adjustments.join() !== payload.available_adjustments.join()) plot();
    document.dispatchEvent(new CustomEvent('daily-bars:updated', {detail: {adjustment: adjustment.value}}));
    return data;
  }
  function stockAttemptFinished(data, baseline) {
    if (!['success', 'partial', 'failed'].includes(data.sync_status)) return false;
    return Boolean(data.last_attempt_at && (data.last_attempt_at !== baseline?.last_attempt_at ||
      baseline?.sync_status === 'running'));
  }
  async function refresh(force = false) {
    if (busy || disposed) return;
    busy = true;
    refreshButton.disabled = true;
    showStatus('正在后台采集行情' + (payload?.bars.length ? '，当前显示已保存行情…' : '…'), 'loading');
    try {
      // Read a current baseline so an older page cannot mistake a previous update for this request.
      const baseline = force ? await loadCache() : payload;
      showStatus('正在提交行情刷新' + (payload?.bars.length ? '，当前显示已保存行情…' : '…'), 'loading');
      const task = await request(endpoint + '/refresh' + (force ? '?force=true' : ''), 'POST');
      if (!task.run_id) throw new Error('服务器未返回行情任务编号');
      const deadline = Date.now() + 6 * 60 * 1000;
      for (let attempt = 0; attempt < 180 && !disposed && Date.now() < deadline; attempt += 1) {
        const run = await request('/api/v1/runs/' + task.run_id);
        const data = await loadCache();
        // A shared run can continue (or fail for another stock) after this stock is committed.
        if (stockAttemptFinished(data, baseline)) return;
        if (!['queued', 'running'].includes(run.status)) {
          const ownIssue = ['partial', 'failed'].includes(data.sync_status);
          const freshSkip = !force && data.sync_status === 'success' && data.bars.length > 0 &&
            !data.needs_refresh && !data.stale;
          if (run.status !== 'completed' && !ownIssue && !freshSkip) {
            const detail = (run.errors || []).join('；');
            throw new Error('本次刷新未确认完成' + (detail ? '：' + detail : ''));
          }
          return;
        }
        showStatus((run.status === 'queued' ? '行情任务排队中' : '等待本股票行情更新') +
          (payload?.bars.length ? '，当前显示已保存行情…' : '…'), 'loading');
        await new Promise(resolve => window.setTimeout(resolve, 2000));
      }
      if (!disposed) {
        const data = await loadCache();
        if (!stockAttemptFinished(data, baseline)) {
          showStatus('已停止等待本次更新。后台任务可能仍在排队或采集，点击“刷新行情”重新检查。' +
            (payload?.bars.length ? '当前显示已保存行情。' : ''), 'stale');
        }
      }
    } catch (error) {
      if (!disposed) {
        showStatus('刷新失败：' + error.message +
          (payload?.bars.length ? '。仍显示已保存行情。' : '。可稍后手动重试。'), 'error');
        if (!payload?.bars.length) empty.classList.remove('hidden');
      }
    } finally {
      busy = false;
      if (!disposed) refreshButton.disabled = false;
    }
  }
  root.querySelectorAll('[data-months]').forEach(button => {
    button.addEventListener('click', () => { months = Number(button.dataset.months); showRange(); });
  });
  adjustment.addEventListener('change', () => {
    preferredAdjustment = adjustment.value;
    try {
      plot();
      document.dispatchEvent(new CustomEvent('daily-bars:updated', {detail: {adjustment: adjustment.value}}));
    } catch (error) { showStatus('图表更新失败：' + error.message, 'error'); }
  });
  root.querySelectorAll('[data-ma]').forEach(input => {
    input.addEventListener('change', () => lines.get(Number(input.dataset.ma))?.applyOptions({visible: input.checked}));
  });
  bollingerToggle.addEventListener('change', () => {
    bollingerLines.forEach(series => series.applyOptions({visible: bollingerToggle.checked}));
  });
  refreshButton.addEventListener('click', () => refresh(true));
  window.addEventListener('pagehide', event => {
    if (!event.persisted) { disposed = true; if (chart) chart.remove(); }
  });
  (async () => {
    refreshButton.disabled = true;
    try {
      const data = await loadCache();
      const retryAt = data.next_retry_at ? Date.parse(data.next_retry_at) : 0;
      if ((!data.bars.length || data.needs_refresh) && !(retryAt > Date.now())) await refresh();
    } catch (error) {
      showStatus('行情加载失败：' + error.message + '。可点击刷新行情重试。', 'error');
      meta.textContent = '尚未读取到本地行情。';
      empty.classList.remove('hidden');
    } finally { if (!disposed) refreshButton.disabled = false; }
  })();
})();
