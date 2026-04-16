import { API, authFetch, escapeHtml } from './api.js';

export async function loadExecutionDetail(execId) {
  const card = document.getElementById('wf-execution-card');
  card.style.display = 'block';
  const el = document.getElementById('wf-execution-detail');
  el.innerHTML = '<div class="loading">Loading...</div>';
  try {
    const data = await authFetch(API + '/workflows/executions/' + execId).then(r => r.json());
    if (data.error) { el.innerHTML = '<div class="error-msg">' + data.error + '</div>'; return; }
    let html = `<div class="exec-header"><span class="badge badge-${data.status}">${data.status}</span> ${data.error ? '<span class="error-msg">' + escapeHtml(data.error) + '</span>' : ''}</div>`;
    html += '<div class="exec-nodes">';
    for (const [nid, status] of Object.entries(data.node_statuses || {})) {
      const output = (data.node_outputs || {})[nid] || '';
      html += `<div class="exec-node exec-node-${status}">
        <span class="exec-node-id">${escapeHtml(nid)}</span>
        <span class="badge badge-${status}">${status}</span>
        ${output ? '<pre class="exec-node-output">' + escapeHtml(output.substring(0, 300)) + '</pre>' : ''}
      </div>`;
    }
    html += '</div>';
    el.innerHTML = html;
    if (data.status === 'running') setTimeout(() => loadExecutionDetail(execId), 3000);
  } catch (e) { el.innerHTML = '<div class="error-msg">' + e.message + '</div>'; }
}
