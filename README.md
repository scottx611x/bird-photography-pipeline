# Bird Tools

A personal bird photography workflow tool. Automates the pipeline from raw import through Lightroom editing to Instagram via Buffer.

## What it does

1. **Scans** `~/Downloads` for date-stamped batch folders (`YYYY-MM-DD-*`)
2. **Imports** the folder into Lightroom Classic via AppleScript
3. **Auto-tones** all photos (Select All → Apply Auto Settings)
4. **Denoise** — you set it on one photo, it copy-pastes to all
5. **Pick** your best shot(s) in Lightroom
6. **Exports** to `~/Desktop/birbs/` via Export with Previous
7. **Posts** a carousel to Instagram via Buffer, scheduled to your next open queue day

## Architecture

```
Browser (localhost:8765)
    ↕ HTTP poll + SSE log stream
Docker (server.py / Flask)
    ↕ host.docker.internal:8766
lr_host.py (Mac, background)
    ↕ AppleScript / osascript
Lightroom Classic
```

## Stack

- **server.py** — Flask web UI, workflow orchestration, Buffer API
- **lr_host.py** — Mac-side HTTP bridge, triggers Lightroom automation
- **lr_auto.py** — AppleScript wrappers for LR import / tone / denoise / export
- **birb_post.py** — Resize images, upload to Buffer S3, queue to Instagram
- **templates/index.html** — Single-page UI (vanilla JS, dark theme)

## Setup

### Requirements

- macOS with Lightroom Classic
- Docker Desktop
- Python 3.12 (via pyenv or system)
- Chrome logged into [publish.buffer.com](https://publish.buffer.com)
- Buffer API token from publish.buffer.com/settings/api

### Install

```bash
git clone git@github.com:scottx611x/birb-tools.git
cd birb-tools
```

### Configure

```bash
export BUFFER_TOKEN="your_token_here"
```

Or add to your shell profile. The `start.sh` script reads `BUFFER_TOKEN` from the environment.

### Run

```bash
bash start.sh
```

Opens the workflow UI at [http://localhost:8765](http://localhost:8765).

`start.sh` handles:
- Extracting Buffer session cookies from Chrome
- Building and starting the Docker container
- Starting `lr_host.py` as a background Mac process

### Stop

```bash
birb down
```

## Folder naming

Drop a folder in `~/Downloads` named `YYYY-MM-DD-*` and click **Scan**:

| Name | Location default |
|------|-----------------|
| `2026-06-01-best` | Rea St. |
| `2026-06-01-pond` | Pond |
| `2026-06-01-waverly` | Waverly |

The UI scans every 30 seconds automatically.

## Workflow UI

- **Progress bar** — numbered stages (Import → Tone → Denoise → Pick → Export → Post); click any completed stage to restart from there
- **Post form** — per-photo species + location fields with auto-spread (type in card 1, fills the rest), drag to reorder carousel, click to preview full-size with ← → cycling
- **Done cards** — shows the posted photos as an overlapping strip, scheduled date, and a link to Buffer
- **Log** — live via SSE, "Show log" in the nav

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `BUFFER_TOKEN` | Yes | Buffer API bearer token |
| `BUFFER_COOKIES` | Auto | Set by `start.sh` from Chrome session |
| `MAC_HOME` | No | Override home dir inside Docker (default `/Users/scott`) |
