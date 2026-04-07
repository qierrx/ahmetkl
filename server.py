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

# ── Find yt-dlp ───────────────────────────────────────────────────────────────
import shutil, site, glob as _glob

def _find_ytdlp():
    return sys.executable

YTDLP = sys.executable
YTDLP_ARGS = ['-m', 'yt_dlp', '--ignore-config']

# ── Find ffmpeg ────────────────────────────────────────────────────────────────
import os as _os

def _find_ffmpeg():
    found = shutil.which('ffmpeg') or shutil.which('ffmpeg.exe')
    if found:
        return str(Path(found).parent)
    appdata = _os.environ.get('LOCALAPPDATA', '')
    if appdata:
        matches = list(Path(appdata).glob('**/bin/ffmpeg.exe'))
        if matches:
            return str(matches[0].parent)
        matches = list(Path(appdata).glob('**/ffmpeg.exe'))
        if matches:
            return str(matches[0].parent)
    # Check common Windows paths
    common_paths = [
        Path('C:/ffmpeg/bin'),
        Path('C:/Program Files/ffmpeg/bin'),
        Path('C:/Program Files (x86)/ffmpeg/bin'),
    ]
    for p in common_paths:
        if (p / 'ffmpeg.exe').exists():
            return str(p)
    return None

FFMPEG_DIR = _find_ffmpeg()

app = Flask(__name__, static_folder='public', static_url_path='')

# Temp directory
import tempfile
if _os.name == 'nt':
    TEMP_DIR = Path(__file__).parent / 'temp_downloads'
else:
    TEMP_DIR = Path(tempfile.gettempdir()) / 'vidget_downloads'
TEMP_DIR.mkdir(exist_ok=True)

PORT = int(_os.environ.get('PORT', 3000))

# ── Cookie / Auth setup ───────────────────────────────────────────────────────
import base64 as _b64

def _resolve_cookies_file():
    env_cookies = _os.environ.get('YOUTUBE_COOKIES', '')
    if env_cookies.strip():
        env_cookies = env_cookies.replace('\\n', '\n').replace('\\t', '\t')
        p = TEMP_DIR / 'yt_cookies.txt'
        p.write_text(env_cookies.strip(), encoding='utf-8')
        lines = len([l for l in env_cookies.splitlines() if l.strip()])
        print(f'[OK] cookies: loaded from YOUTUBE_COOKIES env var ({lines} lines)')
        return p

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

    candidates = [
        Path(__file__).parent / 'cookies.txt',
        Path('/etc/secrets/cookies.txt'),
        Path('/opt/render/project/src/cookies.txt'),
        Path('/tmp/cookies.txt'),
    ]
    for p in candidates:
        if p.exists() and p.stat().st_size > 100:
            print(f'[OK] cookies: found at {p}')
            return p

    print('[INFO] cookies: no cookies file found — using android/tv_embedded client')
    return None

COOKIES_FILE = _resolve_cookies_file()


def _get_auth_args():
    """Return best available yt-dlp auth args."""
    base = ['--extractor-args', 'youtube:player_client=default', '--no-cookies-from-browser']
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        return base + ['--cookies', str(COOKIES_FILE)]
    return base


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
            if (is_h264 and not existing_h264) or \
               (is_h264 == existing_h264 and tbr > existing.get('tbr', 0)):
                quality_map[label] = entry

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
# CORS — allow local network Android devices
# ─────────────────────────────────────────────
@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,DELETE,OPTIONS'
    return response

@app.route('/api/options', methods=['OPTIONS'])
def options():
    return '', 204

# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('public', 'index.html')

@app.route('/api/status')
def status():
    """Health check + tool info for frontend."""
    return jsonify({
        'ok': True,
        'ytdlp': YTDLP != 'yt-dlp' or bool(shutil.which('yt-dlp')),
        'ffmpeg': FFMPEG_DIR is not None,
        'ytdlpPath': YTDLP,
        'ffmpegPath': FFMPEG_DIR,
    })

@app.route('/api/debug')
def debug_info():
    cookie_info = {'found': False, 'path': None, 'lines': 0}
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        content = Path(COOKIES_FILE).read_text(encoding='utf-8', errors='replace')
        lines = [l for l in content.splitlines() if l.strip()]
        cookie_info = {
            'found': True,
            'path': str(COOKIES_FILE),
            'lines': len(lines),
        }
    return jsonify({
        'cookies': cookie_info,
        'os': _os.name,
        'ytdlp': YTDLP,
        'ffmpeg': FFMPEG_DIR,
    })

@app.route('/api/info', methods=['POST'])
def get_info():
    data = request.get_json()
    url = (data or {}).get('url', '').strip()

    if not url:
        return jsonify({'error': 'URL gerekli'}), 400

    try:
        cmd = [YTDLP] + YTDLP_ARGS + [
            '--dump-json', '--no-playlist', '--no-warnings',
            '--socket-timeout', '15',
        ] + _get_auth_args() + [url]

        print("Executing command:", cmd, flush=True)

        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=60,
            encoding='utf-8', errors='replace'
        )

        if result.returncode != 0:
            stderr = result.stderr.strip() if result.stderr else ''
            stdout = result.stdout.strip() if result.stdout else ''
            err_text = stderr or stdout or 'Bilinmeyen hata'
            first_line = err_text.split('\n')[0]

            if 'Unsupported URL' in err_text:
                return jsonify({'error': 'Bu URL desteklenmiyor. YouTube, TikTok, Twitter gibi siteleri deneyin.'}), 400
            if 'Video unavailable' in err_text or 'This video is not available' in err_text:
                return jsonify({'error': 'Bu video mevcut değil veya erişim kısıtlı.'}), 400
            if 'Private video' in err_text:
                return jsonify({'error': 'Bu video gizli (private). İndirilemez.'}), 400
            if 'This content isn' in err_text or 'age-restricted' in err_text:
                return jsonify({'error': 'Bu video yaş kısıtlı. Cookies.txt ekleyerek deneyin.'}), 400
            if 'Sign in' in err_text or 'confirm your age' in err_text:
                return jsonify({'error': 'YouTube giriş istiyor. cookies.txt dosyası eklemek gerekiyor.'}), 400
            if 'HTTP Error 429' in err_text:
                return jsonify({'error': 'YouTube hız sınırına takıldınız. Birkaç dakika bekleyin.'}), 429

            return jsonify({'error': f'Video bilgisi alınamadı: {first_line}'}), 500

        # Parse JSON output (may have multiple lines for playlists)
        stdout = result.stdout.strip()
        # Take only the first JSON object (in case of playlist leak)
        json_line = stdout.split('\n')[0]
        info = json.loads(json_line)

        return jsonify({
            'title': info.get('title', 'Başlıksız Video'),
            'thumbnail': info.get('thumbnail', ''),
            'duration': format_duration(info.get('duration', 0)),
            'uploader': info.get('uploader') or info.get('channel', ''),
            'viewCount': info.get('view_count', 0),
            'formats': parse_formats(info)
        })

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Zaman aşımı — video bilgisi alınamadı. URL\'yi kontrol edin.'}), 500
    except FileNotFoundError:
        return jsonify({'error': 'yt-dlp bulunamadı! install.bat dosyasını çalıştırın.'}), 500
    except json.JSONDecodeError as e:
        return jsonify({'error': f'Video verisi çözümlenemedi: {e}'}), 500
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

    is_youtube = 'youtube.com' in url or 'youtu.be' in url

    if format_id == 'bestaudio':
        args = [YTDLP] + YTDLP_ARGS + [
            '-f', 'bestaudio/best',
            '-x', '--audio-format', 'mp3', '--audio-quality', '0',
            '--no-playlist', '--newline',
            '--socket-timeout', '15',
            '-o', str(output_path),
            url
        ]
    elif is_youtube:
        format_str = f'{format_id}+bestaudio[ext=m4a]/{format_id}+bestaudio/{format_id}/best'
        args = [YTDLP] + YTDLP_ARGS + [
            '-f', format_str,
            '--merge-output-format', 'mp4',
            '--no-playlist', '--newline',
            '--socket-timeout', '15',
            '-o', str(output_path),
            url
        ]
    else:
        # TikTok / Instagram / Twitter / other sites
        format_str = f'{format_id}+bestaudio/{format_id}/best'
        args = [YTDLP] + YTDLP_ARGS + [
            '-f', format_str,
            '--merge-output-format', 'mp4',
            '--no-playlist', '--newline',
            '--socket-timeout', '15',
            '-o', str(output_path),
            url
        ]

    if FFMPEG_DIR:
        args += ['--ffmpeg-location', FFMPEG_DIR]

    args += _get_auth_args()

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
                if not line:
                    continue

                # Pattern 1: [download] XX.X% of ~X.XXMiB at X.XXMiB/s ETA XX:XX
                m = re.search(r'\[download\]\s+([\d.]+)%\s+of\s+[\S]+\s+at\s+([\S]+)\s+ETA\s+(\S+)', line)
                if m:
                    pct = float(m.group(1))
                    speed = m.group(2)
                    eta = m.group(3)
                    progress_store[session_id].append({
                        'percent': pct, 'speed': speed, 'eta': eta, 'status': 'downloading'
                    })
                    continue

                # Pattern 2: [download] XX.X%
                m2 = re.search(r'\[download\]\s+([\d.]+)%', line)
                if m2:
                    pct = float(m2.group(1))
                    progress_store[session_id].append({'percent': pct, 'status': 'downloading'})
                    continue

                # Merging
                if '[Merger]' in line or '[ffmpeg]' in line or 'Merging' in line:
                    progress_store[session_id].append({'percent': 99, 'status': 'merging'})

                # Error lines
                if 'ERROR:' in line:
                    progress_store[session_id].append({'status': 'warning', 'message': line})

            proc.wait()
            process_store.pop(session_id, None)

            # Handle cancelled
            cancelled = any(e.get('status') == 'cancelled' for e in progress_store.get(session_id, []))
            if cancelled:
                return

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
                    progress_store[session_id].append({
                        'status': 'error',
                        'message': 'İndirme tamamlandı ama dosya bulunamadı.'
                    })
            elif proc.returncode == -15:
                # SIGTERM (cancelled on Linux)
                pass
            else:
                progress_store[session_id].append({
                    'status': 'error',
                    'message': 'İndirme başarısız. yt-dlp\'yi güncelleyin: pip install -U yt-dlp'
                })

        except FileNotFoundError:
            process_store.pop(session_id, None)
            progress_store[session_id].append({
                'status': 'error',
                'message': 'yt-dlp bulunamadı! install.bat dosyasını çalıştırın.'
            })
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
        idle = 0
        while True:
            events = progress_store.get(session_id, [])
            if sent < len(events):
                idle = 0
                while sent < len(events):
                    evt = events[sent]
                    yield f"data: {json.dumps(evt)}\n\n"
                    sent += 1
                    if evt.get('status') in ('done', 'error', 'cancelled'):
                        def cleanup():
                            time.sleep(60)
                            progress_store.pop(session_id, None)
                        threading.Thread(target=cleanup, daemon=True).start()
                        return
            else:
                idle += 1
                if idle > 600:  # 3 minutes timeout
                    yield f"data: {json.dumps({'status': 'error', 'message': 'Zaman asimi'})}\n\n"
                    return
            time.sleep(0.3)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        }
    )


@app.route('/api/cancel/<session_id>', methods=['POST', 'DELETE'])
def cancel_download(session_id):
    safe_id = re.sub(r'[^a-zA-Z0-9\-]', '', session_id)
    proc = process_store.pop(safe_id, None)
    if proc:
        try:
            if _os.name == 'nt':
                proc.terminate()
            else:
                import signal
                proc.send_signal(signal.SIGTERM)
        except Exception:
            pass
    if safe_id in progress_store:
        progress_store[safe_id].append({'status': 'cancelled', 'message': 'İndirme iptal edildi'})
    for f in TEMP_DIR.glob(f"vid_{safe_id}.*"):
        try:
            f.unlink()
        except:
            pass
    return jsonify({'ok': True})


@app.route('/api/file/<session_id>/<path:filename>')
def serve_file_named(session_id, filename):
    """Primary download route — filename embedded in URL."""
    safe_id = re.sub(r'[^a-zA-Z0-9\-]', '', session_id)
    files = list(TEMP_DIR.glob(f"vid_{safe_id}.*"))

    if not files:
        return jsonify({'error': 'Dosya bulunamadı'}), 404

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
        time.sleep(30)
        try:
            file_path.unlink()
        except:
            pass

    threading.Thread(target=cleanup_after, daemon=True).start()

    return send_file(
        file_path,
        as_attachment=True,
        download_name=filename,
        mimetype=mimetype
    )


@app.route('/api/file/<session_id>')
def serve_file(session_id):
    """Fallback route — redirects to named route."""
    safe_id = re.sub(r'[^a-zA-Z0-9\-]', '', session_id)
    files = list(TEMP_DIR.glob(f"vid_{safe_id}.*"))
    if not files:
        return jsonify({'error': 'Dosya bulunamadı'}), 404
    ext = files[0].suffix.lstrip('.')
    title = request.args.get('title', 'video')
    safe_title = re.sub(r'[^\w\s\-_().]', '', title).strip() or 'video'
    from flask import redirect
    return redirect(f'/api/file/{safe_id}/{safe_title}.{ext}')


# ── Cleanup old temp files (>2hrs) ────────────────────────────────────────────
def cleanup_old_files():
    while True:
        time.sleep(300)
        now = time.time()
        for f in TEMP_DIR.iterdir():
            try:
                if now - f.stat().st_mtime > 7200:
                    f.unlink()
            except Exception:
                pass

threading.Thread(target=cleanup_old_files, daemon=True).start()


if __name__ == '__main__':
    print("\n" + "="*50)
    print("  VidGet — Video Downloader Server")
    print("="*50)
    print(f"  URL    : http://localhost:{PORT}")
    print(f"  yt-dlp : {YTDLP}")
    print(f"  ffmpeg : {FFMPEG_DIR if FFMPEG_DIR else 'BULUNAMADI (yükleyin!)'}")
    print(f"  temp   : {TEMP_DIR}")
    print("="*50)
    if not FFMPEG_DIR:
        print("\n  [UYARI] ffmpeg bulunamadi!")
        print("  Yuksek kalite indirmek icin ffmpeg gereklidir.")
        print("  install.bat dosyasini calistirarak yukleyebilirsiniz.")
    print("\n  Durdurmak icin: Ctrl+C\n")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
