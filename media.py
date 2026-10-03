"""FFmpeg/FFprobe bilan ishlash uchun yordamchi funksiyalar."""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


class MediaError(RuntimeError):
    pass


def run(cmd: list[str], cwd: Path | None = None, timeout: int | None = None,
        want_stderr: bool = False) -> str:
    log.debug("RUN %s", " ".join(cmd))
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise MediaError(f"{cmd[0]} juda uzoq ishladi va to'xtatildi") from e
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-8:]
        raise MediaError(f"{cmd[0]} xatosi:\n" + "\n".join(tail))
    return (r.stderr or "") if want_stderr else r.stdout


@dataclass(frozen=True)
class VideoInfo:
    duration: float
    width: int
    height: int
    has_audio: bool


def probe(path: Path) -> VideoInfo:
    out = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", str(path)], timeout=120)
    data = json.loads(out)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise MediaError("Faylda video oqimi topilmadi")
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise MediaError("Video davomiyligini aniqlab bo'lmadi")
    w, h = int(video.get("width", 0)), int(video.get("height", 0))
    # Telefon videolarida burilish (rotation) bo'lsa, eni va bo'yini almashtiramiz
    rotation = 0
    for sd in video.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = abs(int(sd["rotation"]))
    if rotation in (90, 270):
        w, h = h, w
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return VideoInfo(duration=duration, width=w, height=h, has_audio=has_audio)


def extract_audio(video: Path, wav: Path, limit: float | None = None) -> None:
    """16 kHz mono 16-bit WAV (Whisper uchun). limit - faqat shu soniyagacha."""
    cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(video), "-map", "0:a:0",
           "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"]
    if limit:
        cmd += ["-t", f"{limit:.1f}"]
    run(cmd + [str(wav)], timeout=3600)
