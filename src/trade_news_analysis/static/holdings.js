'use strict';

(() => {
  const panel = document.getElementById('holdings-panel');
  if (!panel) return;
  const connect = document.getElementById('ibkr-connect');
  const sync = document.getElementById('ibkr-sync');
  const account = document.getElementById('ibkr-account');
  const port = document.getElementById('ibkr-port');
  const clientId = document.getElementById('ibkr-client-id');
  const preset = document.getElementById('ibkr-preset');
  const message = document.getElementById('ibkr-message');
  const controls = [connect, sync, account, port, clientId, preset];
  try {
    const saved = JSON.parse(localStorage.getItem('ibkr-connection') || 'null');
    if (saved && Number.isInteger(saved.port) && saved.port > 0 && saved.port <= 65535
        && Number.isInteger(saved.client_id) && saved.client_id > 0) {
      port.value = saved.port;
      clientId.value = saved.client_id;
      preset.value = [7496, 4001].includes(saved.port) ? String(saved.port) : 'custom';
    }
  } catch (_) { /* Storage can be unavailable in private browsing. */ }
  let busy = false;
  let dirty = false;
  let pendingSave = false;
  const watchlist = document.getElementById('watchlist-table');
  watchlist.addEventListener('input', () => { dirty = true; });
  watchlist.addEventListener('change', () => { dirty = true; });
  watchlist.addEventListener('click', event => {
    if (event.target.closest('.remove, .move-up, .move-down')) dirty = true;
  });
  document.getElementById('add').addEventListener('click', () => { dirty = true; });
  document.addEventListener('watchlist-saving', () => {
    pendingSave = true;
    controls.forEach(control => { control.disabled = true; });
  });
  document.addEventListener('watchlist-saved', () => { pendingSave = false; dirty = false; });
  document.addEventListener('watchlist-save-failed', () => {
    pendingSave = false;
    setBusy(false);
  });
  function invalidateAccounts() {
    account.replaceChildren(new Option('请重新读取账户', ''));
    account.disabled = true;
    sync.disabled = true;
  }
  preset.addEventListener('change', () => {
    if (preset.value !== 'custom') port.value = preset.value;
    invalidateAccounts();
  });
  port.addEventListener('input', invalidateAccounts);
  clientId.addEventListener('input', invalidateAccounts);
  function payload() {
    return {port: Number(port.value), client_id: Number(clientId.value)};
  }
  async function request(endpoint, data) {
    const response = await fetch('/api/v1/holdings/ibkr/' + endpoint, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '连接参数无效，请检查端口和 Client ID');
    return result;
  }
  function setBusy(value) {
    busy = value;
    controls.forEach(control => { control.disabled = value; });
    account.disabled = value || !account.value;
    sync.disabled = value || !account.value;
    document.getElementById('save').disabled = value;
    document.getElementById('add').disabled = value;
    watchlist.inert = value;
  }
  connect.addEventListener('click', async () => {
    if (busy || pendingSave) return;
    setBusy(true);
    message.textContent = '正在连接本机 TWS / IB Gateway…';
    try {
      const data = await request('accounts', payload());
      try { localStorage.setItem('ibkr-connection', JSON.stringify(payload())); } catch (_) { /* Optional preference. */ }
      account.replaceChildren();
      data.accounts.forEach(item => account.add(new Option(item.label, item.account_key)));
      if (!data.accounts.length) account.add(new Option('未找到实盘账户', ''));
      message.textContent = data.accounts.length ? '连接成功。选择账户后同步真实持仓。' : '未找到实盘账户，请确认 TWS 已登录实盘。';
    } catch (error) {
      invalidateAccounts();
      message.textContent = error.message;
    } finally { setBusy(false); }
  });
  sync.addEventListener('click', async () => {
    if (busy || !account.value) return;
    if (dirty || pendingSave) {
      message.textContent = '自选列表有未保存的修改，请先保存，再同步持仓。';
      return;
    }
    setBusy(true);
    message.textContent = '正在读取账户与完整持仓，通常需要数秒…';
    try {
      await request('sync', {...payload(), account_key: account.value});
      message.textContent = '同步完成，正在刷新持仓与分析…';
      window.location.reload();
    } catch (error) {
      message.textContent = error.message + '。原持仓数据保留；刷新页面可查看最近同步状态。';
      setBusy(false);
    }
  });
})();
