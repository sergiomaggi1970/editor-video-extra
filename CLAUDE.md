# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Development
python app.py                    # runs on http://localhost:5000

# Production (as deployed)
gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 300

# Install dependencies
pip install -r requirements.txt
```

There are no automated tests or a linter configured.

## Architecture

The entire application lives in a single file: `app.py` (~1430 lines). The frontend (HTML, CSS, JavaScript) is embedded as a string returned by the `/` route — there are no separate template files.

**Request flow:**
1. User loads `/` → receives the full SPA (canvas-based editor)
2. User fills the form and clicks Process → POST to `/render`
3. `/render` calls ffprobe to read video dimensions/rotation, renders a title overlay PNG with Pillow, builds an FFmpeg filter graph, and runs FFmpeg as a subprocess
4. The processed file is saved to `/tmp/editor_outputs/` and the filename is returned
5. User downloads via `/download/<filename>` or streams via `/stream/<filename>`

**Key sections inside `app.py`:**
| Lines | What it does |
|-------|-------------|
| 1–90 | Flask init, config, FFmpeg/ffprobe helpers |
| 92–183 | Title overlay image generation (Pillow) — supports "extra" and "globo" templates |
| 187–417 | `/render` endpoint — the core processing pipeline |
| 419–600 | Asset endpoints (`/logo`, `/font.ttf`, `/healthz`, `/publish_ef`, etc.) |
| 602–end | Embedded HTML/CSS/JS SPA |

## Templates & Assets

Two editorial templates are supported: **extra** (black boxes, red accent) and **globo** (white boxes, blue accent). Each template has its own fonts and logo:

| Template | Font | Logo |
|----------|------|------|
| extra | `exo2-extrabold.ttf` / `corsario-vf.otf` | `extra_logo.png` |
| globo | `globo-bold.ttf` / `opensans-regular.ttf` | `oglobo_logo.png` |

All font and logo files live at the repository root and are served by dedicated Flask routes (`/font.ttf`, `/font_super.ttf`, `/logo`).

## FFmpeg Pipeline

The `/render` endpoint builds a complex FFmpeg filter graph that chains:
1. Base video filter: rotation correction + crop + scale
2. Title overlay PNG composited for a time-limited window
3. Quality downscale (720p / 540p / original)
4. Watermark (image or text) with configurable opacity and position

FFmpeg is invoked via `subprocess`. CRF 23 / libx264 is the output codec.

## Storage

- Uploads: `/tmp/editor_uploads/` (auto-created, cleaned after processing)
- Outputs: `/tmp/editor_outputs/` (auto-created, keeps last 20 files)

On Railway the filesystem is ephemeral; files do not persist between deploys.

## EF Publisher Integration

`/publish_ef` (lines 524–591) uploads a rendered video to O Globo's internal EF platform (`ef-gcp.globoi.com`). It handles CSRF token extraction from a prior GET and passes session cookies provided by the frontend. This endpoint is only functional inside O Globo's network/credentials.

## Deployment

Deployed on Railway via Nixpacks. `nixpacks.toml` installs Python 3.11 and FFmpeg system-wide. `Procfile` starts Gunicorn with 2 workers and a 300 s timeout (necessary for long video renders).
