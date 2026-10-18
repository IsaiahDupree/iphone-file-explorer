# iPhone File Explorer

A Finder-like web UI to browse all AFC-accessible iPhone files, with Claude AI description on demand.

## Requirements

- macOS
- Python 3.10+
- iPhone connected via USB with "Trust This Computer" accepted
- Anthropic API key (for AI describe feature)

## Quick Start

```bash
cd iphone-file-explorer
./setup.sh
```

Then edit `.env` and set your `ANTHROPIC_API_KEY`, then:

```bash
source venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 5562 --reload
```

Open: http://localhost:5562

## Configuration

| Variable | Description | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Required for AI describe | — |
| `IPHONE_UDID` | Device UDID (required) | Required; set `IPHONE_UDID` in `.env` |

## Features

- Browse AFC-accessible paths: `/DCIM/`, `/Books/`, app Documents (if file sharing enabled)
- Folder tree in sidebar with lazy expand
- Grid view (photo thumbnails) and list view
- Breadcrumb navigation
- Thumbnail generation (HEIC/JPEG/PNG → JPEG, max 300px)
- File download
- Recursive search by keyword, filtered to photos/videos/all
- Claude AI description: vision analysis for images, metadata analysis for other files
- Graceful "Connect iPhone" screen when device is unplugged
- Health poll every 15s — UI auto-recovers when device is reconnected

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Connection + server status |
| GET | `/api/device` | Device name, model, iOS version |
| GET | `/api/files?path=/` | List directory contents |
| GET | `/api/download?path=` | Stream file download |
| GET | `/api/thumbnail?path=` | JPEG thumbnail (max 300px) |
| POST | `/api/describe` | `{"path": "..."}` → Claude analysis |
| GET | `/api/search?q=&path=&type=` | Recursive filename search |

## Architecture

```
main.py          FastAPI app — all routes
iphone_afc.py    AFCBrowser class — pymobiledevice3 wrapper
static/index.html  Full single-page UI (no frameworks)
```

## Troubleshooting

**"iPhone not connected"** — Unplug/replug USB cable, unlock iPhone, tap Trust.

**pymobiledevice3 install fails** — Make sure Xcode command line tools are installed: `xcode-select --install`

**HEIC thumbnails fail** — Install pillow-heif: `pip install pillow-heif`; the app handles HEIC gracefully with an emoji fallback.

**Permission denied on AFC paths** — Only `/DCIM/`, `/Books/`, and app documents (if Files sharing enabled in app settings) are exposed by AFC without a jailbreak.

Set `IPHONE_UDID` in `.env` before starting. Obtain the device identifier with
`idevice_id -l`. Without a configured identifier, the service reports the iPhone
as disconnected. Keep `.env` and device media out of Git.
