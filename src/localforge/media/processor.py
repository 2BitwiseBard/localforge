"""Media processing: video thumbnailing, metadata extraction, format detection.

Uses ffmpeg/ffprobe via subprocess — no heavy Python video dependencies.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("media-processor")

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}

# Semaphore to limit concurrent ffmpeg processes
_ffmpeg_sem = asyncio.Semaphore(2)


def is_video(filename: str) -> bool:
    return Path(filename).suffix.lower() in VIDEO_EXTENSIONS


def is_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in IMAGE_EXTENSIONS


def media_type(filename: str) -> str:
    """Return 'video', 'image', or 'unknown'."""
    if is_video(filename):
        return "video"
    if is_image(filename):
        return "image"
    return "unknown"


def content_type_for(filename: str) -> str:
    """Return MIME type for a media file."""
    ext = Path(filename).suffix.lower()
    return {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".mkv": "video/x-matroska",
        ".avi": "video/x-msvideo",
        ".m4v": "video/mp4",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".svg": "image/svg+xml",
    }.get(ext, "application/octet-stream")


async def create_video_thumbnail(
    video_path: Path,
    output_path: Path,
    time_offset: float = 1.0,
    width: int = 320,
) -> bool:
    """Extract a single frame from a video as a JPEG thumbnail.

    Returns True on success, False on failure.
    """
    async with _ffmpeg_sem:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y",
                "-ss", str(time_offset),
                "-i", str(video_path),
                "-vframes", "1",
                "-vf", f"scale={width}:-1",
                "-q:v", "2",
                str(output_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                log.warning(f"ffmpeg thumbnail failed for {video_path}: {stderr.decode()[:200]}")
                return False
            return output_path.exists()
        except FileNotFoundError:
            log.error("ffmpeg not found — install with: sudo apt install ffmpeg")
            return False
        except asyncio.TimeoutError:
            log.warning(f"ffmpeg thumbnail timed out for {video_path}")
            return False
        except Exception as e:
            log.error(f"Thumbnail creation error: {e}")
            return False


async def get_video_metadata(video_path: Path) -> Optional[dict]:
    """Extract video metadata using ffprobe.

    Returns dict with duration, width, height, codec, fps, or None on error.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(video_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return None

        data = json.loads(stdout.decode())
        fmt = data.get("format", {})

        # Find video stream
        video_stream = None
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                video_stream = stream
                break

        result = {
            "duration": float(fmt.get("duration", 0)),
            "size_bytes": int(fmt.get("size", 0)),
            "format_name": fmt.get("format_name", ""),
        }

        if video_stream:
            result.update({
                "width": int(video_stream.get("width", 0)),
                "height": int(video_stream.get("height", 0)),
                "codec": video_stream.get("codec_name", ""),
                "fps": _parse_fps(video_stream.get("r_frame_rate", "0/1")),
            })

        return result

    except FileNotFoundError:
        log.error("ffprobe not found — install with: sudo apt install ffmpeg")
        return None
    except Exception as e:
        log.error(f"Metadata extraction error: {e}")
        return None


def _parse_fps(rate_str: str) -> float:
    """Parse ffprobe r_frame_rate like '30/1' to float."""
    try:
        if "/" in rate_str:
            num, den = rate_str.split("/")
            return round(float(num) / float(den), 2) if float(den) else 0
        return float(rate_str)
    except (ValueError, ZeroDivisionError):
        return 0


def format_duration(seconds: float) -> str:
    """Format seconds as HH:MM:SS or MM:SS."""
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_file_size(size_bytes: int) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f}{unit}" if unit != "B" else f"{size_bytes}{unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f}TB"
