'use strict';

function researchMessage(element, message) {
  if (!element) return;
  element.textContent = message;
  element.classList.remove('hidden');
}
const changedFields = new WeakMap();
function researchRequest(url, method, payload) {
  return window.RunPoller.request(url, {method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
}
async function refreshResearchSection(form) {
  const section = form.closest('section');
  if (!section?.id) return;
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(window.location.href, {signal: controller.signal});
    if (!response.ok) throw new Error('资料已保存，暂时无法更新页面摘要。');
    const updated = new DOMParser().parseFromString(await response.text(), 'text/html').getElementById(section.id);
    if (!updated) return;
    // Retain every editor in this section, including unrelated unsaved drafts.
    section.querySelectorAll('form').forEach(existing => {
      const replacement = [...updated.querySelectorAll('form')].find(item => item.dataset.endpoint === existing.dataset.endpoint);
      if (replacement) {
        if (existing === form && form.dataset.riskDefaults) {
          replacement.querySelectorAll('[name]').forEach(input => {
            const field = existing.elements.namedItem(input.name);
            if (field) field.value = input.value;
          });
          replacement.querySelectorAll('[data-source]').forEach(note => {
            const old = existing.querySelector('#' + note.id); if (old) old.replaceWith(note);
          });
        }
        replacement.replaceWith(existing);
      }
    });
    section.replaceWith(updated);
  } finally { window.clearTimeout(timer); }
}
document.addEventListener('input', event => {
  const form = event.target.closest('.research-form');
  if (!form || !event.target.name) return;
  const changed = changedFields.get(form) || new Set();
  changed.add(event.target.name); changedFields.set(form, changed); form.dataset.dirty = 'true';
});
document.addEventListener('change', event => {
  if (event.target.matches('[data-related-report]')) {
    event.target.form.elements.namedItem('event_id').value = event.target.selectedOptions[0]?.dataset.eventId || '';
  }
});
document.addEventListener('submit', async event => {
  const form = event.target;
  if (!form.matches('.research-form')) return;
  event.preventDefault();
  if (form.dataset.saving === 'true' || !form.reportValidity()) return;
  const message = form.querySelector('[role="status"]');
  const payload = {};
  form.querySelectorAll('[name]:not(:disabled)').forEach(input => {
    if (form.dataset.riskDefaults && !(changedFields.get(form) || new Set()).has(input.name)) return;
    const value = input.value.trim();
    payload[input.name] = input.type === 'checkbox' ? input.checked : value === '' ? null :
      (input.type === 'number' || ['article_id', 'event_id'].includes(input.name) ? Number(value) : value);
  });
  const controls = [...form.querySelectorAll('button')].map(button => [button, button.disabled]);
  form.dataset.saving = 'true'; form.inert = true;
  controls.forEach(([button]) => { button.disabled = true; });
  researchMessage(message, '正在保存…');
  try {
    await researchRequest(form.dataset.endpoint, form.dataset.method || 'POST', payload);
    changedFields.delete(form); delete form.dataset.dirty;
    researchMessage(message, '已保存，正在更新本节摘要…');
    try { await refreshResearchSection(form); researchMessage(message, '已保存并更新本节资料；其他表单的草稿保留。'); }
    catch (_) { researchMessage(message, '已保存。页面摘要暂未更新，可稍后重新查看；其他草稿保留。'); }
  } catch (error) { researchMessage(message, error.message); }
  finally {
    form.inert = false; delete form.dataset.saving;
    controls.forEach(([button, disabled]) => { button.disabled = disabled; });
  }
});
const refreshButton = document.getElementById('refresh-research');
if (refreshButton) {
  const storageKey = 'research-active-run';
  const message = document.getElementById('research-message');
  let monitorHandle;
  function monitor(runId) {
    monitorHandle?.stop();
    refreshButton.disabled = true;
    monitorHandle = window.RunPoller.start({runId, storageKey,
      onUpdate: run => researchMessage(message, `研究任务 #${runId}：${run.phase || run.status}，可继续编辑资料。`),
      onComplete: run => {
        refreshButton.disabled = false;
        researchMessage(message, ['complete', 'completed'].includes(run.status) ?
          '研究刷新完成。重新查看页面可更新结果，当前草稿保留。' :
          `研究任务${run.status === 'partial' ? '部分完成' : '失败'}：${(run.errors || []).join('；') || '请到运行状态页查看来源结果。'}`);
      },
      onError: error => { refreshButton.disabled = false; researchMessage(message, error.message); }
    });
  }
  refreshButton.addEventListener('click', async () => {
    if (refreshButton.disabled) return;
    const previous = window.RunPoller.restore(storageKey);
    if (previous) { monitor(previous); return; }
    refreshButton.disabled = true;
    try { const result = await researchRequest('/api/v1/research/refresh', 'POST', {}); monitor(result.run_id); }
    catch (error) { refreshButton.disabled = false; researchMessage(message, error.message); }
  });
  const runId = window.RunPoller.restore(storageKey);
  if (runId) monitor(runId);
  window.addEventListener('pageshow', event => {
    if (event.persisted) { const pending = window.RunPoller.restore(storageKey); if (pending) monitor(pending); }
  });
}

const calibrationData = document.getElementById('calibration-data');
if (calibrationData) {
  const data = JSON.parse(calibrationData.textContent);
  const root = document.getElementById('calibration-content');
  const labels = {
    policy_version: '规则版本', evidence_rule_version: '证据规则', recording_version: '行动记录口径', evaluation_version: '评估口径',
    market: '市场', horizon: '周期', action_code: '行动',
    sample_size: '成熟样本', count: '样本数', execution_ready: '执行条件齐备',
    hit_rate: '方向命中率', mean_net_return_pct: '平均扣成本收益 %',
    mean_return_pct: '平均绝对收益 %', mean_excess_return_pct: '平均超额收益 %',
    mean_drawdown_pct: '平均最大回撤 %', status: '验证状态', evidence: '证据程度',
    observed_win_rate: '观察命中率', confidence_bucket: '模型置信度区间',
    mean_confidence: '平均模型置信度', pending_count: '待验证',
    sufficient: '样本是否充足', min_samples: '最低样本要求', minimum_samples: '最低样本要求', note: '说明',
    total_snapshot_count: '已记录快照', completed_count: '成熟结果', calibration_status: '验证状态',
    direction: '事件方向', record_count: '记录数量', independent_sample_count: '独立样本', duplicates_excluded: '排除重复记录',
    direction_accuracy: '事件方向命中率', direction_accuracy_interval_95: '方向命中率 95% 区间',
    cost_known_sample_count: '成本已知样本', mean_directional_benefit_pct: '平均方向收益（含成本，%）',
    absolute_return_percentiles: '绝对收益分位数（%）', worst_close_drawdown_pct: '最差收盘回撤（%）',
    reliability: '置信度分组诊断', calibrated_probability: '校准后概率', confidence_range: '模型置信度区间',
    sample_count: '样本数', empirical_direction_accuracy: '实际方向命中率', interval_95: '95% 区间',
    industry_excess_return_percentiles: '行业超额收益分布（%）', industry_sample_count: '行业可比样本',
    pending_by_reason: '待验证原因', pending_samples: '待验证样本', pending_sample_limit: '最多展示条数',
    snapshot_id: '行动记录', security_id: '证券', recorded_at: '记录时间', reason: '原因',
    due_at: '预计成熟时间', window_start: '窗口起点', window_end: '窗口终点',
    missing_dates: '缺失交易日', missing_fields: '缺失字段', attempts: '尝试次数',
    last_attempt_at: '最近尝试', next_attempt_at: '下次尝试', label: '状态',
    '0.1': '10% 分位', '0.5': '中位数', '0.9': '90% 分位'
  };
  const format = value => value === null || value === undefined ? '未知' :
    typeof value === 'boolean' ? (value ? '是' : '否') :
    typeof value === 'number' ? (Number.isInteger(value) ? String(value) : value.toFixed(3)) :
    ({descriptive_only:'仅描述观察',insufficient_samples:'样本不足',bullish:'偏多',bearish:'偏空',neutral:'无方向',unassessed:'尚未评估',queued:'待验证',not_mature:'等待成熟',source_failed:'来源失败',history_missing:'历史行情缺失',adjustment_unverified:'复权未核验',currency_mismatch:'币种不一致',volume_unknown:'成交量未知',price_invalid:'价格无效',calendar_unavailable:'交易日历不可用',suspension_limit:'停牌超出本批检查范围','original-sources-v2':'原始出处去重 v2','execution-actions-v2':'执行动作 v2','legacy-v1':'历史记录 v1'}[value] || String(value));
  function renderObject(obj, parent) {
    Object.entries(obj).forEach(([key, value]) => {
      if (Array.isArray(value)) {
        const heading = document.createElement('h3'); heading.textContent = key === 'groups' ? '按市场、周期、规则与记录口径分组' : key === 'calibration' ? '置信度与实际结果' : (labels[key] || key); parent.appendChild(heading);
        if (!value.length) { const p = document.createElement('p'); p.textContent = '暂无成熟样本'; parent.appendChild(p); }
        value.forEach(item => {
          const card = document.createElement('article'); card.className = 'social-context-post';
          typeof item === 'object' && item !== null ? renderObject(item, card) : card.appendChild(document.createTextNode(format(item)));
          if (key === 'pending_samples' && item.snapshot_id) {
            const button = document.createElement('button'); button.className = 'secondary'; button.textContent = '重试验证';
            const status = document.createElement('p'); status.setAttribute('role', 'status');
            button.addEventListener('click', async () => {
              if (button.disabled) return; button.disabled = true;
              try { const result = await researchRequest(`/api/v1/research/validation/${item.snapshot_id}/retry`, 'POST', {}); status.textContent = result.message || '已加入验证队列。'; }
              catch (error) { status.textContent = error.message; button.disabled = false; }
            });
            card.append(button, status);
          }
          parent.appendChild(card);
        });
      } else if (value && typeof value === 'object') {
        const details = document.createElement('details'); const title = document.createElement('summary'); title.textContent = labels[key] || key; details.appendChild(title); renderObject(value, details); parent.appendChild(details);
      } else { const span = document.createElement('span'); span.className = 'calibration-stat'; span.textContent = (labels[key] || key) + '：' + format(value); parent.appendChild(span); }
    });
  }
  renderObject(data, root);
}
