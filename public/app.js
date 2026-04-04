// =====================
// State
// =====================
let currentVideoInfo    = null;
let selectedFormat      = null;
let isDownloading       = false;
let progressEventSource = null;
let currentSessionId    = null;

// =====================
// DOM Elements
// =====================
const urlInput       = document.getElementById('urlInput');
const fetchBtn       = document.getElementById('fetchBtn');
const pasteBtn       = document.getElementById('pasteBtn');
const loadingCard    = document.getElementById('loadingCard');
const errorCard      = document.getElementById('errorCard');
const errorText      = document.getElementById('errorText');
const tryAgainBtn    = document.getElementById('tryAgainBtn');
const videoCard      = document.getElementById('videoCard');
const thumbnail      = document.getElementById('thumbnail');
const durationBadge  = document.getElementById('durationBadge');
const videoTitle     = document.getElementById('videoTitle');
const videoUploader  = document.getElementById('videoUploader');
const videoViews     = document.getElementById('videoViews');
const detailSep      = document.getElementById('detailSep');
const formatGrid     = document.getElementById('formatGrid');
const downloadBtn    = document.getElementById('downloadBtn');
const downloadBtnText= document.getElementById('downloadBtnText');
const progressBar    = document.getElementById('progressBar');
const progressInfo   = document.getElementById('progressInfo');
const progressPercent= document.getElementById('progressPercent');
const progressSpeed  = document.getElementById('progressSpeed');
const progressEta    = document.getElementById('progressEta');
const progressStatus = document.getElementById('progressStatus');
const cancelBtn      = document.getElementById('cancelBtn');

// =====================
// Event Listeners
// =====================
fetchBtn.addEventListener('click', handleFetch);

urlInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') handleFetch();
});

urlInput.addEventListener('paste', () => {
  setTimeout(() => {
    const val = urlInput.value.trim();
    if (val && isValidUrl(val)) handleFetch();
  }, 50);
});

pasteBtn.addEventListener('click', async () => {
  try {
    const text = await navigator.clipboard.readText();
    if (text) {
      urlInput.value = text.trim();
      urlInput.focus();
      if (isValidUrl(text.trim())) handleFetch();
    }
  } catch {
    urlInput.focus();
  }
});

tryAgainBtn.addEventListener('click', () => {
  hideAll();
  urlInput.focus();
});

downloadBtn.addEventListener('click', handleDownload);
cancelBtn.addEventListener('click', handleCancel);

document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'v') {
    setTimeout(() => {
      if (document.activeElement !== urlInput) return;
      const val = urlInput.value.trim();
      if (val && isValidUrl(val)) handleFetch();
    }, 100);
  }
});

window.addEventListener('DOMContentLoaded', () => urlInput.focus());

// =====================
// Helpers
// =====================
function isValidUrl(str) {
  try { new URL(str); return true; } catch { return false; }
}

function formatFileSize(bytes) {
  if (!bytes) return '';
  const mb = bytes / (1024 * 1024);
  if (mb < 1)    return `${(bytes / 1024).toFixed(0)} KB`;
  if (mb < 1024) return `${mb.toFixed(1)} MB`;
  return `${(mb / 1024).toFixed(2)} GB`;
}

function formatViewCount(count) {
  if (!count) return '';
  if (count >= 1e9) return `${(count / 1e9).toFixed(1)}B görüntülenme`;
  if (count >= 1e6) return `${(count / 1e6).toFixed(1)}M görüntülenme`;
  if (count >= 1e3) return `${(count / 1e3).toFixed(0)}K görüntülenme`;
  return `${count} görüntülenme`;
}

function getQualityBadge(label, height) {
  if (label === 'Audio Only (MP3)') return { text: 'MP3', cls: 'badge-audio' };
  if (height >= 2160) return { text: '4K',  cls: 'badge-4k' };
  if (height >= 1440) return { text: '2K',  cls: 'badge-4k' };
  if (height >= 1080) return { text: 'FHD', cls: '' };
  if (height >= 720)  return { text: 'HD',  cls: '' };
  return null;
}

// =====================
// UI State
// =====================
function hideAll() {
  loadingCard.classList.add('hidden');
  errorCard.classList.add('hidden');
  videoCard.classList.add('hidden');
}

function showLoading() {
  hideAll();
  loadingCard.classList.remove('hidden');
  fetchBtn.classList.add('loading');
  fetchBtn.querySelector('.btn-text').textContent = 'Analiz ediliyor...';
}

function showError(msg) {
  hideAll();
  errorText.textContent = msg;
  errorCard.classList.remove('hidden');
  fetchBtn.classList.remove('loading');
  fetchBtn.querySelector('.btn-text').textContent = 'Analiz Et';
}

function showVideoCard(info) {
  hideAll();
  fetchBtn.classList.remove('loading');
  fetchBtn.querySelector('.btn-text').textContent = 'Analiz Et';

  if (info.thumbnail) {
    thumbnail.style.display = '';
    thumbnail.src = info.thumbnail;
    thumbnail.onerror = () => { thumbnail.style.display = 'none'; };
  } else {
    thumbnail.style.display = 'none';
  }

  durationBadge.textContent = info.duration || '';
  videoTitle.textContent = info.title || 'Başlıksız Video';

  if (info.uploader) {
    videoUploader.textContent = info.uploader;
    videoUploader.style.display = '';
    detailSep.style.display = '';
  } else {
    videoUploader.style.display = 'none';
    detailSep.style.display = 'none';
  }

  const views = formatViewCount(info.viewCount);
  if (views) {
    videoViews.textContent = views;
    videoViews.style.display = '';
  } else {
    videoViews.style.display = 'none';
  }

  buildFormatGrid(info.formats || []);

  // Reset download state
  downloadBtn.classList.add('hidden');
  downloadBtn.classList.remove('downloading');
  progressBar.style.width = '0%';
  progressBar.style.background = '';
  progressInfo.classList.add('hidden');
  selectedFormat    = null;
  isDownloading     = false;
  currentSessionId  = null;

  videoCard.classList.remove('hidden');
  videoCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// =====================
// Format Grid
// =====================
function buildFormatGrid(formats) {
  formatGrid.innerHTML = '';

  if (formats.length === 0) {
    formatGrid.innerHTML = '<p style="color:var(--text-muted);font-size:13px;grid-column:1/-1">Uygun format bulunamadı</p>';
    return;
  }

  formats.forEach((fmt, index) => {
    const card = document.createElement('div');
    card.className = 'format-card';
    card.dataset.index = index;

    const badge      = getQualityBadge(fmt.label, fmt.height);
    const sizeStr    = formatFileSize(fmt.filesize);
    const extDisplay = (fmt.ext || 'MP4').toUpperCase();
    const labelDisplay = fmt.isAudioOnly ? '🎵 MP3' : fmt.label;

    card.innerHTML = `
      <div class="format-label">${labelDisplay}</div>
      <div class="format-ext">${extDisplay}</div>
      ${sizeStr ? `<div class="format-size">${sizeStr}</div>` : ''}
      ${badge ? `<div class="format-badge ${badge.cls}">${badge.text}</div>` : ''}
      <div class="format-check">
        <svg viewBox="0 0 24 24" fill="none">
          <polyline points="20 6 9 17 4 12"/>
        </svg>
      </div>
    `;

    card.addEventListener('click', () => selectFormat(index, card, fmt));
    formatGrid.appendChild(card);
  });

  // Auto-select best quality (first non-audio)
  const firstVideo = formats.findIndex(f => !f.isAudioOnly);
  if (firstVideo !== -1) {
    const firstCard = formatGrid.querySelector(`[data-index="${firstVideo}"]`);
    if (firstCard) selectFormat(firstVideo, firstCard, formats[firstVideo]);
  }
}

function selectFormat(index, card, fmt) {
  formatGrid.querySelectorAll('.format-card').forEach(c => c.classList.remove('selected'));
  card.classList.add('selected');
  selectedFormat = fmt;

  downloadBtn.classList.remove('hidden');
  const qualStr = fmt.isAudioOnly
    ? 'MP3 Ses İndir'
    : `${fmt.label} ${(fmt.ext || 'MP4').toUpperCase()} İndir`;
  downloadBtnText.textContent = qualStr;
}

// =====================
// Fetch Video Info
// =====================
async function handleFetch() {
  const url = urlInput.value.trim();

  if (!url) {
    urlInput.parentElement.style.borderColor = 'rgba(239,68,68,0.5)';
    setTimeout(() => { urlInput.parentElement.style.borderColor = ''; }, 1500);
    urlInput.focus();
    return;
  }

  if (!isValidUrl(url)) {
    showError('Geçersiz URL. Lütfen tam bir video linkini yapıştırın (https:// ile başlamalı).');
    return;
  }

  showLoading();

  try {
    const res  = await fetch('/api/info', {
      method : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body   : JSON.stringify({ url })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Video bilgisi alınamadı');
    currentVideoInfo = data;
    showVideoCard(data);
  } catch (err) {
    if (err.name === 'TypeError' && err.message.includes('fetch')) {
      showError('Sunucuya bağlanılamadı. Sunucunun çalıştığından emin olun.');
    } else {
      showError(err.message || 'Beklenmeyen bir hata oluştu.');
    }
  }
}

// =====================
// Download
// =====================
async function handleDownload() {
  if (!selectedFormat || !currentVideoInfo || isDownloading) return;

  const url = urlInput.value.trim();
  if (!url) return;

  isDownloading    = true;
  currentSessionId = null;

  downloadBtn.classList.add('downloading');
  downloadBtnText.textContent = 'Hazırlanıyor...';
  progressInfo.classList.remove('hidden');
  progressPercent.textContent = '0%';
  progressSpeed.textContent   = '';
  progressEta.textContent     = '';
  progressStatus.textContent  = selectedFormat.isAudioOnly ? 'Ses indiriliyor...' : 'Video indiriliyor...';
  progressBar.style.width     = '0%';
  progressBar.style.background= '';

  if (progressEventSource) { progressEventSource.close(); progressEventSource = null; }

  try {
    const res = await fetch('/api/download-stream', {
      method : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body   : JSON.stringify({
        url,
        formatId: selectedFormat.formatId,
        height  : selectedFormat.height || 0,
        label   : selectedFormat.label,
        title   : currentVideoInfo.title
      })
    });

    if (!res.ok) {
      const d = await res.json();
      throw new Error(d.error || 'İndirme başlatılamadı');
    }

    const { sessionId } = await res.json();
    currentSessionId = sessionId;
    downloadBtnText.textContent = 'İndiriliyor...';

    progressEventSource = new EventSource(`/api/progress/${sessionId}`);

    progressEventSource.onmessage = (event) => {
      let data;
      try { data = JSON.parse(event.data); } catch { return; }

      if (data.status === 'downloading') {
        const pct = Math.min(Math.round(data.percent || 0), 99);
        progressBar.style.width       = `${pct}%`;
        progressPercent.textContent   = `${pct}%`;
        if (data.speed) progressSpeed.textContent = data.speed;
        if (data.eta)   progressEta.textContent   = `ETA ${data.eta}`;
      }

      if (data.status === 'merging') {
        progressBar.style.width     = '99%';
        progressPercent.textContent = '99%';
        progressStatus.textContent  = 'Video+ses birleştiriliyor (ffmpeg)...';
        progressSpeed.textContent   = '';
        progressEta.textContent     = '';
      }

      if (data.status === 'done' && data.fileReady) {
        progressBar.style.width     = '100%';
        progressPercent.textContent = '100%';
        progressStatus.textContent  = 'Hazır! İndirme başlıyor...';
        progressSpeed.textContent   = '';
        progressEta.textContent     = '';
        downloadBtnText.textContent = '✓ Tamamlandı!';

        progressEventSource.close();
        progressEventSource = null;

        // Build ASCII-safe filename — embed in URL path so browser saves with correct name
        const ext = selectedFormat.ext || 'mp4';
        const safeTitle = (currentVideoInfo.title || 'video')
          .replace(/[^\x20-\x7E]/g, '')          // strip non-ASCII (Turkish etc)
          .replace(/[^a-zA-Z0-9 \-_.()]/g, '_')  // replace special chars
          .replace(/\s+/g, '_')
          .replace(/_+/g, '_')
          .replace(/^_+|_+$/g, '')
          .substring(0, 80) || 'video';
        const filename = `${safeTitle}.${ext}`;

        // /api/file/<sessionId>/<filename> — browser uses filename from URL path
        window.location.href = `/api/file/${sessionId}/${encodeURIComponent(filename)}`;

        setTimeout(resetDownloadState, 4000);
      }

      if (data.status === 'cancelled') {
        progressEventSource?.close();
        progressEventSource  = null;
        currentSessionId     = null;
        progressStatus.textContent  = 'İndirme iptal edildi.';
        progressPercent.textContent = '';
        progressBar.style.width     = '0%';
        setTimeout(resetDownloadState, 2000);
      }

      if (data.status === 'error') {
        progressEventSource?.close();
        progressEventSource = null;
        handleDownloadError(data.message || 'İndirme başarısız');
      }
    };

    progressEventSource.onerror = () => {
      progressEventSource?.close();
      progressEventSource = null;
      // only show error if not already done/cancelled
      if (isDownloading && progressPercent.textContent !== '100%') {
        handleDownloadError('Bağlantı kesildi');
      }
    };

  } catch (err) {
    handleDownloadError(err.message);
  }
}

// =====================
// Cancel
// =====================
async function handleCancel() {
  if (!currentSessionId) return;

  const sid        = currentSessionId;
  currentSessionId = null;

  progressEventSource?.close();
  progressEventSource = null;

  downloadBtnText.textContent = 'İptal ediliyor...';
  progressStatus.textContent  = 'İptal ediliyor...';

  try {
    await fetch(`/api/cancel/${sid}`, { method: 'POST' });
  } catch {}

  progressStatus.textContent  = 'İndirme iptal edildi.';
  progressPercent.textContent = '';
  progressBar.style.width     = '0%';
  setTimeout(resetDownloadState, 2000);
}

// =====================
// Error / Reset
// =====================
function handleDownloadError(msg) {
  isDownloading = false;
  downloadBtn.classList.remove('downloading');
  downloadBtnText.textContent = 'Tekrar Dene';
  progressStatus.textContent  = `Hata: ${msg}`;
  progressPercent.textContent = '';
  progressBar.style.width     = '0%';
  progressBar.style.background= 'rgba(239,68,68,0.5)';

  setTimeout(() => {
    progressBar.style.background = '';
    isDownloading = false;
    downloadBtn.classList.remove('downloading');
    if (selectedFormat) {
      const q = selectedFormat.isAudioOnly
        ? 'MP3 Ses İndir'
        : `${selectedFormat.label} ${(selectedFormat.ext||'MP4').toUpperCase()} İndir`;
      downloadBtnText.textContent = q;
    }
  }, 3000);
}

function resetDownloadState() {
  isDownloading = false;
  downloadBtn.classList.remove('downloading');
  progressInfo.classList.add('hidden');
  if (selectedFormat) {
    const q = selectedFormat.isAudioOnly
      ? 'MP3 Ses İndir'
      : `${selectedFormat.label} ${(selectedFormat.ext||'MP4').toUpperCase()} İndir`;
    downloadBtnText.textContent = q;
  }
}
