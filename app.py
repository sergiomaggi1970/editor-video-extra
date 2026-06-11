#!/usr/bin/env python3
"""
Editor de Vídeo — O Globo / Extra
Servidor Flask com FFmpeg nativo
"""

import os, sys, json, uuid, subprocess, tempfile, shutil, base64, re, io
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template_string
from functools import wraps

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024  # 2GB

import os
BASE_DIR   = Path(__file__).parent
FONT_PATH  = str(BASE_DIR / 'globo-bold.ttf')
LOGO_PATH  = str(BASE_DIR / 'extra_logo.png')
# Use /tmp on Railway (ephemeral, but fine for video processing)
UPLOAD_DIR = Path('/tmp/editor_uploads')
OUTPUT_DIR = Path('/tmp/editor_outputs')
UPLOAD_DIR.mkdir(exist_ok=True, parents=True)
OUTPUT_DIR.mkdir(exist_ok=True, parents=True)

# ── FFmpeg helpers ────────────────────────────────────────────────────────────

def ffmpeg_run(args, cwd=None):
    cmd = ['ffmpeg', '-y'] + args
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg error:\n{result.stderr[-2000:]}")
    return result

def get_video_info(path):
    """Returns (width, height, duration, rotation) of a video file."""
    cmd = ['ffprobe','-v','quiet','-print_format','json','-show_streams','-show_format', str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(r.stdout)
    video = next((s for s in data.get('streams',[]) if s.get('codec_type')=='video'), None)
    fmt = data.get('format', {})
    w = int(video.get('width', 0)) if video else 0
    h = int(video.get('height', 0)) if video else 0
    dur = float(fmt.get('duration', 0))
    rotate = 0
    if video:
        # Tenta tags diretas primeiro
        try: rotate = int(video.get('tags', {}).get('rotate', 0))
        except: rotate = 0
        # Depois side_data_list (FFmpeg moderno)
        if rotate == 0:
            for sd in video.get('side_data_list', []):
                try:
                    rot = int(sd.get('rotation', 0))
                    if rot != 0:
                        # side_data rotation é negativo: -90 = 270 graus
                        rotate = rot % 360
                        break
                except: pass
    return w, h, dur, rotate

def esc_ff(text):
    """Escape text for FFmpeg drawtext filter."""
    return text.replace('\\', '\\\\').replace("'", "\\'").replace(':', '\\:').replace('%', '\\%')


# ── Pillow title overlay (works on all FFmpeg builds) ─────────────────────────

def make_title_overlay(out_w, out_h, supertitle, maintitle, font_pct, title_pos, offset_x=0.0, offset_y=0.0):
    """Render title overlay as transparent RGBA PNG using Pillow."""
    img  = Image.new('RGBA', (out_w, out_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    margin   = int(out_w * 0.028)
    main_sz  = int(out_w * font_pct)
    super_sz = int(main_sz * 0.593)
    pill_px  = int(out_w * 0.018)
    box_px   = int(out_w * 0.018)
    line_h   = int(main_sz * 1.19)
    pill_h   = int(super_sz * 1.55)

    try:
        f_main  = ImageFont.truetype(FONT_PATH, main_sz)
        f_super = ImageFont.truetype(FONT_PATH, super_sz)
    except Exception:
        f_main  = ImageFont.load_default()
        f_super = ImageFont.load_default()

    # Word-wrap maintitle — respeita quebras manuais
    lines = []
    if maintitle:
        max_w = out_w - margin * 2 - box_px * 2
        for para in maintitle.split('\n'):
            cur = ''
            for w in para.split():
                test = (cur + ' ' + w).strip()
                bb = draw.textbbox((0, 0), test, font=f_main)
                if (bb[2] - bb[0]) <= max_w:
                    cur = test
                else:
                    if cur: lines.append(cur)
                    cur = w
            if cur: lines.append(cur)

    total_text_h = len(lines) * line_h
    total_h = (pill_h + 8 + total_text_h) if (supertitle and lines) else               (pill_h if supertitle else total_text_h)

    if title_pos == 'bottom':   cy = int(out_h * 0.695) - total_h
    elif title_pos == 'top':    cy = int(out_h * 0.08)
    else:                        cy = (out_h - total_h) // 2

    # Apply manual offsets
    cy     += int(out_h * offset_y)
    margin += int(out_w * offset_x)

    # Supertitle pill
    if supertitle:
        bb = draw.textbbox((0, 0), supertitle.upper(), font=f_super)
        sw = bb[2] - bb[0]
        draw.rectangle([margin, cy, margin + sw + pill_px * 2, cy + pill_h],
                       fill=(232, 0, 45, 255))
        draw.text((margin + pill_px - bb[0], cy + int((pill_h - (bb[3] - bb[1])) / 2) - bb[1]),
                  supertitle.upper(), font=f_super, fill=(255, 255, 255, 255))
        cy += pill_h + 8

    # Main title lines
    for i, line in enumerate(lines):
        ly = cy + i * line_h
        bb = draw.textbbox((0, 0), line, font=f_main)
        tw = bb[2] - bb[0]
        bw = tw + box_px * 2
        draw.rectangle([margin, ly, margin + bw, ly + line_h],
                       fill=(0, 0, 0, int(255 * 0.9)))
        draw.text((margin + box_px - bb[0], ly + int((line_h - (bb[3] - bb[1])) / 2) - bb[1]),
                  line, font=f_main, fill=(255, 255, 255, 255))

    return img

# ── Render endpoint ────────────────────────────────────────────────────────────

@app.route('/render', methods=['POST'])
def render():
    tmp_dir = tempfile.mkdtemp(prefix='globo_render_')
    try:
        # ── Parse form data ──────────────────────────────────────────
        video_file  = request.files.get('video')
        logo_file   = request.files.get('logo')
        supertitle  = request.form.get('supertitle', '').strip()
        maintitle   = request.form.get('maintitle', '').strip()
        title_dur   = float(request.form.get('title_dur', 6))
        title_pos   = request.form.get('title_pos', 'bottom')   # bottom / top / middle
        font_pct    = float(request.form.get('font_pct', 5.9)) / 100
        title_offset_x = float(request.form.get('title_offset_x', 0)) / 100
        title_offset_y = float(request.form.get('title_offset_y', 0)) / 100
        out_format  = request.form.get('out_format', '9:16')    # 9:16 / 16:9
        quality     = request.form.get('quality', '720p')
        wm_mode     = request.form.get('wm_mode', 'image')      # image / text / none
        wm_pos      = request.form.get('wm_pos', 'topleft')
        wm_size_pct = float(request.form.get('wm_size', 25)) / 100
        wm_margin_x = float(request.form.get('wm_margin_x', 11)) / 100
        wm_margin_y = float(request.form.get('wm_margin_y', 11)) / 100
        wm_opacity  = float(request.form.get('wm_opacity', 100)) / 100
        # Crop params (from interactive crop UI)
        crop_x      = int(request.form.get('crop_x', 0))
        crop_y      = int(request.form.get('crop_y', 0))
        crop_w      = int(request.form.get('crop_w', 0))
        crop_h      = int(request.form.get('crop_h', 0))

        if not video_file:
            return jsonify({'error': 'Nenhum vídeo enviado'}), 400

        # ── Save uploaded files ──────────────────────────────────────
        in_path  = Path(tmp_dir) / 'input.mp4'
        out_path = Path(tmp_dir) / 'output.mp4'
        video_file.save(str(in_path))

        use_custom_logo = False
        logo_path = LOGO_PATH
        if logo_file and logo_file.filename:
            logo_path = str(Path(tmp_dir) / 'logo.png')
            logo_file.save(logo_path)
            use_custom_logo = True

        # ── Video dimensions ─────────────────────────────────────────
        vw, vh, dur, v_rotate = get_video_info(str(in_path))
        if vw == 0:
            return jsonify({'error': 'Não foi possível ler as dimensões do vídeo'}), 400

        # Dimensões reais após aplicar o transpose (para cálculo de crop)
        if v_rotate in (90, 270):
            eff_vw, eff_vh = vh, vw  # transpose inverte largura/altura
        else:
            eff_vw, eff_vh = vw, vh

        # ── Output dimensions ────────────────────────────────────────
        if out_format == '9:16':
            out_w, out_h = 1080, 1920
        else:
            out_w, out_h = 1920, 1080

        # ── Scale filter ─────────────────────────────────────────────
        if quality == '720p':
            scale_str = 'scale=720:-2' if out_format == '9:16' else 'scale=-2:720'
            # Effective output dims at 720p
            if out_format == '9:16':
                eff_w, eff_h = 720, 1280
            else:
                eff_w, eff_h = 1280, 720
        elif quality == '540p':
            scale_str = 'scale=540:-2' if out_format == '9:16' else 'scale=-2:540'
            if out_format == '9:16':
                eff_w, eff_h = 540, 960
            else:
                eff_w, eff_h = 960, 540
        else:
            scale_str = ''
            eff_w, eff_h = out_w, out_h

        # Dimensões efetivas do vídeo após rotação metadata
        if v_rotate in (90, 270):
            eff_vw, eff_vh = vh, vw
        else:
            eff_vw, eff_vh = vw, vh

        # ── Crop filter ──────────────────────────────────────────────
        video_aspect = eff_vw / eff_vh
        target_aspect = out_w / out_h
        needs_crop = abs(video_aspect - target_aspect) > 0.05

        if needs_crop and crop_w > 0 and crop_h > 0:
            crop_str = f'crop={crop_w}:{crop_h}:{crop_x}:{crop_y},scale={out_w}:{out_h}'
        elif needs_crop:
            if video_aspect > target_aspect:
                auto_h = eff_vh
                auto_w = int(eff_vh * target_aspect)
                auto_x = (eff_vw - auto_w) // 2
                auto_y = 0
            else:
                auto_w = eff_vw
                auto_h = int(eff_vw / target_aspect)
                auto_x = 0
                auto_y = (eff_vh - auto_h) // 2
            crop_str = f'crop={auto_w}:{auto_h}:{auto_x}:{auto_y},scale={out_w}:{out_h}'
        else:
            crop_str = ''

        final_w, final_h = out_w, out_h

        # ── Title overlay via Pillow PNG ──────────────────────────────
        title_overlay_path = None
        if supertitle or maintitle:
            overlay_img = make_title_overlay(final_w, final_h, supertitle, maintitle, font_pct, title_pos, title_offset_x, title_offset_y)
            title_overlay_path = str(Path(tmp_dir) / 'title_overlay.png')
            overlay_img.save(title_overlay_path)
        # ── Watermark ────────────────────────────────────────────────
        has_wm_image = (wm_mode == 'image') and Path(logo_path).exists()

        # ── Build FFmpeg command ──────────────────────────────────────
        # Step 1: base video filter
        base_vf = []

        # Mapeamento correto: rotate metadata → filtro transpose para corrigir
        # rotate=90  → frame físico precisa de 90° anti-horário → transpose=2
        # rotate=180 → flip horizontal + vertical
        # rotate=270 → frame físico precisa de 90° horário + flip → transpose=3
        if v_rotate == 90:
            base_vf.append('transpose=1')
        elif v_rotate == 180:
            base_vf.append('vflip,hflip')
        elif v_rotate == 270:
            base_vf.append('transpose=2')

        # Após transpose, as dimensões físicas do frame são eff_vw x eff_vh
        # Agora aplicar crop/scale para chegar em out_w x out_h
        post_tw, post_th = eff_vw, eff_vh

        if crop_str and v_rotate == 0:
            base_vf.append(crop_str)
        else:
            # Após transpose de vídeo com rotate=90/270, frame muda de dimensão
            # Calcular aspect ratio do frame pós-transpose
            if v_rotate in (90, 270):
                pt_w, pt_h = vh, vw  # transpose inverte
            else:
                pt_w, pt_h = vw, vh

            pt_aspect = pt_w / pt_h
            target_aspect = out_w / out_h

            if abs(pt_aspect - target_aspect) > 0.05:
                if pt_aspect > target_aspect:
                    cw2 = int(pt_h * target_aspect)
                    ch2 = pt_h
                    cx2 = (pt_w - cw2) // 2
                    cy2 = 0
                else:
                    cw2 = pt_w
                    ch2 = int(pt_w / target_aspect)
                    cx2 = 0
                    cy2 = (pt_h - ch2) // 2
                base_vf.append(f'crop={cw2}:{ch2}:{cx2}:{cy2}')

            # Sempre scale para out_w x out_h
            base_vf.append(f'scale={out_w}:{out_h}')

        # Step 2: collect inputs and build filter_complex
        inputs = ['-i', str(in_path)]
        fc_parts = []
        input_idx = 1

        # Label for current video stream
        cur_label = '0:v'

        # Crop
        if base_vf:
            fc_parts.append(f'[{cur_label}]' + ','.join(base_vf) + '[base]')
            cur_label = 'base'

        # Title overlay (enable for first title_dur seconds)
        if title_overlay_path:
            inputs += ['-i', title_overlay_path]
            title_input = input_idx; input_idx += 1
            next_label = 'titled'
            fc_parts.append(f'[{cur_label}][{title_input}:v]overlay=0:0:enable=lt(t\\,{title_dur})[{next_label}]')
            cur_label = next_label

        # Scale to quality
        if scale_str:
            next_label = 'scaled'
            fc_parts.append(f'[{cur_label}]{scale_str}[{next_label}]')
            cur_label = next_label

        # Watermark overlay
        if has_wm_image:
            wm_short = min(final_w, final_h)
            wm_w     = int(wm_short * wm_size_pct)
            wm_mx    = int(final_w * wm_margin_x)
            wm_my    = int(final_h * wm_margin_y)
            if   wm_pos == 'topleft':     ox, oy = str(wm_mx), str(wm_my)
            elif wm_pos == 'topright':    ox, oy = f'main_w-overlay_w-{wm_mx}', str(wm_my)
            elif wm_pos == 'bottomleft':  ox, oy = str(wm_mx), f'main_h-overlay_h-{wm_my}'
            else:                         ox, oy = f'main_w-overlay_w-{wm_mx}', f'main_h-overlay_h-{wm_my}'

            inputs += ['-i', logo_path]
            wm_input = input_idx; input_idx += 1
            fc_parts.append(
                f'[{wm_input}:v]scale={wm_w}:-1,format=rgba,colorchannelmixer=aa={wm_opacity}[wm]'
            )
            fc_parts.append(f'[{cur_label}][wm]overlay={ox}:{oy}[out]')
            cur_label = 'out'

        if fc_parts:
            cmd_args = inputs + [
                '-filter_complex', ';'.join(fc_parts),
                '-map', f'[{cur_label}]', '-map', '0:a?',
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                '-c:a', 'copy', '-movflags', '+faststart',
                '-metadata:s:v:0', 'rotate=0',
                str(out_path)
            ]
        else:
            cmd_args = inputs + [
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                '-c:a', 'copy', '-movflags', '+faststart',
                '-metadata:s:v:0', 'rotate=0',
                str(out_path)
            ]

        ffmpeg_run(cmd_args)

        # ── Save output and return ────────────────────────────────────
        out_name = f'globo_{uuid.uuid4().hex[:8]}.mp4'
        final_path = OUTPUT_DIR / out_name
        shutil.copy(str(out_path), str(final_path))

        # Debug: log do comando usado
        cmd_debug = f'[rotate={v_rotate}] ' + ' '.join(str(a) for a in cmd_args)

        # Clean up old output files (keep last 20)
        try:
            files = sorted(OUTPUT_DIR.glob('*.mp4'), key=lambda f: f.stat().st_mtime)
            for old_file in files[:-20]:
                old_file.unlink(missing_ok=True)
        except Exception:
            pass

        return jsonify({'ok': True, 'filename': out_name, 'debug_cmd': cmd_debug})

    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.route('/download/<filename>')
def download(filename):
    path = OUTPUT_DIR / filename
    if not path.exists():
        return 'Not found', 404
    return send_file(str(path), as_attachment=True, download_name=filename)


@app.route('/logo')
def serve_logo():
    """Serve the default logo for canvas preview."""
    if not Path(LOGO_PATH).exists():
        return 'Not found', 404
    return send_file(LOGO_PATH, mimetype='image/png')


@app.route('/debug_rotate/<filename>')
def debug_rotate(filename):
    """Testa os 4 transposes e retorna qual produz frame correto."""
    path = OUTPUT_DIR / filename
    if not path.exists():
        # Tentar uploads também
        return 'Arquivo não encontrado. Use um arquivo já processado.', 404
    results = {}
    for t in range(4):
        cmd = ['ffmpeg', '-y', '-i', str(path),
               '-vf', f'transpose={t},scale=320:320:force_original_aspect_ratio=decrease',
               '-vframes', '1', '-f', 'image2', 'pipe:1']
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0:
            results[f'transpose={t}'] = base64.b64encode(r.stdout).decode()
    html = '<html><body style="background:#000;display:flex;gap:10px;padding:20px">'
    for k, v in results.items():
        html += f'<div style="text-align:center"><p style="color:white">{k}</p><img src="data:image/jpeg;base64,{v}" style="height:300px"></div>'
    html += '</body></html>'
    return html
    """Gera um title overlay de teste e retorna como PNG para diagnóstico."""
    img = make_title_overlay(1080, 1920, 'FLAGRANTE', 'Macacos são vistos em telhados de Vila Isabel', 0.059, 'bottom')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png')


@app.route('/stream/<filename>')
def stream_video(filename):
    """Stream video for browser-side upload to EF."""
    path = OUTPUT_DIR / filename
    if not path.exists():
        return 'Not found', 404
    return send_file(str(path), mimetype='video/mp4', as_attachment=False)


# In-memory job store for extension to pick up
_ef_jobs = {}

@app.route('/ef_job', methods=['POST'])
def ef_job_create():
    """App creates a job; extension fetches it to know what to upload."""
    data = request.get_json()
    job_id = uuid.uuid4().hex[:12]
    _ef_jobs[job_id] = {
        'filename':    data.get('filename'),
        'title':       data.get('title', ''),
        'description': data.get('description', ''),
    }
    return jsonify({'job_id': job_id})

@app.route('/ef_job/<job_id>')
def ef_job_get(job_id):
    """Extension fetches job details."""
    job = _ef_jobs.pop(job_id, None)
    if not job:
        return jsonify({'error': 'job not found'}), 404
    return jsonify(job)


@app.route('/video_info', methods=['POST'])
def video_info():
    """Returns video dimensions and duration for the crop UI."""
    f = request.files.get('video')
    if not f:
        return jsonify({'error': 'no file'}), 400
    tmp = tempfile.NamedTemporaryFile(suffix='.mp4', delete=False)
    f.save(tmp.name)
    try:
        w, h, dur = get_video_info(tmp.name)
        return jsonify({'width': w, 'height': h, 'duration': dur})
    finally:
        os.unlink(tmp.name)


@app.route('/publish_ef', methods=['POST'])
def publish_ef():
    """Upload rendered video to EF publisher."""
    import requests as req_lib

    filename  = request.form.get('filename')
    title     = request.form.get('title', '')
    ef_cookie = request.form.get('ef_cookie', '')  # user pastes their session cookie

    if not filename:
        return jsonify({'error': 'filename required'}), 400

    file_path = OUTPUT_DIR / filename
    if not file_path.exists():
        return jsonify({'error': 'file not found'}), 404

    # Get CSRF token from EF
    session = req_lib.Session()
    if ef_cookie:
        # Accept either full cookie string or just the value
        ef_cookie = ef_cookie.strip()
        if '=' not in ef_cookie:
            # Raw value — assume it's _ef_session
            session.cookies.set('_ef_session', ef_cookie, domain='ef-gcp.globoi.com')
        else:
            for part in ef_cookie.split(';'):
                part = part.strip()
                if '=' in part:
                    k, v = part.split('=', 1)
                    session.cookies.set(k.strip(), v.strip(), domain='ef-gcp.globoi.com')

    try:
        page = session.get('https://ef-gcp.globoi.com/videos/new', timeout=15)
        # Extract authenticity_token
        m = re.search(r'name="authenticity_token"\s+value="([^"]+)"', page.text)
        if not m:
            return jsonify({'error': 'Não foi possível obter o token CSRF. Verifique o cookie de sessão.'}), 400
        csrf = m.group(1)

        file_size = file_path.stat().st_size
        fname = file_path.name

        with open(str(file_path), 'rb') as fh:
            resp = session.post(
                'https://ef-gcp.globoi.com/upload_video',
                data=fh,
                headers={
                    'X-CSRF-Token':        csrf,
                    'Content-Type':        'application/octet-stream',
                    'Accept':              'application/json, text/javascript, */*; q=0.01',
                    'X-File-Name':         fname,
                    'X-File-Type':         'video/mp4',
                    'X-File-Size':         str(file_size),
                    'Content-Disposition': f'attachment; filename="{fname}"',
                    'X-Requested-With':    'XMLHttpRequest',
                    'Referer':             'https://ef-gcp.globoi.com/videos/new',
                },
                timeout=300
            )

        if resp.status_code in (200, 201):
            return jsonify({'ok': True, 'response': resp.json() if resp.text else {}})
        else:
            return jsonify({'error': f'EF retornou {resp.status_code}: {resp.text[:300]}'}), 400

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE,
        has_logo=Path(LOGO_PATH).exists())


# ── HTML Template ─────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Editor de Vídeo — O Globo</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Exo+2:wght@800&display=swap" rel="stylesheet">
<style>
  :root {
    --red: #E8002D; --bg: #0d0d0d; --sidebar: #111; --border: #222;
    --text: #f0f0f0; --muted: #666; --radius: 8px; --font: 'Exo 2', 'Helvetica Neue', Arial, sans-serif;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: var(--font); font-size: 13px; height: 100vh; display: flex; flex-direction: column; }
  header { background: #111; border-bottom: 1px solid var(--border); padding: 0 20px; height: 48px; display: flex; align-items: center; gap: 14px; flex-shrink: 0; }
  .logo { font-size: 16px; font-weight: 800; color: #fff; letter-spacing: -0.5px; }
  .badge { background: var(--red); color: #fff; font-size: 10px; font-weight: 700; padding: 3px 8px; border-radius: 4px; letter-spacing: 0.5px; }
  main { flex: 1; display: grid; grid-template-columns: 240px 1fr; overflow: hidden; }
  .sidebar { background: var(--sidebar); border-right: 1px solid var(--border); padding: 16px; overflow-y: auto; display: flex; flex-direction: column; gap: 14px; }
  .preview-area { background: var(--bg); display: flex; flex-direction: column; align-items: center; justify-content: flex-start; padding: 20px; gap: 12px; overflow-y: auto; }
  .sec-label { font-size: 10px; font-weight: 700; letter-spacing: 1px; color: var(--red); text-transform: uppercase; display: flex; align-items: center; gap: 6px; }
  .sec-label::before { content: ''; width: 8px; height: 8px; background: var(--red); border-radius: 50%; flex-shrink: 0; }
  .divider { border: none; border-top: 1px solid var(--border); }
  label { display: block; font-size: 11px; color: var(--muted); margin-bottom: 4px; }
  input[type=text], input[type=number], textarea, select {
    width: 100%; background: #1a1a1a; border: 1px solid var(--border); border-radius: 6px;
    color: var(--text); padding: 8px 10px; font-size: 12px; font-family: var(--font);
    outline: none; transition: border-color 0.15s;
  }
  input:focus, textarea:focus, select:focus { border-color: var(--red); }
  textarea { resize: vertical; min-height: 70px; }
  .hint { font-size: 10px; color: var(--muted); margin-top: 3px; }
  .drop-zone {
    border: 2px dashed var(--border); border-radius: var(--radius); padding: 18px;
    text-align: center; cursor: pointer; transition: all 0.2s; position: relative; color: var(--muted);
  }
  .drop-zone:hover, .drop-zone.drag { border-color: var(--red); color: var(--text); }
  .drop-zone input[type=file] { position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%; height: 100%; }
  .drop-icon { font-size: 22px; margin-bottom: 6px; }
  .tab-row { display: flex; gap: 5px; }
  .tab-btn { flex: 1; padding: 7px; background: #111; border: 1px solid var(--border); border-radius: 6px; font-size: 11px; color: var(--muted); cursor: pointer; text-align: center; transition: all 0.15s; }
  .tab-btn.active { border-color: var(--red); color: var(--red); background: rgba(232,0,45,0.08); font-weight: 600; }
  .panel { display: none; } .panel.active { display: block; }
  .slider-row { display: flex; align-items: center; gap: 8px; }
  .slider-row input[type=range] { flex: 1; accent-color: var(--red); }
  .slider-val { font-size: 11px; color: var(--muted); min-width: 36px; text-align: right; }
  .wm-pos-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 5px; }
  .pos-btn { padding: 7px; background: #111; border: 1px solid var(--border); border-radius: 6px; font-size: 11px; color: var(--muted); cursor: pointer; text-align: center; transition: all 0.15s; }
  .pos-btn.active { border-color: var(--red); color: var(--red); background: rgba(232,0,45,0.08); }
  .fmt-row { display: flex; gap: 5px; }
  .fmt-btn { flex: 1; padding: 8px; background: #111; border: 1px solid var(--border); border-radius: 6px; font-size: 11px; color: var(--muted); cursor: pointer; text-align: center; transition: all 0.15s; }
  .fmt-btn.active { border-color: var(--red); color: var(--red); background: rgba(232,0,45,0.08); font-weight: 600; }
  .crop-hint { font-size: 11px; text-align: center; margin-top: 4px; color: var(--muted); }
  .btn-primary { background: var(--red); color: #fff; border: none; border-radius: var(--radius); padding: 13px; font-size: 14px; font-weight: 700; font-family: var(--font); cursor: pointer; width: 100%; transition: opacity 0.15s; }
  .btn-primary:hover:not(:disabled) { opacity: 0.85; }
  .btn-primary:disabled { opacity: 0.4; cursor: not-allowed; }
  .btn-ef { background: #1a3a6b; color: #fff; border: none; border-radius: var(--radius); padding: 11px; font-size: 13px; font-weight: 700; font-family: var(--font); cursor: pointer; width: 100%; transition: opacity 0.15s; margin-top: 6px; }
  .btn-ef:hover:not(:disabled) { opacity: 0.85; }
  .btn-ef:disabled { opacity: 0.4; cursor: not-allowed; }
  .progress-wrap { display: none; }
  .progress-wrap.visible { display: block; }
  .progress-lbl { font-size: 11px; color: var(--muted); margin-bottom: 5px; }
  .progress-bg { background: #222; border-radius: 4px; height: 6px; overflow: hidden; }
  .progress-fill { background: var(--red); height: 100%; width: 0%; transition: width 0.3s; border-radius: 4px; }
  .out-msg { padding: 10px; border-radius: 6px; font-size: 12px; line-height: 1.5; display: none; }
  .out-msg.ok { background: #0f2d1a; color: #4ade80; display: block; }
  .out-msg.err { background: #2d0f0f; color: #f87171; display: block; }
  .prev-label { font-size: 10px; color: var(--muted); letter-spacing: 0.5px; align-self: flex-start; }
  .canvas-wrap { position: relative; border-radius: 6px; overflow: hidden; box-shadow: 0 6px 32px rgba(0,0,0,0.7); flex-shrink: 0; }
  canvas { display: block; max-width: 100%; }
  #cropOverlay { position: absolute; inset: 0; cursor: grab; }
  #cropOverlay:active { cursor: grabbing; }
  .crop-controls { display: none; flex-direction: column; gap: 8px; width: 100%; max-width: 780px; }
  .crop-controls.visible { display: flex; }
  .ef-section { background: #0d1a2d; border: 1px solid #1a3a6b; border-radius: var(--radius); padding: 12px; display: flex; flex-direction: column; gap: 8px; }
  .ef-section label { color: #93c5fd; }
  .ef-section input { background: #111827; border-color: #1e3a5f; }
  .ef-section .hint { color: #4b6fa0; }
  img.logo-preview { max-height: 48px; max-width: 100%; object-fit: contain; display: block; margin: 6px auto; filter: invert(1); }
</style>
</head>
<body>
<header>
  <div class="logo">● Globo</div>
  <div class="badge">EDITOR DE VÍDEO</div>
</header>
<main>
<!-- ── Sidebar ── -->
<div class="sidebar">

  <hr class="divider" style="margin:0">

  <div>
    <div class="sec-label">Títulos</div>
    <div style="margin-bottom:6px">
      <span style="font-size:10px;background:#1a3a6b;color:#93c5fd;padding:3px 8px;border-radius:4px;font-weight:600">Exo 2 ExtraBold</span>
      <div class="hint" style="margin-top:4px">Equivalente ao Exo Soft Bold do Extra</div>
    </div>
    <label>Antetítulo (pílula vermelha)</label>
    <input type="text" id="supertitle" placeholder="BREAKING NEWS" oninput="updatePreview()">
    <label style="margin-top:8px">Título principal (caixa preta)</label>
    <textarea id="maintitle" placeholder="Texto do título aqui" oninput="updatePreview()"></textarea>
    <div style="display:flex;gap:8px;margin-top:8px">
      <div style="flex:1">
        <label>Duração (s)</label>
        <input type="number" id="titleDur" value="6" min="1" max="30" style="text-align:center">
      </div>
      <div style="flex:2">
        <label>Posição vertical</label>
        <select id="titlePos" onchange="updatePreview()">
          <option value="bottom">Inferior (padrão)</option>
          <option value="top">Superior</option>
          <option value="middle">Centro</option>
        </select>
      </div>
    </div>
    <label style="margin-top:8px">Tamanho da fonte</label>
    <div class="slider-row">
      <input type="range" id="fontSz" min="2" max="10" step="0.1" value="5.9" oninput="updatePreview();document.getElementById('fontSzVal').textContent=parseFloat(this.value).toFixed(1)+'%'">
      <span class="slider-val" id="fontSzVal">5.9%</span>
    </div>
    <div class="hint">Padrão O Globo = 5.9% da largura</div>
    <label style="margin-top:8px">Ajuste vertical</label>
    <div class="slider-row">
      <input type="range" id="titleOffY" min="-50" max="50" value="0" oninput="updatePreview();document.getElementById('titleOffYVal').textContent=this.value+'%'">
      <span class="slider-val" id="titleOffYVal">0%</span>
    </div>
    <label style="margin-top:6px">Ajuste horizontal</label>
    <div class="slider-row">
      <input type="range" id="titleOffX" min="-40" max="40" value="0" oninput="updatePreview();document.getElementById('titleOffXVal').textContent=this.value+'%'">
      <span class="slider-val" id="titleOffXVal">0%</span>
    </div>
  </div>

  <hr class="divider">

  <div>
    <div class="sec-label">Marca d'água</div>
    <div class="tab-row" id="wmTabs">
      <div class="tab-btn active" data-tab="image">Logo Imagem</div>
      <div class="tab-btn" data-tab="text">Texto</div>
      <div class="tab-btn" data-tab="none">Nenhuma</div>
    </div>

    <div class="panel active" id="panelImage" style="margin-top:8px">
      <div class="drop-zone" id="logoDropZone" style="padding:10px">
        <input type="file" id="logoInput" accept="image/*">
        <div id="logoPreviewWrap">
          {% if has_logo %}
          <div style="font-size:11px;color:#4ade80;margin-bottom:4px">✓ Logo EXTRA carregada — clique para trocar</div>
          {% else %}
          <div style="font-size:11px;color:var(--muted)">Clique para selecionar a logo</div>
          {% endif %}
        </div>
      </div>
      <label style="margin-top:8px">Largura da logo (% do vídeo)</label>
      <div class="slider-row">
        <input type="range" id="wmSize" min="5" max="50" value="25" oninput="updatePreview();document.getElementById('wmSizeVal').textContent=this.value+'%'">
        <span class="slider-val" id="wmSizeVal">25%</span>
      </div>
      <label style="margin-top:8px">Posição</label>
      <div class="wm-pos-grid" id="wmPosGrid">
        <div class="pos-btn active" data-pos="topleft">↖ Sup. Esquerdo</div>
        <div class="pos-btn" data-pos="topright">↗ Sup. Direito</div>
        <div class="pos-btn" data-pos="bottomleft">↙ Inf. Esquerdo</div>
        <div class="pos-btn" data-pos="bottomright">↘ Inf. Direito</div>
      </div>
      <div style="margin-top:8px">
        <label>Margem H (%)</label>
        <div class="slider-row"><input type="range" id="wmMx" min="0" max="30" value="11" oninput="updatePreview();document.getElementById('wmMxVal').textContent=this.value+'%'"><span class="slider-val" id="wmMxVal">11%</span></div>
        <label style="margin-top:6px">Margem V (%)</label>
        <div class="slider-row"><input type="range" id="wmMy" min="0" max="30" value="11" oninput="updatePreview();document.getElementById('wmMyVal').textContent=this.value+'%'"><span class="slider-val" id="wmMyVal">11%</span></div>
      </div>
      <label style="margin-top:8px">Opacidade</label>
      <div class="slider-row">
        <input type="range" id="wmOpac" min="10" max="100" value="100" oninput="updatePreview();document.getElementById('wmOpacVal').textContent=this.value+'%'">
        <span class="slider-val" id="wmOpacVal">100%</span>
      </div>
    </div>

    <div class="panel" id="panelText" style="margin-top:8px">
      <input type="text" id="wmText" placeholder="EXTRA" oninput="updatePreview()">
    </div>
    <div class="panel" id="panelNone"></div>
  </div>

  <hr class="divider">

  <div>
    <div class="sec-label">Formato de saída</div>
    <div class="fmt-row">
      <div class="fmt-btn active" data-fmt="9:16" id="fmt916">📱 9:16 Vertical</div>
      <div class="fmt-btn" data-fmt="16:9" id="fmt169">🖥 16:9 Horizontal</div>
    </div>
    <div class="crop-hint" id="cropHint"></div>
  </div>

  <hr class="divider">

  <div>
    <label>Qualidade de saída</label>
    <select id="quality">
      <option value="original">Original (mais lento)</option>
      <option value="720p" selected>720p (recomendado — mais rápido)</option>
      <option value="540p">540p (mais rápido ainda)</option>
    </select>
    <div class="hint">720p é suficiente para Instagram/redes sociais</div>
  </div>

  <div>
    <button class="btn-primary" id="btnRender" onclick="renderVideo()" disabled>⚙️ Processar Vídeo</button>
    <div class="progress-wrap" id="progressWrap" style="margin-top:10px">
      <div class="progress-lbl" id="progressLbl">Processando…</div>
      <div class="progress-bg"><div class="progress-fill" id="progressFill"></div></div>
    </div>
    <div class="out-msg" id="outMsg"></div>
  </div>

  <hr class="divider">

  <div class="ef-section" id="efSection" style="display:none">
    <div class="sec-label" style="color:#93c5fd">🚀 Publicar no EF</div>
    <div class="hint" style="color:#93c5fd;margin-bottom:8px">Requer a extensão <strong>Editor Globo → EF</strong> instalada no Chrome.</div>
    <button class="btn-ef" id="btnEF" onclick="publishToEF()">🚀 Publicar no EF</button>
    <div class="out-msg" id="efMsg"></div>
  </div>

</div>

<!-- ── Preview ── -->
<div class="preview-area">
  <!-- Drop zone acima do preview — some após carregar vídeo -->
  <div id="dropZoneWrap" style="width:100%;max-width:700px">
    <div class="drop-zone" id="dropZone" style="padding:24px 18px;display:flex;align-items:center;gap:16px;text-align:left">
      <input type="file" id="videoInput" accept="video/*">
      <div style="font-size:28px;flex-shrink:0">🎬</div>
      <div>
        <div style="font-size:13px;color:var(--text);font-weight:600" id="dropText">Arraste um vídeo ou clique para selecionar</div>
        <div style="font-size:11px;color:var(--muted);margin-top:3px">MP4 · MOV · WebM</div>
      </div>
    </div>
  </div>
  <div class="prev-label" id="prevLabel" style="display:none">PREVIEW — atualiza automaticamente ao digitar</div>
  <div class="canvas-wrap" id="canvasWrap" style="display:none">
    <canvas id="previewCanvas" width="390" height="693"></canvas>
    <canvas id="cropOverlay" width="390" height="693" style="display:none"></canvas>
  </div>
  <div class="crop-controls" id="cropControls">
    <div class="slider-row">
      <span style="font-size:11px;color:var(--muted)">Zoom</span>
      <input type="range" id="cropZoom" min="100" max="400" value="100" step="1" oninput="onZoomChange(this.value)">
      <span class="slider-val" id="cropZoomVal">1.0×</span>
    </div>
    <button onclick="resetCrop()" style="background:transparent;border:1px solid var(--border);border-radius:6px;padding:6px 12px;font-size:11px;color:var(--muted);cursor:pointer">↺ Resetar enquadramento</button>
  </div>
</div>
</main>

<script>
// ── State ────────────────────────────────────────────────────────────────────
let videoFile   = null;
let logoFile    = null;   // null = use server default
let videoWidth  = 0, videoHeight = 0;
let outputFormat = '9:16';
let cropX = 0, cropY = 0, cropZoom = 1.0;
let cropDragging = false, cropDragSX = 0, cropDragSY = 0, cropDragOX = 0, cropDragOY = 0;
let lastFilename = null;
let wmPos = 'topleft';
let wmMode = 'image';

// Default logo (loaded from server)
let defaultLogoImg = null;
let customLogoImg  = null;
(function loadDefaultLogo() {
  const img = new Image();
  img.onload = () => { defaultLogoImg = img; updatePreview(); };
  img.src = '/logo';
})();

const hiddenVideo = document.createElement('video');
hiddenVideo.muted = true; hiddenVideo.playsInline = true; hiddenVideo.preload = 'auto';

// ── Video loading ─────────────────────────────────────────────────────────────
document.getElementById('videoInput').addEventListener('change', function(e) {
  const f = e.target.files[0]; if (!f) return; loadVideo(f);
});
const dz = document.getElementById('dropZone');
dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('drag'); });
dz.addEventListener('dragleave', () => dz.classList.remove('drag'));
dz.addEventListener('drop', e => { e.preventDefault(); dz.classList.remove('drag'); const f = e.dataTransfer.files[0]; if (f) loadVideo(f); });

function loadVideo(f) {
  videoFile = f;
  const mb = (f.size/1024/1024).toFixed(1);
  const nm = f.name.length > 28 ? f.name.slice(0,25)+'…' : f.name;

  const url = URL.createObjectURL(f);
  hiddenVideo.onloadedmetadata = null; hiddenVideo.onseeked = null; hiddenVideo.oncanplay = null;
  hiddenVideo.onloadedmetadata = function() {
    videoWidth = hiddenVideo.videoWidth; videoHeight = hiddenVideo.videoHeight;
    hiddenVideo.currentTime = Math.min(1.5, (hiddenVideo.duration||10)*0.1);
    document.getElementById('btnRender').disabled = false;
    // Esconder drop zone, mostrar canvas e label
    document.getElementById('dropZoneWrap').style.display = 'none';
    document.getElementById('prevLabel').style.display = 'block';
    document.getElementById('canvasWrap').style.display = 'block';
    updateFormatUI();
  };
  hiddenVideo.onseeked = function() { if (videoWidth > 0 && hiddenVideo.readyState >= 2) updatePreview(); };
  hiddenVideo.oncanplay = function() { if (videoWidth > 0 && hiddenVideo.readyState >= 2) updatePreview(); };
  hiddenVideo.src = url; hiddenVideo.load();
}

// ── Logo loading ──────────────────────────────────────────────────────────────
document.getElementById('logoInput').addEventListener('change', function(e) {
  const f = e.target.files[0]; if (!f) return;
  logoFile = f;
  const reader = new FileReader();
  reader.onload = function(ev) {
    document.getElementById('logoPreviewWrap').innerHTML =
      '<div style="font-size:11px;color:#4ade80;margin-bottom:4px">✓ ' + f.name + ' — clique para trocar</div>' +
      '<img src="' + ev.target.result + '" class="logo-preview">';
    const img = new Image();
    img.onload = () => { customLogoImg = img; updatePreview(); };
    img.src = ev.target.result;
  };
  reader.readAsDataURL(f);
});

// ── WM tabs ───────────────────────────────────────────────────────────────────
document.getElementById('wmTabs').addEventListener('click', function(e) {
  const btn = e.target.closest('.tab-btn'); if (!btn) return;
  document.querySelectorAll('#wmTabs .tab-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  wmMode = btn.dataset.tab;
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.getElementById('panel' + btn.dataset.tab.charAt(0).toUpperCase() + btn.dataset.tab.slice(1)).classList.add('active');
  updatePreview();
});

// ── WM position ───────────────────────────────────────────────────────────────
document.getElementById('wmPosGrid').addEventListener('click', function(e) {
  const btn = e.target.closest('.pos-btn'); if (!btn) return;
  document.querySelectorAll('.pos-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active'); wmPos = btn.dataset.pos; updatePreview();
});

// ── Format buttons ────────────────────────────────────────────────────────────
document.querySelectorAll('.fmt-btn').forEach(btn => {
  btn.addEventListener('click', function() {
    document.querySelectorAll('.fmt-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active'); outputFormat = btn.dataset.fmt;
    cropX = 0; cropY = 0; cropZoom = 1;
    document.getElementById('cropZoom').value = 100;
    document.getElementById('cropZoomVal').textContent = '1.0×';
    updateFormatUI(); updatePreview();
  });
});

// ── Output dims ───────────────────────────────────────────────────────────────
function getOutputDims() { return outputFormat === '9:16' ? {w:1080,h:1920} : {w:1920,h:1080}; }

function videoNeedsCrop() {
  if (!videoWidth) return false;
  const t = outputFormat === '9:16' ? 9/16 : 16/9;
  return Math.abs(videoWidth/videoHeight - t) > 0.05;
}

function getCropRect() {
  const out = getOutputDims();
  const ta = out.w/out.h, vw = videoWidth, vh = videoHeight;
  let cw, ch;
  if (vw/vh > ta) { ch = vh; cw = Math.round(vh*ta); }
  else             { cw = vw; ch = Math.round(vw/ta); }
  cw = Math.round(cw/cropZoom); ch = Math.round(ch/cropZoom);
  let x = Math.round((vw-cw)/2 + cropX);
  let y = Math.round((vh-ch)/2 + cropY);
  x = Math.max(0, Math.min(vw-cw, x));
  y = Math.max(0, Math.min(vh-ch, y));
  return {x, y, w:cw, h:ch};
}

function updateFormatUI() {
  const hint = document.getElementById('cropHint');
  const ctrl = document.getElementById('cropControls');
  const ov   = document.getElementById('cropOverlay');
  if (!videoWidth) { hint.textContent=''; ctrl.classList.remove('visible'); ov.style.display='none'; return; }
  if (videoNeedsCrop()) {
    hint.style.color='#facc15'; hint.textContent='⚠ Arraste e zoom para enquadrar';
    ctrl.classList.add('visible'); ov.style.display='block';
  } else {
    hint.style.color='#4ade80'; hint.textContent='✓ Vídeo já no formato correto';
    ctrl.classList.remove('visible'); ov.style.display='none';
  }
  resizeCanvas();
}

function resizeCanvas() {
  const out = getOutputDims(); const asp = out.w/out.h;
  const avH = Math.min(window.innerHeight-120, 700);
  let cw, ch;
  if (asp >= 1) { cw = Math.min(780, window.innerWidth-340); ch = Math.round(cw/asp); if(ch>avH){ch=avH;cw=Math.round(avH*asp);} }
  else          { ch = avH; cw = Math.round(avH*asp); }
  ['previewCanvas','cropOverlay'].forEach(id => { const c=document.getElementById(id); c.width=cw; c.height=ch; });
}

// ── Preview drawing ───────────────────────────────────────────────────────────
function updatePreview() {
  const canvas = document.getElementById('previewCanvas');
  const ctx = canvas.getContext('2d');
  resizeCanvas();
  const cw = canvas.width, ch = canvas.height;
  ctx.fillStyle = '#1a1a1a'; ctx.fillRect(0,0,cw,ch);
  if (!hiddenVideo.src || !videoWidth || hiddenVideo.readyState < 2) { drawCropGuide(); return; }

  if (videoNeedsCrop()) {
    const r = getCropRect();
    ctx.drawImage(hiddenVideo, r.x, r.y, r.w, r.h, 0, 0, cw, ch);
  } else {
    const va = videoWidth/videoHeight, ca = cw/ch;
    let sx=0,sy=0,sw=videoWidth,sh=videoHeight;
    if (va>ca) { sw=Math.round(videoHeight*ca); sx=Math.round((videoWidth-sw)/2); }
    else        { sh=Math.round(videoWidth/ca);  sy=Math.round((videoHeight-sh)/2); }
    ctx.drawImage(hiddenVideo, sx, sy, sw, sh, 0, 0, cw, ch);
  }
  drawOverlays(ctx, cw, ch);
  drawCropGuide();
}

function drawOverlays(ctx, cw, ch) {
  const out = getOutputDims();
  const scale = cw / out.w;
  const superT = document.getElementById('supertitle').value.trim();
  const mainT  = document.getElementById('maintitle').value.trim();
  const fontPct = parseFloat(document.getElementById('fontSz').value)/100;
  const titlePos = document.getElementById('titlePos').value;
  const tOffY = parseInt(document.getElementById('titleOffY').value)/100;
  const tOffX = parseInt(document.getElementById('titleOffX').value)/100;

  if (!superT && !mainT) {
    // Ainda desenha a logo mesmo sem título
    if (wmMode === 'image') {
      const logoImg = customLogoImg || defaultLogoImg;
      if (logoImg) {
        const wmSzPct = parseInt(document.getElementById('wmSize').value)/100;
        const wmMxPct = parseInt(document.getElementById('wmMx').value)/100;
        const wmMyPct = parseInt(document.getElementById('wmMy').value)/100;
        const wmOpacity = parseInt(document.getElementById('wmOpac').value)/100;
        const shortSide = Math.min(cw, ch);
        const lw = Math.round(shortSide * wmSzPct);
        const lh = Math.round(lw * logoImg.height / logoImg.width);
        const mx = Math.round(cw * wmMxPct);
        const my = Math.round(ch * wmMyPct);
        let lx, ly;
        if      (wmPos === 'topleft')     { lx = mx;       ly = my; }
        else if (wmPos === 'topright')    { lx = cw-lw-mx; ly = my; }
        else if (wmPos === 'bottomleft')  { lx = mx;       ly = ch-lh-my; }
        else                              { lx = cw-lw-mx; ly = ch-lh-my; }
        ctx.globalAlpha = wmOpacity;
        ctx.drawImage(logoImg, lx, ly, lw, lh);
        ctx.globalAlpha = 1.0;
      }
    }
    return;
  }

  let margin  = Math.round(out.w * 0.028 * scale);
  const mainSz  = Math.round(out.w * fontPct * scale);
  const superSz = Math.round(mainSz * 0.593);
  const pillPadX= Math.round(out.w * 0.018 * scale);
  const boxPadX = Math.round(out.w * 0.018 * scale);
  const lineH   = Math.round(mainSz * 1.19);
  const pillH   = Math.round(superSz * 1.55);

  ctx.font = `800 ${mainSz}px 'Exo 2', 'Helvetica Neue', Arial, sans-serif`;

  // Wrap lines — respeita \n do textarea
  const lines = [];
  if (mainT) {
    const maxW = cw - margin*2 - boxPadX*2;
    for (const para of mainT.split('\n')) {
      let cur = '';
      for (const w of para.split(' ')) {
        if (!w) continue;
        const test = cur ? cur+' '+w : w;
        if (ctx.measureText(test).width <= maxW) { cur = test; }
        else { if (cur) lines.push(cur); cur = w; }
      }
      if (cur) lines.push(cur);
    }
  }

  const totalTextH = lines.length * lineH;
  const totalH = (superT && lines.length) ? pillH+8+totalTextH : (superT ? pillH : totalTextH);
  const outH = ch;

  let by;
  if      (titlePos==='bottom') by = Math.round(outH*0.695) - totalH;
  else if (titlePos==='top')    by = Math.round(outH*0.08);
  else                           by = (outH-totalH)>>1;

  let cy = by + Math.round(ch * tOffY);
  margin += Math.round(cw * tOffX);
  if (superT) {
    ctx.font = `800 ${superSz}px 'Exo 2', 'Helvetica Neue', Arial, sans-serif`;
    const sw = ctx.measureText(superT.toUpperCase()).width;
    ctx.fillStyle = '#E8002D';
    ctx.fillRect(margin, cy, sw+pillPadX*2, pillH);
    ctx.fillStyle = '#fff'; ctx.textBaseline = 'alphabetic';
    ctx.fillText(superT.toUpperCase(), margin+pillPadX, cy+Math.round(pillH*0.72));
    cy += pillH+8;
  }
  ctx.font = `800 ${mainSz}px 'Exo 2', 'Helvetica Neue', Arial, sans-serif`;
  for (let i=0; i<lines.length; i++) {
    const ly = cy + i*lineH;
    const bw = ctx.measureText(lines[i]).width + boxPadX*2;
    ctx.fillStyle = 'rgba(0,0,0,0.9)';
    ctx.fillRect(margin, ly, bw, lineH);
    ctx.fillStyle = '#fff'; ctx.textBaseline = 'alphabetic';
    ctx.fillText(lines[i], margin+boxPadX, ly+Math.round(lineH*0.75));
  }

  // Draw watermark logo on canvas
  if (wmMode === 'image') {
    const logoImg = customLogoImg || defaultLogoImg;
    if (logoImg) {
      const wmSzPct = parseInt(document.getElementById('wmSize').value)/100;
      const wmMxPct = parseInt(document.getElementById('wmMx').value)/100;
      const wmMyPct = parseInt(document.getElementById('wmMy').value)/100;
      const wmOpacity = parseInt(document.getElementById('wmOpac').value)/100;
      const shortSide = Math.min(cw, ch);
      const lw = Math.round(shortSide * wmSzPct);
      const lh = Math.round(lw * logoImg.height / logoImg.width);
      const mx = Math.round(cw * wmMxPct);
      const my = Math.round(ch * wmMyPct);
      let lx, ly;
      if      (wmPos === 'topleft')     { lx = mx;       ly = my; }
      else if (wmPos === 'topright')    { lx = cw-lw-mx; ly = my; }
      else if (wmPos === 'bottomleft')  { lx = mx;       ly = ch-lh-my; }
      else                              { lx = cw-lw-mx; ly = ch-lh-my; }
      ctx.globalAlpha = wmOpacity;
      ctx.drawImage(logoImg, lx, ly, lw, lh);
      ctx.globalAlpha = 1.0;
    }
  }
}

function drawCropGuide() {
  const ov = document.getElementById('cropOverlay');
  if (ov.style.display==='none') return;
  const ctx = ov.getContext('2d'); const cw=ov.width, ch=ov.height;
  ctx.clearRect(0,0,cw,ch);
  ctx.strokeStyle='rgba(255,255,255,0.2)'; ctx.lineWidth=0.5;
  for(let i=1;i<3;i++){ctx.beginPath();ctx.moveTo(cw*i/3,0);ctx.lineTo(cw*i/3,ch);ctx.stroke();ctx.beginPath();ctx.moveTo(0,ch*i/3);ctx.lineTo(cw,ch*i/3);ctx.stroke();}
  ctx.strokeStyle='rgba(255,255,255,0.6)'; ctx.lineWidth=1.5; ctx.strokeRect(1,1,cw-2,ch-2);
}

// ── Crop drag ─────────────────────────────────────────────────────────────────
const cropOv = document.getElementById('cropOverlay');
cropOv.addEventListener('mousedown', e => { if(!videoNeedsCrop())return; cropDragging=true; cropDragSX=e.clientX; cropDragSY=e.clientY; cropDragOX=cropX; cropDragOY=cropY; });
window.addEventListener('mousemove', e => {
  if(!cropDragging)return;
  const canvas=document.getElementById('previewCanvas'); const r=getCropRect();
  const sx=r.w/canvas.width, sy=r.h/canvas.height;
  cropX=cropDragOX-(e.clientX-cropDragSX)*sx; cropY=cropDragOY-(e.clientY-cropDragSY)*sy; updatePreview();
});
window.addEventListener('mouseup', ()=>cropDragging=false);
cropOv.addEventListener('wheel', e=>{ if(!videoNeedsCrop())return; e.preventDefault(); cropZoom=Math.max(1,Math.min(4,cropZoom+(e.deltaY>0?-0.05:0.05))); document.getElementById('cropZoom').value=Math.round(cropZoom*100); document.getElementById('cropZoomVal').textContent=cropZoom.toFixed(1)+'×'; updatePreview(); },{passive:false});
// Touch
cropOv.addEventListener('touchstart', e=>{ if(!videoNeedsCrop()||e.touches.length!==1)return; cropDragging=true; cropDragSX=e.touches[0].clientX; cropDragSY=e.touches[0].clientY; cropDragOX=cropX; cropDragOY=cropY; },{passive:true});
cropOv.addEventListener('touchmove', e=>{ if(!cropDragging||e.touches.length!==1)return; const canvas=document.getElementById('previewCanvas'); const r=getCropRect(); const sx=r.w/canvas.width,sy=r.h/canvas.height; cropX=cropDragOX-(e.touches[0].clientX-cropDragSX)*sx; cropY=cropDragOY-(e.touches[0].clientY-cropDragSY)*sy; updatePreview(); },{passive:true});
cropOv.addEventListener('touchend', ()=>cropDragging=false);

function onZoomChange(v) { cropZoom=v/100; document.getElementById('cropZoomVal').textContent=cropZoom.toFixed(1)+'×'; updatePreview(); }
function resetCrop() { cropX=0; cropY=0; cropZoom=1; document.getElementById('cropZoom').value=100; document.getElementById('cropZoomVal').textContent='1.0×'; updatePreview(); }

// ── Render ────────────────────────────────────────────────────────────────────
async function renderVideo() {
  if (!videoFile) return;
  const btn = document.getElementById('btnRender');
  btn.disabled = true;
  setProgress(5, 'Enviando vídeo para o servidor…');
  showOut('', '');

  const fd = new FormData();
  fd.append('video', videoFile);
  if (logoFile) fd.append('logo', logoFile);

  fd.append('supertitle',  document.getElementById('supertitle').value);
  fd.append('maintitle',   document.getElementById('maintitle').value);
  fd.append('title_dur',   document.getElementById('titleDur').value);
  fd.append('title_pos',      document.getElementById('titlePos').value);
  fd.append('font_pct',       document.getElementById('fontSz').value);
  fd.append('title_offset_y', document.getElementById('titleOffY').value);
  fd.append('title_offset_x', document.getElementById('titleOffX').value);
  fd.append('out_format',  outputFormat);
  fd.append('quality',     document.getElementById('quality').value);
  fd.append('wm_mode',     wmMode);
  fd.append('wm_pos',      wmPos);
  fd.append('wm_size',     document.getElementById('wmSize').value);
  fd.append('wm_margin_x', document.getElementById('wmMx').value);
  fd.append('wm_margin_y', document.getElementById('wmMy').value);
  fd.append('wm_opacity',  document.getElementById('wmOpac').value);

  // Crop params
  if (videoNeedsCrop()) {
    const r = getCropRect();
    fd.append('crop_x', r.x); fd.append('crop_y', r.y);
    fd.append('crop_w', r.w); fd.append('crop_h', r.h);
  }

  setProgress(20, 'Processando com FFmpeg…');
  try {
    const resp = await fetch('/render', { method: 'POST', body: fd });
    const data = await resp.json();
    if (!resp.ok || data.error) {
      showOut('err', '❌ Erro: ' + (data.error || resp.statusText));
      btn.disabled = false;
      hideProgress();
      return;
    }
    setProgress(100, '✅ Concluído!');
    lastFilename = data.filename;
    showOut('ok', `✅ <strong>${data.filename}</strong> processado com sucesso! <a href="/download/${data.filename}" download style="color:#4ade80">⬇ Baixar</a>`);

    // Show EF section
    document.getElementById('efSection').style.display = 'block';
    document.getElementById('btnRender').disabled = false;
  } catch(e) {
    showOut('err', '❌ Erro de rede: ' + e.message);
    btn.disabled = false;
    hideProgress();
  }
}

function setProgress(pct, lbl) {
  document.getElementById('progressWrap').classList.add('visible');
  document.getElementById('progressFill').style.width = pct + '%';
  document.getElementById('progressLbl').textContent = lbl;
}
function hideProgress() { document.getElementById('progressWrap').classList.remove('visible'); }
function showOut(type, html) {
  const el = document.getElementById('outMsg');
  el.className = 'out-msg' + (type ? ' '+type : '');
  el.innerHTML = html;
}

// ── Publish to EF ─────────────────────────────────────────────────────────────
async function publishToEF() {
  if (!lastFilename) { alert('Processe o vídeo primeiro.'); return; }
  const btn   = document.getElementById('btnEF');
  const efMsg = document.getElementById('efMsg');

  const ante  = document.getElementById('supertitle').value.trim();
  const title = document.getElementById('maintitle').value.trim();
  const fullTitle = ante && title ? ante + ' — ' + title : (ante || title);

  // Store job data for the extension to pick up
  btn.disabled = true; btn.textContent = '⏳ Abrindo EF…';
  efMsg.className = ''; efMsg.textContent = '';

  try {
    // Register the pending job on the server so extension can fetch it
    const resp = await fetch('/ef_job', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: lastFilename, title: fullTitle, description: title })
    });
    const job = await resp.json();

    // Open EF — the extension content script will pick up the job
    window.open('https://ef-gcp.globoi.com/videos/new?ef_job=' + job.job_id, '_blank');

    btn.disabled = false; btn.textContent = '🚀 Publicar no EF';
    efMsg.className = 'out-msg ok';
    efMsg.textContent = '✅ EF aberto — a extensão está enviando o vídeo automaticamente.';
  } catch(e) {
    btn.disabled = false; btn.textContent = '🚀 Publicar no EF';
    efMsg.className = 'out-msg err';
    efMsg.textContent = '❌ Erro: ' + e.message;
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────
resizeCanvas();
window.addEventListener('resize', () => { resizeCanvas(); updatePreview(); });
</script>
</body>
</html>"""

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    print(f"Editor de Vídeo rodando na porta {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
