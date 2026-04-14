// AI Hub Dashboard — vanilla JS
// PWA + Multi-user + Chat History + Photos + Voice + Notifications

const API = window.location.origin + '/api';

// API key from sessionStorage for authenticated requests (cleared on tab close)
let apiKey = sessionStorage.getItem('ai-hub-key') || '';
let currentUser = null;

function authHeaders(extra = {}) {
  return { 'Authorization': `Bearer ${apiKey}`, ...extra };
}

async function authFetch(url, opts = {}) {
  opts.headers = { ...authHeaders(), ...(opts.headers || {}) };
  return fetch(url, opts);
}
const apiFetch = authFetch;

// =====================================================================
// PWA Registration
// =====================================================================
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/static/sw.js').catch(() => {});
}

// =====================================================================
// Connection status indicator
// =====================================================================
const connDot = document.getElementById('conn-dot');
async function checkConnection() {
  try {
    const r = await fetch('/health', {signal: AbortSignal.timeout(3000)});
    const ok = r.ok;
    connDot.className = 'conn-dot ' + (ok ? 'conn-online' : 'conn-offline');
    connDot.title = ok ? 'Backend online' : 'Backend error';
    return ok;
  } catch {
    connDot.className = 'conn-dot conn-offline';
    connDot.title = 'Backend unreachable';
    return false;
  }
}
checkConnection();
setInterval(checkConnection, 15000);

// =====================================================================
// Auth / User
// =====================================================================
async function initUser() {
  if (!apiKey) {
    apiKey = prompt('Enter your AI Hub API key:') || '';
    if (apiKey) sessionStorage.setItem('ai-hub-key', apiKey);
  }
  try {
    const resp = await authFetch(API + '/me');
    if (resp.ok) {
      currentUser = await resp.json();
      document.getElementById('user-badge').textContent = currentUser.name || currentUser.id;
    } else if (resp.status === 401) {
      sessionStorage.removeItem('ai-hub-key');
      apiKey = prompt('Invalid key. Enter your AI Hub API key:') || '';
      if (apiKey) { sessionStorage.setItem('ai-hub-key', apiKey); return initUser(); }
    }
  } catch (e) {
    document.getElementById('user-badge').textContent = 'offline';
  }
}

// =====================================================================
// Tabs
// =====================================================================
document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
    const tab = btn.dataset.tab;
    if (tab === 'search') { loadIndexes(); loadIndexMgmt(); }
    if (tab === 'knowledge') loadKGStats();
    if (tab === 'media') loadPhotos();
    if (tab === 'config') { loadGenParams(); loadPresets(); loadLoras(); }
    if (tab === 'research') loadResearchSessions();
    if (tab === 'workflows') loadWorkflows();
  });
});

// =====================================================================
// Status + GPU Metrics
// =====================================================================
async function loadStatus() {
  try {
    const [health, status, metrics] = await Promise.all([
      fetch('/health').then(r => r.json()),
      authFetch(API + '/status').then(r => r.json()),
      authFetch(API + '/metrics').then(r => r.json()).catch(() => ({})),
    ]);
    const badge = document.getElementById('model-badge');
    const modelName = health.model?.model_name || status.model?.name || '--';
    badge.textContent = modelName.replace('.gguf', '').substring(0, 30);

    const si = document.getElementById('status-info');
    si.innerHTML = statusRow('Model', modelName, health.model?.status === 'loaded' ? 'ok' : 'error')
      + statusRow('Uptime', formatUptime(health.uptime_seconds), 'ok')
      + statusRow('LoRA', (health.model?.lora_names || []).join(', ') || 'none');

    // Slot & server config info from status endpoint
    if (status.slots) {
      const s = status.slots;
      si.innerHTML += statusRow('Parallel Slots', `${s.active} / ${s.total} active`)
        + statusRow('Context / Slot', s.ctx_per_slot?.toLocaleString() || '--')
        + statusRow('Total Context', s.ctx_total?.toLocaleString() || '--');
    }
    if (status.server_config) {
      const sc = status.server_config;
      if (sc.gpu_layers) si.innerHTML += statusRow('GPU Layers', sc.gpu_layers);
      if (sc.batch_size) si.innerHTML += statusRow('Batch Size', sc.batch_size);
      if (sc.flash_attn) si.innerHTML += statusRow('Flash Attn', sc.flash_attn);
    }

    const hi = document.getElementById('health-info');
    hi.innerHTML = statusRow('Gateway', health.status, health.status === 'ok' ? 'ok' : 'error')
      + statusRow('Backend', health.model?.status || 'unknown',
          health.model?.status === 'loaded' ? 'ok' : 'error');

    renderGPUMetrics(metrics);
  } catch (e) {
    document.getElementById('status-info').textContent = 'Failed to load: ' + e.message;
  }
}

// Auto-refresh status tab every 30s when visible
let _statusInterval = null;
function startStatusRefresh() {
  if (_statusInterval) return;
  _statusInterval = setInterval(() => {
    const tab = document.querySelector('.tab.active');
    if (tab && tab.dataset.tab === 'status') {
      loadStatus(); loadMeshStatus();
    }
  }, 30000);
}
startStatusRefresh();

function renderGPUMetrics(metrics) {
  const el = document.getElementById('gpu-metrics');
  if (!metrics.gpu) { el.textContent = 'GPU metrics unavailable'; return; }
  const g = metrics.gpu;
  const usedPct = Math.round((g.vram_used_mb / g.vram_total_mb) * 100);
  el.innerHTML = `
    ${statusRow('GPU', g.name)}
    ${statusRow('VRAM', `${(g.vram_used_mb/1024).toFixed(1)} / ${(g.vram_total_mb/1024).toFixed(1)} GB`)}
    <div class="vram-bar-container">
      <div class="vram-bar" style="width:${usedPct}%;background:${usedPct>90?'var(--red)':usedPct>70?'var(--yellow)':'var(--green)'}"></div>
      <span class="vram-label">${usedPct}%</span>
    </div>
    ${statusRow('GPU Util', g.utilization_pct + '%')}
    ${statusRow('Temp', g.temperature_c + '°C', g.temperature_c > 80 ? 'error' : '')}
  `;
}

function statusRow(label, value, cls) {
  const c = cls ? ' status-' + cls : '';
  return `<div class="status-row"><span class="status-label">${label}</span><span class="status-value${c}">${value||'--'}</span></div>`;
}
function formatUptime(s) { if(!s)return'--'; const h=Math.floor(s/3600),m=Math.floor((s%3600)/60); return h>0?`${h}h ${m}m`:`${m}m`; }

// =====================================================================
// Hub Mode & Character
// =====================================================================
async function loadModes() {
  try {
    const [modesData, charsData] = await Promise.all([
      authFetch(API + '/modes').then(r => r.json()),
      authFetch(API + '/characters').then(r => r.json()),
    ]);

    const modeSel = document.getElementById('mode-select');
    modeSel.innerHTML = '<option value="">(no mode)</option>';
    for (const [key, cfg] of Object.entries(modesData.modes || {})) {
      const opt = document.createElement('option');
      opt.value = key;
      opt.textContent = key;
      if (key === modesData.current) opt.selected = true;
      modeSel.appendChild(opt);
    }

    const charSel = document.getElementById('character-select');
    charSel.innerHTML = '<option value="">(no character)</option>';
    for (const [key, cfg] of Object.entries(charsData.characters || {})) {
      const opt = document.createElement('option');
      opt.value = key;
      opt.textContent = cfg.name || key;
      if (key === charsData.current) opt.selected = true;
      charSel.appendChild(opt);
    }

    // Show current mode info
    const infoEl = document.getElementById('mode-info');
    if (modesData.current) {
      const m = modesData.modes[modesData.current] || {};
      infoEl.innerHTML = `Mode: <strong>${modesData.current}</strong> &middot; temp=${m.temperature || '?'} &middot; max_tokens=${m.max_tokens || '?'} &middot; model=${(m.prefer_model||['any'])[0]}`;
    } else {
      infoEl.textContent = 'No mode active — using default settings';
    }
    if (charsData.current) {
      infoEl.innerHTML += ` &middot; Character: <strong>${charsData.current}</strong>`;
    }
  } catch(e) {}
}

document.getElementById('mode-apply').addEventListener('click', async () => {
  const mode = document.getElementById('mode-select').value;
  const char = document.getElementById('character-select').value;
  try {
    await authFetch(API+'/modes/set', {method:'POST', headers:{'Content-Type':'application/json',...authHeaders()}, body:JSON.stringify({mode})});
    if (char !== undefined) {
      await authFetch(API+'/characters/set', {method:'POST', headers:{'Content-Type':'application/json',...authHeaders()}, body:JSON.stringify({character: char})});
    }
    showToast(mode ? `Mode: ${mode}` : 'Mode cleared');
    loadModes();
  } catch(e) { showToast('Failed to set mode', 'error'); }
});

async function loadMeshStatus() {
  const el = document.getElementById('mesh-info');
  try {
    const data = await authFetch(API + '/mesh/status').then(r => r.json());
    const workers = data.workers || [];
    if (workers.length === 0) {
      el.innerHTML = '<div class="empty-state">No worker nodes connected. Run <code>localforge-worker --hub ai-hub:8100</code> on a device to join the mesh.</div>';
      return;
    }
    el.innerHTML = workers.map(w => {
      const caps = w.capabilities || {};
      const capFlags = Object.keys(caps).filter(k => caps[k] === true);
      const healthCls = w.healthy ? 'ok' : 'error';
      const gpu = caps.gpu_name ? ` (${caps.gpu_name})` : '';
      return `<div class="mesh-node">
        ${statusRow('Node', `${w.hostname}:${w.port}${gpu}`, healthCls)}
        ${statusRow('Tier', w.tier)}
        ${statusRow('Tasks', `${w.active_tasks} active, ${(w.stats?.tasks_completed||0)} done`)}
        ${statusRow('Capabilities', capFlags.join(', ') || 'none')}
        ${statusRow('Uptime', formatUptime(w.uptime_s))}
        ${statusRow('Heartbeat', `${w.heartbeat_age_s}s ago`, w.heartbeat_age_s > 60 ? 'error' : '')}
      </div>`;
    }).join('<hr style="border-color:var(--border);margin:8px 0">');
  } catch(e) {
    el.textContent = 'Mesh status unavailable';
  }
}

// =====================================================================
// Model Swap
// =====================================================================
const modelSelect = document.getElementById('model-select');
async function loadModels() {
  try {
    const data = await authFetch(API + '/models').then(r => r.json());
    modelSelect.innerHTML = '<option value="">-- switch model --</option>';
    (data.models || []).forEach(m => {
      const name = typeof m === 'string' ? m : m.name || m;
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name.replace('.gguf','').substring(0,40);
      if (name === data.current) opt.selected = true;
      modelSelect.appendChild(opt);
    });
  } catch(e) { modelSelect.innerHTML = '<option>Error</option>'; }
}
function collectLoadParams() {
  const body = {};
  // Integer params
  const intFields = [
    ['ctrl-ctx-size', 'ctx_size'], ['ctrl-gpu-layers', 'gpu_layers'],
    ['ctrl-threads', 'threads'], ['ctrl-threads-batch', 'threads_batch'],
    ['ctrl-batch-size', 'batch_size'], ['ctrl-ubatch-size', 'ubatch_size'],
    ['ctrl-rope-freq-base', 'rope_freq_base'], ['ctrl-parallel', 'parallel'],
    ['ctrl-draft-max', 'draft_max'], ['ctrl-gpu-layers-draft', 'gpu_layers_draft'],
    ['ctrl-ctx-size-draft', 'ctx_size_draft'],
    ['ctrl-ngram-n', 'spec_ngram_size_n'], ['ctrl-ngram-m', 'spec_ngram_size_m'],
    ['ctrl-ngram-hits', 'spec_ngram_min_hits'],
  ];
  for (const [id, key] of intFields) {
    const el = document.getElementById(id);
    if (el && el.value !== '') body[key] = parseInt(el.value);
  }
  // Select/string params
  const selFields = [
    ['ctrl-cache-type', 'cache_type'], ['ctrl-spec-type', 'spec_type'],
  ];
  for (const [id, key] of selFields) {
    const el = document.getElementById(id);
    if (el && el.value) body[key] = el.value;
  }
  // Flash attention (boolean)
  const faEl = document.getElementById('ctrl-flash-attn');
  if (faEl && faEl.value) body.flash_attn = faEl.value === 'true';
  // Tensor split (comma-separated floats → string)
  const tsEl = document.getElementById('ctrl-tensor-split');
  if (tsEl && tsEl.value.trim()) body.tensor_split = tsEl.value.trim();
  // Draft model
  const dmEl = document.getElementById('ctrl-model-draft');
  if (dmEl && dmEl.value) body.model_draft = dmEl.value;
  return body;
}

// Pre-fill loading params from config.yaml when model is selected
modelSelect.addEventListener('change', async () => {
  const model = modelSelect.value;
  if (!model) return;

  // Fetch config overrides for this model and pre-fill fields
  try {
    const cfgData = await authFetch(API+'/models/config?model='+encodeURIComponent(model)).then(r=>r.json());
    const cfg = cfgData.config || {};
    if (cfg.ctx_size) document.getElementById('ctrl-ctx-size').value = cfg.ctx_size;
    if (cfg.gpu_layers != null) document.getElementById('ctrl-gpu-layers').value = cfg.gpu_layers;
    if (cfg.flash_attn != null) document.getElementById('ctrl-flash-attn').value = String(cfg.flash_attn);
    if (cfg.cache_type) document.getElementById('ctrl-cache-type').value = cfg.cache_type;
    if (cfg.parallel) document.getElementById('ctrl-parallel').value = cfg.parallel;
    if (cfgData.matched_pattern) {
      showToast(`Config loaded: ${cfgData.matched_pattern} (ctx=${cfg.ctx_size||'default'}, gpu=${cfg.gpu_layers||'all'})`);
    }
  } catch(e) { /* non-critical, continue with swap */ }

  if (!confirm(`Swap to ${model.replace('.gguf','')}?`)) { modelSelect.value=''; return; }
  const badge = document.getElementById('model-badge');
  badge.textContent = 'Loading...'; badge.style.background = 'var(--yellow)';
  try {
    const swapBody = {model_name: model, ...collectLoadParams()};
    const resp = await authFetch(API+'/swap', { method:'POST', headers:{'Content-Type':'application/json',...authHeaders()}, body:JSON.stringify(swapBody) });
    const data = await resp.json();
    if (data.error) { showToast('Swap error: '+data.error, 'error'); }
    else if (data.applied) { showToast(`Loaded: ctx=${data.applied.ctx_size}, gpu=${data.applied.gpu_layers}`); }
  } catch(e) { showToast('Swap error: '+e.message, 'error'); }
  badge.style.background = ''; loadStatus(); loadModels();
});

// =====================================================================
// Chat
// =====================================================================
const chatMessages = document.getElementById('chat-messages');
const chatPrompt = document.getElementById('chat-prompt');
const chatSend = document.getElementById('chat-send');
let chatHistory = []; // current conversation messages
let currentChatId = null;

chatSend.addEventListener('click', sendChat);
chatPrompt.addEventListener('keydown', e => { if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat();} });

// Image upload
let pendingImage = null;
const imageInput = document.getElementById('image-input');
const imagePreview = document.getElementById('image-preview-area');
const imageThumbnail = document.getElementById('image-thumbnail');
document.getElementById('image-clear').addEventListener('click', () => {
  pendingImage=null; imageInput.value=''; imagePreview.style.display='none';
});
imageInput.addEventListener('change', e => {
  const f=e.target.files[0]; if(!f)return;
  pendingImage=f; imageThumbnail.src=URL.createObjectURL(f); imagePreview.style.display='flex';
});

async function sendChat() {
  const prompt = chatPrompt.value.trim();
  if (!prompt && !pendingImage) return;
  addMessage(prompt || '[Image analysis]', 'user');
  chatHistory.push({role:'user', content:prompt||'[Image]', timestamp:Date.now()});
  chatPrompt.value = ''; chatSend.disabled = true;
  const msgEl = addMessage('', 'assistant');

  try {
    let resp;
    if (pendingImage) {
      const fd = new FormData();
      fd.append('image', pendingImage);
      fd.append('question', prompt || 'Describe this image in detail.');
      resp = await authFetch(API+'/upload-image', {method:'POST', body:fd});
      pendingImage=null; imageInput.value=''; imagePreview.style.display='none';
    } else {
      const sysPrompt = document.getElementById('sys-prompt')?.value?.trim() || '';
      resp = await authFetch(API+'/chat', {
        method:'POST',
        headers:{'Content-Type':'application/json',...authHeaders()},
        body:JSON.stringify({
          messages: chatHistory.filter(m=>m.role==='user'||m.role==='assistant').map(m=>({role:m.role,content:m.content})),
          system_prompt: sysPrompt,
        }),
      });
    }
    const fullText = await streamResponse(resp, msgEl);
    chatHistory.push({role:'assistant', content:fullText, timestamp:Date.now()});
    if (ttsEnabled && fullText) speak(fullText);
    document.getElementById('regen-area').style.display = 'block';
  } catch(e) { msgEl.textContent = 'Error: '+e.message; }
  chatSend.disabled = false;
}

async function streamResponse(resp, msgEl) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer='', fullText='', tokenCount=0;
  const startTime = performance.now();

  // Token speed display element
  let speedEl = msgEl.parentElement.querySelector('.token-speed');
  if (!speedEl) {
    speedEl = document.createElement('div');
    speedEl.className = 'token-speed';
    msgEl.insertAdjacentElement('afterend', speedEl);
  }

  while(true) {
    const{done,value}=await reader.read(); if(done)break;
    buffer+=decoder.decode(value,{stream:true});
    const lines=buffer.split('\n'); buffer=lines.pop();
    for(const line of lines) {
      if(line.startsWith('data: ')) {
        const data=line.slice(6); if(data==='[DONE]')continue;
        try{
          const c=JSON.parse(data);
          if(c.content){
            fullText+=c.content;
            tokenCount++;
            // Update display with markdown rendering
            msgEl.innerHTML = renderMarkdown(fullText);
            chatMessages.scrollTop=chatMessages.scrollHeight;
            // Update speed every 5 tokens
            if(tokenCount%5===0) {
              const elapsed=(performance.now()-startTime)/1000;
              const tps=elapsed>0?(tokenCount/elapsed).toFixed(1):'...';
              speedEl.textContent=`${tokenCount} tokens | ${tps} tok/s`;
            }
          }
          if(c.error) msgEl.innerHTML+=`<br><span style="color:var(--red)">[Error: ${escapeHtml(c.error)}]</span>`;
        }catch(e){}
      }
    }
  }
  // Final stats
  const elapsed=(performance.now()-startTime)/1000;
  const tps=elapsed>0?(tokenCount/elapsed).toFixed(1):'0';
  speedEl.textContent=`${tokenCount} tokens | ${tps} tok/s | ${elapsed.toFixed(1)}s`;
  // Final markdown render
  msgEl.innerHTML = renderMarkdown(fullText);
  return fullText;
}

// Lightweight markdown renderer (no external deps)
function renderMarkdown(text) {
  if (!text) return '';
  let html = escapeHtml(text);
  // Code blocks (```lang\n...\n```)
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
  // Inline code
  html = html.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  // Bold
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  // Italic
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
  // Headers
  html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
  // Blockquotes
  html = html.replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');
  // Unordered lists
  html = html.replace(/^- (.+)$/gm, '<li>$1</li>');
  html = html.replace(/(<li>.*<\/li>\n?)+/g, '<ul>$&</ul>');
  // Links
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
  // Line breaks (but not inside pre blocks)
  html = html.replace(/(?<!\n)\n(?!\n)/g, '<br>');
  html = html.replace(/\n\n+/g, '<br><br>');
  return html;
}

function addMessage(text, role) {
  const div = document.createElement('div');
  div.className = 'msg msg-' + role;
  if (role === 'assistant') {
    div.innerHTML = renderMarkdown(text);
  } else {
    div.textContent = text;
  }
  chatMessages.appendChild(div);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  return div;
}

// Chat history
document.getElementById('chat-new-btn').addEventListener('click', () => {
  chatHistory=[]; currentChatId=null; chatMessages.innerHTML='';
  document.getElementById('regen-area').style.display='none';
});

document.getElementById('chat-save-btn').addEventListener('click', async () => {
  if(!chatHistory.length) return;
  try {
    const resp = await authFetch(API+'/chats/save', {method:'POST', headers:{'Content-Type':'application/json',...authHeaders()},
      body:JSON.stringify({messages:chatHistory,id:currentChatId})});
    const data = await resp.json();
    currentChatId = data.id;
    showToast('Chat saved: '+data.title);
  } catch(e) { showToast('Save failed','error'); }
});

const historyPanel = document.getElementById('chat-history-panel');
const historyList = document.getElementById('chat-history-list');
document.getElementById('chat-history-btn').addEventListener('click', async () => {
  if(historyPanel.style.display!=='none'){historyPanel.style.display='none';return;}
  historyPanel.style.display='block';
  try {
    let chatPage = 1;
    const renderChats = async (page) => {
      const data = await authFetch(API+`/chats?page=${page}&limit=30`).then(r=>r.json());
      if(page===1 && !data.chats?.length){historyList.innerHTML='<div class="empty-state">No saved chats</div>';return;}
      if(page===1) historyList.innerHTML='';
      historyList.insertAdjacentHTML('beforeend', data.chats.map(c=>`
        <div class="history-item" data-id="${c.id}">
          <div class="history-title">${escapeHtml(c.title)}</div>
          <div class="history-meta">${c.message_count} msgs | ${new Date(c.updated*1000).toLocaleDateString()}</div>
        </div>
      `).join(''));
      // Remove old load-more button if exists
      historyList.querySelector('.load-more-btn')?.remove();
      if(data.has_more){
        historyList.insertAdjacentHTML('beforeend',
          `<button class="load-more-btn btn btn-sm" style="width:100%;margin-top:8px">Load more</button>`);
        historyList.querySelector('.load-more-btn').addEventListener('click', ()=>renderChats(++chatPage));
      }
      historyList.querySelectorAll('.history-item:not([data-bound])').forEach(el=>{
        el.dataset.bound='1';
        el.addEventListener('click', async()=>{
          const data = await authFetch(API+'/chats/'+el.dataset.id).then(r=>r.json());
          chatHistory=data.messages||[]; currentChatId=data.id; chatMessages.innerHTML='';
          chatHistory.forEach(m=>addMessage(m.content,m.role));
          historyPanel.style.display='none';
        });
      });
    };
    await renderChats(chatPage);
  } catch(e) { historyList.innerHTML='Error loading history'; }
});

// =====================================================================
// Voice: STT (microphone)
// =====================================================================
const micBtn = document.getElementById('mic-btn');
const recIndicator = document.getElementById('recording-indicator');
let mediaRecorder = null, audioChunks = [];

micBtn.addEventListener('click', async () => {
  if (mediaRecorder?.state === 'recording') { mediaRecorder.stop(); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio:true});
    mediaRecorder = new MediaRecorder(stream, {mimeType:'audio/webm'});
    audioChunks = [];
    mediaRecorder.ondataavailable = e => audioChunks.push(e.data);
    mediaRecorder.onstop = async () => {
      recIndicator.style.display='none'; micBtn.textContent='\u{1F3A4}';
      stream.getTracks().forEach(t=>t.stop());
      const blob = new Blob(audioChunks, {type:'audio/webm'});
      const fd = new FormData(); fd.append('file', blob, 'recording.webm');
      try {
        const data = await authFetch(API+'/transcribe',{method:'POST',body:fd}).then(r=>r.json());
        if(data.text){chatPrompt.value+=data.text;chatPrompt.focus();}
        else if(data.error) showToast('Transcription: '+data.error,'error');
      } catch(e) { showToast('Transcription failed','error'); }
    };
    mediaRecorder.start();
    recIndicator.style.display='flex'; micBtn.textContent='\u23F9';
  } catch(e) { showToast('Microphone unavailable','error'); }
});

// =====================================================================
// Voice: TTS
// =====================================================================
let ttsEnabled = localStorage.getItem('tts') === 'true';
const ttsToggle = document.getElementById('tts-toggle');
const voiceSelect = document.getElementById('voice-select');

function updateTTSButton() { ttsToggle.textContent=ttsEnabled?'\u{1F50A}':'\u{1F508}'; ttsToggle.classList.toggle('active',ttsEnabled); }
updateTTSButton();
ttsToggle.addEventListener('click', () => { ttsEnabled=!ttsEnabled; localStorage.setItem('tts',ttsEnabled); updateTTSButton(); });

function loadVoices() {
  const voices=speechSynthesis.getVoices(); voiceSelect.innerHTML='';
  const saved=localStorage.getItem('tts-voice');
  voices.forEach(v=>{const o=document.createElement('option');o.value=v.name;o.textContent=`${v.name} (${v.lang})`;if(v.name===saved)o.selected=true;voiceSelect.appendChild(o);});
}
speechSynthesis.onvoiceschanged=loadVoices; loadVoices();
voiceSelect.addEventListener('change',()=>localStorage.setItem('tts-voice',voiceSelect.value));

function speak(text) {
  if(!text)return; speechSynthesis.cancel();
  const u=new SpeechSynthesisUtterance(text);
  const saved=localStorage.getItem('tts-voice');
  if(saved){const v=speechSynthesis.getVoices().find(v=>v.name===saved);if(v)u.voice=v;}
  speechSynthesis.speak(u);
}

// =====================================================================
// Search / RAG
// =====================================================================
const searchIndex=document.getElementById('search-index'), searchQuery=document.getElementById('search-query');
const searchBtn=document.getElementById('search-btn'), searchResults=document.getElementById('search-results');

async function loadIndexes() {
  try {
    const data=await authFetch(API+'/indexes').then(r=>r.json());
    searchIndex.innerHTML='';
    (data.indexes||[]).forEach(n=>{const o=document.createElement('option');o.value=n;o.textContent=n;searchIndex.appendChild(o);});
    if(!data.indexes?.length) searchIndex.innerHTML='<option>No indexes</option>';
  } catch(e) { searchIndex.innerHTML='<option>Error</option>'; }
}
searchBtn.addEventListener('click', doSearch);
searchQuery.addEventListener('keydown', e=>{if(e.key==='Enter')doSearch();});

async function doSearch() {
  const query=searchQuery.value.trim(), index=searchIndex.value;
  if(!query||!index)return;
  const mode=document.querySelector('input[name="search-mode"]:checked').value;
  searchResults.innerHTML='<div class="loading">Searching...</div>';
  try {
    const data=await authFetch(API+'/search',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({query,index_name:index,mode})}).then(r=>r.json());
    if(data.error){searchResults.innerHTML=`<div class="error-msg">${data.error}</div>`;return;}
    const r=data.result||'';
    searchResults.innerHTML=`<pre class="search-result-text">${escapeHtml(typeof r==='string'?r:JSON.stringify(r,null,2))}</pre>`;
  } catch(e) { searchResults.innerHTML=`<div class="error-msg">${e.message}</div>`; }
}

// =====================================================================
// Media Gallery (Photos + Videos)
// =====================================================================
const photoGallery=document.getElementById('photo-gallery');
const videoGallery=document.getElementById('video-gallery');
const mediaUpload=document.getElementById('media-upload');
const photoSearchInput=document.getElementById('photo-search-input');
const photoSearchBtn=document.getElementById('photo-search-btn');

// Blob URL cache for authed media (prevents auth issues with <img>/<video> src)
const _blobCache = new Map();
const _BLOB_CACHE_MAX = 50;
async function fetchAuthedBlob(url) {
  if (_blobCache.has(url)) return _blobCache.get(url);
  // Evict oldest entries when cache exceeds threshold
  if (_blobCache.size >= _BLOB_CACHE_MAX) {
    const oldest = _blobCache.keys().next().value;
    URL.revokeObjectURL(_blobCache.get(oldest));
    _blobCache.delete(oldest);
  }
  try {
    const resp = await authFetch(url);
    if (!resp.ok) return '';
    const blob = await resp.blob();
    const blobUrl = URL.createObjectURL(blob);
    _blobCache.set(url, blobUrl);
    return blobUrl;
  } catch { return ''; }
}

// Authed URL for <video> and window.open (uses ?token= query param)
function authedUrl(url) {
  const sep = url.includes('?') ? '&' : '?';
  return url + sep + 'token=' + encodeURIComponent(apiKey);
}

// Media sub-tabs
document.querySelectorAll('.media-tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.media-tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const t = btn.dataset.media;
    photoGallery.style.display = t === 'photos' ? '' : 'none';
    videoGallery.style.display = t === 'videos' ? '' : 'none';
    if (t === 'videos') loadVideos();
  });
});

async function loadPhotos() {
  try {
    const data=await authFetch(API+'/photos').then(r=>r.json());
    renderPhotos(data.photos||[]);
  } catch(e) { photoGallery.innerHTML='<div class="error-msg">'+e.message+'</div>'; }
}

async function renderPhotos(photos) {
  if(!photos.length){photoGallery.innerHTML='<div class="empty-state">No photos yet. Upload some!</div>';return;}
  photoGallery.innerHTML = photos.map(p=>`
    <div class="photo-card" data-url="${escapeAttr(p.url)}" data-thumb="${escapeAttr(p.thumbnail)}">
      <div class="photo-loading">Loading...</div>
      <div class="photo-info">
        <div class="photo-desc">${escapeHtml((p.description||p.filename).substring(0,80))}</div>
        ${p.tags?.length?'<div class="photo-tags">'+p.tags.map(t=>'<span class="tag">'+escapeHtml(t)+'</span>').join('')+'</div>':''}
      </div>
    </div>
  `).join('');

  // Load thumbnails via authed fetch (fixes the auth bug)
  const cards = photoGallery.querySelectorAll('.photo-card');
  await Promise.all(Array.from(cards).map(async card => {
    const thumbUrl = card.dataset.thumb;
    const fullUrl = card.dataset.url;
    const blobUrl = await fetchAuthedBlob(thumbUrl);
    if (blobUrl) {
      const img = document.createElement('img');
      img.src = blobUrl;
      img.alt = card.querySelector('.photo-desc')?.textContent || '';
      img.loading = 'lazy';
      img.addEventListener('click', () => window.open(authedUrl(fullUrl), '_blank'));
      const loader = card.querySelector('.photo-loading');
      if (loader) loader.replaceWith(img);
    } else {
      const loader = card.querySelector('.photo-loading');
      if (loader) loader.textContent = 'Failed to load';
    }
  }));
}

async function loadVideos() {
  try {
    const data = await authFetch(API+'/videos').then(r=>r.json());
    renderVideos(data.videos||[]);
  } catch(e) { videoGallery.innerHTML='<div class="error-msg">'+e.message+'</div>'; }
}

async function renderVideos(videos) {
  if(!videos.length){videoGallery.innerHTML='<div class="empty-state">No videos yet. Upload some!</div>';return;}
  videoGallery.innerHTML = videos.map(v=>`
    <div class="video-card">
      <video src="${authedUrl(v.url)}" poster="${authedUrl(v.thumbnail)}" controls preload="metadata" class="video-player"></video>
      <div class="video-info">
        <div class="video-desc">${escapeHtml((v.description||v.filename).substring(0,80))}</div>
        <div class="video-meta">${v.duration||''} ${v.resolution||''}</div>
      </div>
    </div>
  `).join('');
}

mediaUpload.addEventListener('change', async e => {
  const files = Array.from(e.target.files);
  if(!files.length)return;
  const imageFiles = files.filter(f=>f.type.startsWith('image/'));
  const videoFiles = files.filter(f=>f.type.startsWith('video/'));
  showToast(`Uploading ${files.length} file(s)...`);
  for(const file of imageFiles) {
    const fd=new FormData(); fd.append('image',file); fd.append('auto_tag','true');
    try { await authFetch(API+'/photos/upload',{method:'POST',body:fd}); }
    catch(e) { showToast('Upload failed: '+e.message,'error'); }
  }
  for(const file of videoFiles) {
    const fd=new FormData(); fd.append('video',file); fd.append('auto_tag','true');
    try { await authFetch(API+'/videos/upload',{method:'POST',body:fd}); }
    catch(e) { showToast('Upload failed: '+e.message,'error'); }
  }
  mediaUpload.value='';
  if(imageFiles.length) loadPhotos();
  if(videoFiles.length) loadVideos();
  showToast('Upload complete!');
});

photoSearchBtn.addEventListener('click', async()=>{
  const q=photoSearchInput.value.trim(); if(!q){loadPhotos();return;}
  try {
    const data=await authFetch(API+'/photos/search',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({query:q})}).then(r=>r.json());
    renderPhotos(data.results||[]);
  } catch(e) { photoGallery.innerHTML='<div class="error-msg">'+e.message+'</div>'; }
});
photoSearchInput.addEventListener('keydown',e=>{if(e.key==='Enter')photoSearchBtn.click();});

// =====================================================================
// Agents
// =====================================================================
function timeAgo(ts) {
  if (!ts) return 'never';
  const diff = Math.floor(Date.now() / 1000 - ts);
  if (diff < 60) return diff + 's ago';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  return Math.floor(diff / 86400) + 'd ago';
}

// Agent toolbar handlers
document.getElementById('agent-metrics-btn')?.addEventListener('click', async()=>{
  const panel=document.getElementById('agent-panel');
  panel.style.display=panel.style.display==='none'?'block':'none';
  if(panel.style.display==='none')return;
  panel.innerHTML='<div class="loading">Loading metrics...</div>';
  try{
    const data=await authFetch(API+'/agents/metrics').then(r=>r.json());
    panel.innerHTML=`<div class="metrics-grid">
      ${statusRow('Total Agents',data.total_agents)}
      ${statusRow('Running',data.running,'ok')}
      ${statusRow('Paused',data.paused)}
      ${statusRow('Task Queue Depth',data.task_queue_depth)}
      ${statusRow('Workers',data.workers)}
      ${statusRow('Bus Subscribers',data.bus_subscribers)}
    </div>`;
  }catch(e){panel.innerHTML='<div class="error-msg">'+e.message+'</div>';}
});

document.getElementById('agent-tasks-btn')?.addEventListener('click', async()=>{
  const panel=document.getElementById('agent-panel');
  panel.style.display=panel.style.display==='none'?'block':'none';
  if(panel.style.display==='none')return;
  panel.innerHTML='<div class="loading">Loading tasks...</div>';
  try{
    const data=await authFetch(API+'/agents/tasks').then(r=>r.json());
    const tasks=data.tasks||[];
    if(!tasks.length){panel.innerHTML='<div class="empty-state">No tasks in queue</div>';return;}
    panel.innerHTML='<div class="task-list">'+tasks.map(t=>`
      <div class="task-item task-${t.status}">
        <span class="task-id">${t.id.substring(0,8)}</span>
        <span class="badge badge-${t.status}">${t.status}</span>
        <span class="task-queue">${t.queue}</span>
        <span class="task-priority">P${t.priority}</span>
        ${t.error?`<span class="task-error">${escapeHtml(t.error).substring(0,60)}</span>`:''}
      </div>
    `).join('')+'</div>';
  }catch(e){panel.innerHTML='<div class="error-msg">'+e.message+'</div>';}
});

document.getElementById('agent-bus-btn')?.addEventListener('click', async()=>{
  const panel=document.getElementById('agent-panel');
  panel.style.display=panel.style.display==='none'?'block':'none';
  if(panel.style.display==='none')return;
  panel.innerHTML='<div class="loading">Loading messages...</div>';
  try{
    const data=await authFetch(API+'/agents/bus').then(r=>r.json());
    const msgs=data.messages||[];
    if(!msgs.length){panel.innerHTML='<div class="empty-state">No recent messages</div>';return;}
    panel.innerHTML='<div class="bus-messages">'+msgs.map(m=>`
      <div class="bus-msg">
        <span class="bus-topic">${escapeHtml(m.topic)}</span>
        <span class="bus-sender">${escapeHtml(m.sender)}</span>
        <span class="bus-time">${timeAgo(m.timestamp)}</span>
      </div>
    `).join('')+'</div>';
  }catch(e){panel.innerHTML='<div class="error-msg">'+e.message+'</div>';}
});

// =====================================================================
// Approval Queue
// =====================================================================
async function loadApprovals() {
  try {
    const data = await authFetch(API + '/approvals').then(r => r.json());
    const el = document.getElementById('approval-list');
    const pending = data.pending || [];
    if (pending.length === 0) {
      el.innerHTML = '<div class="empty-state">No pending approvals</div>';
      // Hide card if no pending and no recent
      const card = document.getElementById('approval-card');
      if (!(data.recent || []).length) card.style.display = 'none';
      else {
        card.style.display = '';
        el.innerHTML = (data.recent || []).map(r =>
          `<div class="approval-item approval-${r.status}">
            <span class="approval-tool">${escapeHtml(r.tool_name)}</span>
            <span class="approval-agent">${escapeHtml(r.agent_id)}</span>
            <span class="badge" style="background:${r.status==='approved'?'var(--green)':'var(--red)'}">${r.status}</span>
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
          <div class="approval-args"><code>${escapeHtml(JSON.stringify(r.arguments).substring(0,120))}</code></div>
        </div>
        <div class="approval-actions">
          <button class="btn-primary btn-small" onclick="decideApproval('${r.id}','approve')">Approve</button>
          <button class="btn-danger btn-small" onclick="decideApproval('${r.id}','deny')">Deny</button>
        </div>
      </div>`
    ).join('');
  } catch(e) {}
}

async function decideApproval(id, action) {
  try {
    await authFetch(API+'/approvals/decide', {
      method:'POST', headers:{'Content-Type':'application/json',...authHeaders()},
      body:JSON.stringify({id, action}),
    });
    showToast(`Approval ${action}d`);
    loadApprovals();
  } catch(e) { showToast('Failed','error'); }
}

async function loadAgents() {
  try {
    const data=await authFetch(API+'/agents').then(r=>r.json());
    const el=document.getElementById('agents-list');
    if(!data.agents?.length){el.textContent='No agents configured';return;}
    el.innerHTML = data.agents.map(a=>{
      const statusCls=a.status==='running'?'status-ok':a.status==='error'?'status-error':a.status==='paused'?'status-warn':a.status==='disabled'?'':'status-warn';
      const label=a.enabled===false?'disabled':(a.status||'unknown');
      const triggers=(a.triggers||[]).join(', ');
      const isEnabled = a.enabled !== false;
      const isPaused = a.paused === true;
      const avgDur = a.avg_duration ? (a.avg_duration).toFixed(1)+'s' : '';
      return `<div class="agent-card-wrap" data-agent="${a.id}"><div class="agent-card"><div class="agent-info">
        <div class="agent-name">${a.id}${a.children?.length?' <span class="agent-children">'+a.children.length+' children</span>':''}</div>
        <div class="agent-meta"><span class="trust-badge trust-${a.trust}">${a.trust}</span> ${a.schedule||'manual'}${triggers?' | triggers: '+triggers:''}${avgDur?' | avg: '+avgDur:''}</div>
        <div class="agent-run-info"><span class="agent-last-run" data-agent="${a.id}">...</span></div>
      </div><div class="agent-actions">
        ${isEnabled?`<button class="trigger-btn" data-agent="${a.id}" title="Run now">&#x25B6;</button>`:''}
        ${isEnabled&&!isPaused?`<button class="pause-btn" data-agent="${a.id}" title="Pause">&#x23F8;</button>`:''}
        ${isPaused?`<button class="resume-btn" data-agent="${a.id}" title="Resume">&#x23EF;</button>`:''}
        <button class="config-btn" data-agent="${a.id}" title="Configure">&#x2699;</button>
        <button class="logs-btn" data-agent="${a.id}" title="Show logs">Logs</button>
        <span class="badge ${statusCls}">${label}</span>
      </div></div>
      <div class="agent-config-panel" id="config-${a.id}" style="display:none"></div>
      <div class="agent-logs-panel" id="logs-${a.id}" style="display:none"></div>
      </div>`;
    }).join('');

    // Fetch last_run / run_count for each agent asynchronously
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

    // Trigger buttons
    el.querySelectorAll('.trigger-btn').forEach(btn=>{
      btn.addEventListener('click',async(e)=>{
        e.stopPropagation();
        btn.disabled=true;btn.textContent='...';
        try{await authFetch(API+`/agents/${btn.dataset.agent}/trigger`,{method:'POST'});btn.textContent='\u2713';setTimeout(()=>{btn.textContent='\u25B6';btn.disabled=false;loadAgents();},2000);}
        catch(e){btn.textContent='\u2717';setTimeout(()=>{btn.textContent='\u25B6';btn.disabled=false;},2000);}
      });
    });

    // Pause buttons
    el.querySelectorAll('.pause-btn').forEach(btn=>{
      btn.addEventListener('click',async(e)=>{
        e.stopPropagation();
        try{await authFetch(API+`/agents/${btn.dataset.agent}/pause`,{method:'POST'});setTimeout(loadAgents,500);}
        catch(e){showToast('Pause failed','error');}
      });
    });

    // Resume buttons
    el.querySelectorAll('.resume-btn').forEach(btn=>{
      btn.addEventListener('click',async(e)=>{
        e.stopPropagation();
        try{await authFetch(API+`/agents/${btn.dataset.agent}/resume`,{method:'POST'});setTimeout(loadAgents,500);}
        catch(e){showToast('Resume failed','error');}
      });
    });

    // Config buttons
    el.querySelectorAll('.config-btn').forEach(btn=>{
      btn.addEventListener('click',async(e)=>{
        e.stopPropagation();
        const agentId = btn.dataset.agent;
        const panel = document.getElementById('config-'+agentId);
        // Close logs if open
        const logsPanel = document.getElementById('logs-'+agentId);
        if (logsPanel.style.display !== 'none') { logsPanel.style.display='none'; el.querySelector(`.logs-btn[data-agent="${agentId}"]`)?.classList.remove('active'); }
        if (panel.style.display !== 'none') { panel.style.display='none'; btn.classList.remove('active'); return; }
        panel.innerHTML='<div class="loading">Loading config...</div>';
        panel.style.display='block';
        btn.classList.add('active');
        try {
          const d = await authFetch(API+`/agents/${agentId}/config`).then(r=>r.json());
          if (d.error) { panel.innerHTML=`<div class="error-msg">${escapeHtml(d.error)}</div>`; return; }
          renderAgentConfig(panel, agentId, d.config);
        } catch(err) { panel.innerHTML=`<div class="error-msg">${err.message}</div>`; }
      });
    });

    // Logs buttons
    el.querySelectorAll('.logs-btn').forEach(btn=>{
      btn.addEventListener('click',async(e)=>{
        e.stopPropagation();
        const agentId = btn.dataset.agent;
        const panel = document.getElementById('logs-'+agentId);
        // Close config if open
        const cfgPanel = document.getElementById('config-'+agentId);
        if (cfgPanel.style.display !== 'none') { cfgPanel.style.display='none'; el.querySelector(`.config-btn[data-agent="${agentId}"]`)?.classList.remove('active'); }
        if (panel.style.display !== 'none') { panel.style.display='none'; btn.classList.remove('active'); return; }
        panel.innerHTML='<div class="loading">Loading logs...</div>';
        panel.style.display='block';
        btn.classList.add('active');
        try {
          const d = await authFetch(API+`/agents/${agentId}/logs`).then(r=>r.json());
          if (d.error) { panel.innerHTML=`<div class="error-msg">${escapeHtml(d.error)}</div>`; return; }
          if (!d.logs?.length) { panel.innerHTML='<div class="empty-state">No log entries</div>'; return; }
          panel.innerHTML = d.logs.map(l => `<div class="agent-log-line">${escapeHtml(l)}</div>`).join('');
          panel.scrollTop = panel.scrollHeight;
        } catch(err) { panel.innerHTML=`<div class="error-msg">${err.message}</div>`; }
      });
    });
  } catch(e) { document.getElementById('agents-list').textContent='Failed to load agents'; }
}

function renderAgentConfig(panel, agentId, cfg) {
  const isEnabled = cfg.enabled !== false;
  const schedule = cfg.schedule || '';
  const trust = cfg.trust || 'monitor';
  const agentConfig = cfg.config || {};
  const triggers = cfg.triggers || [];

  // Parse schedule to human-readable
  let scheduleHint = '';
  if (schedule.startsWith('*/')) {
    const mins = parseInt(schedule.substring(2));
    if (mins) scheduleHint = mins >= 60 ? `every ${mins/60}h` : `every ${mins}m`;
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
          <option value="monitor" ${trust==='monitor'?'selected':''}>monitor (read-only)</option>
          <option value="safe" ${trust==='safe'?'selected':''}>safe (+ indexing, notes, review)</option>
          <option value="full" ${trust==='full'?'selected':''}>full (all tools)</option>
        </select>
      </div>
      <div class="config-row">
        <label>Schedule <span class="config-hint">${scheduleHint}</span></label>
        <input type="text" id="cfg-schedule-${agentId}" class="config-input" value="${escapeAttr(schedule)}" placeholder="*/5 * * * *">
      </div>
      ${agentConfig.topics ? `
      <div class="config-row config-row-col">
        <label>Topics</label>
        <textarea id="cfg-topics-${agentId}" class="config-textarea" rows="3">${(agentConfig.topics||[]).join('\n')}</textarea>
      </div>` : ''}
      ${agentConfig.focus ? `
      <div class="config-row">
        <label>Focus</label>
        <input type="text" id="cfg-focus-${agentId}" class="config-input" value="${escapeAttr(agentConfig.focus)}">
      </div>` : ''}
      ${agentConfig.directories ? `
      <div class="config-row config-row-col">
        <label>Directories</label>
        <textarea id="cfg-dirs-${agentId}" class="config-textarea" rows="2">${(agentConfig.directories||[]).map(d=>typeof d==='string'?d:d.directory||d.name||JSON.stringify(d)).join('\n')}</textarea>
      </div>` : ''}
      ${triggers.length ? `
      <div class="config-row config-row-col">
        <label>Triggers</label>
        <div class="trigger-list">${triggers.map(t=>`<span class="trigger-tag">${t.type||'unknown'}${t.paths?' ('+t.patterns?.join(',')+')':''}</span>`).join('')}</div>
      </div>` : ''}
      <div class="config-actions">
        <button class="config-save-btn" data-agent="${agentId}">Save Changes</button>
        <span class="config-status" id="cfg-status-${agentId}"></span>
      </div>
    </div>
  `;

  // Save handler
  panel.querySelector('.config-save-btn').addEventListener('click', async () => {
    const statusEl = document.getElementById(`cfg-status-${agentId}`);
    statusEl.textContent = 'Saving...';
    statusEl.className = 'config-status';

    const patch = {
      enabled: document.getElementById(`cfg-enabled-${agentId}`).checked,
      trust: document.getElementById(`cfg-trust-${agentId}`).value,
      schedule: document.getElementById(`cfg-schedule-${agentId}`).value.trim(),
    };

    // Collect config sub-fields
    const configPatch = {};
    const topicsEl = document.getElementById(`cfg-topics-${agentId}`);
    if (topicsEl) {
      configPatch.topics = topicsEl.value.split('\n').map(s=>s.trim()).filter(Boolean);
    }
    const focusEl = document.getElementById(`cfg-focus-${agentId}`);
    if (focusEl) {
      configPatch.focus = focusEl.value.trim();
    }
    if (Object.keys(configPatch).length) {
      patch.config = configPatch;
    }

    try {
      const resp = await authFetch(API+`/agents/${agentId}/config`, {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json', ...authHeaders()},
        body: JSON.stringify(patch),
      });
      const d = await resp.json();
      if (d.error) {
        statusEl.textContent = d.error;
        statusEl.className = 'config-status config-status-error';
      } else {
        statusEl.textContent = 'Saved: ' + (d.changed||[]).join(', ');
        statusEl.className = 'config-status config-status-ok';
        setTimeout(() => loadAgents(), 1500);
      }
    } catch(err) {
      statusEl.textContent = 'Error: ' + err.message;
      statusEl.className = 'config-status config-status-error';
    }
  });
}

// =====================================================================
// Notes
// =====================================================================
async function loadNotes() {
  try {
    const data=await authFetch(API+'/notes').then(r=>r.json());
    const el=document.getElementById('notes-list');
    if(!data.notes?.length){el.textContent='No notes saved';return;}
    el.innerHTML=data.notes.map(n=>`<div class="note-item-wrap" data-topic="${escapeAttr(n.topic)}">
      <div class="note-item"><span class="note-topic">${escapeHtml(n.topic)}</span><span class="note-meta">${formatBytes(n.size)} | ${new Date(n.modified*1000).toLocaleDateString()}</span></div>
      <div class="note-content-panel" style="display:none"></div>
    </div>`).join('');
    el.querySelectorAll('.note-item-wrap').forEach(wrap=>{
      const header = wrap.querySelector('.note-item');
      const panel = wrap.querySelector('.note-content-panel');
      header.addEventListener('click', async()=>{
        if (panel.style.display !== 'none') { panel.style.display='none'; wrap.classList.remove('expanded'); return; }
        panel.innerHTML='<div class="loading">Loading...</div>';
        panel.style.display='block';
        wrap.classList.add('expanded');
        try {
          const d = await authFetch(API+'/notes/'+encodeURIComponent(wrap.dataset.topic)).then(r=>r.json());
          if (d.error) { panel.innerHTML=`<div class="error-msg">${escapeHtml(d.error)}</div>`; return; }
          panel.innerHTML=`<pre class="note-content-text">${escapeHtml(d.content)}</pre>`;
        } catch(err) { panel.innerHTML=`<div class="error-msg">${err.message}</div>`; }
      });
    });
  } catch(e) { document.getElementById('notes-list').textContent='Failed to load notes'; }
}
function formatBytes(b){return b<1024?b+'B':(b/1024).toFixed(1)+'KB';}

// =====================================================================
// Knowledge Graph
// =====================================================================
async function loadKGStats() {
  try {
    const data=await authFetch(API+'/kg/stats').then(r=>r.json());
    const el=document.getElementById('kg-stats');
    if(data.error){el.textContent=data.error;return;}
    let html=statusRow('Entities',data.total_entities||0,'ok')+statusRow('Relations',data.total_relations||0,'ok');
    if(data.entities_by_type){html+='<div style="margin-top:8px">';for(const[t,c]of Object.entries(data.entities_by_type))html+=`<span class="type-badge type-${t}">${t}:${c}</span> `;html+='</div>';}
    el.innerHTML=html;
  } catch(e) { document.getElementById('kg-stats').textContent='Failed'; }
}

document.getElementById('kg-search-btn').addEventListener('click', doKGSearch);
document.getElementById('kg-search-input').addEventListener('keydown',e=>{if(e.key==='Enter')doKGSearch();});

async function doKGSearch() {
  const query=document.getElementById('kg-search-input').value.trim(); if(!query)return;
  const type=document.getElementById('kg-type-filter').value||undefined;
  const el=document.getElementById('kg-results');
  el.innerHTML='<div class="loading">Searching...</div>';
  try {
    const data=await authFetch(API+'/kg/search',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({query,entity_type:type})}).then(r=>r.json());
    if(data.error){el.innerHTML=`<div class="error-msg">${data.error}</div>`;return;}
    if(!data.results?.length){el.innerHTML='<div class="empty-state">No entities found</div>';return;}
    el.innerHTML=data.results.map(e=>`
      <div class="entity-card" data-name="${escapeAttr(e.name)}">
        <div class="entity-header"><span class="type-badge type-${e.type||'concept'}">${e.type||'concept'}</span><span class="entity-name">${escapeHtml(e.name)}</span></div>
        <div class="entity-content">${escapeHtml((e.content||'').substring(0,200))}</div>
        <div class="entity-relations" style="display:none"></div>
      </div>
    `).join('');
    el.querySelectorAll('.entity-card').forEach(card=>{
      card.addEventListener('click',async()=>{
        const rel=card.querySelector('.entity-relations');
        if(rel.style.display!=='none'){rel.style.display='none';return;}
        rel.innerHTML='<div class="loading">Loading...</div>';rel.style.display='block';
        try{
          const ctx=await authFetch(API+'/kg/context',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({name:card.dataset.name})}).then(r=>r.json());
          if(ctx.error||!ctx.entity){rel.innerHTML=ctx.error||'Not found';return;}
          const rels=ctx.relations||[];
          rel.innerHTML=rels.length?'<div class="relation-tree">'+rels.map(r=>`<div class="relation-item"><span class="relation-type">${r.relation_type||r.relation}</span><span class="relation-arrow">&rarr;</span><span class="relation-target">${escapeHtml(r.to_name||r.name||'')}</span></div>`).join('')+'</div>':'<div class="empty-state">No relations</div>';
        }catch(e){rel.innerHTML='Error: '+e.message;}
      });
    });
  } catch(e) { el.innerHTML=`<div class="error-msg">${e.message}</div>`; }
}

document.getElementById('kg-add-btn').addEventListener('click',async()=>{
  const name=document.getElementById('kg-add-name').value.trim();
  if(!name){showToast('Name required','error');return;}
  try{
    const data=await authFetch(API+'/kg/add',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({name,entity_type:document.getElementById('kg-add-type').value,content:document.getElementById('kg-add-content').value.trim()})}).then(r=>r.json());
    if(data.error)showToast('Error: '+data.error,'error');
    else{document.getElementById('kg-add-name').value='';document.getElementById('kg-add-content').value='';loadKGStats();showToast('Entity added');}
  }catch(e){showToast('Error: '+e.message,'error');}
});

// =====================================================================
// Chat: Regenerate, Export, System Prompt
// =====================================================================

document.getElementById('sys-prompt-toggle').addEventListener('click', () => {
  const panel = document.getElementById('sys-prompt-panel');
  panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
});

document.getElementById('regen-btn').addEventListener('click', async () => {
  if (chatHistory.length < 2 || chatHistory[chatHistory.length-1].role !== 'assistant') return;
  chatHistory.pop();
  const msgs = chatMessages.querySelectorAll('.msg');
  if (msgs.length) msgs[msgs.length-1].remove();
  const msgEl = addMessage('', 'assistant');
  chatSend.disabled = true;
  try {
    const sysPrompt = document.getElementById('sys-prompt')?.value?.trim() || '';
    const resp = await authFetch(API+'/chat', {
      method:'POST',
      headers:{'Content-Type':'application/json',...authHeaders()},
      body:JSON.stringify({
        messages: chatHistory.filter(m=>m.role==='user'||m.role==='assistant').map(m=>({role:m.role,content:m.content})),
        system_prompt: sysPrompt,
      }),
    });
    const fullText = await streamResponse(resp, msgEl);
    chatHistory.push({role:'assistant', content:fullText, timestamp:Date.now()});
    if (ttsEnabled && fullText) speak(fullText);
  } catch(e) { msgEl.textContent = 'Error: '+e.message; }
  chatSend.disabled = false;
});

document.getElementById('chat-export-btn').addEventListener('click', () => {
  if (!chatHistory.length) { showToast('No chat to export','error'); return; }
  const content = chatHistory.map(m => `**${m.role}**: ${m.content}`).join('\n\n---\n\n');
  const blob = new Blob([content], {type:'text/markdown'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `chat-${new Date().toISOString().slice(0,10)}.md`;
  a.click();
  URL.revokeObjectURL(a.href);
  showToast('Chat exported');
});

// =====================================================================
// Config: Generation Parameters
// =====================================================================

document.querySelectorAll('.param-row input[type="range"]').forEach(slider => {
  const valEl = slider.closest('.param-row').querySelector('.param-value');
  if (valEl) slider.addEventListener('input', () => { valEl.textContent = slider.value; });
});

async function loadGenParams() {
  try {
    const data = await authFetch(API+'/generation-params').then(r=>r.json());
    const mapping = {
      'param-temp': 'temperature', 'param-top-p': 'top_p', 'param-min-p': 'min_p',
      'param-top-k': 'top_k', 'param-rep-pen': 'repetition_penalty',
      'param-max-tokens': 'max_tokens', 'param-seed': 'seed',
    };
    for (const [elId, key] of Object.entries(mapping)) {
      const el = document.getElementById(elId);
      if (el && data[key] !== undefined && data[key] !== null) el.value = data[key];
    }
    document.querySelectorAll('.param-row input[type="range"]').forEach(s => {
      const v = s.closest('.param-row').querySelector('.param-value');
      if (v) v.textContent = s.value;
    });
  } catch(e) {}
}

document.getElementById('params-apply').addEventListener('click', async () => {
  const st = document.getElementById('params-status');
  st.textContent = 'Applying...'; st.className = 'config-status';
  const params = {
    temperature: parseFloat(document.getElementById('param-temp').value),
    top_p: parseFloat(document.getElementById('param-top-p').value),
    min_p: parseFloat(document.getElementById('param-min-p').value),
    top_k: parseInt(document.getElementById('param-top-k').value),
    repetition_penalty: parseFloat(document.getElementById('param-rep-pen').value),
    max_tokens: parseInt(document.getElementById('param-max-tokens').value),
  };
  const seed = parseInt(document.getElementById('param-seed').value);
  if (seed >= 0) params.seed = seed;
  try {
    const d = await authFetch(API+'/generation-params',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify(params)}).then(r=>r.json());
    if (d.error) { st.textContent = d.error; st.className = 'config-status config-status-error'; }
    else { st.textContent = 'Applied!'; st.className = 'config-status config-status-ok'; setTimeout(()=>{st.textContent='';},3000); }
  } catch(e) { st.textContent = 'Error: '+e.message; st.className = 'config-status config-status-error'; }
});

document.getElementById('params-reset').addEventListener('click', async () => {
  try {
    await authFetch(API+'/generation-params',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({reset:true})});
    loadGenParams(); showToast('Params reset to defaults');
  } catch(e) { showToast('Reset failed','error'); }
});

// =====================================================================
// Config: Presets
// =====================================================================

async function loadPresets() {
  try {
    const data = await authFetch(API+'/presets').then(r=>r.json());
    const el = document.getElementById('preset-select');
    el.innerHTML = '<option value="">-- select preset --</option>';
    (data.presets||[]).forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.name;
      let hint = '';
      if (p.temperature != null) hint += ` T=${p.temperature}`;
      if (p.top_p != null) hint += ` P=${p.top_p}`;
      opt.textContent = p.name + (hint ? ` (${hint.trim()})` : '');
      el.appendChild(opt);
    });
  } catch(e) {}
}

document.getElementById('preset-load').addEventListener('click', async () => {
  const name = document.getElementById('preset-select').value;
  if (!name) { showToast('Select a preset','error'); return; }
  try {
    const d = await authFetch(API+'/presets/load',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({preset_name:name})}).then(r=>r.json());
    if (d.error) showToast('Error: '+d.error,'error');
    else { showToast('Preset loaded: '+name); loadGenParams(); }
  } catch(e) { showToast('Failed','error'); }
});

// =====================================================================
// Config: Model Controls
// =====================================================================

document.getElementById('model-unload').addEventListener('click', async () => {
  if (!confirm('Unload current model? This will free VRAM.')) return;
  try {
    await authFetch(API+'/model/unload',{method:'POST'});
    showToast('Model unloaded'); loadStatus(); loadModels();
  } catch(e) { showToast('Unload failed: '+e.message,'error'); }
});

document.getElementById('run-benchmark').addEventListener('click', async () => {
  const el = document.getElementById('benchmark-result');
  el.textContent = 'Running...'; el.className = 'config-status';
  try {
    const d = await authFetch(API+'/benchmark',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({prompt_length:'short'})}).then(r=>r.json());
    if (d.error) { el.textContent = d.error; el.className = 'config-status config-status-error'; }
    else {
      const text = typeof d.result==='string' ? d.result : JSON.stringify(d.result);
      el.textContent = text.substring(0,200); el.className = 'config-status config-status-ok';
    }
  } catch(e) { el.textContent = 'Error'; el.className = 'config-status config-status-error'; }
});

// =====================================================================
// Config: Model Loading Parameters
// =====================================================================

// Populate draft-model dropdown from model list
async function loadDraftModels() {
  const sel = document.getElementById('ctrl-model-draft');
  if (!sel) return;
  try {
    const data = await authFetch(API + '/models').then(r => r.json());
    const current = sel.value;
    sel.innerHTML = '<option value="">none</option>';
    (data.models || []).forEach(m => {
      const name = typeof m === 'string' ? m : m.name || m;
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name.replace('.gguf','').substring(0,45);
      if (name === current) opt.selected = true;
      sel.appendChild(opt);
    });
  } catch(e) {}
}

// Clear all loading parameter fields
document.getElementById('load-params-clear').addEventListener('click', () => {
  const numberIds = [
    'ctrl-ctx-size', 'ctrl-gpu-layers', 'ctrl-threads', 'ctrl-threads-batch',
    'ctrl-batch-size', 'ctrl-ubatch-size', 'ctrl-rope-freq-base', 'ctrl-parallel',
    'ctrl-draft-max', 'ctrl-gpu-layers-draft', 'ctrl-ctx-size-draft',
    'ctrl-ngram-n', 'ctrl-ngram-m', 'ctrl-ngram-hits',
  ];
  for (const id of numberIds) {
    const el = document.getElementById(id);
    if (el) el.value = '';
  }
  const selectIds = ['ctrl-cache-type', 'ctrl-flash-attn', 'ctrl-spec-type', 'ctrl-model-draft'];
  for (const id of selectIds) {
    const el = document.getElementById(id);
    if (el) el.value = '';
  }
  const tsEl = document.getElementById('ctrl-tensor-split');
  if (tsEl) tsEl.value = '';
  showToast('Loading parameters cleared');
});

// Show/hide spec decoding fields based on spec type
document.getElementById('ctrl-spec-type').addEventListener('change', () => {
  const specType = document.getElementById('ctrl-spec-type').value;
  const draftFields = ['ctrl-model-draft', 'ctrl-draft-max', 'ctrl-gpu-layers-draft', 'ctrl-ctx-size-draft'];
  const ngramFields = ['ctrl-ngram-n', 'ctrl-ngram-m', 'ctrl-ngram-hits'];
  for (const id of draftFields) {
    const row = document.getElementById(id)?.closest('.param-row');
    if (row) row.style.display = specType === 'draft' ? '' : 'none';
  }
  for (const id of ngramFields) {
    const row = document.getElementById(id)?.closest('.param-row');
    if (row) row.style.display = specType === 'ngram' ? '' : 'none';
  }
});

// Initialize: hide spec fields, load draft models
(function initLoadParams() {
  document.getElementById('ctrl-spec-type').dispatchEvent(new Event('change'));
  loadDraftModels();
})();

// =====================================================================
// Config: LoRA Management
// =====================================================================

async function loadLoras() {
  try {
    const data = await authFetch(API+'/loras').then(r=>r.json());
    const info = document.getElementById('lora-info');
    const list = document.getElementById('lora-list');
    const loaded = data.loaded||[];
    info.innerHTML = loaded.length
      ? statusRow('Loaded', loaded.join(', '), 'ok')
      : statusRow('Loaded', 'none');
    if (data.available?.length) {
      list.innerHTML = data.available.map(name => {
        const isLoaded = loaded.includes(name);
        return `<div class="lora-item${isLoaded?' lora-loaded':''}">
          <span>${escapeHtml(name)}</span>
          ${isLoaded?'<span class="badge" style="background:var(--green);font-size:0.65rem">active</span>'
            :`<button class="btn-small" onclick="loadSingleLora('${escapeAttr(name)}')">Load</button>`}
        </div>`;
      }).join('');
    } else {
      list.innerHTML = '<div class="empty-state">No LoRA adapters found</div>';
    }
  } catch(e) { document.getElementById('lora-info').textContent = 'Failed to load'; }
}

async function loadSingleLora(name) {
  showToast('Loading LoRA: '+name+'...');
  try {
    const d = await authFetch(API+'/loras/load',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({lora_names:[name]})}).then(r=>r.json());
    if (d.error) showToast('Error: '+d.error,'error');
    else { showToast('LoRA loaded: '+name); loadLoras(); loadStatus(); }
  } catch(e) { showToast('Failed','error'); }
}

document.getElementById('lora-unload-all').addEventListener('click', async () => {
  try {
    await authFetch(API+'/loras/unload',{method:'POST'});
    showToast('LoRAs unloaded'); loadLoras(); loadStatus();
  } catch(e) { showToast('Failed','error'); }
});

// =====================================================================
// Search: Index Management
// =====================================================================

async function loadIndexMgmt() {
  try {
    const data = await authFetch(API+'/indexes').then(r=>r.json());
    const el = document.getElementById('index-mgmt-list');
    if (!el) return;
    if (!data.indexes?.length) { el.innerHTML='<div class="empty-state">No indexes. Create one below.</div>'; return; }
    el.innerHTML = data.indexes.map(name => `
      <div class="index-item">
        <span class="index-name">${escapeHtml(name)}</span>
        <div class="index-actions">
          <button class="btn-small" onclick="refreshIndex('${escapeAttr(name)}')">Refresh</button>
          <button class="btn-small btn-danger-small" onclick="deleteIndex('${escapeAttr(name)}')">Delete</button>
        </div>
      </div>
    `).join('');
  } catch(e) {}
}

async function refreshIndex(name) {
  showToast('Refreshing '+name+'...');
  try {
    await authFetch(API+`/indexes/${encodeURIComponent(name)}/refresh`,{method:'POST'});
    showToast('Refreshed: '+name);
  } catch(e) { showToast('Refresh failed','error'); }
}

async function deleteIndex(name) {
  if (!confirm(`Delete index "${name}"?`)) return;
  try {
    await authFetch(API+`/indexes/${encodeURIComponent(name)}/delete`,{method:'POST'});
    showToast('Deleted: '+name); loadIndexMgmt(); loadIndexes();
  } catch(e) { showToast('Delete failed','error'); }
}

document.getElementById('idx-create-btn')?.addEventListener('click', async () => {
  const name = document.getElementById('idx-name').value.trim();
  const directory = document.getElementById('idx-directory').value.trim();
  const glob = document.getElementById('idx-glob').value.trim() || '**/*.*';
  const embed = document.getElementById('idx-embed').checked;
  if (!name || !directory) { showToast('Name and directory required','error'); return; }
  showToast('Creating index '+name+'...');
  try {
    const d = await authFetch(API+'/indexes/create',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({name,directory,glob_pattern:glob,embed})}).then(r=>r.json());
    if (d.error) { showToast('Error: '+d.error,'error'); return; }
    showToast('Index created: '+name);
    document.getElementById('idx-name').value='';
    document.getElementById('idx-directory').value='';
    loadIndexMgmt(); loadIndexes();
  } catch(e) { showToast('Create failed','error'); }
});

// =====================================================================
// Research Sessions
// =====================================================================
async function loadResearchSessions() {
  const el = document.getElementById('research-sessions');
  try {
    const data = await authFetch(API+'/research/sessions').then(r=>r.json());
    const sessions = data.sessions||[];
    if(!sessions.length){el.innerHTML='<div class="empty-state">No research sessions. Start one above!</div>';return;}
    el.innerHTML = sessions.map(s=>`
      <div class="research-session-card" data-id="${s.id}">
        <div class="research-question">${escapeHtml(s.question)}</div>
        <div class="research-meta">
          <span class="badge badge-${s.status}">${s.status}</span>
          <span>${s.finding_count} sources</span>
          <span>${timeAgo(s.updated_at)}</span>
        </div>
      </div>
    `).join('');
    el.querySelectorAll('.research-session-card').forEach(card=>{
      card.addEventListener('click',()=>loadResearchDetail(card.dataset.id));
    });
  } catch(e) { el.innerHTML='<div class="error-msg">'+e.message+'</div>'; }
}

async function loadResearchDetail(sessionId) {
  const card = document.getElementById('research-detail-card');
  const el = document.getElementById('research-detail');
  card.style.display='block';
  el.innerHTML='<div class="loading">Loading...</div>';
  try {
    const data = await authFetch(API+'/research/sessions/'+sessionId).then(r=>r.json());
    if(data.error){el.innerHTML='<div class="error-msg">'+data.error+'</div>';return;}
    document.getElementById('research-detail-title').textContent = data.question;
    let html = '';
    if(data.findings?.length) {
      html += '<h3>Sources</h3><div class="findings-list">';
      data.findings.forEach((f,i) => {
        const credClass = f.credibility >= 0.7 ? 'cred-high' : f.credibility >= 0.4 ? 'cred-med' : 'cred-low';
        html += `<div class="finding-item">
          <div class="finding-header">
            <span class="finding-num">[${i+1}]</span>
            <a href="${escapeAttr(f.url)}" target="_blank" class="finding-title">${escapeHtml(f.title||f.url)}</a>
            <span class="cred-badge ${credClass}">${Math.round(f.credibility*100)}%</span>
          </div>
          <div class="finding-excerpt">${escapeHtml((f.excerpt||'').substring(0,300))}</div>
        </div>`;
      });
      html += '</div>';
    }
    if(data.synthesis) {
      html += '<h3>Synthesis</h3><div class="research-synthesis">' + escapeHtml(data.synthesis) + '</div>';
    }
    el.innerHTML = html || '<div class="empty-state">No findings yet</div>';
  } catch(e) { el.innerHTML='<div class="error-msg">'+e.message+'</div>'; }
}

document.getElementById('research-start-btn')?.addEventListener('click', async()=>{
  const q=document.getElementById('research-query').value.trim();
  if(!q){showToast('Enter a research question','error');return;}
  showToast('Starting research...');
  try{
    const data=await authFetch(API+'/research/start',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({question:q})}).then(r=>r.json());
    if(data.error){showToast(data.error,'error');return;}
    showToast('Research started: '+data.session_id);
    document.getElementById('research-query').value='';
    setTimeout(loadResearchSessions,2000);
  }catch(e){showToast('Failed: '+e.message,'error');}
});

// =====================================================================
// Workflows
// =====================================================================
async function loadWorkflows() {
  const el = document.getElementById('workflow-list');
  try {
    const data = await authFetch(API+'/workflows').then(r=>r.json());
    const wfs = data.workflows||[];
    if(!wfs.length){el.innerHTML='<div class="empty-state">No workflows defined. Create one!</div>';return;}
    el.innerHTML = wfs.map(w=>`
      <div class="workflow-item" data-id="${escapeAttr(w.id)}">
        <div class="workflow-name">${escapeHtml(w.name)}</div>
        <div class="workflow-desc">${escapeHtml((w.description||'').substring(0,100))}</div>
        <div class="workflow-meta">${w.node_count||0} nodes</div>
      </div>
    `).join('');
    el.querySelectorAll('.workflow-item').forEach(item=>{
      item.addEventListener('click',async()=>{
        try{
          const data=await authFetch(API+'/workflows/'+item.dataset.id).then(r=>r.json());
          document.getElementById('wf-editor-card').style.display='block';
          document.getElementById('wf-editor').value=JSON.stringify(data.workflow,null,2);
        }catch(e){showToast('Failed to load workflow','error');}
      });
    });
  } catch(e) { el.innerHTML='<div class="error-msg">'+e.message+'</div>'; }
}

document.getElementById('wf-new-btn')?.addEventListener('click',()=>{
  document.getElementById('wf-editor-card').style.display='block';
  document.getElementById('wf-editor').value=JSON.stringify({
    id:'',name:'New Workflow',description:'',
    nodes:[{id:'start',type:'prompt',config:{template:'{input}',system:''}}],
    edges:[],variables:{}
  },null,2);
});

document.getElementById('wf-save-btn')?.addEventListener('click',async()=>{
  const status=document.getElementById('wf-status');
  try{
    const wf=JSON.parse(document.getElementById('wf-editor').value);
    const data=await authFetch(API+'/workflows',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify(wf)}).then(r=>r.json());
    if(data.error){status.textContent=data.error;status.className='config-status config-status-error';}
    else{status.textContent='Saved!';status.className='config-status config-status-ok';loadWorkflows();}
  }catch(e){status.textContent='Invalid JSON';status.className='config-status config-status-error';}
});

document.getElementById('wf-run-btn')?.addEventListener('click',async()=>{
  const status=document.getElementById('wf-status');
  try{
    const wf=JSON.parse(document.getElementById('wf-editor').value);
    const input=document.getElementById('wf-input').value.trim();
    status.textContent='Running...';status.className='config-status';
    const data=await authFetch(API+'/workflows/run',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({workflow:wf,initial_input:input})}).then(r=>r.json());
    if(data.error){status.textContent=data.error;status.className='config-status config-status-error';}
    else{
      status.textContent='Running: '+data.execution_id;status.className='config-status config-status-ok';
      setTimeout(()=>loadExecutionDetail(data.execution_id),3000);
    }
  }catch(e){status.textContent='Error: '+e.message;status.className='config-status config-status-error';}
});

document.getElementById('wf-executions-btn')?.addEventListener('click',async()=>{
  const card=document.getElementById('wf-execution-card');
  card.style.display='block';
  const el=document.getElementById('wf-execution-detail');
  el.innerHTML='<div class="loading">Loading...</div>';
  try{
    const data=await authFetch(API+'/workflows/executions').then(r=>r.json());
    const execs=data.executions||[];
    if(!execs.length){el.innerHTML='<div class="empty-state">No executions yet</div>';return;}
    el.innerHTML=execs.map(e=>`
      <div class="exec-item exec-${e.status}" onclick="loadExecutionDetail('${e.execution_id}')">
        <span class="exec-id">${e.execution_id}</span>
        <span class="badge badge-${e.status}">${e.status}</span>
        <span>${e.node_count} nodes</span>
        <span>${timeAgo(e.started_at)}</span>
      </div>
    `).join('');
  }catch(e){el.innerHTML='<div class="error-msg">'+e.message+'</div>';}
});

async function loadExecutionDetail(execId) {
  const card=document.getElementById('wf-execution-card');
  card.style.display='block';
  const el=document.getElementById('wf-execution-detail');
  el.innerHTML='<div class="loading">Loading...</div>';
  try{
    const data=await authFetch(API+'/workflows/executions/'+execId).then(r=>r.json());
    if(data.error){el.innerHTML='<div class="error-msg">'+data.error+'</div>';return;}
    let html = `<div class="exec-header"><span class="badge badge-${data.status}">${data.status}</span> ${data.error?'<span class="error-msg">'+escapeHtml(data.error)+'</span>':''}</div>`;
    html += '<div class="exec-nodes">';
    for(const[nid,status] of Object.entries(data.node_statuses||{})){
      const output = (data.node_outputs||{})[nid]||'';
      html += `<div class="exec-node exec-node-${status}">
        <span class="exec-node-id">${escapeHtml(nid)}</span>
        <span class="badge badge-${status}">${status}</span>
        ${output?'<pre class="exec-node-output">'+escapeHtml(output.substring(0,300))+'</pre>':''}
      </div>`;
    }
    html += '</div>';
    el.innerHTML=html;
    // Auto-refresh if still running
    if(data.status==='running') setTimeout(()=>loadExecutionDetail(execId),3000);
  }catch(e){el.innerHTML='<div class="error-msg">'+e.message+'</div>';}
}

// =====================================================================
// Knowledge Graph Visualization
// =====================================================================
document.getElementById('kg-viz-btn')?.addEventListener('click', async()=>{
  const center=document.getElementById('kg-viz-center').value.trim();
  const canvas=document.getElementById('kg-canvas');
  canvas.style.display='block';
  try{
    const url = API+'/kg/graph'+(center?'?center='+encodeURIComponent(center):'');
    const data=await authFetch(url).then(r=>r.json());
    if(data.error){showToast(data.error,'error');return;}
    renderKGGraph(canvas, data.nodes||[], data.edges||[]);
  }catch(e){showToast('Graph failed: '+e.message,'error');}
});

function renderKGGraph(canvas, nodes, edges) {
  if(!nodes.length){canvas.style.display='none';showToast('No nodes to visualize');return;}
  const ctx = canvas.getContext('2d');
  const W = canvas.width = canvas.clientWidth * (window.devicePixelRatio||1);
  const H = canvas.height = 500 * (window.devicePixelRatio||1);
  ctx.scale(window.devicePixelRatio||1, window.devicePixelRatio||1);
  const w = canvas.clientWidth, h = 500;

  // Initialize positions randomly
  const pos = {};
  nodes.forEach(n => { pos[n.id] = { x: w/2 + (Math.random()-0.5)*w*0.6, y: h/2 + (Math.random()-0.5)*h*0.6 }; });

  // Type colors
  const colors = {concept:'#58a6ff',code_module:'#3fb950',decision:'#d29922',learning:'#bc8cff',
    person:'#f78166',tool:'#8b949e',project:'#79c0ff',task:'#d2a8ff',event:'#ffa657',artifact:'#7ee787'};

  // Force-directed simulation
  for(let iter=0;iter<200;iter++){
    const alpha = 0.1 * (1 - iter/200);
    // Repulsion between all nodes
    for(let i=0;i<nodes.length;i++){
      for(let j=i+1;j<nodes.length;j++){
        const a=pos[nodes[i].id], b=pos[nodes[j].id];
        let dx=b.x-a.x, dy=b.y-a.y;
        const dist=Math.sqrt(dx*dx+dy*dy)||1;
        const force = 5000 / (dist*dist);
        dx/=dist; dy/=dist;
        a.x-=dx*force*alpha; a.y-=dy*force*alpha;
        b.x+=dx*force*alpha; b.y+=dy*force*alpha;
      }
    }
    // Attraction along edges
    edges.forEach(e=>{
      const a=pos[e.from], b=pos[e.to];
      if(!a||!b)return;
      let dx=b.x-a.x, dy=b.y-a.y;
      const dist=Math.sqrt(dx*dx+dy*dy)||1;
      const force=(dist-100)*0.01;
      dx/=dist; dy/=dist;
      a.x+=dx*force*alpha; a.y+=dy*force*alpha;
      b.x-=dx*force*alpha; b.y-=dy*force*alpha;
    });
    // Keep in bounds
    nodes.forEach(n=>{
      const p=pos[n.id];
      p.x=Math.max(40,Math.min(w-40,p.x));
      p.y=Math.max(40,Math.min(h-40,p.y));
    });
  }

  // Draw
  ctx.clearRect(0,0,w,h);
  ctx.fillStyle='#0d1117'; ctx.fillRect(0,0,w,h);

  // Edges
  edges.forEach(e=>{
    const a=pos[e.from], b=pos[e.to];
    if(!a||!b)return;
    ctx.beginPath(); ctx.moveTo(a.x,a.y); ctx.lineTo(b.x,b.y);
    ctx.strokeStyle='#30363d'; ctx.lineWidth=1; ctx.stroke();
    // Edge label
    ctx.fillStyle='#484f58'; ctx.font='9px monospace';
    ctx.fillText(e.relation||'',(a.x+b.x)/2,(a.y+b.y)/2);
  });

  // Nodes
  nodes.forEach(n=>{
    const p=pos[n.id];
    const r=n.depth===0?12:8;
    ctx.beginPath(); ctx.arc(p.x,p.y,r,0,Math.PI*2);
    ctx.fillStyle=colors[n.type]||'#8b949e'; ctx.fill();
    ctx.strokeStyle='#0d1117'; ctx.lineWidth=2; ctx.stroke();
    ctx.fillStyle='#e6edf3'; ctx.font='11px monospace'; ctx.textAlign='center';
    ctx.fillText(n.name.substring(0,20),p.x,p.y+r+14);
  });
}

// =====================================================================
// Notifications (SSE)
// =====================================================================
function connectSSE() {
  if (!apiKey) return;
  const es = new EventSource(API + '/events?token=' + encodeURIComponent(apiKey));
  es.onmessage = event => {
    try {
      const data = JSON.parse(event.data);
      if (data.type === 'connected') return;
      showToast(`${data.title}: ${data.body}`);
      // Browser notification
      if (Notification.permission === 'granted') {
        new Notification(data.title, { body: data.body, icon: '/static/icon-192.svg' });
      }
    } catch(e) {}
  };
  es.onerror = () => { setTimeout(connectSSE, 5000); es.close(); };
}

// Request notification permission
if ('Notification' in window && Notification.permission === 'default') {
  // Will ask on first interaction
  document.addEventListener('click', function askNotif() {
    Notification.requestPermission();
    document.removeEventListener('click', askNotif);
  }, { once: true });
}

// =====================================================================
// Toast notifications (in-app)
// =====================================================================
function showToast(msg, type = 'info') {
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.textContent = msg;
  container.appendChild(toast);
  setTimeout(() => { toast.classList.add('toast-fade'); setTimeout(() => toast.remove(), 500); }, 4000);
}

// =====================================================================
// Utilities
// =====================================================================
function escapeHtml(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function escapeAttr(s){return(s||'').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
function debounce(fn,ms=300){let t;return(...a)=>{clearTimeout(t);t=setTimeout(()=>fn(...a),ms);}}
// =====================================================================
// Model Sync
// =====================================================================
document.getElementById('sync-models-btn')?.addEventListener('click', async () => {
  const btn = document.getElementById('sync-models-btn');
  const status = document.getElementById('sync-models-status');
  const result = document.getElementById('sync-models-result');
  btn.disabled = true;
  status.textContent = 'Syncing...';
  result.style.display = 'none';
  try {
    const resp = await apiFetch('/api/sync-models', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({clean:true})});
    const data = await resp.json();
    result.textContent = data.result || data.error || 'Done';
    result.style.display = 'block';
    status.textContent = '';
    // Refresh model list after sync
    loadModels();
    showToast('Models synced', 'success');
  } catch(e) {
    status.textContent = 'Error: ' + e.message;
  } finally {
    btn.disabled = false;
  }
});

// =====================================================================
// Training Pipeline
// =====================================================================
async function loadTrainingOverview() {
  const el = document.getElementById('training-overview');
  if (!el) return;
  try {
    const resp = await apiFetch('/api/training?what=all');
    const data = await resp.json();
    el.textContent = data.result || 'No training data yet.';
    // Populate dataset dropdown
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
  } catch(e) {
    el.textContent = 'Error loading training data: ' + e.message;
  }
}

async function loadTrainingStatus() {
  const el = document.getElementById('training-status');
  if (!el) return;
  try {
    const resp = await apiFetch('/api/training/status');
    const data = await resp.json();
    el.textContent = data.result || 'No active run';
  } catch(e) {
    el.textContent = 'No training runs found.';
  }
}

document.getElementById('train-prepare-btn')?.addEventListener('click', async () => {
  const btn = document.getElementById('train-prepare-btn');
  const result = document.getElementById('train-prepare-result');
  const mode = document.getElementById('train-mode').value;
  const body = {mode};
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
    const resp = await apiFetch('/api/training/prepare', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const data = await resp.json();
    result.textContent = data.result || data.error;
    result.style.display = 'block';
    showToast('Dataset prepared', 'success');
    loadTrainingOverview();
  } catch(e) {
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
    const resp = await apiFetch('/api/training/start', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const data = await resp.json();
    if (data.result && data.result.includes('currently loaded')) {
      warn.textContent = data.result;
      warn.style.display = 'block';
    } else {
      showToast('Training started!', 'success');
      loadTrainingStatus();
    }
  } catch(e) {
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
    const resp = await apiFetch('/api/training/feedback', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({prompt, response, rating})});
    const data = await resp.json();
    resultEl.textContent = data.result || 'Feedback recorded.';
    document.getElementById('fb-prompt').value = '';
    document.getElementById('fb-response').value = '';
    showToast('Feedback recorded', 'success');
  } catch(e) {
    resultEl.textContent = 'Error: ' + e.message;
  }
});

// Show/hide repo/glob fields based on training mode
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

// =====================================================================
// Init
// =====================================================================
(async () => {
  await initUser();
  loadStatus();
  loadModels();
  loadAgents();
  loadNotes();
  loadMeshStatus();
  loadModes();
  loadApprovals();
  loadTrainingOverview();
  loadTrainingStatus();
  // SSE notifications with query param auth
  connectSSE();
  setInterval(loadStatus, 30000);
  setInterval(loadMeshStatus, 30000);
  setInterval(loadApprovals, 15000);
  setInterval(loadTrainingStatus, 30000); // Monitor active training runs
})();
