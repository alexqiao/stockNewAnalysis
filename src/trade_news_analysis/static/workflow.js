(() => {
  'use strict';

  const taskLabels = {pending: '待核实', done: '已完成核验', dismissed: '已搁置', expired: '已过期'};
  const claimLabels = {
    pending: '待核实', partially_supported: '部分有依据', verified: '已人工核实',
    refuted: '已被反证', expired: '已过期'
  };
  const auditLabels = {
    created: '建立核验项', manual_review: '人工更新', source_changed: '来源变化，重新核验',
    restored: '恢复核验', deadline: '复核期限已到', not_in_plan: '已不在当前行动计划',
    source_unavailable: '原始来源已不可用'
  };

  function formatTime(value) {
    const date = new Date(value);
    if (!value || Number.isNaN(date.getTime())) return value || '未填写';
    return date.toLocaleString('zh-CN', {hour12: false, timeZoneName: 'short'});
  }

  function formatTimes(scope) {
    scope.querySelectorAll('[data-workflow-time]').forEach(time => {
      time.textContent = formatTime(time.getAttribute('datetime'));
    });
  }

  function message(scope, text, success = false) {
    const target = scope.querySelector('[data-workflow-message]');
    if (!target) return;
    target.hidden = false;
    target.className = success ? 'muted' : 'notice';
    target.textContent = text;
    if (success) {
      const refresh = document.createElement('a');
      refresh.href = window.location.href;
      refresh.textContent = ' 刷新页面查看最新判断';
      target.appendChild(refresh);
    }
  }

  function safeLink(url, label) {
    const link = document.createElement('a');
    try {
      const parsed = new URL(url);
      if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('invalid URL');
      link.href = parsed.href;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
    } catch (_error) {
      link.removeAttribute('href');
    }
    link.textContent = label;
    return link;
  }

  async function request(url, method, payload) {
    const options = {method, headers: {'Content-Type': 'application/json'}};
    if (payload !== undefined) options.body = JSON.stringify(payload);
    return window.RunPoller.request(url, options);
  }

  function showConflict(form, latest) {
    form.querySelector('[data-conflict]')?.remove();
    const panel = document.createElement('div'); panel.dataset.conflict = '';
    const title = document.createElement('p');
    title.textContent = `最新记录（第 ${latest.revision} 版）：${latest.status_label || latest.status}；${latest.note || '无备注'}；复核期限：${formatTime(latest.review_due_at)}`;
    panel.appendChild(title);
    (latest.evidence || []).forEach(evidence => {
      const row = document.createElement('p');
      row.textContent = `${evidence.stance === 'support' ? '支持' : '反证'}：${evidence.note} · ${evidence.source_url || evidence.url}`;
      panel.appendChild(row);
    });
    const acknowledge = document.createElement('button'); acknowledge.type = 'button'; acknowledge.className = 'secondary';
    acknowledge.textContent = '已核对最新记录，保留草稿继续编辑';
    acknowledge.addEventListener('click', () => {
      form.dataset.revision = String(latest.revision);
      form.dataset.currentDue = latest.review_due_at || ''; panel.remove();
      message(form, '草稿已保留。请确认状态和完整证据列表，再点击保存。');
    });
    panel.appendChild(acknowledge); form.appendChild(panel);
  }

  function deadlinePayload(form, payload) {
    const raw = form.elements.namedItem('review_due_at').value;
    if (raw) {
      const date = new Date(raw);
      if (Number.isNaN(date.getTime())) throw new Error('请填写有效的复核期限。');
      if (payload.status !== 'expired' && date <= new Date()) {
        throw new Error('新的复核期限须晚于当前时间。');
      }
      payload.review_due_at = date.toISOString();
    } else if (payload.status !== 'expired' && form.dataset.currentDue) {
      const current = new Date(form.dataset.currentDue);
      if (!Number.isNaN(current.getTime()) && current <= new Date()) {
        throw new Error('当前复核期限已到；重新核验时请填写新的复核期限。');
      }
    }
  }

  function evidencePayload(form) {
    return Array.from(form.querySelectorAll('[data-evidence-list] [data-evidence-row]')).map(row => {
      const field = name => row.querySelector(`[data-evidence-field="${name}"]`);
      const evidence = {
        url: field('url').value.trim(), source_url: field('source_url').value.trim(),
        stance: field('stance').value, note: field('note').value.trim(),
        is_official: field('is_official').checked
      };
      if (!evidence.url || !evidence.note) throw new Error('每份材料都需要链接和具体核验说明。');
      return evidence;
    });
  }

  function addEvidence(form, url = '') {
    const template = form.querySelector('[data-evidence-template]');
    const fragment = template.content.cloneNode(true);
    const row = fragment.querySelector('[data-evidence-row]');
    row.querySelector('[data-evidence-field="url"]').value = url;
    row.querySelector('[data-evidence-field="source_url"]').value = url;
    row.querySelector('[data-evidence-field="is_official"]').checked = false;
    form.querySelector('[data-evidence-list]').appendChild(fragment);
    const focusField = row.querySelector(url ? '[data-evidence-field="note"]' : '[data-evidence-field="url"]');
    focusField.focus();
  }

  function renderHistory(item, history, labels) {
    const list = item.querySelector('[data-workflow-history]');
    if (!list) return;
    list.replaceChildren();
    [...(history || [])].reverse().forEach(entry => {
      const row = document.createElement('li');
      const heading = document.createElement('p');
      heading.textContent = `${formatTime(entry.created_at)} · ${auditLabels[entry.reason] || entry.reason}`;
      const detail = document.createElement('p');
      detail.textContent = `${labels[entry.from_status] || '未建立'} → ${labels[entry.to_status] || entry.to_status}${entry.note ? ` · ${entry.note}` : ''}`;
      row.append(heading, detail);
      const snapshot = entry.snapshot || {};
      const original = snapshot.claim_text || (snapshot.payload || {}).detail;
      if (original) {
        const text = document.createElement('p');
        text.className = 'muted';
        text.textContent = `当时核验内容：${original}`;
        row.appendChild(text);
      }
      if ((snapshot.evidence || []).length) {
        const sources = document.createElement('ul');
        snapshot.evidence.forEach(evidence => {
          const source = document.createElement('li');
          source.append(
            `${evidence.stance === 'support' ? '支持' : '反证'} · `,
            safeLink(evidence.source_url || evidence.url, '当时引用来源 ↗'),
            ` · ${evidence.note || ''}`
          );
          sources.appendChild(source);
        });
        row.appendChild(sources);
      }
      list.appendChild(row);
    });
    const count = item.querySelector('[data-history-count]');
    if (count) count.textContent = String((history || []).length);
  }

  function renderSaved(form, result) {
    const item = form.closest('[data-workflow-item]');
    form.dataset.revision = String(result.revision);
    form.querySelector('[data-conflict]')?.remove();
    const labels = form.dataset.workflowForm === 'claim' ? claimLabels : taskLabels;
    item.querySelector('[data-workflow-status]').textContent = result.status_label || labels[result.status];
    const due = item.querySelector('[data-workflow-due]');
    if (due) {
      due.setAttribute('datetime', result.review_due_at || '');
      due.textContent = formatTime(result.review_due_at);
    }
    form.dataset.currentDue = result.review_due_at || '';
    form.elements.namedItem('review_due_at').value = '';
    form.elements.namedItem('status').value = result.status;
    const note = item.querySelector('[data-workflow-note]');
    if (note) {
      note.textContent = result.note || '';
      note.hidden = !result.note;
    }
    renderHistory(item, result.history, labels);
    if (form.dataset.workflowForm !== 'claim') return;
    const counts = item.querySelector('[data-evidence-counts]');
    counts.textContent = `独立材料 ${result.independent_evidence_count} 份 · 官方支持 ${result.official_support_count} 份 · 官方反证 ${result.official_conflict_count} 份`;
    const summary = item.querySelector('[data-evidence-summary]');
    summary.replaceChildren();
    (result.evidence || []).forEach(evidence => {
      const row = document.createElement('li');
      row.append(
        `${evidence.stance === 'support' ? '支持' : '反证 / 冲突'} · ${evidence.is_official ? '已人工确认官方来源' : '尚非官方确认'} · `,
        safeLink(evidence.source_url || evidence.url, '原始出处 ↗')
      );
      const detail = document.createElement('p');
      detail.textContent = evidence.note;
      row.appendChild(detail);
      summary.appendChild(row);
    });
  }

  document.addEventListener('submit', async event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.matches('[data-workflow-form]')) return;
    event.preventDefault();
    if (form.dataset.saving === 'true' || !form.reportValidity()) return;
    const submit = form.querySelector('button[type="submit"]');
    try {
      const payload = {
        expected_revision: Number(form.dataset.revision),
        status: form.elements.namedItem('status').value,
        note: form.elements.namedItem('note').value.trim()
      };
      if (!payload.note) throw new Error('请填写具体核验依据或状态变更原因。');
      deadlinePayload(form, payload);
      if (form.dataset.workflowForm === 'claim') payload.evidence = evidencePayload(form);
      form.dataset.saving = 'true';
      submit.disabled = true;
      form.inert = true;
      message(form, '正在保存核验记录…');
      const resource = form.dataset.workflowForm === 'claim' ? 'claims' : 'tasks';
      const result = await request(`/api/v1/research/${resource}/${form.dataset.recordId}`, 'PATCH', payload);
      renderSaved(form, result);
      message(form, '已保存核验记录；其他草稿保留。');
    } catch (error) {
      message(form, error instanceof Error ? error.message : '保存失败，请重试。');
      if (error.status === 409 && error.detail?.latest) showConflict(form, error.detail.latest);
    } finally {
      delete form.dataset.saving;
      form.inert = false;
      submit.disabled = false;
    }
  });

  document.addEventListener('click', async event => {
    if (!(event.target instanceof Element)) return;
    const button = event.target.closest('button');
    if (!button || button.disabled) return;
    if (button.matches('[data-add-evidence]')) {
      addEvidence(button.closest('form'));
    } else if (button.matches('[data-remove-evidence]')) {
      button.closest('[data-evidence-row]').remove();
    } else if (button.matches('[data-add-candidate]')) {
      const item = button.closest('[data-workflow-item]');
      const editor = item.querySelector('[data-claim-editor]');
      if (editor.querySelector('form').dataset.saving === 'true') return;
      editor.open = true;
      addEvidence(editor.querySelector('form'), button.dataset.candidateUrl || '');
    } else if (button.matches('[data-workflow-capture]')) {
      const scope = button.closest('[data-workflow-item]') || button.parentElement;
      button.disabled = true;
      try {
        message(scope, '正在保存当前研究，请稍候…', true);
        await request('/api/v1/research/capture', 'POST');
        message(scope, '已保存当前研究。刷新页面后可记录核验结果。', true);
      } catch (error) {
        message(scope, error instanceof Error ? error.message : '保存失败，请重试。');
      } finally {
        button.disabled = false;
      }
    }
  });

  document.addEventListener('toggle', async event => {
    const details = event.target;
    if (!details.matches?.('[data-workflow-history-url]') || !details.open || details.dataset.loaded) return;
    details.dataset.loaded = 'loading';
    try {
      const result = await request(details.dataset.workflowHistoryUrl, 'GET');
      renderHistory(details.closest('[data-workflow-item]'), result.history, claimLabels);
      details.dataset.loaded = 'true';
    } catch (error) {
      delete details.dataset.loaded;
      const row = document.createElement('li'); row.textContent = error.message + '；折叠后重开可重试。';
      details.querySelector('[data-workflow-history]').appendChild(row);
    }
  }, true);

  formatTimes(document);
})();
