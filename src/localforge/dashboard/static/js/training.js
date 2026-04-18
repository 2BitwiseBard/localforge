import { apiFetch, API, showToast, escapeHtml, escapeAttr } from './api.js';

let _pollTimer = null;

export function loadTrainingOverview() {
  loadPreflight();
  loadTrainingDatasets();
  loadTrainingRuns();
  loadTrainingLoras();
}

export async function loadTrainingLoras() {
  const el = document.getElementById('train-loras-list');
  if (!el) return;
  try {
    const data = await apiFetch(`${API}/training/loras`).then(r => r.json());
    if (!data.loras || !data.loras.length) {
      el.innerHTML = '<div style="color:#8b949e;font-size:0.85em;padding:8px 0;">No LoRA adapters yet — complete a training run first.</div>';
      return;
    }
    el.innerHTML = data.loras.map(l => `
      <div style="display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid var(--border);font-size:0.85em;flex-wrap:wrap;gap:6px;">
        <div>
          <span style="font-family:monospace;">${escapeHtml(l.name)}</span>
          <span style="color:#8b949e;font-size:0.8em;margin-left:8px;">${escapeHtml(l.base_model.split('/').pop())}</span>
          ${l.has_gguf ? `<span style="color:#3fb950;font-size:0.75em;margin-left:6px;">&#x2713; GGUF: ${escapeHtml(l.gguf_files.join(', '))}</span>` : ''}
        </div>
        <button class="btn-small lora-load-btn" data-path="${escapeAttr(l.path)}" data-name="${escapeAttr(l.name)}">Load LoRA</button>
      </div>`).join('');
    el.querySelectorAll('.lora-load-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        btn.disabled = true; btn.textContent = 'Loading…';
        try {
          const resp = await apiFetch(`${API}/loras/load`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: btn.dataset.path }),
          });
          const d = await resp.json();
          showToast(d.result || 'LoRA loaded', 'success');
        } catch (err) {
          showToast('Load failed: ' + err.message, 'error');
        } finally {
          btn.disabled = false; btn.textContent = 'Load LoRA';
        }
      });
    });
  } catch (e) {
    if (el) el.textContent = 'Error loading adapters: ' + e.message;
  }
}

// ── Pre-flight (Step 1) ────────────────────────────────────────────────────

export async function loadPreflight() {
  const gpuEl = document.getElementById('train-gpu-status');
  const barWrap = document.getElementById('train-vram-bar-wrap');
  const bar = document.getElementById('train-vram-bar');
  const label = document.getElementById('train-vram-label');
  const warn = document.getElementById('train-vram-warn');
  const modelEl = document.getElementById('train-model-status');
  const unloadBtn = document.getElementById('train-unload-btn');

  if (gpuEl) gpuEl.textContent = 'Checking GPU…';
  if (modelEl) modelEl.textContent = 'Checking…';

  try {
    const resp = await apiFetch(`${API}/training/preflight`);
    const data = await resp.json();

    // GPU section
    if (data.gpu) {
      const { used_mb, free_mb, total_mb, name } = data.gpu;
      const usedGb = (used_mb / 1024).toFixed(1);
      const freeGb = (free_mb / 1024).toFixed(1);
      const totalGb = (total_mb / 1024).toFixed(1);
      const pct = Math.round(used_mb / total_mb * 100);

      if (gpuEl) gpuEl.textContent = `${name} — ${usedGb} GB used / ${totalGb} GB total`;
      if (barWrap) barWrap.style.display = '';
      if (label) label.textContent = `${freeGb} GB free`;
      if (bar) {
        bar.style.width = pct + '%';
        bar.style.background = pct > 85 ? '#f0883e' : pct > 60 ? '#d29922' : '#3fb950';
      }
      if (warn) {
        if (free_mb < 4096) {
          warn.style.display = '';
          warn.textContent = `⚠ Only ${freeGb} GB free. Training needs 4–16 GB depending on model size. Unload the inference model first.`;
        } else {
          warn.style.display = 'none';
        }
      }
    } else {
      if (gpuEl) gpuEl.textContent = 'GPU info unavailable (nvidia-smi not found)';
    }

    // RAM section
    const ramEl = document.getElementById('train-ram-status');
    if (ramEl) {
      if (data.ram) {
        const { total_gb, available_gb } = data.ram;
        const color = available_gb < 8 ? '#f0883e' : available_gb < 16 ? '#d29922' : '#8b949e';
        ramEl.innerHTML = `<span style="color:${color};">RAM: ${available_gb} GB available / ${total_gb} GB total</span>`;
      } else {
        ramEl.textContent = '';
      }
    }

    // Model section
    if (data.model_loaded) {
      if (modelEl) modelEl.innerHTML = `<span style="color:#f0883e;">&#9888; Loaded: <strong>${escapeHtml(data.model_loaded)}</strong></span><br><span style="color:#8b949e;font-size:0.8em;">This is using GPU VRAM. Unload it before training to avoid OOM errors.</span>`;
      if (unloadBtn) unloadBtn.style.display = '';
    } else {
      if (modelEl) modelEl.innerHTML = `<span style="color:#3fb950;">&#10003; No inference model loaded — GPU VRAM is free for training.</span>`;
      if (unloadBtn) unloadBtn.style.display = 'none';
    }
  } catch (e) {
    if (gpuEl) gpuEl.textContent = 'Error: ' + e.message;
  }
}

async function unloadInferenceModel() {
  const btn = document.getElementById('train-unload-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Unloading…'; }
  try {
    await apiFetch(`${API}/model/unload`, { method: 'POST' });
    showToast('Inference model unloaded — VRAM freed', 'success');
    await loadPreflight();
  } catch (e) {
    showToast('Unload failed: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Unload Inference Model'; }
  }
}

// ── Datasets (Step 2) ──────────────────────────────────────────────────────

export async function loadTrainingDatasets() {
  const tbody = document.getElementById('train-dataset-tbody');
  const sel = document.getElementById('train-dataset-select');
  if (!tbody) return;

  try {
    const resp = await apiFetch(`${API}/training?what=datasets&fmt=json`);
    const data = await resp.json();

    if (!data.datasets || data.datasets.length === 0) {
      tbody.innerHTML = '<tr><td colspan="3" style="padding:12px 8px;color:#8b949e;">No datasets yet. Use Prepare Dataset below.</td></tr>';
      if (sel) sel.innerHTML = '<option value="">No datasets found</option>';
      return;
    }

    tbody.innerHTML = data.datasets.map(ds => `
      <tr style="border-bottom:1px solid var(--border);">
        <td style="padding:6px 8px;font-family:monospace;font-size:0.85em;">${escapeHtml(ds.name)}</td>
        <td style="padding:6px 8px;text-align:right;color:#8b949e;">${ds.examples > 0 ? ds.examples.toLocaleString() : '—'}</td>
        <td style="padding:6px 8px;text-align:right;color:#8b949e;">${_fmtSize(ds.size_kb)}</td>
      </tr>`).join('');

    if (sel) {
      sel.innerHTML = '<option value="">Select dataset…</option>' +
        data.datasets.map(ds =>
          `<option value="${escapeHtml(ds.name)}">${escapeHtml(ds.name)} (${ds.examples > 0 ? ds.examples.toLocaleString() + ' ex' : _fmtSize(ds.size_kb)})</option>`
        ).join('');
    }
  } catch (e) {
    if (tbody) tbody.innerHTML = `<tr><td colspan="3" style="padding:12px 8px;color:#f0883e;">Error: ${escapeHtml(e.message)}</td></tr>`;
  }
}

function _fmtSize(kb) {
  if (kb >= 1024 * 1024) return (kb / 1024 / 1024).toFixed(1) + ' GB';
  if (kb >= 1024) return (kb / 1024).toFixed(0) + ' MB';
  return kb + ' KB';
}

// ── Training runs list ──────────────────────────────────────────────────────

export async function loadTrainingRuns() {
  const el = document.getElementById('train-runs-list');
  if (!el) return;
  try {
    const resp = await apiFetch(`${API}/training?what=runs`);
    const data = await resp.json();
    const text = data.result || '';
    if (!text || text.includes('No training runs')) {
      el.textContent = 'No completed runs yet.';
      return;
    }
    const lines = text.split('\n').filter(l => l.match(/^\s+\S/));
    el.innerHTML = lines.map(line => {
      const parts = line.trim().split(/\s{2,}/);
      const name = parts[0] || '';
      const status = parts[1]?.replace(/[\[\]]/g, '') || '';
      const model = parts[2] || '';
      const date = parts[3] || '';
      const statusColor = status === 'done' || status === 'completed' ? '#3fb950' : status === 'failed' ? '#f0883e' : '#8b949e';
      return `<div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border);font-size:0.85em;flex-wrap:wrap;gap:4px;">
        <span style="font-family:monospace;">${escapeHtml(name)}</span>
        <span style="color:${statusColor};">${escapeHtml(status)}</span>
        <span style="color:#8b949e;font-size:0.8em;">${escapeHtml(model)}</span>
        <span style="color:#8b949e;font-size:0.75em;">${escapeHtml(date)}</span>
      </div>`;
    }).join('') || '<div style="color:#8b949e;">No runs found.</div>';
  } catch (e) {
    if (el) el.textContent = 'Error loading runs.';
  }
}

// ── Active run status polling (Step 3) ────────────────────────────────────

export async function loadTrainingStatus() {
  const card = document.getElementById('train-active-card');
  const badge = document.getElementById('train-run-badge');
  const meta = document.getElementById('train-run-meta');
  const log = document.getElementById('train-run-log');
  const progressWrap = document.getElementById('train-progress-wrap');
  const progressBar = document.getElementById('train-progress-bar');
  const progressLabel = document.getElementById('train-progress-label');
  if (!card) return;

  try {
    const resp = await apiFetch(`${API}/training/status?tail=50`);
    const data = await resp.json();

    if (!data.active && !data.run_name) {
      card.style.display = 'none';
      _stopPoll();
      return;
    }

    card.style.display = '';

    // Badge
    if (badge) {
      const st = data.run_status || 'unknown';
      badge.textContent = st.toLowerCase();
      badge.className = 'mesh-status-pill ' + (data.active ? 'pill-online' : st === 'COMPLETED' ? 'pill-idle' : 'pill-offline');
    }

    // Meta line
    if (meta) {
      const parts = [
        data.run_name && `Run: ${data.run_name}`,
        data.base_model && escapeHtml(data.base_model.split('/').pop()),
        data.dataset && `dataset: ${escapeHtml(data.dataset)}`,
        data.elapsed && `elapsed: ${data.elapsed}`,
        data.loss && `loss: ${data.loss}`,
      ].filter(Boolean);
      meta.textContent = parts.join('  ·  ');
    }

    // Progress bar — parse "Epoch X/Y" or "step X of Y" from log
    if (progressWrap) {
      const logText = (data.log_lines || []).join('\n');
      const epochMatch = logText.match(/[Ee]poch\s+(\d+)\s*[/of]+\s*(\d+)/);
      const stepMatch = logText.match(/\[(\d+)\/(\d+)/);
      if (epochMatch) {
        const cur = parseInt(epochMatch[1]), tot = parseInt(epochMatch[2]);
        const pct = Math.round(cur / tot * 100);
        progressWrap.style.display = '';
        if (progressBar) progressBar.style.width = pct + '%';
        if (progressLabel) progressLabel.textContent = `Epoch ${cur}/${tot}`;
      } else if (stepMatch) {
        const cur = parseInt(stepMatch[1]), tot = parseInt(stepMatch[2]);
        const pct = Math.round(cur / tot * 100);
        progressWrap.style.display = '';
        if (progressBar) progressBar.style.width = pct + '%';
        if (progressLabel) progressLabel.textContent = `Step ${cur}/${tot} (${pct}%)`;
      } else {
        progressWrap.style.display = 'none';
      }
    }

    // Log
    if (log && data.log_lines && data.log_lines.length > 0) {
      const atBottom = log.scrollHeight - log.clientHeight - log.scrollTop < 40;
      log.textContent = data.log_lines.join('\n');
      if (atBottom || data.active) log.scrollTop = log.scrollHeight;
    } else if (log && data.result) {
      const sep = data.result.indexOf('──');
      if (sep >= 0) log.textContent = data.result.slice(data.result.indexOf('\n', sep) + 1);
    }

    // Start polling if active
    if (data.active) _startPoll();
    else _stopPoll();

  } catch (e) {
    if (card) card.style.display = 'none';
  }
}

function _startPoll() {
  if (_pollTimer) return;
  _pollTimer = setInterval(loadTrainingStatus, 4000);
  const ind = document.getElementById('train-poll-indicator');
  if (ind) ind.style.display = '';
}

function _stopPoll() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
  const ind = document.getElementById('train-poll-indicator');
  if (ind) ind.style.display = 'none';
}

// ── VRAM hint ──────────────────────────────────────────────────────────────

const VRAM_TABLE = [
  { prefix: 'Qwen3-0.6B',  bf16: '~2 GB',  qlora: '~1 GB',  rank: 8 },
  { prefix: 'Qwen3-1.7B',  bf16: '~3 GB',  qlora: '~2 GB',  rank: 8 },
  { prefix: 'Qwen3-4B',    bf16: '~8 GB',  qlora: '~4 GB',  rank: 16 },
  { prefix: 'Qwen3-8B',    bf16: '~16 GB (tight)', qlora: '~6 GB', rank: 32 },
  { prefix: 'Qwen3-14B',   bf16: 'OOM',    qlora: '~9 GB',  rank: 16 },
  { prefix: 'Qwen3-30B',   bf16: 'OOM',    qlora: '~16 GB (tight)', rank: 8 },
  { prefix: 'gemma-3-1b',  bf16: '~2 GB',  qlora: '~2 GB',  rank: 8 },
  { prefix: 'gemma-3-4b',  bf16: '~8 GB',  qlora: '~4 GB',  rank: 16 },
  { prefix: 'gemma-3-12b', bf16: 'OOM',    qlora: '~8 GB',  rank: 16 },
  { prefix: 'gemma-3-27b', bf16: 'OOM',    qlora: '~15 GB (tight)', rank: 8 },
  { prefix: 'Llama-4-Scout', bf16: 'OOM',  qlora: '~10 GB', rank: 16 },
  { prefix: 'Mistral-Small-3', bf16: 'OOM',qlora: '~13 GB', rank: 8 },
  { prefix: 'Phi-4-mini',  bf16: '~6 GB',  qlora: '~3 GB',  rank: 16 },
  { prefix: 'Qwen2.5-7B',  bf16: 'OOM',    qlora: '~5 GB',  rank: 16 },
];

function updateVramHint() {
  const hint = document.getElementById('train-vram-hint');
  if (!hint) return;
  const model = document.getElementById('train-base-model')?.value || '';
  const bf16 = document.getElementById('train-bf16')?.checked;
  const fullFt = document.getElementById('train-full-finetune')?.checked;
  const gradCk = document.getElementById('train-grad-ck')?.checked;
  const entry = VRAM_TABLE.find(e => model.includes(e.prefix));
  if (!entry) { hint.textContent = ''; return; }
  const vram = fullFt ? 'Requires full VRAM' : bf16 ? entry.bf16 : entry.qlora;
  const mode = fullFt ? 'Full fine-tune' : bf16 ? 'bf16 LoRA' : 'QLoRA (4-bit)';
  const isOom = vram === 'OOM';
  const isTight = vram.includes('tight');
  let html = `<span style="color:${isOom ? '#f0883e' : isTight ? '#d29922' : '#8b949e'}">&#x1F4BB; <strong>${mode}</strong>: estimated training VRAM ${vram}. RTX 3080 Ti = 16 GB.</span>`;
  if ((isTight || isOom) && !bf16 && !fullFt) {
    if (gradCk) {
      html += ` <span style="color:#3fb950;">&#x2713; Gradient checkpointing enabled — saves ~20-30% VRAM at ~20% speed cost.</span>`;
    } else {
      html += ` <span style="color:#d29922;">&#x1F4A1; Tip: enable gradient checkpointing below to save ~20-30% VRAM.</span>`;
    }
  }
  if (isOom) {
    html += ` <span style="color:#f0883e;">Consider a smaller model or enable gradient checkpointing.</span>`;
  }
  hint.innerHTML = html;
}

// ── Prepare mode UI ────────────────────────────────────────────────────────

function updatePrepareUI(mode) {
  const repo = document.getElementById('train-repo');
  const glob = document.getElementById('train-glob');
  const qa = document.getElementById('train-qa-input');
  const hfId = document.getElementById('train-hf-dataset-id');
  const hfOpts = document.getElementById('train-hf-opts');
  const mergeInputs = document.getElementById('train-merge-inputs');
  const mergeCap = document.getElementById('train-merge-cap-label');

  [repo, glob, qa, hfId, mergeInputs].forEach(el => el && (el.style.display = 'none'));
  [hfOpts, mergeCap].forEach(el => el && (el.style.display = 'none'));

  if (mode === 'git-diffs') {
    repo.style.display = '';
    repo.placeholder = 'Repository path (e.g. ~/Development/platform_next)';
  } else if (mode === 'code-pairs') {
    repo.style.display = '';
    glob.style.display = '';
    repo.placeholder = 'Source directory (e.g. ~/Development/platform_next/src)';
  } else if (mode === 'from-qa') {
    qa.style.display = '';
  } else if (mode === 'huggingface') {
    hfId.style.display = '';
    hfOpts.style.display = 'flex';
  } else if (mode === 'merge') {
    mergeInputs.style.display = '';
    mergeCap.style.display = '';
  }
}

// ── Init ──────────────────────────────────────────────────────────────────

export function initTraining() {
  // Preflight
  document.getElementById('train-preflight-refresh')?.addEventListener('click', loadPreflight);
  document.getElementById('train-unload-btn')?.addEventListener('click', unloadInferenceModel);

  // Prepare dataset
  document.getElementById('train-prepare-btn')?.addEventListener('click', async () => {
    const btn = document.getElementById('train-prepare-btn');
    const result = document.getElementById('train-prepare-result');
    const mode = document.getElementById('train-mode').value;

    const body = { mode };
    if (mode === 'git-diffs') body.repo = document.getElementById('train-repo').value;
    if (mode === 'code-pairs') {
      body.directory = document.getElementById('train-repo').value;
      const g = document.getElementById('train-glob').value;
      if (g) body.glob_pattern = g;
    }
    if (mode === 'from-qa') body.input = document.getElementById('train-qa-input').value;
    if (mode === 'huggingface') {
      body.dataset_id = document.getElementById('train-hf-dataset-id').value;
      body.hf_split = document.getElementById('train-hf-split').value || 'train';
      const mx = parseInt(document.getElementById('train-hf-max').value);
      if (mx > 0) body.max_examples = mx;
    }
    if (mode === 'merge') {
      const raw = document.getElementById('train-merge-inputs').value;
      body.inputs = raw.split(',').map(s => s.trim()).filter(Boolean);
      const cap = parseInt(document.getElementById('train-merge-cap').value);
      if (cap > 0) body.max_per_source = cap;
    }

    const name = document.getElementById('train-dataset-name').value;
    if (name) body.name = name;

    btn.disabled = true;
    btn.textContent = 'Preparing…';
    if (result) result.style.display = 'none';
    try {
      const resp = await apiFetch(`${API}/training/prepare`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await resp.json();
      if (result) {
        result.textContent = data.result || data.error;
        result.style.display = 'block';
      }
      showToast('Dataset prepared', 'success');
      await loadTrainingDatasets();
    } catch (e) {
      if (result) { result.textContent = 'Error: ' + e.message; result.style.display = 'block'; }
    } finally {
      btn.disabled = false;
      btn.textContent = 'Prepare Dataset';
    }
  });

  // Start training
  document.getElementById('train-start-btn')?.addEventListener('click', async () => {
    const btn = document.getElementById('train-start-btn');
    const warn = document.getElementById('train-start-warn');
    const dataset = document.getElementById('train-dataset-select').value;
    if (!dataset) { if (warn) { warn.textContent = 'Select a dataset first.'; warn.style.display = 'block'; } return; }

    const body = {
      dataset,
      base_model: document.getElementById('train-base-model').value,
      epochs: parseInt(document.getElementById('train-epochs').value) || 3,
      batch_size: parseInt(document.getElementById('train-batch').value) || 2,
      learning_rate: parseFloat(document.getElementById('train-lr').value) || 0.0002,
      lora_rank: parseInt(document.getElementById('train-lora-rank').value) || 16,
      lora_alpha: parseInt(document.getElementById('train-lora-alpha').value) || 32,
      max_seq_len: parseInt(document.getElementById('train-max-seq').value) || 2048,
      export_gguf: document.getElementById('train-gguf').value || 'q4_k_m,q8_0',
      bf16_lora: document.getElementById('train-bf16').checked,
      full_finetune: document.getElementById('train-full-finetune')?.checked || false,
      gradient_checkpointing: document.getElementById('train-grad-ck')?.checked || false,
      grad_accum: parseInt(document.getElementById('train-grad-accum').value) || 4,
      val_split: parseFloat(document.getElementById('train-val-split').value) || 0.1,
      early_stopping: parseInt(document.getElementById('train-early-stop').value) || 0,
      lr_scheduler: document.getElementById('train-lr-sched').value || 'cosine',
    };
    const runName = document.getElementById('train-run-name').value;
    if (runName) body.name = runName;

    btn.disabled = true;
    btn.textContent = 'Starting…';
    if (warn) warn.style.display = 'none';
    try {
      const resp = await apiFetch(`${API}/training/start`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await resp.json();
      if (data.error || (data.result && data.result.includes('currently loaded'))) {
        if (warn) { warn.textContent = data.error || data.result; warn.style.display = 'block'; }
      } else {
        showToast('Training started!', 'success');
        const card = document.getElementById('train-active-card');
        if (card) card.style.display = '';
        loadTrainingStatus();
        _startPoll();
      }
    } catch (e) {
      if (warn) { warn.textContent = 'Error: ' + e.message; warn.style.display = 'block'; }
    } finally {
      btn.disabled = false;
      btn.textContent = '▶ Start Training Run';
    }
  });

  // Refresh log button
  document.getElementById('train-refresh-log-btn')?.addEventListener('click', loadTrainingStatus);

  // Refresh datasets button
  document.getElementById('train-refresh-datasets-btn')?.addEventListener('click', () => {
    loadTrainingDatasets();
    loadTrainingRuns();
  });

  // Feedback
  document.getElementById('fb-submit-btn')?.addEventListener('click', async () => {
    const prompt = document.getElementById('fb-prompt').value;
    const response = document.getElementById('fb-response').value;
    const rating = parseInt(document.getElementById('fb-rating').value);
    const resultEl = document.getElementById('fb-result');
    if (!prompt || !response) { if (resultEl) resultEl.textContent = 'Prompt and response are required.'; return; }
    try {
      const resp = await apiFetch(`${API}/training/feedback`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt, response, rating }),
      });
      const data = await resp.json();
      if (resultEl) resultEl.textContent = data.result || 'Feedback recorded.';
      document.getElementById('fb-prompt').value = '';
      document.getElementById('fb-response').value = '';
      showToast('Feedback recorded', 'success');
    } catch (e) {
      if (resultEl) resultEl.textContent = 'Error: ' + e.message;
    }
  });

  // Mode switcher
  document.getElementById('train-mode')?.addEventListener('change', e => updatePrepareUI(e.target.value));

  // Preset buttons
  document.querySelectorAll('.train-preset-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.getElementById('train-base-model').value = btn.dataset.model;
      document.getElementById('train-lora-rank').value = btn.dataset.rank;
      if (btn.dataset.alpha) document.getElementById('train-lora-alpha').value = btn.dataset.alpha;
      document.getElementById('train-bf16').checked = btn.dataset.bf16 === 'true';
      updateVramHint();
    });
  });

  // VRAM hint live update
  document.getElementById('train-base-model')?.addEventListener('input', updateVramHint);
  document.getElementById('train-bf16')?.addEventListener('change', updateVramHint);
  document.getElementById('train-full-finetune')?.addEventListener('change', updateVramHint);
  document.getElementById('train-grad-ck')?.addEventListener('change', updateVramHint);

  // LoRA adapters refresh
  document.getElementById('train-refresh-loras-btn')?.addEventListener('click', loadTrainingLoras);

  // Init — no eager API calls; data loads when the training tab is opened
  updatePrepareUI('git-diffs');
  updateVramHint();
}
