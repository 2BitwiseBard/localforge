import { API, authFetch, authHeaders, escapeHtml, showToast, renderMarkdown } from './api.js';

const chatMessages = document.getElementById('chat-messages');
const chatPrompt = document.getElementById('chat-prompt');
const chatSend = document.getElementById('chat-send');
const chatStop = document.getElementById('chat-stop');
let chatHistory = [];
let currentChatId = null;
let _abortController = null;

// Populate model indicator from health endpoint
async function updateChatModelIndicator() {
  try {
    const h = await fetch('/health').then(r => r.json());
    const name = h.model?.model_name || '';
    const ind = document.getElementById('chat-model-indicator');
    if (ind) ind.textContent = name ? name.replace('.gguf', '').substring(0, 28) : 'no model';
  } catch {}
}
updateChatModelIndicator();

chatSend.addEventListener('click', sendChat);
chatPrompt.addEventListener('keydown', e => { if (e.key === 'Enter' && (e.ctrlKey || !e.shiftKey)) { e.preventDefault(); sendChat(); } });
chatStop?.addEventListener('click', () => { if (_abortController) { _abortController.abort(); _abortController = null; } });

// Image upload
let pendingImage = null;
const imageInput = document.getElementById('image-input');
const imagePreview = document.getElementById('image-preview-area');
const imageThumbnail = document.getElementById('image-thumbnail');
document.getElementById('image-clear').addEventListener('click', () => {
  pendingImage = null; imageInput.value = ''; imagePreview.style.display = 'none';
});
imageInput.addEventListener('change', e => {
  const f = e.target.files[0]; if (!f) return;
  pendingImage = f; imageThumbnail.src = URL.createObjectURL(f); imagePreview.style.display = 'flex';
});

function _setChatSending(sending) {
  chatSend.disabled = sending;
  if (chatStop) chatStop.style.display = sending ? '' : 'none';
}

async function sendChat() {
  const prompt = chatPrompt.value.trim();
  if (!prompt && !pendingImage) return;
  if (_abortController) return;

  addMessage(prompt || '[Image analysis]', 'user');
  chatHistory.push({ role: 'user', content: prompt || '[Image]', timestamp: Date.now() });
  chatPrompt.value = '';
  _setChatSending(true);
  const msgEl = addMessage('', 'assistant');

  _abortController = new AbortController();
  try {
    let resp;
    if (pendingImage) {
      const fd = new FormData();
      fd.append('image', pendingImage);
      fd.append('question', prompt || 'Describe this image in detail.');
      resp = await authFetch(API + '/upload-image', { method: 'POST', body: fd, signal: _abortController.signal });
      pendingImage = null; imageInput.value = ''; imagePreview.style.display = 'none';
    } else {
      const sysPrompt = document.getElementById('sys-prompt')?.value?.trim() || '';
      resp = await authFetch(API + '/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({
          messages: chatHistory.filter(m => m.role === 'user' || m.role === 'assistant').map(m => ({ role: m.role, content: m.content })),
          system_prompt: sysPrompt,
        }),
        signal: _abortController.signal,
      });
    }
    msgEl.classList.add('msg-streaming');
    const fullText = await streamResponse(resp, msgEl, _abortController.signal);
    msgEl.classList.remove('msg-streaming');
    if (fullText) {
      chatHistory.push({ role: 'assistant', content: fullText, timestamp: Date.now() });
      if (ttsEnabled) speak(fullText);
      document.getElementById('regen-area').style.display = 'block';
    }
  } catch (e) {
    msgEl.classList.remove('msg-streaming');
    if (e.name === 'AbortError') {
      if (!msgEl.textContent) msgEl.textContent = '[stopped]';
    } else {
      msgEl.innerHTML += `<span style="color:var(--red);display:block;margin-top:4px;">Error: ${escapeHtml(e.message)}</span>`;
    }
  } finally {
    _abortController = null;
    _setChatSending(false);
    updateChatModelIndicator();
  }
}

async function streamResponse(resp, msgEl, signal) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '', fullText = '', tokenCount = 0;
  const startTime = performance.now();

  let speedEl = msgEl.parentElement.querySelector('.token-speed');
  if (!speedEl) {
    speedEl = document.createElement('div');
    speedEl.className = 'token-speed';
    msgEl.insertAdjacentElement('afterend', speedEl);
  }

  while (true) {
    if (signal?.aborted) { reader.cancel(); break; }
    const { done, value } = await reader.read(); if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n'); buffer = lines.pop();
    for (const line of lines) {
      if (line.startsWith('data: ')) {
        const data = line.slice(6); if (data === '[DONE]') continue;
        try {
          const c = JSON.parse(data);
          if (c.content) {
            if (!fullText) msgEl.innerHTML = '';  // clear thinking dots on first token
            fullText += c.content;
            tokenCount++;
            msgEl.innerHTML = renderMarkdown(fullText);
            chatMessages.scrollTop = chatMessages.scrollHeight;
            if (tokenCount % 5 === 0) {
              const elapsed = (performance.now() - startTime) / 1000;
              const tps = elapsed > 0 ? (tokenCount / elapsed).toFixed(1) : '...';
              speedEl.textContent = `${tokenCount} tokens | ${tps} tok/s`;
            }
          }
          if (c.error) msgEl.innerHTML += `<br><span style="color:var(--red)">[Error: ${escapeHtml(c.error)}]</span>`;
        } catch (e) {}
      }
    }
  }
  const elapsed = (performance.now() - startTime) / 1000;
  const tps = elapsed > 0 ? (tokenCount / elapsed).toFixed(1) : '0';
  speedEl.textContent = `${tokenCount} tokens | ${tps} tok/s | ${elapsed.toFixed(1)}s`;
  msgEl.innerHTML = renderMarkdown(fullText);
  return fullText;
}

function addMessage(text, role) {
  const div = document.createElement('div');
  div.className = 'msg msg-' + role;
  if (role === 'assistant') {
    if (text) {
      div.innerHTML = renderMarkdown(text);
    } else {
      div.innerHTML = '<span class="thinking-dots"><span></span><span></span><span></span></span>';
    }
  } else {
    div.textContent = text;
  }
  chatMessages.appendChild(div);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  return div;
}

// Chat history
document.getElementById('chat-new-btn').addEventListener('click', () => {
  chatHistory = []; currentChatId = null; chatMessages.innerHTML = '';
  document.getElementById('regen-area').style.display = 'none';
});

document.getElementById('chat-save-btn').addEventListener('click', async () => {
  if (!chatHistory.length) return;
  try {
    const resp = await authFetch(API + '/chats/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ messages: chatHistory, id: currentChatId }),
    });
    const data = await resp.json();
    currentChatId = data.id;
    showToast('Chat saved: ' + data.title);
  } catch (e) { showToast('Save failed', 'error'); }
});

document.getElementById('chat-export-btn').addEventListener('click', () => {
  if (!chatHistory.length) { showToast('Nothing to export', 'error'); return; }
  const lines = chatHistory.map(m => {
    const role = m.role === 'user' ? '**You**' : '**AI**';
    return `${role}\n\n${m.content}\n`;
  });
  const md = lines.join('\n---\n\n');
  const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([md], { type: 'text/markdown' }));
  a.download = `chat-${ts}.md`;
  a.click();
  URL.revokeObjectURL(a.href);
});

const historyPanel = document.getElementById('chat-history-panel');
const historyList = document.getElementById('chat-history-list');
document.getElementById('chat-history-btn').addEventListener('click', async () => {
  if (historyPanel.style.display !== 'none') { historyPanel.style.display = 'none'; return; }
  historyPanel.style.display = 'block';
  try {
    let chatPage = 1;
    const renderChats = async (page) => {
      const data = await authFetch(API + `/chats?page=${page}&limit=30`).then(r => r.json());
      if (page === 1 && !data.chats?.length) { historyList.innerHTML = '<div class="empty-state">No saved chats</div>'; return; }
      if (page === 1) historyList.innerHTML = '';
      historyList.insertAdjacentHTML('beforeend', data.chats.map(c => `
        <div class="history-item" data-id="${c.id}">
          <div class="history-title">${escapeHtml(c.title)}</div>
          <div class="history-meta">${c.message_count} msgs | ${new Date(c.updated * 1000).toLocaleDateString()}</div>
        </div>
      `).join(''));
      historyList.querySelector('.load-more-btn')?.remove();
      if (data.has_more) {
        historyList.insertAdjacentHTML('beforeend',
          `<button class="load-more-btn btn btn-sm" style="width:100%;margin-top:8px">Load more</button>`);
        historyList.querySelector('.load-more-btn').addEventListener('click', () => renderChats(++chatPage));
      }
      historyList.querySelectorAll('.history-item:not([data-bound])').forEach(el => {
        el.dataset.bound = '1';
        el.addEventListener('click', async () => {
          const data = await authFetch(API + '/chats/' + el.dataset.id).then(r => r.json());
          chatHistory = data.messages || []; currentChatId = data.id; chatMessages.innerHTML = '';
          chatHistory.forEach(m => addMessage(m.content, m.role));
          historyPanel.style.display = 'none';
        });
      });
    };
    await renderChats(chatPage);
  } catch (e) { historyList.innerHTML = 'Error loading history'; }
});

// ---------------------------------------------------------------------------
// Voice: STT (microphone)
// ---------------------------------------------------------------------------
const micBtn = document.getElementById('mic-btn');
const recIndicator = document.getElementById('recording-indicator');
let mediaRecorder = null, audioChunks = [];

micBtn.addEventListener('click', async () => {
  if (mediaRecorder?.state === 'recording') { mediaRecorder.stop(); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm' });
    audioChunks = [];
    mediaRecorder.ondataavailable = e => audioChunks.push(e.data);
    mediaRecorder.onstop = async () => {
      recIndicator.style.display = 'none'; micBtn.textContent = '\u{1F3A4}';
      stream.getTracks().forEach(t => t.stop());
      const blob = new Blob(audioChunks, { type: 'audio/webm' });
      const fd = new FormData(); fd.append('file', blob, 'recording.webm');
      try {
        const data = await authFetch(API + '/transcribe', { method: 'POST', body: fd }).then(r => r.json());
        if (data.text) { chatPrompt.value += data.text; chatPrompt.focus(); }
        else if (data.error) showToast('Transcription: ' + data.error, 'error');
      } catch (e) { showToast('Transcription failed', 'error'); }
    };
    mediaRecorder.start();
    recIndicator.style.display = 'flex'; micBtn.textContent = '\u23F9';
  } catch (e) { showToast('Microphone unavailable', 'error'); }
});

// ---------------------------------------------------------------------------
// Voice: TTS
// ---------------------------------------------------------------------------
let ttsEnabled = localStorage.getItem('tts') === 'true';
const ttsToggle = document.getElementById('tts-toggle');
const voiceSelect = document.getElementById('voice-select');

function updateTTSButton() {
  ttsToggle.textContent = ttsEnabled ? '\u{1F50A}' : '\u{1F508}';
  ttsToggle.classList.toggle('active', ttsEnabled);
}
updateTTSButton();
ttsToggle.addEventListener('click', () => { ttsEnabled = !ttsEnabled; localStorage.setItem('tts', ttsEnabled); updateTTSButton(); });

function loadVoices() {
  const voices = speechSynthesis.getVoices(); voiceSelect.innerHTML = '';
  const saved = localStorage.getItem('tts-voice');
  voices.forEach(v => {
    const o = document.createElement('option'); o.value = v.name; o.textContent = `${v.name} (${v.lang})`;
    if (v.name === saved) o.selected = true; voiceSelect.appendChild(o);
  });
}
speechSynthesis.onvoiceschanged = loadVoices; loadVoices();
voiceSelect.addEventListener('change', () => localStorage.setItem('tts-voice', voiceSelect.value));

function speak(text) {
  if (!text) return; speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  const saved = localStorage.getItem('tts-voice');
  if (saved) { const v = speechSynthesis.getVoices().find(v => v.name === saved); if (v) u.voice = v; }
  speechSynthesis.speak(u);
}

// ---------------------------------------------------------------------------
// Chat: Regenerate, Export, System Prompt
// ---------------------------------------------------------------------------
document.getElementById('sys-prompt-toggle').addEventListener('click', () => {
  const panel = document.getElementById('sys-prompt-panel');
  panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
});

document.getElementById('regen-btn').addEventListener('click', async () => {
  if (chatHistory.length < 2 || chatHistory[chatHistory.length - 1].role !== 'assistant') return;
  if (_abortController) return;
  chatHistory.pop();
  const msgs = chatMessages.querySelectorAll('.msg');
  if (msgs.length) msgs[msgs.length - 1].remove();
  const msgEl = addMessage('', 'assistant');
  _setChatSending(true);
  _abortController = new AbortController();
  try {
    const sysPrompt = document.getElementById('sys-prompt')?.value?.trim() || '';
    const resp = await authFetch(API + '/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({
        messages: chatHistory.filter(m => m.role === 'user' || m.role === 'assistant').map(m => ({ role: m.role, content: m.content })),
        system_prompt: sysPrompt,
      }),
      signal: _abortController.signal,
    });
    msgEl.classList.add('msg-streaming');
    const fullText = await streamResponse(resp, msgEl, _abortController.signal);
    msgEl.classList.remove('msg-streaming');
    if (fullText) {
      chatHistory.push({ role: 'assistant', content: fullText, timestamp: Date.now() });
      if (ttsEnabled) speak(fullText);
    }
  } catch (e) {
    msgEl.classList.remove('msg-streaming');
    if (e.name !== 'AbortError') msgEl.textContent = 'Error: ' + e.message;
  } finally {
    _abortController = null;
    _setChatSending(false);
  }
});

document.getElementById('chat-export-btn').addEventListener('click', () => {
  if (!chatHistory.length) { showToast('No chat to export', 'error'); return; }
  const content = chatHistory.map(m => `**${m.role}**: ${m.content}`).join('\n\n---\n\n');
  const blob = new Blob([content], { type: 'text/markdown' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `chat-${new Date().toISOString().slice(0, 10)}.md`;
  a.click();
  URL.revokeObjectURL(a.href);
  showToast('Chat exported');
});
