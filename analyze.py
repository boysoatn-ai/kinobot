"""Filmni tahlil qilish: nutqni matnga aylantirish va ovoz balandligi xaritasi.

Natija film ish papkasiga analysis.json sifatida saqlanadi, shuning uchun bir film
uchun ikkinchi tizer (30 soniyalik yoki boshqa variant) bir zumda tayyorlanadi.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from config import settings
from media import extract_audio, probe
from visual import scene_changes

log = logging.getLogger(__name__)

_model = None
_model_lock = threading.Lock()


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Analysis:
    duration: float
    width: int
    height: int
    has_audio: bool
    segments: list[Segment]
    loudness: list[float]  # har bir soniya uchun dB
    cuts: list[float] = field(default_factory=list)  # sahna almashingan vaqtlar

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_json(cls, s: str) -> "Analysis":
        d = json.loads(s)
        d["segments"] = [Segment(**x) for x in d["segments"]]
        return cls(**d)


def _get_model():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            threads = max(1, os.cpu_count() or 1)
            log.info("Whisper modeli yuklanmoqda: %s (%d oqim)", settings.whisper_model, threads)
            _model = WhisperModel(settings.whisper_model, device="cpu",
                                  compute_type="int8", cpu_threads=threads)
        return _model


def _read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise RuntimeError("Kutilmagan audio formati")
        raw = w.readframes(w.getnframes())
    pcm = np.frombuffer(raw, dtype=np.int16)
    audio = pcm.astype(np.float32)
    audio /= 32768.0
    return audio


def loudness_per_second(audio: np.ndarray, sr: int = 16000) -> list[float]:
    n = len(audio) // sr
    if n == 0:
        return []
    out: list[float] = []
    chunk = 600  # 10 daqiqalik bo'laklarda - 2 soatlik filmda ham xotirani tejaydi
    for i in range(0, n, chunk):
        k = min(chunk, n - i)
        frames = audio[i * sr:(i + k) * sr].reshape(k, sr)
        rms = np.sqrt(np.mean(np.square(frames, dtype=np.float32), axis=1)) + 1e-9
        out += [round(float(x), 1) for x in 20 * np.log10(rms)]
    return out


def analyze(video: Path, workdir: Path,
            progress: Callable[[str, float], None] | None = None) -> Analysis:
    """Filmni tahlil qiladi (keshdan foydalanadi). progress(bosqich, 0..1) chaqiriladi."""
    cache = workdir / "analysis.json"
    if cache.exists():
        try:
            return Analysis.from_json(cache.read_text(encoding="utf-8"))
        except Exception:
            log.warning("Kesh buzilgan, qayta tahlil qilinadi")

    workdir.mkdir(parents=True, exist_ok=True)
    info = probe(video)

    if progress:
        progress("scenes", 0.0)
    cuts = scene_changes(video, info.duration)
    log.info("Sahna almashinuvlari: %d", len(cuts))

    segments: list[Segment] = []
    loud: list[float] = []

    # Filmning oxirgi qismi (spoyler zonasi) tizerda hech qachon ishlatilmaydi - uni tinglash shart emas.
    # Bu 2 soatlik filmda 15-20 daqiqa tejaydi.
    listen_until = info.duration
    if info.duration > 600:
        listen_until = min(info.duration, info.duration * settings.spoiler_guard + 30)

    if info.has_audio:
        wav = workdir / "audio.wav"
        try:
            extract_audio(video, wav, listen_until if listen_until < info.duration else None)
            audio = _read_wav(wav)
        finally:
            wav.unlink(missing_ok=True)
        loud = loudness_per_second(audio)

        model = _get_model()
        seg_iter, _ = model.transcribe(
            audio,
            language=settings.language,
            beam_size=settings.whisper_beam,
            vad_filter=True,
            condition_on_previous_text=False,  # uzun filmlarda takrorlanib qolishning oldini oladi
        )
        last_report = 0.0
        for s in seg_iter:
            text = s.text.strip()
            if text:
                segments.append(Segment(round(s.start, 2), round(s.end, 2), text))
            if progress and s.end - last_report >= 30:
                last_report = s.end
                progress("speech", min(s.end / listen_until, 0.99))
        del audio

    result = Analysis(duration=info.duration, width=info.width, height=info.height,
                      has_audio=info.has_audio, segments=segments, loudness=loud, cuts=cuts)
    cache.write_text(result.to_json(), encoding="utf-8")
    if progress:
        progress("speech", 1.0)
    return result
