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
FONT_PATH  = str(BASE_DIR / 'exo2-extrabold.ttf')
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
    # Ler rotação do side_data_list (Display Matrix) ou tags
    rotate = 0
    if video:
        try: rotate = int(video.get('tags', {}).get('rotate', 0))
        except: rotate = 0
        if rotate == 0:
            for sd in video.get('side_data_list', []):
                try:
                    rot = int(sd.get('rotation', 0))
                    if rot != 0:
                        rotate = (-rot) % 360  # converte -90 → 270
                        break
                except: pass
    return w, h, dur, rotate


def prerotate_video(in_path, out_path, rotation):
    """Pré-rotaciona vídeo fisicamente e remove Display Matrix."""
    if rotation == 90:
        vf = 'transpose=1'
    elif rotation == 180:
        vf = 'vflip,hflip'
    elif rotation == 270:
        vf = 'transpose=2'
    else:
        return False
    cmd = ['ffmpeg', '-y', '-i', str(in_path),
           '-vf', vf,
           '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '18',
           '-c:a', 'copy',
           '-map_metadata', '-1',
           '-map_chapters', '-1',
           str(out_path)]
    r = subprocess.run(cmd, capture_output=True)
    return r.returncode == 0


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

        # FFmpeg moderno AUTO-ROTACIONA inputs com Display Matrix.
        # O frame que entra no filter_complex já vem na orientação correta.
        # Só precisamos usar as dimensões efetivas (pós-rotação) nos cálculos.
        if v_rotate in (90, 270):
            vw, vh = vh, vw

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

        # ── Crop filter ──────────────────────────────────────────────
        video_aspect = vw / vh
        target_aspect = out_w / out_h
        needs_crop = abs(video_aspect - target_aspect) > 0.05

        if needs_crop and crop_w > 0 and crop_h > 0:
            crop_str = f'crop={crop_w}:{crop_h}:{crop_x}:{crop_y},scale={out_w}:{out_h}'
        elif needs_crop:
            # Auto-center crop
            if video_aspect > target_aspect:
                auto_h = vh
                auto_w = int(vh * target_aspect)
                auto_x = (vw - auto_w) // 2
                auto_y = 0
            else:
                auto_w = vw
                auto_h = int(vw / target_aspect)
                auto_x = 0
                auto_y = (vh - auto_h) // 2
            crop_str = f'crop={auto_w}:{auto_h}:{auto_x}:{auto_y},scale={out_w}:{out_h}'
        else:
            crop_str = ''

        # ── Title overlay via Pillow PNG (works on all FFmpeg builds) ──
        title_filters = []
        title_overlay_path = None
        if supertitle or maintitle:
            overlay_img = make_title_overlay(out_w, out_h, supertitle, maintitle, font_pct, title_pos, title_offset_x, title_offset_y)
            title_overlay_path = str(Path(tmp_dir) / 'title_overlay.png')
            overlay_img.save(title_overlay_path)

        # ── Watermark ────────────────────────────────────────────────
        has_wm_image = (wm_mode == 'image') and Path(logo_path).exists()

        # ── Build FFmpeg command ──────────────────────────────────────
        # Pipeline:
        # [0:v] → crop/scale → [base]
        # [base][title_png] → overlay(enable=lt(t,dur)) → [titled]
        # [titled][logo_png] → overlay → [out]
        # Then scale to quality

        # Step 1: base video filter (rotação manual + crop + scale)
        # Estratégia determinística: -display_rotation 0 no input sobrescreve
        # a Display Matrix (nenhum autorotate em qualquer versão do FFmpeg),
        # e aplicamos o transpose manualmente. Output sempre limpo.
        base_vf = []
        if v_rotate == 90:
            base_vf.append('transpose=1')
        elif v_rotate == 180:
            base_vf.append('hflip,vflip')
        elif v_rotate == 270:
            base_vf.append('transpose=2')
        if crop_str:
            base_vf.append(crop_str)
        elif vw != out_w or vh != out_h:
            # Sem crop mas vídeo não está nas dimensões alvo — escala para elas
            base_vf.append(f'scale={out_w}:{out_h}')

        # Step 2: collect inputs and build filter_complex
        inputs = []
        if v_rotate != 0:
            inputs += ['-display_rotation', '0']
        inputs += ['-i', str(in_path)]
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
            wm_short = min(eff_w, eff_h)
            wm_w     = int(wm_short * wm_size_pct)
            wm_mx    = int(eff_w * wm_margin_x)
            wm_my    = int(eff_h * wm_margin_y)
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
                '-map_metadata', '-1',
                '-map_chapters', '-1',
                str(out_path)
            ]
        else:
            cmd_args = inputs + [
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                '-c:a', 'copy', '-movflags', '+faststart',
                '-map_metadata', '-1',
                '-map_chapters', '-1',
                str(out_path)
            ]

        ffmpeg_run(cmd_args)

        # ── Save output and return ────────────────────────────────────
        out_name = f'globo_{uuid.uuid4().hex[:8]}.mp4'
        final_path = OUTPUT_DIR / out_name
        shutil.copy(str(out_path), str(final_path))

        # Debug: log do comando usado
        cmd_debug = ' '.join(str(a) for a in cmd_args)

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


@app.route('/font.ttf')
def serve_font():
    if not Path(FONT_PATH).exists():
        return 'Not found', 404
    return send_file(FONT_PATH, mimetype='font/ttf')


@app.route('/logo')
def serve_logo():
    """Serve the default logo for canvas preview."""
    if not Path(LOGO_PATH).exists():
        return 'Not found', 404
    return send_file(LOGO_PATH, mimetype='image/png')


@app.route('/debug_title')
def debug_title():
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


HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Editor de Vídeo · Extra</title>
<style>
:root {
  --red:#E8002D; --bg:#0a0a0a; --panel:#111214; --panel2:#161719;
  --border:#1e2023; --border2:#2a2d31; --text:#e8e8e8; --muted:#5a5e65;
  --green:#22c55e; --yellow:#eab308; --font:'Helvetica Neue',Helvetica,Arial,sans-serif;
  --radius:7px; --sw:288px;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--font);font-size:13px;height:100vh;display:flex;flex-direction:column;overflow:hidden}

header{height:44px;background:#0d0d0f;border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 16px;gap:12px;flex-shrink:0;user-select:none}
.hlogo{display:flex;align-items:center;gap:8px}
.hdot{width:10px;height:10px;background:var(--red);border-radius:50%;animation:pulse 2.5s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.5;transform:scale(.85)}}
.hname{font-size:13px;font-weight:800;letter-spacing:2px;text-transform:uppercase;color:#fff}
.hsep{width:1px;height:20px;background:var(--border2)}
.hsub{font-size:11px;color:var(--muted);letter-spacing:.5px}
.spill{margin-left:auto;font-size:11px;padding:3px 10px;border-radius:20px;font-weight:600}
.spill.loading{background:rgba(234,179,8,.12);color:var(--yellow)}
.spill.ready{background:rgba(34,197,94,.12);color:var(--green)}
.spill.error{background:rgba(232,0,45,.12);color:var(--red)}

main{flex:1;display:grid;grid-template-columns:var(--sw) 1fr;overflow:hidden}

.sb{background:var(--panel);border-right:1px solid var(--border);overflow-y:auto;overflow-x:hidden;display:flex;flex-direction:column;scrollbar-width:thin;scrollbar-color:var(--border2) transparent}
.sb::-webkit-scrollbar{width:4px}.sb::-webkit-scrollbar-thumb{background:var(--border2);border-radius:4px}

.blk{padding:14px 16px;border-bottom:1px solid var(--border)}
.slbl{font-size:9px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:10px;display:flex;align-items:center;gap:6px}
.slbl::after{content:'';flex:1;height:1px;background:var(--border)}

.dz{position:relative;border:1.5px dashed var(--border2);border-radius:var(--radius);padding:16px 12px;text-align:center;cursor:pointer;transition:all .2s;background:var(--panel2)}
.dz:hover,.dz.drag{border-color:var(--red);background:rgba(232,0,45,.04)}
.dz input[type=file]{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.dz-icon{font-size:20px;margin-bottom:6px}
.dz-main{font-size:12px;color:var(--text);font-weight:500}
.dz-sub{font-size:10px;color:var(--muted);margin-top:3px}

label{display:block;font-size:10px;color:var(--muted);margin-bottom:4px;margin-top:10px;letter-spacing:.3px}
label:first-child{margin-top:0}
input[type=text],input[type=number],textarea,select{width:100%;background:var(--panel2);border:1px solid var(--border2);border-radius:var(--radius);color:var(--text);padding:7px 10px;font-size:12px;font-family:var(--font);outline:none;transition:border-color .15s;-webkit-appearance:none}
input:focus,textarea:focus,select:focus{border-color:var(--red)}
textarea{resize:vertical;min-height:58px;line-height:1.4}
select{background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%235a5e65'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 10px center;padding-right:26px;cursor:pointer}
.two{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.hint{font-size:10px;color:var(--muted);margin-top:4px;line-height:1.4}

.rrow{display:flex;align-items:center;gap:8px;margin-top:6px}
.rrow input[type=range]{flex:1;accent-color:var(--red);height:3px;cursor:pointer}
.rval{font-size:11px;color:var(--muted);min-width:38px;text-align:right;font-variant-numeric:tabular-nums}

.tabs{display:flex;gap:4px}
.tb{flex:1;padding:6px 4px;font-size:11px;font-family:var(--font);background:var(--panel2);border:1px solid var(--border2);border-radius:var(--radius);color:var(--muted);cursor:pointer;text-align:center;transition:all .15s;white-space:nowrap}
.tb:hover{border-color:var(--border);color:var(--text)}
.tb.on{border-color:var(--red);color:var(--red);background:rgba(232,0,45,.06);font-weight:600}

.pgrid{display:grid;grid-template-columns:1fr 1fr;gap:4px;margin-top:6px}
.pb{padding:6px;font-size:11px;font-family:var(--font);background:var(--panel2);border:1px solid var(--border2);border-radius:var(--radius);color:var(--muted);cursor:pointer;text-align:center;transition:all .15s}
.pb:hover{border-color:var(--border);color:var(--text)}
.pb.on{border-color:var(--red);color:var(--red);background:rgba(232,0,45,.06);font-weight:600}

.fi{font-size:10px;padding:5px 8px;border-radius:5px;margin-top:6px;line-height:1.4;display:none}
.fi.ok{background:rgba(34,197,94,.08);color:#4ade80;display:block}
.fi.warn{background:rgba(234,179,8,.08);color:#fbbf24;display:block}

.btn{width:100%;padding:11px;border:none;border-radius:var(--radius);font-size:13px;font-weight:700;font-family:var(--font);cursor:pointer;transition:all .15s;letter-spacing:.2px}
.btn-r{background:var(--red);color:#fff;box-shadow:0 2px 12px rgba(232,0,45,.25)}
.btn-r:hover:not(:disabled){background:#b8001f;box-shadow:0 4px 20px rgba(232,0,45,.35);transform:translateY(-1px)}
.btn-r:disabled{opacity:.35;cursor:not-allowed;transform:none;box-shadow:none}
.btn-r:active:not(:disabled){transform:translateY(0)}

.prog{display:none;margin-top:10px}
.prog.on{display:block}
.prog-lbl{font-size:11px;color:var(--muted);margin-bottom:6px;display:flex;justify-content:space-between}
.prog-trk{height:3px;background:var(--border2);border-radius:3px;overflow:hidden}
.prog-fill{height:100%;background:var(--red);border-radius:3px;width:0%;transition:width .4s ease}

.omsg{margin-top:10px;padding:9px 11px;border-radius:var(--radius);font-size:12px;line-height:1.5;display:none}
.omsg.ok{background:rgba(34,197,94,.08);color:#4ade80;border:1px solid rgba(34,197,94,.15);display:block}
.omsg.err{background:rgba(232,0,45,.08);color:#f87171;border:1px solid rgba(232,0,45,.15);display:block}
.omsg a{color:inherit;font-weight:700}

.logo-box{background:var(--panel2);border:1px solid var(--border2);border-radius:var(--radius);padding:10px;text-align:center;cursor:pointer;position:relative;transition:border-color .15s;min-height:52px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px}
.logo-box:hover{border-color:var(--border)}
.logo-box input[type=file]{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.logo-box img{max-height:32px;max-width:120px;object-fit:contain;filter:brightness(0) invert(1)}
.logo-box .ll{font-size:10px;color:var(--muted)}

.prev-area{background:#070709;display:flex;flex-direction:column;align-items:center;justify-content:flex-start;padding:20px 24px;gap:12px;overflow-y:auto}
.prev-lbl{font-size:9px;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);align-self:flex-start}
.cwrap{position:relative;border-radius:8px;overflow:hidden;box-shadow:0 8px 40px rgba(0,0,0,.8),0 0 0 1px rgba(255,255,255,.04);flex-shrink:0;cursor:grab}
.cwrap:active{cursor:grabbing}
canvas{display:block}

#overlay{background:rgba(7,7,9,.95);position:fixed;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;z-index:100;transition:opacity .4s}
#overlay.gone{opacity:0;pointer-events:none}
.spin{width:36px;height:36px;border:3px solid var(--border2);border-top-color:var(--red);border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.spin-txt{font-size:13px;color:var(--muted);letter-spacing:.5px}
</style>
</head>
<body>

<div id="overlay">
  <div class="spin"></div>
  <div class="spin-txt" id="spinTxt" style="display:none"></div>
</div>

<header>
  <div class="hlogo"><div class="hdot"></div><span class="hname">Extra</span></div>
  <div class="hsep"></div>
  <span class="hsub">Editor de Vídeo</span>
  <div class="spill loading" id="pill">Carregando…</div>
</header>

<main>
<div class="sb">

  <div class="blk">
    <div class="slbl">Vídeo</div>
    <div class="dz" id="dz">
      <input type="file" id="vi" accept="video/*">
      <div class="dz-icon">🎬</div>
      <div class="dz-main" id="dzTxt">Arraste ou clique para selecionar</div>
      <div class="dz-sub">MP4 · MOV · WebM</div>
    </div>
  </div>

  <div class="blk">
    <div class="slbl">Títulos</div>
    <label>Antetítulo · pílula vermelha</label>
    <input type="text" id="supr" placeholder="EX: EXCLUSIVO" maxlength="60">
    <label>Título principal · caixa preta</label>
    <textarea id="titl" placeholder="Digite o título da matéria" rows="3"></textarea>
    <div class="two" style="margin-top:10px">
      <div><label>Duração (s)</label><input type="number" id="tdur" value="6" min="1" max="60" style="text-align:center"></div>
      <div><label>Posição base</label><select id="tpos"><option value="bottom">Inferior</option><option value="top">Superior</option><option value="middle">Centro</option></select></div>
    </div>
    <label style="margin-top:10px">Ajuste vertical</label>
    <div class="rrow"><input type="range" id="tOffY" min="-50" max="50" value="0"><span class="rval" id="tOffYV">0%</span></div>
    <label>Ajuste horizontal</label>
    <div class="rrow"><input type="range" id="tOffX" min="-40" max="40" value="0"><span class="rval" id="tOffXV">0%</span></div>
    <label>Tamanho da fonte</label>
    <div class="rrow"><input type="range" id="fsz" min="2" max="10" step="0.1" value="5.9"><span class="rval" id="fszV">5.9%</span></div>
    <div class="hint">Padrão O Globo = 5.9% da largura</div>
  </div>

  <div class="blk">
    <div class="slbl">Marca d'água</div>
    <div class="tabs" id="wmT">
      <div class="tb on" data-t="image">Imagem</div>
      <div class="tb" data-t="text">Texto</div>
      <div class="tb" data-t="none">Nenhuma</div>
    </div>
    <div id="pImage" style="margin-top:10px">
      <div class="logo-box" id="lgBox">
        <input type="file" id="lgI" accept="image/*">
        <div id="lgC"><div class="ll" id="lgLbl">Clique para selecionar logo</div></div>
      </div>
      <label>Largura · % do vídeo</label>
      <div class="rrow"><input type="range" id="wmSz" min="5" max="50" value="25"><span class="rval" id="wmSzV">25%</span></div>
      <label>Posição</label>
      <div class="pgrid" id="wmP">
        <div class="pb on" data-p="topleft">↖ Sup. Esq.</div>
        <div class="pb" data-p="topright">↗ Sup. Dir.</div>
        <div class="pb" data-p="bottomleft">↙ Inf. Esq.</div>
        <div class="pb" data-p="bottomright">↘ Inf. Dir.</div>
      </div>
      <div class="two">
        <div><label>Margem H</label><div class="rrow"><input type="range" id="wmMx" min="0" max="30" value="4"><span class="rval" id="wmMxV">4%</span></div></div>
        <div><label>Margem V</label><div class="rrow"><input type="range" id="wmMy" min="0" max="30" value="4"><span class="rval" id="wmMyV">4%</span></div></div>
      </div>
      <label>Opacidade</label>
      <div class="rrow"><input type="range" id="wmOp" min="10" max="100" value="100"><span class="rval" id="wmOpV">100%</span></div>
    </div>
    <div id="pText" style="margin-top:10px;display:none"><input type="text" id="wmTx" placeholder="EXTRA"></div>
    <div id="pNone"></div>
  </div>

  <div class="blk">
    <div class="slbl">Formato de saída</div>
    <div class="tabs" id="fmtT">
      <div class="tb on" data-f="9:16">📱 9:16 Vertical</div>
      <div class="tb" data-f="16:9">🖥 16:9 Horizontal</div>
    </div>
    <div class="fi" id="fmtI"></div>
  </div>

  <div class="blk" id="cropB" style="display:none">
    <div class="slbl">Enquadramento</div>
    <div class="hint" style="margin-bottom:8px">Arraste o preview · scroll = zoom</div>
    <label>Zoom</label>
    <div class="rrow"><input type="range" id="czS" min="100" max="500" value="100"><span class="rval" id="czV">1.0×</span></div>
    <button id="czR" style="margin-top:8px;background:transparent;border:1px solid var(--border2);border-radius:var(--radius);padding:5px 12px;font-size:11px;color:var(--muted);cursor:pointer">↺ Resetar</button>
  </div>

  <div class="blk">
    <div class="slbl">Processamento</div>
    <label>Qualidade</label>
    <select id="qual">
      <option value="720p" selected>720p · recomendado (rápido)</option>
      <option value="540p">540p · mais rápido ainda</option>
      <option value="original">Original · sem redimensionar</option>
    </select>
    <div class="hint" style="margin-bottom:12px">720p é suficiente para Instagram</div>
    <button class="btn btn-r" id="btnR" disabled>⚙ Processar Vídeo</button>
    <div class="prog" id="progA">
      <div class="prog-lbl"><span id="progL">Processando…</span><span id="progP">0%</span></div>
      <div class="prog-trk"><div class="prog-fill" id="progF"></div></div>
    </div>
    <div class="omsg" id="omsg"></div>
  </div>

</div>

<div class="prev-area">
  <div class="prev-lbl">Preview · atualiza ao digitar</div>
  <div style="position:relative;display:inline-block;line-height:0">
  <div class="cwrap" id="cw">
    <canvas id="cv" width="390" height="693"></canvas>
    <canvas id="cg" width="390" height="693" style="position:absolute;inset:0;pointer-events:none;display:none"></canvas>
  </div>
</div>
</div>
</main>

<video id="hv" muted playsinline preload="auto" style="display:none"></video>

<script>
// ── fetchFile polyfill ────────────────────────────────────────────────────────
async function fetchFile(src) {
  if (src instanceof File || src instanceof Blob) {
    return new Uint8Array(await src.arrayBuffer());
  }
  if (typeof src === 'string') {
    const r = await fetch(src);
    return new Uint8Array(await r.arrayBuffer());
  }
  return new Uint8Array();
}

// ── State ─────────────────────────────────────────────────────────────────────
let ready=true, ff=null;
let vFile=null, lgURL=null, lgIsDefault=false;
let vW=0, vH=0;
let fmt='9:16', wmMode='image', wmPos='topleft';
let cX=0,cY=0,cZ=1;
let cDrag=false,cSX=0,cSY=0,cOX=0,cOY=0;
let lastVURL=null;
let lgImg=null, lgImgSrc=null;
const hv=document.getElementById('hv');

// ── FFmpeg init ───────────────────────────────────────────────────────────────
async function initFF() {
  try {
    const {FFmpeg} = FFmpegWASM;
    ff = new FFmpeg();
    ff.on('log',({message:m})=>console.log('[FF]',m));
    ff.on('progress',({progress:p})=>setProg(Math.round(p*100),`Renderizando… ${Math.round(p*100)}%`));
    await ff.load({
      coreURL:   './ffmpeg-core.js',
      wasmURL:   './ffmpeg-core.wasm',
      workerURL: './814.ffmpeg.js',
    });
    ready=true;
    setPill('Pronto','ready');
    document.getElementById('overlay').classList.add('gone');
    loadDefaultLogo();
  } catch(e) {
    setPill('Erro','error');
    document.getElementById('spinTxt').textContent='Erro: '+e.message;
    console.error(e);
  }
}
function setPill(t,c){const p=document.getElementById('pill');p.textContent=t;p.className='spill '+c;}

// ── Default logo ──────────────────────────────────────────────────────────────
async function loadDefaultLogo(){
  try{
    const r=await fetch('./extra_logo.png');
    if(!r.ok)return;
    const b=await r.blob();
    lgURL=URL.createObjectURL(b); lgIsDefault=true;
    setLogoPreview(lgURL,'Logo EXTRA · clique para trocar');
    drawPrev();
  }catch(e){console.log('no default logo')}
}
function setLogoPreview(url,lbl){
  const c=document.getElementById('lgC');
  c.innerHTML=`<img src="${url}"><div class="ll">${lbl}</div>`;
  lgImg=null; lgImgSrc=null; // reset cached img
}

// ── Video ─────────────────────────────────────────────────────────────────────
document.getElementById('vi').addEventListener('change',e=>{const f=e.target.files[0];if(f)loadV(f);});
const dz=document.getElementById('dz');
dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('drag');});
dz.addEventListener('dragleave',()=>dz.classList.remove('drag'));
dz.addEventListener('drop',e=>{e.preventDefault();dz.classList.remove('drag');const f=e.dataTransfer.files[0];if(f)loadV(f);});

function loadV(f){
  vFile=f;
  const mb=(f.size/1024/1024).toFixed(1), nm=f.name.length>24?f.name.slice(0,21)+'…':f.name;
  document.getElementById('dzTxt').textContent=`✓ ${nm}  (${mb} MB)`;
  dz.style.borderColor='var(--green)';
  if(lastVURL)URL.revokeObjectURL(lastVURL);
  lastVURL=URL.createObjectURL(f);
  hv.onloadedmetadata=null;hv.onseeked=null;hv.oncanplay=null;
  hv.onloadedmetadata=()=>{
    vW=hv.videoWidth;vH=hv.videoHeight;
    hv.currentTime=Math.min(1.5,(hv.duration||10)*.1);
    document.getElementById('btnR').disabled=!ready;
    const zb=document.getElementById('zoomBtns');
    if(zb) zb.style.display='flex';
    updateFmtUI();
  };
  hv.onseeked=()=>{if(vW&&hv.readyState>=2)drawPrev();};
  hv.oncanplay=()=>{if(vW&&hv.readyState>=2)drawPrev();};
  hv.src=lastVURL; hv.load();
}

// ── Logo input ────────────────────────────────────────────────────────────────
document.getElementById('lgI').addEventListener('change',e=>{
  const f=e.target.files[0];if(!f)return;
  if(lgURL&&!lgIsDefault)URL.revokeObjectURL(lgURL);
  lgURL=URL.createObjectURL(f);lgIsDefault=false;
  setLogoPreview(lgURL,f.name+' · clique para trocar');
  drawPrev();
});

// ── WM tabs ───────────────────────────────────────────────────────────────────
document.getElementById('wmT').addEventListener('click',e=>{
  const b=e.target.closest('.tb');if(!b)return;
  document.querySelectorAll('#wmT .tb').forEach(x=>x.classList.remove('on'));
  b.classList.add('on'); wmMode=b.dataset.t;
  document.getElementById('pImage').style.display=wmMode==='image'?'block':'none';
  document.getElementById('pText').style.display=wmMode==='text'?'block':'none';
  drawPrev();
});

// ── WM pos ────────────────────────────────────────────────────────────────────
document.getElementById('wmP').addEventListener('click',e=>{
  const b=e.target.closest('.pb');if(!b)return;
  document.querySelectorAll('.pb').forEach(x=>x.classList.remove('on'));
  b.classList.add('on');wmPos=b.dataset.p;drawPrev();
});

// ── Format ────────────────────────────────────────────────────────────────────
document.getElementById('fmtT').addEventListener('click',e=>{
  const b=e.target.closest('.tb');if(!b)return;
  document.querySelectorAll('#fmtT .tb').forEach(x=>x.classList.remove('on'));
  b.classList.add('on');fmt=b.dataset.f;
  cX=0;cY=0;cZ=1;document.getElementById('czS').value=100;document.getElementById('czV').textContent='1.0×';
  updateFmtUI();drawPrev();
});

// ── Sliders ───────────────────────────────────────────────────────────────────
function sl(id,vid,sfx){
  document.getElementById(id).addEventListener('input',function(){
    document.getElementById(vid).textContent=this.value+sfx;drawPrev();
  });
}
sl('fsz','fszV','%');sl('wmSz','wmSzV','%');
sl('tOffY','tOffYV','%');sl('tOffX','tOffXV','%');sl('wmMx','wmMxV','%');sl('wmMy','wmMyV','%');sl('wmOp','wmOpV','%');
document.getElementById('czS').addEventListener('input',function(){
  cZ=this.value/100;
  document.getElementById('czV').textContent=cZ.toFixed(2)+'×';
  updateCropBlockVisibility();
  drawPrev();
});
document.getElementById('tpos').addEventListener('change',drawPrev);
['supr','titl','wmTx'].forEach(id=>document.getElementById(id).addEventListener('input',drawPrev));
document.getElementById('czR').addEventListener('click',()=>{
  cX=0;cY=0;cZ=1;
  document.getElementById('czS').value=100;
  document.getElementById('czV').textContent='1.0×';
  updateCropBlockVisibility();
  drawPrev();
});

function zoomStep(delta){
  cZ=Math.max(1,Math.min(5,cZ+delta));
  document.getElementById('czS').value=Math.round(cZ*100);
  document.getElementById('czV').textContent=cZ.toFixed(2)+'×';
  updateCropBlockVisibility();
  drawPrev();
}
function zoomReset(){
  cX=0;cY=0;cZ=1;
  document.getElementById('czS').value=100;
  document.getElementById('czV').textContent='1.0×';
  updateCropBlockVisibility();
  drawPrev();
}
function updateCropBlockVisibility(){
  const cb=document.getElementById('cropB');
  if(!vW){cb.style.display='none';return;}
  if(needsCrop()||cZ>1.001) cb.style.display='block';
  else cb.style.display='none';
}

// ── Dims ──────────────────────────────────────────────────────────────────────
function outDims(){return fmt==='9:16'?{w:1080,h:1920}:{w:1920,h:1080};}
function needsCrop(){
  if(!vW)return false;
  const t=fmt==='9:16'?9/16:16/9;
  return Math.abs(vW/vH-t)>.05;
}
function cropRect(){
  const o=outDims(),ta=o.w/o.h;
  let cw,ch;
  if(vW/vH>ta){ch=vH;cw=Math.round(vH*ta);}else{cw=vW;ch=Math.round(vW/ta);}
  cw=Math.round(cw/cZ);ch=Math.round(ch/cZ);
  let x=Math.round((vW-cw)/2+cX),y=Math.round((vH-ch)/2+cY);
  x=Math.max(0,Math.min(vW-cw,x));y=Math.max(0,Math.min(vH-ch,y));
  return{x,y,w:cw,h:ch};
}

function updateFmtUI(){
  const fi=document.getElementById('fmtI'),cb=document.getElementById('cropB');
  if(!vW){fi.style.display='none';cb.style.display='none';return;}
  fi.style.display='block';
  if(needsCrop()){
    fi.className='fi warn';fi.textContent='⚠ Vídeo em formato diferente — arraste e zoom para enquadrar';
    cb.style.display='block';
  }else{
    fi.className='fi ok';fi.textContent='✓ Vídeo no formato correto';
    cb.style.display='block';  // always show zoom controls
  }
  resizeCanvases();
}

function resizeCanvases(){
  const o=outDims(),asp=o.w/o.h;
  const avH=Math.min(window.innerHeight-110,700);
  const swW=parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sw'))||288;
  let cw,ch;
  if(asp>=1){cw=Math.min(760,window.innerWidth-swW-48);ch=Math.round(cw/asp);if(ch>avH){ch=avH;cw=Math.round(avH*asp);}}
  else{ch=avH;cw=Math.round(avH*asp);}
  ['cv','cg'].forEach(id=>{const c=document.getElementById(id);c.width=cw;c.height=ch;});
  const cg=document.getElementById('cg');
  cg.style.display=(vW&&needsCrop())?'block':'none';
}

// ── Logo img cache ────────────────────────────────────────────────────────────
function getLgImg(){
  if(!lgURL)return null;
  if(lgImg&&lgImgSrc===lgURL)return lgImg;
  const img=new Image();img.src=lgURL;img._src=lgURL;
  img.onload=()=>{lgImg=img;lgImgSrc=lgURL;drawPrev();};
  return null;
}

// ── Preview ───────────────────────────────────────────────────────────────────
function drawPrev(){
  const cv=document.getElementById('cv'),ctx=cv.getContext('2d');
  resizeCanvases();
  const cw=cv.width,ch=cv.height;
  ctx.fillStyle='#111';ctx.fillRect(0,0,cw,ch);
  if(!hv.src||!vW||hv.readyState<2){drawGuide();return;}
  // Always use cropRect so zoom/pan works even when format matches
  {const r=cropRect();ctx.drawImage(hv,r.x,r.y,r.w,r.h,0,0,cw,ch);}
  drawTitles(ctx,cw,ch);
  drawWm(ctx,cw,ch);
  drawGuide();
}

function drawTitles(ctx,cw,ch){
  const o=outDims(),scale=cw/o.w;
  const sT=document.getElementById('supr').value.trim();
  const mT=document.getElementById('titl').value.trim();
  if(!sT&&!mT)return;
  const fp=parseFloat(document.getElementById('fsz').value)/100;
  const pos=document.getElementById('tpos').value;
  const mg=Math.round(o.w*.028*scale),mSz=Math.round(o.w*fp*scale),sSz=Math.round(mSz*.593);
  const pPx=Math.round(o.w*.018*scale),bPx=Math.round(o.w*.018*scale);
  const lH=Math.round(mSz*1.19),pH=Math.round(sSz*1.55);
  const lines=[];
  if(mT){
    ctx.font=`800 ${mSz}px GloboBold,'Helvetica Neue',Arial,sans-serif`;
    const mxW=cw-mg*2-bPx*2;
    for(const para of mT.split('\n')){
      let cur='';
      for(const w of para.split(' ')){
        if(!w)continue;
        const test=cur?cur+' '+w:w;
        if(ctx.measureText(test).width<=mxW)cur=test;
        else{if(cur)lines.push(cur);cur=w;}
      }
      if(cur)lines.push(cur);
    }
  }
  const totH=(sT&&lines.length?pH+8+lines.length*lH:sT?pH:lines.length*lH);
  const tOffY=parseInt(document.getElementById('tOffY').value)/100;
  const tOffX=parseInt(document.getElementById('tOffX').value)/100;
  let cy=pos==='bottom'?Math.round(ch*.695)-totH:pos==='top'?Math.round(ch*.08):(ch-totH)>>1;
  cy += Math.round(ch*tOffY);
  const mgX = mg + Math.round(cw*tOffX);
  if(sT){
    ctx.font=`800 ${sSz}px GloboBold,'Helvetica Neue',Arial,sans-serif`;
    const sw=ctx.measureText(sT.toUpperCase()).width;
    ctx.fillStyle='#E8002D';ctx.fillRect(mgX,cy,sw+pPx*2,pH);
    ctx.fillStyle='#fff';ctx.textBaseline='alphabetic';
    ctx.fillText(sT.toUpperCase(),mgX+pPx,cy+Math.round(pH*.72));cy+=pH+8;
  }
  ctx.font=`800 ${mSz}px GloboBold,'Helvetica Neue',Arial,sans-serif`;
  for(let i=0;i<lines.length;i++){
    const ly=cy+i*lH,bw=ctx.measureText(lines[i]).width+bPx*2;
    ctx.fillStyle='rgba(0,0,0,.9)';ctx.fillRect(mgX,ly,bw,lH);
    ctx.fillStyle='#fff';ctx.textBaseline='alphabetic';
    ctx.fillText(lines[i],mgX+bPx,ly+Math.round(lH*.77));
  }
}

function drawWm(ctx,cw,ch){
  if(wmMode!=='image')return;
  const img=getLgImg();if(!img)return;
  const sz=parseInt(document.getElementById('wmSz').value)/100;
  const mx=parseInt(document.getElementById('wmMx').value)/100;
  const my=parseInt(document.getElementById('wmMy').value)/100;
  const op=parseInt(document.getElementById('wmOp').value)/100;
  const side=Math.min(cw,ch),ww=Math.round(side*sz),wh=Math.round(ww*(img.naturalHeight/img.naturalWidth));
  const mxP=Math.round(cw*mx),myP=Math.round(ch*my);
  let ox,oy;
  if(wmPos==='topleft'){ox=mxP;oy=myP;}
  else if(wmPos==='topright'){ox=cw-ww-mxP;oy=myP;}
  else if(wmPos==='bottomleft'){ox=mxP;oy=ch-wh-myP;}
  else{ox=cw-ww-mxP;oy=ch-wh-myP;}
  ctx.globalAlpha=op;ctx.drawImage(img,ox,oy,ww,wh);ctx.globalAlpha=1;
}

function drawGuide(){
  const cg=document.getElementById('cg');
  if(cg.style.display==='none')return;
  const ctx=cg.getContext('2d'),cw=cg.width,ch=cg.height;
  ctx.clearRect(0,0,cw,ch);
  ctx.strokeStyle='rgba(255,255,255,.18)';ctx.lineWidth=.5;
  for(let i=1;i<3;i++){
    ctx.beginPath();ctx.moveTo(cw*i/3,0);ctx.lineTo(cw*i/3,ch);ctx.stroke();
    ctx.beginPath();ctx.moveTo(0,ch*i/3);ctx.lineTo(cw,ch*i/3);ctx.stroke();
  }
  ctx.strokeStyle='rgba(255,255,255,.5)';ctx.lineWidth=1;ctx.strokeRect(.5,.5,cw-1,ch-1);
}

// ── Drag/scroll crop ──────────────────────────────────────────────────────────
const cw_el=document.getElementById('cw');
cw_el.addEventListener('mousedown',e=>{cDrag=true;cSX=e.clientX;cSY=e.clientY;cOX=cX;cOY=cY;});
window.addEventListener('mousemove',e=>{
  if(!cDrag)return;
  const cv=document.getElementById('cv'),r=cropRect();
  const sx=r.w/cv.width,sy=r.h/cv.height;
  cX=cOX-(e.clientX-cSX)*sx;cY=cOY-(e.clientY-cSY)*sy;drawPrev();
});
window.addEventListener('mouseup',()=>cDrag=false);
cw_el.addEventListener('wheel',e=>{
  e.preventDefault();
  const delta = e.ctrlKey ? e.deltaY * 0.01 : e.deltaY > 0 ? -0.08 : 0.08;
  cZ = Math.max(1, Math.min(5, cZ + delta));
  document.getElementById('czS').value = Math.round(cZ*100);
  document.getElementById('czV').textContent = cZ.toFixed(2)+'×';
  updateCropBlockVisibility();
  drawPrev();
},{passive:false});
let lastPinchDist = 0;
cw_el.addEventListener('touchstart',e=>{
  if(e.touches.length===1){
    cDrag=true;cSX=e.touches[0].clientX;cSY=e.touches[0].clientY;cOX=cX;cOY=cY;
  } else if(e.touches.length===2){
    cDrag=false;
    const dx=e.touches[0].clientX-e.touches[1].clientX;
    const dy=e.touches[0].clientY-e.touches[1].clientY;
    lastPinchDist=Math.sqrt(dx*dx+dy*dy);
  }
},{passive:true});
cw_el.addEventListener('touchmove',e=>{
  if(e.touches.length===2){
    // Pinch to zoom
    const dx=e.touches[0].clientX-e.touches[1].clientX;
    const dy=e.touches[0].clientY-e.touches[1].clientY;
    const dist=Math.sqrt(dx*dx+dy*dy);
    if(lastPinchDist>0){
      cZ=Math.max(1,Math.min(5,cZ*(dist/lastPinchDist)));
      document.getElementById('czS').value=Math.round(cZ*100);
      document.getElementById('czV').textContent=cZ.toFixed(2)+'×';
      updateCropBlockVisibility();
      drawPrev();
    }
    lastPinchDist=dist;
    return;
  }
  if(!cDrag||e.touches.length!==1)return;
  const cv=document.getElementById('cv'),r=cropRect();
  const sx=r.w/cv.width,sy=r.h/cv.height;
  cX=cOX-(e.touches[0].clientX-cSX)*sx;cY=cOY-(e.touches[0].clientY-cSY)*sy;drawPrev();
},{passive:true});
cw_el.addEventListener('touchend',e=>{if(e.touches.length===0)cDrag=false;lastPinchDist=0;});
window.addEventListener('resize',()=>{resizeCanvases();drawPrev();});

// ── Render ────────────────────────────────────────────────────────────────────
document.getElementById('btnR').addEventListener('click',renderVideo);

async function renderVideo(){
  if(!vFile)return;
  const btn=document.getElementById('btnR');
  btn.disabled=true; setOut('','');
  try{
    setProg(10,'Enviando para o servidor…');
    const fd=new FormData();
    fd.append('video', vFile);
    if(lgURL&&wmMode==='image'){
      const r=await fetch(lgURL); const b=await r.blob();
      fd.append('logo', b, 'logo.png');
    }
    const o=outDims();
    const r=cropRect();
    fd.append('supertitle', document.getElementById('supr').value.trim());
    fd.append('maintitle',  document.getElementById('titl').value.trim());
    fd.append('title_dur',  document.getElementById('tdur').value||'6');
    fd.append('title_pos',  document.getElementById('tpos').value);
    fd.append('font_pct',   document.getElementById('fsz').value);
    fd.append('title_offset_y', document.getElementById('tOffY').value);
    fd.append('title_offset_x', document.getElementById('tOffX').value);
    fd.append('out_format',  fmt);
    fd.append('quality',     document.getElementById('qual').value);
    fd.append('wm_mode',     wmMode);
    fd.append('wm_pos',      wmPos);
    fd.append('wm_size',     document.getElementById('wmSz').value);
    fd.append('wm_margin_x', document.getElementById('wmMx').value);
    fd.append('wm_margin_y', document.getElementById('wmMy').value);
    fd.append('wm_opacity',  document.getElementById('wmOp').value);
    // Sempre enviar crop/zoom
    fd.append('crop_x', r.x); fd.append('crop_y', r.y);
    fd.append('crop_w', r.w); fd.append('crop_h', r.h);
    setProg(20,'Processando…');
    const resp=await fetch('/render',{method:'POST',body:fd});
    const data=await resp.json();
    if(!resp.ok||data.error) throw new Error(data.error||resp.statusText);
    setProg(100,'✓ Concluído!');
    const name=data.filename;
    setOut('ok',`✓ <strong>${name}</strong> &nbsp; <a href="/download/${name}" download style="background:var(--red);color:#fff;padding:5px 12px;border-radius:5px;text-decoration:none;font-weight:700">⬇ Baixar</a>`);
  }catch(e){
    setOut('err','❌ Erro: '+e.message);console.error(e);
  }finally{btn.disabled=false;}
}

function setProg(pct,lbl){
  const a=document.getElementById('progA');a.classList.add('on');
  document.getElementById('progF').style.width=pct+'%';
  document.getElementById('progL').textContent=lbl;
  document.getElementById('progP').textContent=pct+'%';
}
function setOut(t,h){const e=document.getElementById('omsg');e.className='omsg'+(t?' '+t:'');e.innerHTML=h;}

// ── Zoom buttons — created in JS, injected after canvas wrap ─────────────────
function createZoomButtons() {
  // Find the wrapper div (position:relative parent of cwrap)
  const wrapper = document.getElementById('cw').parentElement;
  const btns = document.createElement('div');
  btns.id = 'zoomBtns';
  btns.style.cssText = [
    'position:absolute','bottom:12px','right:12px',
    'display:none','flex-direction:column','gap:6px','z-index:50'
  ].join(';');

  const btnStyle = [
    'width:36px','height:36px',
    'background:rgba(10,10,10,0.8)',
    'border:1px solid rgba(255,255,255,0.25)',
    'border-radius:8px','color:#fff','font-size:22px',
    'cursor:pointer','line-height:1','font-weight:300',
    'display:flex','align-items:center','justify-content:center',
    'transition:background .15s'
  ].join(';');

  [
    ['+', () => zoomStep(0.25),  'Zoom in'],
    ['−', () => zoomStep(-0.25), 'Zoom out'],
    ['↺', () => zoomReset(),     'Resetar zoom'],
  ].forEach(([label, fn, title]) => {
    const b = document.createElement('button');
    b.textContent = label;
    b.title = title;
    b.style.cssText = btnStyle + (label === '↺' ? ';font-size:14px;color:#aaa' : '');
    b.addEventListener('mouseenter', () => b.style.background = 'rgba(232,0,45,0.8)');
    b.addEventListener('mouseleave', () => b.style.background = 'rgba(10,10,10,0.8)');
    b.addEventListener('click', e => { e.stopPropagation(); fn(); });
    btns.appendChild(b);
  });

  wrapper.appendChild(btns);
}

// ── Boot ──────────────────────────────────────────────────────────────────────
resizeCanvases();
createZoomButtons();

// Sem FFmpeg local — app usa servidor. Esconder overlay imediatamente.
document.getElementById('overlay').classList.add('gone');
setPill('Pronto','ready');

// Carregar logo padrão do servidor
(async function(){
  try{
    const r=await fetch('/logo');
    if(!r.ok)return;
    const b=await r.blob();
    lgURL=URL.createObjectURL(b);
    setLogoPreview(lgURL,'Logo EXTRA · clique para trocar');
  }catch(e){}
})();


// Load custom font for preview + OffscreenCanvas title rendering
(async function loadCustomFont() {
  try {
    const font = new FontFace('GloboBold', 'url(/font.ttf)', {weight:'800'});
    await font.load();
    document.fonts.add(font);
    console.log('[Font] GloboBold loaded ✓');
    drawPrev(); // redraw preview with correct font
  } catch(e) {
    console.warn('[Font] Could not load GloboBold:', e.message);
  }
})();
</script>
</body>
</html>

"""


if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    print(f"Editor de Vídeo rodando na porta {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
