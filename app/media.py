"""Fetch the reel and render it down to what the model needs: audio + frames.

Two ways in:

`fetch_direct` takes a media URL Meta itself handed us in a DM webhook. It is a
plain download — sanctioned, no cookies, nothing to break when Meta reshuffles
its HTML. Prefer it whenever it is available.

`fetch` scrapes a page URL with yt-dlp, for links that arrive through the iOS
share sheet instead of a DM. That path needs a logged-in cookie jar and will
break periodically.

Both converge on the same ffmpeg rendering.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx


class MediaError(RuntimeError):
    """Raised when the reel could not be fetched or decoded."""


@dataclass
class Media:
    # None when the reel carries no audio track at all — silent, or a
    # video-only stream. The frames still stand on their own.
    audio_path: Path | None
    frames: list[bytes] = field(default_factory=list)
    title: str | None = None
    description: str | None = None
    uploader: str | None = None
    duration: float | None = None


def fetch(url: str, workdir: Path, *, cookies_file: str | None, frame_count: int,
          max_duration_seconds: int) -> Media:
    """Scrape a reel page URL. The fragile path — see the module docstring."""
    _require_ffmpeg()
    info, video_path = _download(url, workdir, cookies_file)
    return _render(video_path, workdir, info, frame_count, max_duration_seconds)


def fetch_direct(media_url: str, workdir: Path, *, frame_count: int,
                 max_duration_seconds: int, title: str | None = None) -> Media:
    """Download a media URL Meta gave us directly. The sanctioned path."""
    _require_ffmpeg()
    video_path = workdir / "source.mp4"
    try:
        with httpx.stream("GET", media_url, follow_redirects=True, timeout=60.0) as response:
            response.raise_for_status()
            with video_path.open("wb") as fh:
                for chunk in response.iter_bytes(chunk_size=1 << 16):
                    fh.write(chunk)
    except httpx.HTTPError as exc:
        raise MediaError(
            f"Could not download the media Meta pointed at ({exc}). These CDN links "
            "expire quickly — a delayed retry will not help."
        ) from exc

    if video_path.stat().st_size == 0:
        raise MediaError("Meta's media URL returned an empty file.")

    return _render(video_path, workdir, {"title": title}, frame_count, max_duration_seconds)


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise MediaError("ffmpeg is not installed or not on PATH.")


def _render(video_path: Path, workdir: Path, info: dict, frame_count: int,
            max_duration_seconds: int) -> Media:
    duration = info.get("duration") or _probe_duration(video_path)
    if duration and duration > max_duration_seconds:
        raise MediaError(
            f"Reel is {duration:.0f}s, longer than the {max_duration_seconds}s limit."
        )

    # Plenty of reels are silent — a caption over music the platform stripped,
    # or a video-only stream, which is what Instagram tends to hand a datacenter
    # IP. There is nothing to transcribe then, and that is not a failure: the
    # frames still carry the whole note.
    audio_path: Path | None = None
    if _has_audio_stream(video_path):
        audio_path = workdir / "audio.wav"
        _run(
            # 16 kHz mono is what Whisper wants; anything richer is thrown away.
            ["ffmpeg", "-nostdin", "-y", "-i", str(video_path),
             "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(audio_path)],
            "extract audio",
        )

    return Media(
        audio_path=audio_path,
        frames=_extract_frames(video_path, workdir, duration, frame_count),
        title=info.get("title"),
        description=info.get("description"),
        uploader=info.get("uploader") or info.get("channel"),
        duration=duration,
    )


def _has_audio_stream(video_path: Path) -> bool:
    """Ask before extracting: ffmpeg's failure here is indistinguishable from a
    real error, and a silent reel is not one."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True,
    )
    return bool(result.stdout.strip())


def _probe_duration(video_path: Path) -> float | None:
    """A direct download carries no metadata, so ask the file itself."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def _download(url: str, workdir: Path, cookies_file: str | None) -> tuple[dict, Path]:
    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise MediaError("yt-dlp is not installed.") from exc

    opts: dict = {
        "outtmpl": str(workdir / "source.%(ext)s"),
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
    }
    if cookies_file:
        opts["cookiefile"] = cookies_file

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:
        raise MediaError(
            f"Could not fetch the reel ({exc}). Most Instagram and Facebook URLs need a "
            "logged-in cookie jar — see YTDLP_COOKIES_FILE in the README."
        ) from exc

    candidates = sorted(workdir.glob("source.*"), key=lambda p: p.stat().st_size, reverse=True)
    if not candidates:
        raise MediaError("yt-dlp reported success but wrote no file.")
    return info, candidates[0]


def _extract_frames(video: Path, workdir: Path, duration: float | None, count: int) -> list[bytes]:
    """Evenly spaced stills.

    Reels routinely put the actual numbers on screen and never say them aloud,
    so the frames are not decoration — without them the transcript alone loses
    the part you wanted.
    """
    if count <= 0:
        return []
    span = duration or 30.0
    # Sample strictly inside the clip; the first and last frames are usually a
    # title card and a "follow for more".
    offsets = [span * (i + 1) / (count + 1) for i in range(count)]

    frames: list[bytes] = []
    for index, offset in enumerate(offsets):
        out = workdir / f"frame_{index}.jpg"
        try:
            _run(
                ["ffmpeg", "-nostdin", "-y", "-ss", f"{offset:.2f}", "-i", str(video),
                 "-frames:v", "1", "-vf", "scale=768:-2", "-q:v", "4", str(out)],
                f"extract frame at {offset:.1f}s",
            )
        except MediaError:
            continue  # A missing frame is survivable; a missing transcript is not.
        if out.exists():
            frames.append(out.read_bytes())
    return frames


def _run(cmd: list[str], what: str) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-3:]
        raise MediaError(f"ffmpeg failed to {what}: {' '.join(tail)}")
