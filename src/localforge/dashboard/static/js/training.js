import { API, apiFetch, showToast, escapeHtml } from './api.js';

export async function loadTrainingOverview() {
  const el = document.getElementById('training-overview');
  if (!el) return;
  try {
    const resp = await apiFetch('/api/training?what=all');
    const data = await resp.json();
    el.textContent = data.result || 'No training data yet.';
    const select = document.getElementById('train-dataset-select');
    if (select) {
      const dResp = await apiFetch('/api/training?what=datasets');
      const dData = await dResp.json();
      select.innerHTML = '<option value="">Select dataset...</option>';
      const lines = (dData.result || '').split('\n');
      for (const line of lines) {
        const match = line.match(/^\s+(\S+\.jsonl)/);
        if (match) {
          const opt = document.createElement('option');
          opt.value = match[1];
          opt.textContent = line.trim();
          select.appendChild(opt);
        }
      }
    }
  } catch (e) {
    el.textContent = 'Error loading training data: ' + e.message;
  }
}

export async function loadTrainingStatus() {
  const el = document.getElementById('training-status');
  if (!el) return;
  try {
    const resp = await apiFetch('/api/training/status');
    const data = await resp.json();
    el.textContent = data.result || 'No active run';
  } catch (e) {
    el.textContent = 'No training runs found.';
  }
}

export function initTraining() {
  document.getElementById('train-prepare-btn')?.addEventListener('click', async () => {
    const btn = document.getElementById('train-prepare-btn');
    const result = document.getElementById('train-prepare-result');
    const mode = document.getElementById('train-mode').value;
    const body = { mode };
    if (mode === 'git-diffs') body.repo = document.getElementById('train-repo').value;
    if (mode === 'code-pairs') {
      body.directory = document.getElementById('train-repo').value;
      const glob = document.getElementById('train-glob').value;
      if (glob) body.glob_pattern = glob;
    }
    const name = document.getElementById('train-dataset-name').value;
    if (name) body.name = name;
    btn.disabled = true;
    btn.textContent = 'Preparing...';
    result.style.display = 'none';
    try {
      const resp = await apiFetch('/api/training/prepare', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await resp.json();
      result.textContent = data.result || data.error;
      result.style.display = 'block';
      showToast('Dataset prepared', 'success');
      loadTrainingOverview();
    } catch (e) {
      result.textContent = 'Error: ' + e.message;
      result.style.display = 'block';
    } finally {
      btn.disabled = false;
      btn.textContent = 'Prepare Dataset';
    }
  });

  document.getElementById('train-start-btn')?.addEventListener('click', async () => {
    const btn = document.getElementById('train-start-btn');
    const warn = document.getElementById('train-start-warn');
    const dataset = document.getElementById('train-dataset-select').value;
    if (!dataset) { warn.textContent = 'Select a dataset first.'; warn.style.display = 'block'; return; }
    const body = {
      dataset,
      base_model: document.getElementById('train-base-model').value,
      epochs: parseInt(document.getElementById('train-epochs').value) || 3,
      batch_size: parseInt(document.getElementById('train-batch').value) || 2,
      learning_rate: parseFloat(document.getElementById('train-lr').value) || 0.0002,
      lora_rank: parseInt(document.getElementById('train-lora-rank').value) || 16,
      max_seq_len: parseInt(document.getElementById('train-max-seq').value) || 2048,
      export_gguf: document.getElementById('train-gguf').value || 'q4_k_m',
    };
    btn.disabled = true;
    btn.textContent = 'Starting...';
    warn.style.display = 'none';
    try {
      const resp = await apiFetch('/api/training/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await resp.json();
      if (data.result && data.result.includes('currently loaded')) {
        warn.textContent = data.result;
        warn.style.display = 'block';
      } else {
        showToast('Training started!', 'success');
        loadTrainingStatus();
      }
    } catch (e) {
      warn.textContent = 'Error: ' + e.message;
      warn.style.display = 'block';
    } finally {
      btn.disabled = false;
      btn.textContent = 'Start Training';
    }
  });

  document.getElementById('fb-submit-btn')?.addEventListener('click', async () => {
    const prompt = document.getElementById('fb-prompt').value;
    const response = document.getElementById('fb-response').value;
    const rating = parseInt(document.getElementById('fb-rating').value);
    const resultEl = document.getElementById('fb-result');
    if (!prompt || !response) { resultEl.textContent = 'Prompt and response are required.'; return; }
    try {
      const resp = await apiFetch('/api/training/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt, response, rating }),
      });
      const data = await resp.json();
      resultEl.textContent = data.result || 'Feedback recorded.';
      document.getElementById('fb-prompt').value = '';
      document.getElementById('fb-response').value = '';
      showToast('Feedback recorded', 'success');
    } catch (e) {
      resultEl.textContent = 'Error: ' + e.message;
    }
  });

  document.getElementById('train-mode')?.addEventListener('change', (e) => {
    const mode = e.target.value;
    const repoEl = document.getElementById('train-repo');
    const globEl = document.getElementById('train-glob');
    if (mode === 'git-diffs') {
      repoEl.placeholder = 'Repository path (e.g. ~/Development/my-project)';
      repoEl.style.display = '';
      globEl.style.display = 'none';
    } else if (mode === 'code-pairs') {
      repoEl.placeholder = 'Source directory path';
      repoEl.style.display = '';
      globEl.style.display = '';
    } else {
      repoEl.style.display = 'none';
      globEl.style.display = 'none';
    }
  });
}
