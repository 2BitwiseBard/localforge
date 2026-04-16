import { API, authFetch, authHeaders, escapeHtml, escapeAttr, showToast, statusRow, timeAgo } from './api.js';

export async function loadApprovals() {
  try {
    const data = await authFetch(API + '/approvals').then(r => r.json());
    const el = document.getElementById('approval-list');
    const pending = data.pending || [];
    if (pending.length === 0) {
      el.innerHTML = '<div class="empty-state">No pending approvals</div>';
      const card = document.getElementById('approval-card');
      if (!(data.recent || []).length) card.style.display = 'none';
      else {
        card.style.display = '';
        el.innerHTML = (data.recent || []).map(r =>
          `<div class="approval-item approval-${r.status}">
            <span class="approval-tool">${escapeHtml(r.tool_name)}</span>
            <span class="approval-agent">${escapeHtml(r.agent_id)}</span>
            <span class="badge" style="background:${r.status === 'approved' ? 'var(--green)' : 'var(--red)'}">${r.status}</span>
          </div>`
        ).join('');
      }
      return;
    }
    document.getElementById('approval-card').style.display = '';
    el.innerHTML = pending.map(r =>
      `<div class="approval-item approval-pending">
        <div class="approval-info">
          <strong>${escapeHtml(r.tool_name)}</strong>
          <span class="approval-agent">by ${escapeHtml(r.agent_id)}</span>
          <span class="param-hint">${r.remaining_seconds}s remaining</span>
          <div class="approval-args"><code>${escapeHtml(JSON.stringify(r.arguments).substring(0, 120))}</code></div>
        </div>
        <div class="approval-actions">
          <button class="btn-primary btn-small approval-decide-btn" data-id="${r.id}" data-action="approve">Approve</button>
          <button class="btn-danger btn-small approval-decide-btn" data-id="${r.id}" data-action="deny">Deny</button>
        </div>
      </div>`
    ).join('');
    el.querySelectorAll('.approval-decide-btn').forEach(btn => {
      btn.addEventListener('click', () => decideApproval(btn.dataset.id, btn.dataset.action));
    });
  } catch (e) {}
}

async function decideApproval(id, action) {
  try {
    await authFetch(API + '/approvals/decide', {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ id, action }),
    });
    showToast(`Approval ${action}d`);
    loadApprovals();
  } catch (e) { showToast('Failed', 'error'); }
}

export async function loadAgents() {
  try {
    const data = await authFetch(API + '/agents').then(r => r.json());
    const el = document.getElementById('agents-list');
    if (!data.agents?.length) { el.textContent = 'No agents configured'; return; }
    el.innerHTML = data.agents.map(a => {
      const statusCls = a.status === 'running' ? 'status-ok' : a.status === 'error' ? 'status-error' : a.status === 'paused' ? 'status-warn' : a.status === 'disabled' ? '' : 'status-warn';
      const label = a.enabled === false ? 'disabled' : (a.status || 'unknown');
      const triggers = (a.triggers || []).join(', ');
      const isEnabled = a.enabled !== false;
      const isPaused = a.paused === true;
      const avgDur = a.avg_duration ? (a.avg_duration).toFixed(1) + 's' : '';
      return `<div class="agent-card-wrap" data-agent="${a.id}"><div class="agent-card"><div class="agent-info">
        <div class="agent-name">${a.id}${a.children?.length ? ' <span class="agent-children">' + a.children.length + ' children</span>' : ''}</div>
        <div class="agent-meta"><span class="trust-badge trust-${a.trust}">${a.trust}</span> ${a.schedule || 'manual'}${triggers ? ' | triggers: ' + triggers : ''}${avgDur ? ' | avg: ' + avgDur : ''}</div>
        <div class="agent-run-info"><span class="agent-last-run" data-agent="${a.id}">...</span></div>
      </div><div class="agent-actions">
        ${isEnabled ? `<button class="trigger-btn" data-agent="${a.id}" title="Run now">&#x25B6;</button>` : ''}
        ${isEnabled && !isPaused ? `<button class="pause-btn" data-agent="${a.id}" title="Pause">&#x23F8;</button>` : ''}
        ${isPaused ? `<button class="resume-btn" data-agent="${a.id}" title="Resume">&#x23EF;</button>` : ''}
        <button class="config-btn" data-agent="${a.id}" title="Configure">&#x2699;</button>
        <button class="logs-btn" data-agent="${a.id}" title="Show logs">Logs</button>
        <span class="badge ${statusCls}">${label}</span>
      </div></div>
      <div class="agent-config-panel" id="config-${a.id}" style="display:none"></div>
      <div class="agent-logs-panel" id="logs-${a.id}" style="display:none"></div>
      </div>`;
    }).join('');

    data.agents.forEach(a => {
      authFetch(API + `/agents/${a.id}/logs`).then(r => r.json()).then(d => {
        const runEl = el.querySelector(`.agent-last-run[data-agent="${a.id}"]`);
        if (runEl) {
          const parts = [];
          if (d.last_run) parts.push('last: ' + timeAgo(d.last_run));
          if (d.run_count) parts.push('runs: ' + d.run_count);
          runEl.textContent = parts.join(' | ') || 'no runs yet';
        }
      }).catch(() => {});
    });

    el.querySelectorAll('.trigger-btn').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        btn.disabled = true; btn.textContent = '...';
        try { await authFetch(API + `/agents/${btn.dataset.agent}/trigger`, { method: 'POST' }); btn.textContent = '\u2713'; setTimeout(() => { btn.textContent = '\u25B6'; btn.disabled = false; loadAgents(); }, 2000); }
        catch (e) { btn.textContent = '\u2717'; setTimeout(() => { btn.textContent = '\u25B6'; btn.disabled = false; }, 2000); }
      });
    });

    el.querySelectorAll('.pause-btn').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        try { await authFetch(API + `/agents/${btn.dataset.agent}/pause`, { method: 'POST' }); setTimeout(loadAgents, 500); }
        catch (e) { showToast('Pause failed', 'error'); }
      });
    });

    el.querySelectorAll('.resume-btn').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        try { await authFetch(API + `/agents/${btn.dataset.agent}/resume`, { method: 'POST' }); setTimeout(loadAgents, 500); }
        catch (e) { showToast('Resume failed', 'error'); }
      });
    });

    el.querySelectorAll('.config-btn').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const agentId = btn.dataset.agent;
        const panel = document.getElementById('config-' + agentId);
        const logsPanel = document.getElementById('logs-' + agentId);
        if (logsPanel.style.display !== 'none') { logsPanel.style.display = 'none'; el.querySelector(`.logs-btn[data-agent="${agentId}"]`)?.classList.remove('active'); }
        if (panel.style.display !== 'none') { panel.style.display = 'none'; btn.classList.remove('active'); return; }
        panel.innerHTML = '<div class="loading">Loading config...</div>';
        panel.style.display = 'block';
        btn.classList.add('active');
        try {
          const d = await authFetch(API + `/agents/${agentId}/config`).then(r => r.json());
          if (d.error) { panel.innerHTML = `<div class="error-msg">${escapeHtml(d.error)}</div>`; return; }
          renderAgentConfig(panel, agentId, d.config);
        } catch (err) { panel.innerHTML = `<div class="error-msg">${err.message}</div>`; }
      });
    });

    el.querySelectorAll('.logs-btn').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const agentId = btn.dataset.agent;
        const panel = document.getElementById('logs-' + agentId);
        const cfgPanel = document.getElementById('config-' + agentId);
        if (cfgPanel.style.display !== 'none') { cfgPanel.style.display = 'none'; el.querySelector(`.config-btn[data-agent="${agentId}"]`)?.classList.remove('active'); }
        if (panel.style.display !== 'none') { panel.style.display = 'none'; btn.classList.remove('active'); return; }
        panel.innerHTML = '<div class="loading">Loading logs...</div>';
        panel.style.display = 'block';
        btn.classList.add('active');
        try {
          const d = await authFetch(API + `/agents/${agentId}/logs`).then(r => r.json());
          if (d.error) { panel.innerHTML = `<div class="error-msg">${escapeHtml(d.error)}</div>`; return; }
          if (!d.logs?.length) { panel.innerHTML = '<div class="empty-state">No log entries</div>'; return; }
          panel.innerHTML = d.logs.map(l => `<div class="agent-log-line">${escapeHtml(l)}</div>`).join('');
          panel.scrollTop = panel.scrollHeight;
        } catch (err) { panel.innerHTML = `<div class="error-msg">${err.message}</div>`; }
      });
    });
  } catch (e) { document.getElementById('agents-list').textContent = 'Failed to load agents'; }
}

function renderAgentConfig(panel, agentId, cfg) {
  const isEnabled = cfg.enabled !== false;
  const schedule = cfg.schedule || '';
  const trust = cfg.trust || 'monitor';
  const agentConfig = cfg.config || {};
  const triggers = cfg.triggers || [];

  let scheduleHint = '';
  if (schedule.startsWith('*/')) {
    const mins = parseInt(schedule.substring(2));
    if (mins) scheduleHint = mins >= 60 ? `every ${mins / 60}h` : `every ${mins}m`;
  }

  panel.innerHTML = `
    <div class="agent-config-form">
      <div class="config-row">
        <label>Enabled</label>
        <label class="toggle-switch">
          <input type="checkbox" id="cfg-enabled-${agentId}" ${isEnabled ? 'checked' : ''}>
          <span class="toggle-slider"></span>
        </label>
      </div>
      <div class="config-row">
        <label>Trust Level</label>
        <select id="cfg-trust-${agentId}" class="config-select">
          <option value="monitor" ${trust === 'monitor' ? 'selected' : ''}>monitor (read-only)</option>
          <option value="safe" ${trust === 'safe' ? 'selected' : ''}>safe (+ indexing, notes, review)</option>
          <option value="full" ${trust === 'full' ? 'selected' : ''}>full (all tools)</option>
        </select>
      </div>
      <div class="config-row">
        <label>Schedule <span class="config-hint">${scheduleHint}</span></label>
        <input type="text" id="cfg-schedule-${agentId}" class="config-input" value="${escapeAttr(schedule)}" placeholder="*/5 * * * *">
      </div>
      ${agentConfig.topics ? `
      <div class="config-row config-row-col">
        <label>Topics</label>
        <textarea id="cfg-topics-${agentId}" class="config-textarea" rows="3">${(agentConfig.topics || []).join('\n')}</textarea>
      </div>` : ''}
      ${agentConfig.focus ? `
      <div class="config-row">
        <label>Focus</label>
        <input type="text" id="cfg-focus-${agentId}" class="config-input" value="${escapeAttr(agentConfig.focus)}">
      </div>` : ''}
      ${agentConfig.directories ? `
      <div class="config-row config-row-col">
        <label>Directories</label>
        <textarea id="cfg-dirs-${agentId}" class="config-textarea" rows="2">${(agentConfig.directories || []).map(d => typeof d === 'string' ? d : d.directory || d.name || JSON.stringify(d)).join('\n')}</textarea>
      </div>` : ''}
      ${triggers.length ? `
      <div class="config-row config-row-col">
        <label>Triggers</label>
        <div class="trigger-list">${triggers.map(t => `<span class="trigger-tag">${t.type || 'unknown'}${t.paths ? ' (' + t.patterns?.join(',') + ')' : ''}</span>`).join('')}</div>
      </div>` : ''}
      <div class="config-actions">
        <button class="config-save-btn" data-agent="${agentId}">Save Changes</button>
        <span class="config-status" id="cfg-status-${agentId}"></span>
      </div>
    </div>
  `;

  panel.querySelector('.config-save-btn').addEventListener('click', async () => {
    const statusEl = document.getElementById(`cfg-status-${agentId}`);
    statusEl.textContent = 'Saving...';
    statusEl.className = 'config-status';

    const patch = {
      enabled: document.getElementById(`cfg-enabled-${agentId}`).checked,
      trust: document.getElementById(`cfg-trust-${agentId}`).value,
      schedule: document.getElementById(`cfg-schedule-${agentId}`).value.trim(),
    };

    const configPatch = {};
    const topicsEl = document.getElementById(`cfg-topics-${agentId}`);
    if (topicsEl) {
      configPatch.topics = topicsEl.value.split('\n').map(s => s.trim()).filter(Boolean);
    }
    const focusEl = document.getElementById(`cfg-focus-${agentId}`);
    if (focusEl) {
      configPatch.focus = focusEl.value.trim();
    }
    if (Object.keys(configPatch).length) {
      patch.config = configPatch;
    }

    try {
      const resp = await authFetch(API + `/agents/${agentId}/config`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(patch),
      });
      const d = await resp.json();
      if (d.error) {
        statusEl.textContent = d.error;
        statusEl.className = 'config-status config-status-error';
      } else {
        statusEl.textContent = 'Saved: ' + (d.changed || []).join(', ');
        statusEl.className = 'config-status config-status-ok';
        setTimeout(() => loadAgents(), 1500);
      }
    } catch (err) {
      statusEl.textContent = 'Error: ' + err.message;
      statusEl.className = 'config-status config-status-error';
    }
  });
}

export function initAgentToolbar() {
  document.getElementById('agent-metrics-btn')?.addEventListener('click', async () => {
    const panel = document.getElementById('agent-panel');
    panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
    if (panel.style.display === 'none') return;
    panel.innerHTML = '<div class="loading">Loading metrics...</div>';
    try {
      const data = await authFetch(API + '/agents/metrics').then(r => r.json());
      panel.innerHTML = `<div class="metrics-grid">
        ${statusRow('Total Agents', data.total_agents)}
        ${statusRow('Running', data.running, 'ok')}
        ${statusRow('Paused', data.paused)}
        ${statusRow('Task Queue Depth', data.task_queue_depth)}
        ${statusRow('Workers', data.workers)}
        ${statusRow('Bus Subscribers', data.bus_subscribers)}
      </div>`;
    } catch (e) { panel.innerHTML = '<div class="error-msg">' + e.message + '</div>'; }
  });

  document.getElementById('agent-tasks-btn')?.addEventListener('click', async () => {
    const panel = document.getElementById('agent-panel');
    panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
    if (panel.style.display === 'none') return;
    panel.innerHTML = '<div class="loading">Loading tasks...</div>';
    try {
      const data = await authFetch(API + '/agents/tasks').then(r => r.json());
      const tasks = data.tasks || [];
      if (!tasks.length) { panel.innerHTML = '<div class="empty-state">No tasks in queue</div>'; return; }
      panel.innerHTML = '<div class="task-list">' + tasks.map(t => `
        <div class="task-item task-${t.status}">
          <span class="task-id">${t.id.substring(0, 8)}</span>
          <span class="badge badge-${t.status}">${t.status}</span>
          <span class="task-queue">${t.queue}</span>
          <span class="task-priority">P${t.priority}</span>
          ${t.error ? `<span class="task-error">${escapeHtml(t.error).substring(0, 60)}</span>` : ''}
        </div>
      `).join('') + '</div>';
    } catch (e) { panel.innerHTML = '<div class="error-msg">' + e.message + '</div>'; }
  });

  document.getElementById('agent-bus-btn')?.addEventListener('click', async () => {
    const panel = document.getElementById('agent-panel');
    panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
    if (panel.style.display === 'none') return;
    panel.innerHTML = '<div class="loading">Loading messages...</div>';
    try {
      const data = await authFetch(API + '/agents/bus').then(r => r.json());
      const msgs = data.messages || [];
      if (!msgs.length) { panel.innerHTML = '<div class="empty-state">No recent messages</div>'; return; }
      panel.innerHTML = '<div class="bus-messages">' + msgs.map(m => `
        <div class="bus-msg">
          <span class="bus-topic">${escapeHtml(m.topic)}</span>
          <span class="bus-sender">${escapeHtml(m.sender)}</span>
          <span class="bus-time">${timeAgo(m.timestamp)}</span>
        </div>
      `).join('') + '</div>';
    } catch (e) { panel.innerHTML = '<div class="error-msg">' + e.message + '</div>'; }
  });
}
