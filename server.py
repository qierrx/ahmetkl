# -*- coding: utf-8 -*-
import os
import sys
import json
import uuid
import subprocess
import threading
import time
import re
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, Response, send_file, stream_with_context

# Fix stdout encoding for Windows
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Find yt-dlp executable (may not be on PATH)
import shutil, site, glob as _glob

def _find_ytdlp():
    # 1. Check PATH first
    found = shutil.which('yt-dlp') or shutil.which('yt-dlp.exe')
    if found:
        return found
    # 2. Check all known Python script dirs
    search_dirs = []
    try:
        search_dirs += site.getsitepackages()
    except Exception:
        pass
    try:
        search_dirs.append(site.getusersitepackages())
    except Exception:
        pass
    for sp in search_dirs:
        for candidate in [
            Path(sp).parent / 'Scripts' / 'yt-dlp.exe',
            Path(sp) / 'Scripts' / 'yt-dlp.exe',
        ]:
            if candidate.exists():
                return str(candidate)
    # 3. Glob search under AppData\Local\Python
    import os
    appdata = os.environ.get('LOCALAPPDATA', '')
    if appdata:
        matches = list(Path(appdata).glob('Python**/Scripts/yt-dlp.exe'))
        if matches:
            return str(matches[0])
    # 4. Glob search under AppData\Roaming\Python
    roaming = os.environ.get('APPDATA', '')
    if roaming:
        matches = list(Path(roaming).glob('Python**/Scripts/yt-dlp.exe'))
        if matches:
            return str(matches[0])
    return 'yt-dlp'  # last resort fallback

YTDLP = _find_ytdlp()

# Find ffmpeg (needed for merging video+audio streams)
import os as _os
def _find_ffmpeg():
    found = shutil.which('ffmpeg') or shutil.which('ffmpeg.exe')
    if found:
        return str(Path(found).parent)
    # winget installs here (Windows)
    appdata = _os.environ.get('LOCALAPPDATA', '')
    if appdata:
        matches = list(Path(appdata).glob('**/bin/ffmpeg.exe'))
        if matches:
            return str(matches[0].parent)
        matches = list(Path(appdata).glob('**/ffmpeg.exe'))
        if matches:
            return str(matches[0].parent)
    return None

FFMPEG_DIR = _find_ffmpeg()

app = Flask(__name__, static_folder='public', static_url_path='')

# Use /tmp on Linux cloud servers, local folder on Windows
import tempfile
if _os.name == 'nt':  # Windows
    TEMP_DIR = Path(__file__).parent / 'temp_downloads'
else:  # Linux (cloud)
    TEMP_DIR = Path(tempfile.gettempdir()) / 'vidget_downloads'
TEMP_DIR.mkdir(exist_ok=True)

PORT = int(_os.environ.get('PORT', 3000))

# ── Cookie / Auth setup ───────────────────────────────────────────────────────
# Priority:
#   1. YOUTUBE_COOKIES env variable (Render → Environment → paste cookies text)
#   2. cookies.txt file in any of several well-known locations
#   3. Fall back to android/tv_embedded client (no cookies)

import base64 as _b64

def _resolve_cookies_file():
    """Write cookies to a temp file if needed and return its path, or None."""
    # Option 1: environment variable (plain text cookies)
    env_cookies = _os.environ.get('YOUTUBE_COOKIES', '')
    if env_cookies.strip():
        # Render/some UIs store literal \n instead of real newlines — fix that
        env_cookies = env_cookies.replace('\\n', '\n').replace('\\t', '\t')
        p = TEMP_DIR / 'yt_cookies.txt'
        p.write_text(env_cookies.strip(), encoding='utf-8')
        lines = len([l for l in env_cookies.splitlines() if l.strip()])
        print(f'[OK] cookies: loaded from YOUTUBE_COOKIES env var ({lines} lines)')
        return p

    # Option 2: environment variable (base64 encoded)
    env_b64 = _os.environ.get('YOUTUBE_COOKIES_B64', '')
    if env_b64.strip():
        try:
            decoded = _b64.b64decode(env_b64).decode('utf-8')
            p = TEMP_DIR / 'yt_cookies.txt'
            p.write_text(decoded, encoding='utf-8')
            print('[OK] cookies: loaded from YOUTUBE_COOKIES_B64 env var')
            return p
        except Exception as e:
            print(f'[WARN] cookies: YOUTUBE_COOKIES_B64 decode failed: {e}')

    # Option 3: look for cookies.txt in various locations
    candidates = [
        Path(__file__).parent / 'cookies.txt',          # app root (works locally + Render)
        Path('/etc/secrets/cookies.txt'),                 # Render Docker secret mount
        Path('/opt/render/project/src/cookies.txt'),     # Render managed service
        Path('/tmp/cookies.txt'),                         # generic tmp
    ]
    for p in candidates:
        if p.exists() and p.stat().st_size > 100:
            print(f'[OK] cookies: found at {p}')
            return p

    print('[WARN] cookies: no cookies file found — using android client only')
    return None

COOKIES_FILE = _resolve_cookies_file()


def _get_auth_args():
    """Return best available yt-dlp auth args to bypass YouTube bot detection."""
    base = ['--extractor-args', 'youtube:player_client=android,tv_embedded,web']

    if _os.name == 'nt':
        # Windows: auto-read cookies from installed browser (no manual work!)
        for browser in ['chrome', 'edge', 'firefox', 'brave']:
            browser_path = shutil.which(browser) or shutil.which(browser + '.exe')
            if not browser_path:
                known = {
                    'chrome': [r'C:\Program Files\Google\Chrome\Application\chrome.exe',
                               r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'],
                    'edge':   [r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
                               r'C:\Program Files\Microsoft\Edge\Application\msedge.exe'],
                    'firefox':[r'C:\Program Files\Mozilla Firefox\firefox.exe',
                               r'C:\Program Files (x86)\Mozilla Firefox\firefox.exe'],
                }
                for p in known.get(browser, []):
                    if Path(p).exists():
                        browser_path = p
                        break
            if browser_path:
                return base + ['--cookies-from-browser', browser]

    # Linux (Render/cloud) OR Windows without browser found:
    if COOKIES_FILE and COOKIES_FILE.exists():
        return base + ['--cookies', str(COOKIES_FILE)]

    return base  # android client only, no cookies

progress_store = {}  # sessionId -> list of events
process_store  = {}  # sessionId -> Popen (for cancel)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def format_duration(seconds):
    if not seconds:
        return ''
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

def format_filesize(size):
    if not size:
        return ''
    mb = size / (1024 * 1024)
    if mb < 1:
        return f"{size/1024:.0f} KB"
    if mb < 1024:
        return f"{mb:.1f} MB"
    return f"{mb/1024:.2f} GB"

def parse_formats(info):
    formats = info.get('formats', [])
    quality_map = {}

    for fmt in formats:
        ext = fmt.get('ext', '')
        height = fmt.get('height')
        fps = fmt.get('fps')
        vcodec = fmt.get('vcodec', 'none') or 'none'
        acodec = fmt.get('acodec', 'none') or 'none'
        filesize = fmt.get('filesize') or fmt.get('filesize_approx')
        format_id = fmt.get('format_id', '')
        tbr = fmt.get('tbr', 0) or 0

        has_video = vcodec and vcodec != 'none'
        has_audio = acodec and acodec != 'none'

        if not has_video or not height:
            continue

        fps_int = int(fps) if fps else 30
        label = f"{height}p{fps_int if fps_int > 30 else ''}"

        # H.264 = universally compatible. VP9/AV1/HEVC need extra codecs on Windows.
        is_h264 = 'avc' in vcodec.lower() or 'h264' in vcodec.lower()
        entry = {
            'formatId': format_id,
            'label': label,
            'height': height,
            'fps': fps_int,
            'ext': 'mp4',
            'filesize': filesize,
            'tbr': tbr,
            'hasAudio': has_audio,
            'vcodec': vcodec,
            'is_h264': is_h264,
        }

        if label not in quality_map:
            quality_map[label] = entry
        else:
            existing = quality_map[label]
            existing_h264 = existing.get('is_h264', False)
            # Prefer H.264 over other codecs; within same codec prefer higher bitrate
            if (is_h264 and not existing_h264) or \
               (is_h264 == existing_h264 and tbr > existing.get('tbr', 0)):
                quality_map[label] = entry

    # Sort by quality desc
    sorted_qualities = sorted(quality_map.values(), key=lambda x: x['height'], reverse=True)

    # Best audio only
    best_audio = None
    for fmt in formats:
        vcodec = fmt.get('vcodec', 'none')
        acodec = fmt.get('acodec', 'none')
        if vcodec == 'none' and acodec and acodec != 'none':
            abr = fmt.get('abr', 0) or 0
            if best_audio is None or abr > (best_audio.get('abr', 0) or 0):
                best_audio = fmt

    audio_option = []
    if best_audio:
        audio_option = [{
            'formatId': 'bestaudio',
            'label': 'Audio Only (MP3)',
            'height': 0,
            'ext': 'mp3',
            'filesize': best_audio.get('filesize') or best_audio.get('filesize_approx'),
            'isAudioOnly': True
        }]

    return sorted_qualities + audio_option

# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('public', 'index.html')

@app.route('/api/debug')
def debug_info():
    """Cookie durumunu ve ayarları göster — Render log kontrolü için."""
    cookie_info = {'found': False, 'path': None, 'lines': 0, 'first_line': ''}
    if COOKIES_FILE and COOKIES_FILE.exists():
        content = COOKIES_FILE.read_text(encoding='utf-8', errors='replace')
        lines = [l for l in content.splitlines() if l.strip()]
        cookie_info = {
            'found': True,
            'path': str(COOKIES_FILE),
            'lines': len(lines),
            'first_line': lines[0] if lines else ''
        }
    return jsonify({
        'cookies': cookie_info,
        'os': _os.name,
        'ytdlp': YTDLP,
        'ffmpeg': FFMPEG_DIR,
        'env_vars': {
            'YOUTUBE_COOKIES': bool(_os.environ.get('YOUTUBE_COOKIES')),
            'YOUTUBE_COOKIES_B64': bool(_os.environ.get('YOUTUBE_COOKIES_B64')),
        }
    })

@app.route('/api/info', methods=['POST'])
def get_info():
    data = request.get_json()
    url = (data or {}).get('url', '').strip()

    if not url:
        return jsonify({'error': 'URL gerekli'}), 400

    try:
        cmd = [
            YTDLP,
            '--dump-json', '--no-playlist', '--no-warnings',
        ] + _get_auth_args() + [url]
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=30, encoding='utf-8', errors='replace'
        )
        if result.returncode != 0:
            err = result.stderr.strip().split('\n')[0] if result.stderr else 'Bilinmeyen hata'
            if 'Unsupported URL' in err:
                return jsonify({'error': 'Bu URL desteklenmiyor.'}), 400
            return jsonify({'error': f'Video bilgisi alınamadı: {err}'}), 500

        info = json.loads(result.stdout.strip())

        return jsonify({
            'title': info.get('title', 'Başlıksız Video'),
            'thumbnail': info.get('thumbnail', ''),
            'duration': format_duration(info.get('duration', 0)),
            'uploader': info.get('uploader') or info.get('channel', ''),
            'viewCount': info.get('view_count', 0),
            'formats': parse_formats(info)
        })

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Zaman aşımı — video bilgisi alınamadı'}), 500
    except FileNotFoundError:
        return jsonify({'error': 'yt-dlp kurulu değil. setup.bat dosyasını çalıştırın.'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/download-stream', methods=['POST'])
def download_stream():
    data = request.get_json()
    url       = (data or {}).get('url', '').strip()
    format_id = (data or {}).get('formatId', 'best')
    height    = int((data or {}).get('height', 0) or 0)
    label     = (data or {}).get('label', 'video')
    title     = (data or {}).get('title', 'video')

    if not url or not format_id:
        return jsonify({'error': 'URL ve format gerekli'}), 400

    session_id  = str(uuid.uuid4())
    output_path = TEMP_DIR / f"vid_{session_id}.%(ext)s"

    # Build yt-dlp args
    is_youtube = 'youtube.com' in url or 'youtu.be' in url

    if format_id == 'bestaudio':
        args = [
            YTDLP,
            '-f', 'bestaudio',
            '-x', '--audio-format', 'mp3', '--audio-quality', '0',
            '--no-playlist', '--newline',
            '-o', str(output_path),
            url
        ]
    elif is_youtube:
        # YouTube: smart H.264 format selection (no re-encoding needed)
        h = height if height > 0 else 2160
        format_str = (
            f'bestvideo[vcodec*=avc][height={h}]+bestaudio[ext=m4a]'
            f'/bestvideo[vcodec*=avc][height={h}]+bestaudio'
            f'/bestvideo[height={h}]+bestaudio[ext=m4a]'
            f'/bestvideo[height={h}]+bestaudio'
            f'/bestvideo[vcodec*=avc][height<={h}]+bestaudio[ext=m4a]'
            f'/bestvideo[vcodec*=avc][height<={h}]+bestaudio'
            f'/bestvideo[height<={h}]+bestaudio'
            f'/best'
        )
        args = [
            YTDLP,
            '-f', format_str,
            '--merge-output-format', 'mp4',
            '--no-playlist', '--newline',
            '-o', str(output_path),
            url
        ]
    else:
        # TikTok / Instagram / Twitter / diğer siteler:
        # H.264 formatı tercih et; yoksa ffmpeg ile H.264'e dönüştür
        h = height if height > 0 else 2160
        format_str = (
            f'bestvideo[vcodec*=avc][height<={h}]+bestaudio'
            f'/bestvideo[height<={h}]+bestaudio'
            f'/best[height<={h}]'
            f'/best'
        )
        args = [
            YTDLP,
            '-f', format_str,
            '--merge-output-format', 'mp4',
            # Force H.264 re-encode — fixes HEVC/VP9 codec uyumsuzluğunu
            '--postprocessor-args',
            'ffmpeg:-c:v libx264 -preset fast -crf 23 -c:a aac -movflags +faststart',
            '--no-playlist', '--newline',
            '-o', str(output_path),
            url
        ]

    # Pass ffmpeg location (needed for merging streams)
    if FFMPEG_DIR:
        args += ['--ffmpeg-location', FFMPEG_DIR]

    # Auth: auto picks best method (browser cookies on Win, cookies.txt on Linux)
    args += _get_auth_args()

    # Initialize progress store
    progress_store[session_id] = []

    def run_download():
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding='utf-8',
                errors='replace'
            )
            process_store[session_id] = proc

            for line in proc.stdout:
                line = line.strip()
                # Parse download progress
                m = re.search(r'\[download\]\s+([\d.]+)%\s+of.*?at\s+([\S]+)\s+ETA\s+(\S+)', line)
                if m:
                    pct = float(m.group(1))
                    speed = m.group(2)
                    eta = m.group(3)
                    progress_store[session_id].append({
                        'percent': pct, 'speed': speed, 'eta': eta, 'status': 'downloading'
                    })
                    continue

                m2 = re.search(r'\[download\]\s+([\d.]+)%', line)
                if m2:
                    pct = float(m2.group(1))
                    progress_store[session_id].append({'percent': pct, 'status': 'downloading'})
                    continue

                if '[Merger]' in line or '[ffmpeg]' in line:
                    progress_store[session_id].append({'percent': 99, 'status': 'merging'})

            proc.wait()
            process_store.pop(session_id, None)

            if proc.returncode == 0:
                files = list(TEMP_DIR.glob(f"vid_{session_id}.*"))
                if files:
                    progress_store[session_id].append({
                        'percent': 100,
                        'status': 'done',
                        'fileReady': True,
                        'sessionId': session_id
                    })
                else:
                    progress_store[session_id].append({'status': 'error', 'message': 'Dosya bulunamadi'})
            elif proc.returncode == -15 or proc.returncode == 1:
                # -15 = SIGTERM (cancelled), don't add error if already cancelled
                if not any(e.get('status') == 'cancelled' for e in progress_store.get(session_id, [])):
                    progress_store[session_id].append({'status': 'error', 'message': 'Indirme basarisiz'})
            else:
                progress_store[session_id].append({'status': 'error', 'message': 'Indirme basarisiz'})

        except FileNotFoundError:
            process_store.pop(session_id, None)
            progress_store[session_id].append({'status': 'error', 'message': 'yt-dlp kurulu degil'})
        except Exception as e:
            process_store.pop(session_id, None)
            progress_store[session_id].append({'status': 'error', 'message': str(e)})

    t = threading.Thread(target=run_download, daemon=True)
    t.start()

    return jsonify({'sessionId': session_id})


@app.route('/api/progress/<session_id>')
def progress(session_id):
    def generate():
        sent = 0
        while True:
            events = progress_store.get(session_id, [])
            while sent < len(events):
                evt = events[sent]
                yield f"data: {json.dumps(evt)}\n\n"
                sent += 1
                if evt.get('status') in ('done', 'error'):
                    # Clean up store after a delay
                    def cleanup():
                        time.sleep(30)
                        progress_store.pop(session_id, None)
                    threading.Thread(target=cleanup, daemon=True).start()
                    return
            time.sleep(0.3)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )


@app.route('/api/cancel/<session_id>', methods=['POST', 'DELETE'])
def cancel_download(session_id):
    safe_id = re.sub(r'[^a-zA-Z0-9\-]', '', session_id)
    proc = process_store.pop(safe_id, None)
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass
    if safe_id in progress_store:
        progress_store[safe_id].append({'status': 'cancelled', 'message': 'Indirme iptal edildi'})
    # Clean up temp files
    for f in TEMP_DIR.glob(f"vid_{safe_id}.*"):
        try: f.unlink()
        except: pass
    return jsonify({'ok': True})


@app.route('/api/file/<session_id>/<path:filename>')
def serve_file_named(session_id, filename):
    """Primary download route — filename embedded in URL so browser saves correctly."""
    safe_id = re.sub(r'[^a-zA-Z0-9\-]', '', session_id)
    files = list(TEMP_DIR.glob(f"vid_{safe_id}.*"))

    if not files:
        return jsonify({'error': 'Dosya bulunamadi'}), 404

    file_path = files[0]
    ext = file_path.suffix.lstrip('.')

    mime_map = {
        'mp4': 'video/mp4',
        'webm': 'video/webm',
        'mkv': 'video/x-matroska',
        'mp3': 'audio/mpeg',
        'm4a': 'audio/mp4',
    }
    mimetype = mime_map.get(ext.lower(), 'application/octet-stream')

    def cleanup_after():
        time.sleep(15)
        try: file_path.unlink()
        except: pass

    threading.Thread(target=cleanup_after, daemon=True).start()

    return send_file(
        file_path,
        as_attachment=True,
        download_name=filename,
        mimetype=mimetype
    )


@app.route('/api/file/<session_id>')
def serve_file(session_id):
    """Fallback route (redirects to named route)."""
    safe_id = re.sub(r'[^a-zA-Z0-9\-]', '', session_id)
    files = list(TEMP_DIR.glob(f"vid_{safe_id}.*"))
    if not files:
        return jsonify({'error': 'Dosya bulunamadi'}), 404
    ext = files[0].suffix.lstrip('.')
    title = request.args.get('title', 'video')
    safe_title = re.sub(r'[^\w\s\-_().]', '', title).strip() or 'video'
    from flask import redirect
    return redirect(f'/api/file/{safe_id}/{safe_title}.{ext}')




# Cleanup old temp files (>1hr)
def cleanup_old_files():
    while True:
        time.sleep(300)
        now = time.time()
        for f in TEMP_DIR.iterdir():
            try:
                if now - f.stat().st_mtime > 3600:
                    f.unlink()
            except Exception:
                pass

threading.Thread(target=cleanup_old_files, daemon=True).start()


if __name__ == '__main__':
    print("\nVidGet - Video Downloader Server")
    print(f"[OK] http://localhost:{PORT} adresinde calisiyor")
    print(f"[OK] yt-dlp : {YTDLP}")
    print(f"[OK] ffmpeg : {FFMPEG_DIR if FFMPEG_DIR else 'BULUNAMADI'}")
    print(f"[OK] temp   : {TEMP_DIR}")
    print("Durdurmak icin: Ctrl+C\n")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
