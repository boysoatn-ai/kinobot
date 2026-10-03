"""Montaj: tanlangan bo'laklarni kesib, yumshoq o'tishlar, yozuvlar va yakuniy kadr bilan bitta rolikka yig'adi."""
from __future__ import annotations

import logging
import shutil
import textwrap
from pathlib import Path

from analyze import Analysis
from config import settings
from highlights import TeaserPlan
from media import MediaError, run

log = logging.getLogger(__name__)

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]
FPS = 30
FADE = 0.2


def _font() -> str | None:
    return next((f for f in FONT_CANDIDATES if Path(f).exists()), None)


def _even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


def output_size(an: Analysis) -> tuple[int, int]:
    w, h = an.width or 1280, an.height or 720
    if w > settings.max_width:
        h = h * settings.max_width / w
        w = settings.max_width
    return _even(w), _even(h)


def _wrap(text: str, width: int) -> str:
    return "\n".join(textwrap.wrap(text.strip(), width=width)[:3])


def _drawtext(textfile: str, h: int, y: str, size_div: int, enable: str | None = None) -> str:
    font = _font()
    if not font:
        return ""
    f = (f"drawtext=fontfile={font}:textfile={textfile}:expansion=none:"
         f"fontcolor=white:fontsize={max(18, h // size_div)}:line_spacing=8:"
         f"box=1:boxcolor=black@0.55:boxborderw=18:x=(w-text_w)/2:y={y}")
    if enable:
        f += f":enable='{enable}'"
    return f


def _render_clip(video: Path, start: float, dur: float, out: Path, size: tuple[int, int],
                 has_audio: bool, overlay: str = "") -> None:
    w, h = size
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={FPS},"
          f"fade=t=in:st=0:d={FADE},fade=t=out:st={max(0.0, dur - FADE):.3f}:d={FADE}")
    if overlay:
        vf += "," + overlay
    af = (f"aresample=48000,aformat=channel_layouts=stereo,"
          f"afade=t=in:st=0:d={FADE},afade=t=out:st={max(0.0, dur - 0.3):.3f}:d=0.3")
    cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
           "-ss", f"{start:.3f}", "-i", str(video.resolve())]
    if not has_audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    cmd += ["-t", f"{dur:.3f}", "-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0",
            "-vf", vf, "-af", af,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
            "-video_track_timescale", "15360", out.name]
    run(cmd, cwd=out.parent, timeout=600)


def _render_end_card(out: Path, size: tuple[int, int], textfile: str, dur: float = 2.5) -> None:
    w, h = size
    vf = f"fade=t=in:st=0:d=0.4"
    dt = _drawtext(textfile, h, "(h-text_h)/2", 16)
    if dt:
        vf += "," + dt
    run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:r={FPS}:d={dur}",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
         "-t", f"{dur}", "-vf", vf,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
         "-video_track_timescale", "15360", out.name], cwd=out.parent, timeout=120)


def render_teaser(video: Path, an: Analysis, plan: TeaserPlan, out_path: Path, title: str) -> Path:
    """Rejaga ko'ra yakuniy rolikni yig'adi va yo'lini qaytaradi."""
    if not plan.clips:
        raise MediaError("Montaj uchun bo'lak topilmadi")
    tmp = out_path.parent / (out_path.stem + "_parts")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    size = output_size(an)
    w, h = size
    chars = max(18, w // 34)

    try:
        parts: list[Path] = []
        # Boshidagi "ilmoq" yozuvi birinchi bo'lakning birinchi ~2.8 soniyasida
        hook = plan.hook.strip()
        if hook:
            (tmp / "hook.txt").write_text(_wrap(hook, chars), encoding="utf-8")
        for i, c in enumerate(plan.clips):
            part = tmp / f"p{i:02}.mp4"
            overlay = ""
            if i == 0 and hook:
                overlay = _drawtext("hook.txt", h, "h-text_h-h/10", 18,
                                    enable=f"lt(t,{min(2.8, c.dur - 0.3):.2f})")
            _render_clip(video, c.start, c.dur, part, size, an.has_audio, overlay)
            parts.append(part)

        end_text = plan.ending.strip() or "Davomini filmda ko'ring"
        (tmp / "end.txt").write_text(_wrap(end_text, chars) + "\n\n" + _wrap(title, chars),
                                     encoding="utf-8")
        end = tmp / "zz_end.mp4"
        _render_end_card(end, size, "end.txt")
        parts.append(end)

        lst = tmp / "list.txt"
        lst.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
        run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", lst.name, "-c", "copy", "-movflags", "+faststart", str(out_path.resolve())],
            cwd=tmp, timeout=300)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out_path
