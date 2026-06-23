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

# Apply DB schema (idempotent — safe to re-run)
python3 -c "
from dotenv import load_dotenv; load_dotenv()
import psycopg2, os
conn = psycopg2.connect(os.environ['DATABASE_URL'], connect_timeout=10)
conn.autocommit = True
conn.cursor().execute(open('schema.sql').read())
conn.close()
"
```

There are no automated tests or a linter configured.

## Architecture

The entire application lives in a single file: `app.py` (~1750 lines). The frontend (HTML, CSS, JavaScript) is embedded as a string returned by the `/` route — there are no separate template files.

**Key sections inside `app.py`:**
| Lines | What it does |
|-------|-------------|
| 1–35 | Imports, `load_dotenv()`, `get_db_connection()` |
| 37–160 | FFmpeg/ffprobe helpers, `normalize_clip_for_timeline()` |
| 165–260 | `make_title_overlay()` — Pillow PNG generation, extra/globo templates |
| 262–490 | `/render` endpoint — single-video processing pipeline |
| 490–560 | Asset endpoints (`/logo`, `/font.ttf`, `/healthz`, `/stream`, etc.) |
| 560–730 | Timeline API — clip upload, list, delete, reorder |
| 730–940 | `_run_finalize()` + finalize endpoints |
| 940–end | EF publisher integration + embedded HTML/CSS/JS SPA |

## Templates & Assets

Two editorial templates: **extra** (black boxes, red accent) and **globo** (white boxes, blue accent).

| Template | Main font | Super font | Logo |
|----------|-----------|------------|------|
| extra | `exo2-extrabold.ttf` | `exo2-extrabold.ttf` | `extra_logo.png` |
| globo | `corsario-vf.otf` | `opensans-regular.ttf` | `oglobo_logo.png` |

## Database

Connection via `DATABASE_URL` in `.env` (Supabase Postgres, pooler port 6543). Must include `sslmode=require&gssencmode=disable` to work behind corporate firewalls.

`get_db_connection()` opens a new connection per call with `connect_timeout=10`. Always close with `conn = cur = None / try / finally` pattern used throughout.

### Schema (`schema.sql`)

**`timeline_clips`** — one row per uploaded clip:

| Column | Type | Notes |
|--------|------|-------|
| `id` | serial PK | |
| `timeline_id` | uuid | groups clips into a timeline |
| `position` | integer | 1-based, ordered |
| `original_filename` | text | |
| `local_path` | text | absolute path to normalised clip on disk |
| `status` | text | default `'uploaded'` |
| `output_format` | text | `vertical` / `square` / `horizontal` |
| `created_at` | timestamptz | |

**`render_jobs`** — one row per finalize job:

| Column | Type | Notes |
|--------|------|-------|
| `id` | uuid PK | |
| `timeline_id` | uuid | |
| `status` | text | `processing` → `done` / `error` |
| `output_path` | text | absolute path to `final_*.mp4` on success |
| `error` | text | ffmpeg stderr tail on failure |
| `created_at` / `updated_at` | timestamptz | |

## Timeline API

All endpoints under `/api/timeline/`.

| Method | Path | What it does |
|--------|------|-------------|
| `POST` | `/api/timeline/clip` | Upload + normalise a clip (`multipart/form-data`: `video`, `timeline_id?`, `output_format`) |
| `GET` | `/api/timeline/<timeline_id>` | List clips ordered by `position` |
| `DELETE` | `/api/timeline/<timeline_id>/clip/<clip_id>` | Remove clip from DB and disk |
| `PATCH` | `/api/timeline/<timeline_id>/reorder` | Body: `{"clip_ids": [3,1,2]}` — updates `position` |
| `POST` | `/api/timeline/<timeline_id>/finalize` | Start background render job (see below) |
| `GET` | `/api/timeline/<timeline_id>/finalize/<job_id>` | Poll job status |

### Clip normalisation (`normalize_clip_for_timeline`)

Every uploaded clip is re-encoded to the target format before being stored:
- Scale + center-crop to target resolution (`vertical` 1080×1920, `square` 1080×1080, `horizontal` 1920×1080)
- 30 fps, libx264 CRF 23, AAC 128k stereo
- Silent audio track injected via `anullsrc` if the source has no audio
- 120 s subprocess timeout → 504 if exceeded

### Finalize flow (`_run_finalize`)

Runs in a `daemon=True` thread. Five sequential stages, each updating `render_jobs` to `error` on failure:

1. Fetch `local_path` list from `timeline_clips` ordered by `position`
2. `ffmpeg -f concat -safe 0 -c copy` → `tmp_concat_*.mp4` (300 s timeout)
3. `ffprobe` to read `out_w` / `out_h` from the concat output
4. `make_title_overlay()` → PNG; then ffmpeg filter_complex: title overlay + logo watermark → `final_*.mp4`
5. On success: update `render_jobs` → `done`, delete `timeline_clips` rows and `clip_*.mp4` files. On any failure: delete partial `final_*.mp4`.

**Finalize body (JSON):**

| Field | Required | Default |
|-------|----------|---------|
| `template` | yes | — |
| `maintitle` | yes | — |
| `supertitle` | no | `""` |
| `font_pct` | no | `5.9` |
| `title_pos` | no | `bottom` |
| `title_offset_x/y` | no | `0` |
| `title_dur` | no | `6` |
| `wm_mode` | no | `image` |
| `wm_pos` | no | `topleft` |
| `wm_size` | no | `25` |
| `wm_margin_x/y` | no | `11` |
| `wm_opacity` | no | `100` |

Logo always uses the template default (`extra_logo.png` / `oglobo_logo.png`). Custom logo upload is not supported in finalize.

## Single-video Pipeline (`/render`)

Original editor flow for processing one video at a time (used by the SPA):
rotation correction → crop/scale → title overlay → quality downscale → watermark. Output saved to `/tmp/editor_outputs/` (keeps last 20 files).

## Storage

| Path | Purpose |
|------|---------|
| `/tmp/editor_uploads/` | Transient uploads for `/render` |
| `/tmp/editor_outputs/` | `/render` outputs (last 20 kept) |
| `/tmp/timeline_<uuid>/` | Per-timeline working dir; `clip_*.mp4` deleted after finalize, `final_*.mp4` kept |

On Railway the filesystem is ephemeral — files do not persist between deploys.

## EF Publisher Integration

`/publish_ef` uploads a rendered video to O Globo's internal EF platform (`ef-gcp.globoi.com`), handling CSRF token extraction and session cookies. Only functional inside O Globo's network/credentials.

## Deployment

Railway via Nixpacks. `nixpacks.toml` installs Python 3.11 + FFmpeg. `Procfile` starts Gunicorn with 2 workers and a 300 s timeout.

> **Note:** Gunicorn's 2 workers means `_run_finalize` threads are bound to the worker that received the `POST /finalize` request. Job status is read from the DB so any worker can serve the polling `GET`. If the worker restarts mid-render, the job stays `processing` forever — acceptable for current scale.
