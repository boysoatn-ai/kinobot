"""Vizual tahlil: sahna almashinuvlari (montaj tezligi) va kadrlarni ajratib olish.

Sahna almashinuvi zich bo'lgan joylar odatda harakat, jang, quvish yoki keskin lahzalardir.
Kadrlar esa AI rejissyorga "ko'rish" imkonini beradi: u lahzani faqat matndan emas, rasmidan ham baholaydi.
"""
from __future__ import annotations

import base64
import logging
import re
from pathlib import Path

from media import run

log = logging.getLogger(__name__)

THUMB_W = 480


def scene_changes(video: Path, duration: float, threshold: float = 0.3) -> list[float]:
    """Sahna almashingan vaqtlar (soniya).

    Butun filmni dekodlash 2 soatlik filmda 20+ daqiqa oladi, shuning uchun faqat kalit kadrlar
    (keyframe) dekodlanadi - bu 20-50 baravar tez va montaj zichligini aniqlash uchun yetarli.
    """
    try:
        err = run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info",
                   "-skip_frame", "nokey", "-i", str(video.resolve()),
                   "-an", "-sn", "-vf", f"scale=160:-2,select='gt(scene,{threshold})',showinfo",
                   "-vsync", "vfr", "-f", "null", "-"],
                  timeout=max(300, int(duration * 0.3)), want_stderr=True)
    except Exception as e:
        log.warning("Sahna tahlili bajarilmadi: %s", e)
        return []
    return _parse_showinfo(err)


def _parse_showinfo(text: str) -> list[float]:
    times = [float(m) for m in re.findall(r"pts_time:\s*([0-9.]+)", text)]
    return sorted(set(round(t, 2) for t in times))


def cut_density(cuts: list[float], duration: float, window: int = 10) -> list[int]:
    """Har bir `window` soniyalik oynadagi sahna almashinuvlari soni."""
    n = int(duration // window) + 1
    dens = [0] * n
    for t in cuts:
        i = int(t // window)
        if 0 <= i < n:
            dens[i] += 1
    return dens


def extract_frame(video: Path, t: float, out: Path, width: int = THUMB_W) -> Path | None:
    try:
        run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-ss", f"{max(0.0, t):.2f}",
             "-i", str(video.resolve()), "-frames:v", "1", "-vf", f"scale={width}:-2",
             "-q:v", "6", str(out)], timeout=60)
        return out if out.exists() and out.stat().st_size > 0 else None
    except Exception as e:
        log.warning("Kadr olinmadi (%.1f s): %s", t, e)
        return None


def frame_to_b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")
