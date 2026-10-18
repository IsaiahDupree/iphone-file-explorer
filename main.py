"""
main.py — iPhone File Explorer FastAPI backend.

Endpoints:
  GET  /                   → serve HTML UI
  GET  /api/health         → connection status
  GET  /api/device         → device info
  GET  /api/files?path=/   → list directory
  GET  /api/download?path= → stream file
  GET  /api/thumbnail?path=→ JPEG thumbnail (max 300px)
  POST /api/describe       → Claude AI description
  GET  /api/search?q=&path=&type=  → recursive search

Start:
  uvicorn main:app --host 0.0.0.0 --port 5562 --reload
"""

from __future__ import annotations

import base64
import io
import logging
import mimetypes
import os
import time
from pathlib import Path
from typing import Optional

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App + AFC browser singleton
# ---------------------------------------------------------------------------

app = FastAPI(title="iPhone File Explorer", version="1.0.0")

UDID = os.getenv("IPHONE_UDID", "")

# Lazy-import so the server still starts if pymobiledevice3 is not installed
try:
    from iphone_afc import AFCBrowser
    browser = AFCBrowser(udid=UDID)
except ImportError:
    browser = None  # type: ignore
    logger.warning("pymobiledevice3 not available — AFC features disabled")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-opus-4-6"

# ---------------------------------------------------------------------------
# Static files + HTML UI
# ---------------------------------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = STATIC_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="UI not found — did you create static/index.html?")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    connected = bool(browser and browser.is_connected())
    return {
        "status": "ok",
        "iphone_connected": connected,
        "udid": UDID,
        "timestamp": time.time(),
    }


# ---------------------------------------------------------------------------
# Device info
# ---------------------------------------------------------------------------

@app.get("/api/device")
async def device_info():
    _require_browser()
    if not browser.is_connected():
        raise HTTPException(status_code=503, detail="iPhone not connected")
    info = browser.device_info()
    return {"connected": True, **info}


# ---------------------------------------------------------------------------
# List directory
# ---------------------------------------------------------------------------

@app.get("/api/files")
async def list_files(path: str = Query("/", description="AFC path to list")):
    _require_browser()
    if not browser.is_connected():
        raise HTTPException(status_code=503, detail="iPhone not connected")
    try:
        entries = browser.list_dir(path)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return [
        {
            "name": e.name,
            "path": e.path,
            "is_dir": e.is_dir,
            "size": e.size,
            "modified": e.modified,
        }
        for e in entries
    ]


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

@app.get("/api/download")
async def download_file(path: str = Query(..., description="AFC path to download")):
    _require_browser()
    if not browser.is_connected():
        raise HTTPException(status_code=503, detail="iPhone not connected")

    filename = path.rsplit("/", 1)[-1] or "file"
    mime, _ = mimetypes.guess_type(filename)
    mime = mime or "application/octet-stream"

    def _generator():
        try:
            yield from browser.read_file_chunked(path)
        except RuntimeError as exc:
            logger.error("Download error for %s: %s", path, exc)

    return StreamingResponse(
        _generator(),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Thumbnail
# ---------------------------------------------------------------------------

@app.get("/api/thumbnail")
async def thumbnail(path: str = Query(..., description="AFC path to image")):
    _require_browser()
    if not browser.is_connected():
        raise HTTPException(status_code=503, detail="iPhone not connected")

    try:
        data = browser.read_file(path)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    try:
        img = Image.open(io.BytesIO(data))
        img.thumbnail((300, 300), Image.LANCZOS)
        # Convert to RGB if needed (e.g. HEIC decoded to RGBA)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80, optimize=True)
        buf.seek(0)
        return StreamingResponse(buf, media_type="image/jpeg")
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Cannot create thumbnail: {exc}")


# ---------------------------------------------------------------------------
# AI describe
# ---------------------------------------------------------------------------

class DescribeRequest(BaseModel):
    path: str


@app.post("/api/describe")
async def describe_file(req: DescribeRequest):
    _require_browser()
    if not browser.is_connected():
        raise HTTPException(status_code=503, detail="iPhone not connected")
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not set")

    try:
        data = browser.read_file(req.path)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    filename = req.path.rsplit("/", 1)[-1]
    mime, _ = mimetypes.guess_type(filename)
    mime = mime or "application/octet-stream"

    # Only vision-capable mimetypes go as images; everything else we describe by name
    is_image = mime.startswith("image/")
    is_video = mime.startswith("video/")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    if is_image:
        # Re-encode as JPEG if needed (HEIC etc.)
        try:
            img = Image.open(io.BytesIO(data))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            image_data = base64.standard_b64encode(buf.getvalue()).decode("utf-8")
            send_mime = "image/jpeg"
        except Exception:
            image_data = base64.standard_b64encode(data).decode("utf-8")
            send_mime = mime

        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": send_mime,
                                "data": image_data,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "Analyze this image from someone's iPhone. "
                                "Provide:\n"
                                "1. A concise description (2-3 sentences) of what you see.\n"
                                "2. A list of 5-10 descriptive tags (comma-separated).\n"
                                "3. Any notable details (people, location clues, date/time if visible, etc.).\n\n"
                                "Format your response as JSON with keys: "
                                '"description", "tags" (array of strings), "details" (string).'
                            ),
                        },
                    ],
                }
            ],
        )
    elif is_video:
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=512,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"I have a video file from my iPhone named '{filename}' "
                        f"({len(data):,} bytes, {mime}). "
                        "Without seeing the video, provide a helpful placeholder description. "
                        'Return JSON with keys: "description", "tags" (array), "details".'
                    ),
                }
            ],
        )
    else:
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=512,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"I have a file from my iPhone named '{filename}' "
                        f"({len(data):,} bytes, MIME: {mime}). "
                        "Describe what this file likely is and what it might contain. "
                        'Return JSON with keys: "description", "tags" (array), "details".'
                    ),
                }
            ],
        )

    raw = message.content[0].text.strip()

    # Try to extract JSON from the response
    import json, re
    json_match = re.search(r"\{[\s\S]*\}", raw)
    if json_match:
        try:
            result = json.loads(json_match.group())
            return result
        except json.JSONDecodeError:
            pass

    # Fallback: return raw text
    return {"description": raw, "tags": [], "details": ""}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@app.get("/api/search")
async def search_files(
    q: str = Query("", description="Keyword to search for"),
    path: str = Query("/", description="Root path for search"),
    type: str = Query("all", description="Filter: photos, videos, or all"),
):
    _require_browser()
    if not browser.is_connected():
        raise HTTPException(status_code=503, detail="iPhone not connected")

    if type not in ("photos", "videos", "all"):
        raise HTTPException(status_code=400, detail="type must be photos, videos, or all")

    try:
        entries = browser.search(path, q, file_type=type)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return [
        {
            "name": e.name,
            "path": e.path,
            "is_dir": e.is_dir,
            "size": e.size,
            "modified": e.modified,
        }
        for e in entries
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_browser():
    if browser is None:
        raise HTTPException(
            status_code=503,
            detail="pymobiledevice3 not installed. Run: pip install pymobiledevice3",
        )


# ---------------------------------------------------------------------------
# Entry point (for `python main.py`)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=5562, reload=True)
