import os, uuid, json, glob, queue, threading, tempfile, subprocess, re, shutil
from flask import Flask, render_template, request, Response, send_file, jsonify
import yt_dlp

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024 * 1024  # 10 GB

downloads  = {}   # {id: {queue, out_dir, filename}}
transcodes = {}   # {id: {queue, out_dir, filename}}

COOKIES_PATH = os.environ.get('COOKIES_FILE', '/tmp/ytdlp-cookies.txt')


# ── yt-dlp ─────────────────────────────────────────────────────────────────

def make_dl_hooks(dl_id):
    q = downloads[dl_id]['queue']

    def progress_hook(d):
        if d['status'] == 'downloading':
            q.put({
                'type': 'progress',
                'percent': d.get('_percent_str', '0%').strip(),
                'speed':   d.get('_speed_str',   '–').strip(),
                'eta':     d.get('_eta_str',      '–').strip(),
                'filename': os.path.basename(d.get('filename', '')),
            })

    def postprocessor_hook(d):
        if d['status'] == 'started' and 'Merger' in d.get('postprocessor', ''):
            q.put({'type': 'merging'})

    return progress_hook, postprocessor_hook


def build_dl_format(res, fps):
    if res == 'audio':
        return 'bestaudio/best'
    fps_filt = f'[fps<={fps}]' if fps != 'any' else ''
    if res == 'best':
        return (f'bestvideo{fps_filt}[ext=mp4]+bestaudio[ext=m4a]'
                f'/bestvideo{fps_filt}+bestaudio/best')
    h = {'1080': 1080, '720': 720, '480': 480}.get(res, 1080)
    return (f'bestvideo[height<={h}]{fps_filt}[ext=mp4]+bestaudio[ext=m4a]'
            f'/bestvideo[height<={h}]{fps_filt}+bestaudio/best')


def run_download(dl_id, url, fmt, fps='any'):
    out_dir = downloads[dl_id]['out_dir']
    q       = downloads[dl_id]['queue']
    ph, pp  = make_dl_hooks(dl_id)

    ydl_opts = {
        'outtmpl':            os.path.join(out_dir, '%(title)s.%(ext)s'),
        'progress_hooks':     [ph],
        'postprocessor_hooks':[pp],
        'format':             build_dl_format(fmt, fps),
        'noplaylist':         True,
        'merge_output_format':'mp4',
        'quiet':              True,
    }
    if os.path.exists(COOKIES_PATH):
        ydl_opts['cookiefile'] = COOKIES_PATH

    if fmt == 'audio':
        ydl_opts['postprocessors'] = [{'key':'FFmpegExtractAudio',
                                        'preferredcodec':'mp3','preferredquality':'192'}]
        del ydl_opts['merge_output_format']

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        media = {'.mp4','.mp3','.mkv','.webm','.m4a','.opus','.flac','.wav'}
        files = [f for f in glob.glob(os.path.join(out_dir,'*'))
                 if os.path.isfile(f) and os.path.splitext(f)[1].lower() in media
                 and not f.endswith('.part')]
        if files:
            latest = max(files, key=os.path.getmtime)
            downloads[dl_id]['filename'] = latest
            q.put({'type':'ready','filename':os.path.basename(latest)})
        else:
            q.put({'type':'error','message':'No output file found'})
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        q.put({'type':'error','message': msg.split('ERROR:')[-1].strip() if 'ERROR:' in msg else msg})
    except Exception as e:
        q.put({'type':'error','message':f'Unexpected error: {e}'})


# ── HandBrake ──────────────────────────────────────────────────────────────

HB_RE = re.compile(r'(\d+\.\d+)\s*%(?:.*?(\d+\.\d+)\s*fps.*?ETA\s+(\S+))?')

def build_hb_cmd(input_path, output_path, s):
    enc_map = {'h264':'x264', 'h265':'x265', 'av1':'svt_av1'}
    cmd = [
        'HandBrakeCLI',
        '-i', input_path,
        '-o', output_path,
        '-e', enc_map.get(s.get('encoder','h264'), 'x264'),
        '-q', str(s.get('quality', '20')),
        '-f', 'av_mkv' if s.get('format','mp4') == 'mkv' else 'av_mp4',
        '--optimize',
    ]
    res = s.get('resolution', 'original')
    if res != 'original':
        cmd += ['--maxHeight', res]

    fps = s.get('fps', 'original')
    if fps != 'original':
        cmd += ['-r', fps, '--cfr']

    audio = s.get('audio', 'aac_192')
    if audio == 'copy':
        cmd += ['-E', 'copy']
    elif audio == 'mp3_192':
        cmd += ['-E', 'mp3', '-B', '192']
    else:
        cmd += ['-E', 'ca_aac', '-B', '192' if '192' in audio else '128']

    return cmd


def run_transcode(tc_id, input_path, settings):
    q       = transcodes[tc_id]['queue']
    out_dir = transcodes[tc_id]['out_dir']

    if not shutil.which('HandBrakeCLI'):
        q.put({'type':'error',
               'message':'HandBrakeCLI not found.\n'
                         'Install: sudo apt install handbrake-cli\n'
                         '  macOS: brew install handbrake'})
        return

    base     = os.path.splitext(os.path.basename(input_path))[0]
    ext      = settings.get('format', 'mp4')
    enc      = settings.get('encoder', 'h264')
    quality  = settings.get('quality', '20')
    out_name = f"{base}_{enc}_rf{quality}.{ext}"
    out_path = os.path.join(out_dir, out_name)

    cmd = build_hb_cmd(input_path, out_path, settings)

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        for line in proc.stdout:
            line = line.strip()
            if 'Encoding:' in line:
                m = HB_RE.search(line)
                if m:
                    q.put({'type':'progress', 'percent': m.group(1)+'%',
                           'fps': m.group(2) or '–', 'eta': m.group(3) or '–'})
            elif 'Mux' in line:
                q.put({'type':'muxing'})
        proc.wait()
        if proc.returncode == 0 and os.path.exists(out_path):
            transcodes[tc_id]['filename'] = out_path
            q.put({'type':'ready', 'filename': out_name})
        else:
            q.put({'type':'error', 'message':f'HandBrakeCLI exited with code {proc.returncode}'})
    except FileNotFoundError:
        q.put({'type':'error', 'message':'HandBrakeCLI not found in PATH'})
    except Exception as e:
        q.put({'type':'error', 'message': str(e)})


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


# ── Download routes ──

@app.route('/download', methods=['POST'])
def start_download():
    data = request.json or {}
    url  = data.get('url','').strip()
    fmt  = data.get('format','best')
    fps  = data.get('fps','any')
    if not url:
        return jsonify({'error':'No URL provided'}), 400
    dl_id = str(uuid.uuid4())
    downloads[dl_id] = {'queue': queue.Queue(), 'out_dir': tempfile.mkdtemp(), 'filename': None}
    threading.Thread(target=run_download, args=(dl_id, url, fmt, fps), daemon=True).start()
    return jsonify({'id': dl_id})


@app.route('/progress/<dl_id>')
def dl_progress(dl_id):
    if dl_id not in downloads:
        return jsonify({'error':'Not found'}), 404
    def gen():
        q = downloads[dl_id]['queue']
        while True:
            try:
                ev = q.get(timeout=60)
                yield f'data: {json.dumps(ev)}\n\n'
                if ev['type'] in ('ready','error'): break
            except queue.Empty:
                yield 'data: {"type":"ping"}\n\n'
    return Response(gen(), mimetype='text/event-stream',
                    headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})


@app.route('/file/<dl_id>')
def dl_file(dl_id):
    if dl_id not in downloads:
        return jsonify({'error':'Not found'}), 404
    info = downloads[dl_id]
    f = info.get('filename')
    if not f or not os.path.exists(f):
        media = {'.mp4','.mp3','.mkv','.webm','.m4a','.opus','.flac'}
        files = [x for x in glob.glob(os.path.join(info['out_dir'],'*'))
                 if os.path.splitext(x)[1].lower() in media and not x.endswith('.part')]
        if not files: return jsonify({'error':'File not ready'}), 404
        f = max(files, key=os.path.getmtime)
    return send_file(f, as_attachment=True)


@app.route('/cookies', methods=['GET'])
def cookies_status():
    exists = os.path.exists(COOKIES_PATH)
    size   = os.path.getsize(COOKIES_PATH) if exists else 0
    return jsonify({'loaded': exists, 'size': size})


@app.route('/cookies', methods=['POST'])
def upload_cookies():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'No file selected'}), 400
    f.save(COOKIES_PATH)
    size = os.path.getsize(COOKIES_PATH)
    return jsonify({'ok': True, 'size': size})


@app.route('/cookies', methods=['DELETE'])
def delete_cookies():
    if os.path.exists(COOKIES_PATH):
        os.remove(COOKIES_PATH)
    return jsonify({'ok': True})


@app.route('/last-download')
def last_download():
    completed = [(k,v) for k,v in downloads.items() if v.get('filename') and os.path.exists(v['filename'])]
    if not completed:
        return jsonify({'filename': None})
    latest_id, latest = max(completed, key=lambda x: os.path.getmtime(x[1]['filename']))
    return jsonify({'id': latest_id, 'filename': os.path.basename(latest['filename'])})


# ── Transcode routes ──

@app.route('/transcode', methods=['POST'])
def start_transcode():
    settings = {}
    if request.content_type and 'multipart' in request.content_type:
        if 'file' not in request.files:
            return jsonify({'error':'No file provided'}), 400
        f = request.files['file']
        in_dir = tempfile.mkdtemp()
        input_path = os.path.join(in_dir, f.filename)
        f.save(input_path)
        settings = dict(request.form)
        settings = {k: v[0] if isinstance(v, list) else v for k, v in settings.items()}
    else:
        data    = request.json or {}
        dl_id   = data.get('dl_id')
        settings = data.get('settings', {})
        if not dl_id or dl_id not in downloads:
            return jsonify({'error':'No source provided'}), 400
        input_path = downloads[dl_id].get('filename')
        if not input_path or not os.path.exists(input_path):
            return jsonify({'error':'Source file not found'}), 404

    tc_id = str(uuid.uuid4())
    transcodes[tc_id] = {'queue': queue.Queue(), 'out_dir': tempfile.mkdtemp(), 'filename': None}
    threading.Thread(target=run_transcode, args=(tc_id, input_path, settings), daemon=True).start()
    return jsonify({'id': tc_id})


@app.route('/transcode-progress/<tc_id>')
def tc_progress(tc_id):
    if tc_id not in transcodes:
        return jsonify({'error':'Not found'}), 404
    def gen():
        q = transcodes[tc_id]['queue']
        while True:
            try:
                ev = q.get(timeout=60)
                yield f'data: {json.dumps(ev)}\n\n'
                if ev['type'] in ('ready','error'): break
            except queue.Empty:
                yield 'data: {"type":"ping"}\n\n'
    return Response(gen(), mimetype='text/event-stream',
                    headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})


@app.route('/transcode-file/<tc_id>')
def tc_file(tc_id):
    if tc_id not in transcodes:
        return jsonify({'error':'Not found'}), 404
    f = transcodes[tc_id].get('filename')
    if not f or not os.path.exists(f):
        return jsonify({'error':'File not ready'}), 404
    return send_file(f, as_attachment=True)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'ytdlp-web running at http://localhost:{port}')
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
