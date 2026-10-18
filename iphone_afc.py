"""
iphone_afc.py — AFC (Apple File Conduit) browser for pymobiledevice3.

Each public method creates a fresh AFC connection in its own thread/event loop
to avoid cross-thread event loop issues with pymobiledevice3's async API.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

IPHONE_UDID = os.getenv("IPHONE_UDID", "")


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------

def _run_in_thread(coro_factory):
    """Run an async operation in a dedicated thread with its own event loop."""
    def _worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro_factory())
        finally:
            loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_worker).result()


async def _make_afc(udid: str):
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService
    lockdown = await create_using_usbmux(serial=udid)
    return AfcService(lockdown)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class FileEntry:
    name: str
    path: str
    is_dir: bool
    size: int
    modified: float


# ---------------------------------------------------------------------------
# AFC browser
# ---------------------------------------------------------------------------

class AFCBrowser:
    def __init__(self, udid: Optional[str] = None):
        self.udid = udid or IPHONE_UDID

    def is_connected(self) -> bool:
        import subprocess
        try:
            result = subprocess.run(["idevice_id", "-l"], capture_output=True, text=True, timeout=3)
            return bool(self.udid) and self.udid in result.stdout.splitlines()
        except Exception:
            return False

    def device_info(self) -> dict:
        import subprocess
        info = {"name": "Unknown", "identifier": self.udid, "product_type": "", "product_version": "", "build_version": ""}
        for field, key in [("name", "DeviceName"), ("product_type", "ProductType"), ("product_version", "ProductVersion")]:
            try:
                r = subprocess.run(["ideviceinfo", "-u", self.udid, "-k", key], capture_output=True, text=True, timeout=5)
                if r.returncode == 0 and r.stdout.strip():
                    info[field] = r.stdout.strip()
            except Exception:
                pass
        return info

    def list_dir(self, path: str) -> list[FileEntry]:
        path = _normalise(path)
        udid = self.udid

        async def _list():
            afc = await _make_afc(udid)
            try:
                names = await afc.listdir(path)
            except Exception as exc:
                raise RuntimeError(f"Cannot list {path!r}: {exc}") from exc

            entries: list[FileEntry] = []
            for name in sorted(names):
                if name in (".", ".."):
                    continue
                child_path = path.rstrip("/") + "/" + name
                try:
                    info = await afc.stat(child_path)
                    is_dir = _is_directory(info)
                    size = int(info.get("st_size", 0)) if not is_dir else 0
                    mtime = info.get("st_mtime", 0)
                    if hasattr(mtime, "timestamp"):
                        mtime = mtime.timestamp()
                    mtime = _parse_mtime(mtime)
                except Exception:
                    is_dir = False
                    size = 0
                    mtime = 0.0
                entries.append(FileEntry(name=name, path=child_path, is_dir=is_dir, size=size, modified=mtime))

            entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
            return entries

        return _run_in_thread(lambda: _list())

    def get_file_info(self, path: str) -> FileEntry:
        path = _normalise(path)
        udid = self.udid

        async def _stat():
            afc = await _make_afc(udid)
            try:
                info = await afc.stat(path)
            except Exception as exc:
                raise RuntimeError(f"Cannot stat {path!r}: {exc}") from exc
            name = path.rsplit("/", 1)[-1] or "/"
            is_dir = _is_directory(info)
            size = int(info.get("st_size", 0)) if not is_dir else 0
            mtime = info.get("st_mtime", 0)
            if hasattr(mtime, "timestamp"):
                mtime = mtime.timestamp()
            return FileEntry(name=name, path=path, is_dir=is_dir, size=size, modified=_parse_mtime(mtime))

        return _run_in_thread(lambda: _stat())

    def read_file(self, path: str) -> bytes:
        path = _normalise(path)
        udid = self.udid

        async def _read():
            afc = await _make_afc(udid)
            async with afc.open(path, "rb") as fh:
                return await fh.read()

        try:
            return _run_in_thread(lambda: _read())
        except Exception as exc:
            raise RuntimeError(f"Cannot read {path!r}: {exc}") from exc

    def read_file_chunked(self, path: str, chunk_size: int = 65536):
        data = self.read_file(path)
        for i in range(0, len(data), chunk_size):
            yield data[i:i + chunk_size]

    def search(self, root: str, keyword: str, file_type: str = "all") -> list[FileEntry]:
        root = _normalise(root)
        keyword_lower = keyword.lower()
        udid = self.udid

        async def _search():
            afc = await _make_afc(udid)
            results: list[FileEntry] = []

            async def _walk(path: str, depth: int):
                if depth > 12 or len(results) >= 500:
                    return
                try:
                    names = await afc.listdir(path)
                except Exception:
                    return
                for name in names:
                    if name in (".", ".."):
                        continue
                    child = path.rstrip("/") + "/" + name
                    try:
                        info = await afc.stat(child)
                        is_dir = _is_directory(info)
                    except Exception:
                        continue
                    if is_dir:
                        await _walk(child, depth + 1)
                    else:
                        if keyword_lower and keyword_lower not in name.lower():
                            continue
                        if file_type == "photos" and not _is_photo(name):
                            continue
                        if file_type == "videos" and not _is_video(name):
                            continue
                        mtime = info.get("st_mtime", 0)
                        if hasattr(mtime, "timestamp"):
                            mtime = mtime.timestamp()
                        results.append(FileEntry(
                            name=name, path=child, is_dir=False,
                            size=int(info.get("st_size", 0)),
                            modified=_parse_mtime(mtime),
                        ))

            await _walk(root, 0)
            return results

        return _run_in_thread(lambda: _search())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise(path: str) -> str:
    path = "/" + path.strip("/")
    return path or "/"


def _is_directory(info: dict) -> bool:
    ifmt = info.get("st_ifmt", "")
    if isinstance(ifmt, str):
        return ifmt in ("S_IFDIR", "S_IFLNK")
    mode = int(info.get("st_mode", 0))
    return stat.S_ISDIR(mode)


def _parse_mtime(raw) -> float:
    if raw is None:
        return 0.0
    val = float(raw)
    if val > 1e12:
        val = val / 1e9
    if val < 946684800 or val > 4102444800:
        return 0.0
    return val


def _is_photo(name: str) -> bool:
    return name.lower().rsplit(".", 1)[-1] in {
        "jpg", "jpeg", "png", "gif", "heic", "heif", "webp", "bmp", "tiff", "tif", "raw", "dng",
    }


def _is_video(name: str) -> bool:
    return name.lower().rsplit(".", 1)[-1] in {
        "mp4", "mov", "m4v", "avi", "mkv", "3gp", "wmv", "flv", "webm",
    }
