import { API, apiKey, authFetch, escapeHtml, escapeAttr, showToast } from './api.js';

const photoGallery = document.getElementById('photo-gallery');
const videoGallery = document.getElementById('video-gallery');
const mediaUpload = document.getElementById('media-upload');
const photoSearchInput = document.getElementById('photo-search-input');
const photoSearchBtn = document.getElementById('photo-search-btn');

const _blobCache = new Map();
const _BLOB_CACHE_MAX = 50;
async function fetchAuthedBlob(url) {
  if (_blobCache.has(url)) return _blobCache.get(url);
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

export async function loadPhotos() {
  try {
    const data = await authFetch(API + '/photos').then(r => r.json());
    renderPhotos(data.photos || []);
  } catch (e) { photoGallery.innerHTML = '<div class="error-msg">' + e.message + '</div>'; }
}

async function renderPhotos(photos) {
  if (!photos.length) { photoGallery.innerHTML = '<div class="empty-state">No photos yet. Upload some!</div>'; return; }
  photoGallery.innerHTML = photos.map(p => `
    <div class="photo-card" data-url="${escapeAttr(p.url)}" data-thumb="${escapeAttr(p.thumbnail)}">
      <div class="photo-loading">Loading...</div>
      <div class="photo-info">
        <div class="photo-desc">${escapeHtml((p.description || p.filename).substring(0, 80))}</div>
        ${p.tags?.length ? '<div class="photo-tags">' + p.tags.map(t => '<span class="tag">' + escapeHtml(t) + '</span>').join('') + '</div>' : ''}
      </div>
    </div>
  `).join('');

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
    const data = await authFetch(API + '/videos').then(r => r.json());
    renderVideos(data.videos || []);
  } catch (e) { videoGallery.innerHTML = '<div class="error-msg">' + e.message + '</div>'; }
}

async function renderVideos(videos) {
  if (!videos.length) { videoGallery.innerHTML = '<div class="empty-state">No videos yet. Upload some!</div>'; return; }
  videoGallery.innerHTML = videos.map(v => `
    <div class="video-card">
      <video src="${authedUrl(v.url)}" poster="${authedUrl(v.thumbnail)}" controls preload="metadata" class="video-player"></video>
      <div class="video-info">
        <div class="video-desc">${escapeHtml((v.description || v.filename).substring(0, 80))}</div>
        <div class="video-meta">${v.duration || ''} ${v.resolution || ''}</div>
      </div>
    </div>
  `).join('');
}

mediaUpload.addEventListener('change', async e => {
  const files = Array.from(e.target.files);
  if (!files.length) return;
  const imageFiles = files.filter(f => f.type.startsWith('image/'));
  const videoFiles = files.filter(f => f.type.startsWith('video/'));
  showToast(`Uploading ${files.length} file(s)...`);
  for (const file of imageFiles) {
    const fd = new FormData(); fd.append('image', file); fd.append('auto_tag', 'true');
    try { await authFetch(API + '/photos/upload', { method: 'POST', body: fd }); }
    catch (e) { showToast('Upload failed: ' + e.message, 'error'); }
  }
  for (const file of videoFiles) {
    const fd = new FormData(); fd.append('video', file); fd.append('auto_tag', 'true');
    try { await authFetch(API + '/videos/upload', { method: 'POST', body: fd }); }
    catch (e) { showToast('Upload failed: ' + e.message, 'error'); }
  }
  mediaUpload.value = '';
  if (imageFiles.length) loadPhotos();
  if (videoFiles.length) loadVideos();
  showToast('Upload complete!');
});

photoSearchBtn.addEventListener('click', async () => {
  const q = photoSearchInput.value.trim(); if (!q) { loadPhotos(); return; }
  try {
    const data = await authFetch(API + '/photos/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: q }),
    }).then(r => r.json());
    renderPhotos(data.results || []);
  } catch (e) { photoGallery.innerHTML = '<div class="error-msg">' + e.message + '</div>'; }
});
photoSearchInput.addEventListener('keydown', e => { if (e.key === 'Enter') photoSearchBtn.click(); });
