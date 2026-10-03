"""AI rejissyor: filmni "o'qiydi", "eshitadi" va "ko'radi", so'ng tizer montaj rejasini tuzadi.

Bosqichlar:
  0. research_film()   - internetdan film haqida spoylersiz ma'lumot (ixtiyoriy, keshlanadi)
  1. understand_story() - butun nutq matni + ovoz + sahna xaritasi asosida Claude filmni tushunadi:
                          qahramon, asosiy sir, janr, va 14-18 ta nomzod lahza tanlaydi
  2. direct_teaser()   - har bir nomzod lahzaning KADRINI ko'rib (vision), Claude yakuniy
                          montajni tuzadi: tartib, uzunlik, boshlang'ich yozuv, yakuniy savol
  3. sanitize()        - kod rejani qat'iy tekshiradi (chegaralar, qoplanish, uzunlik, spoyler zonasi)
  4. fallback_plan()   - AI ishlamasa: ovoz avjlari + sahna zichligi bo'yicha oddiy tizer
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import anthropic
import numpy as np

from analyze import Analysis, Segment
from config import settings
import usage
from visual import cut_density, extract_frame, frame_to_b64

log = logging.getLogger(__name__)

MIN_CLIP = 2.5
MAX_CLIP = 14.0
SNAP = 1.5
MAX_CANDIDATES = 18


@dataclass
class Clip:
    start: float
    end: float
    why: str = ""

    @property
    def dur(self) -> float:
        return self.end - self.start


@dataclass
class Candidate:
    t: float            # lahza markazi
    start: float
    end: float
    why: str
    kind: str           # "ai" / "loud" / "cuts"
    frame: Path | None = None


@dataclass
class Story:
    genre: str = ""
    protagonist: str = ""
    hook_question: str = ""   # tomoshabinni ushlab turadigan asosiy savol
    tone: str = ""
    spoiler_notes: str = ""
    candidates: list[Candidate] = field(default_factory=list)


@dataclass
class TeaserPlan:
    clips: list[Clip]
    hook: str
    ending: str
    summary: str
    source: str  # "ai+vision" / "ai" / "fallback"


# ----------------------------------------------------------------- umumiy
def _client() -> anthropic.Anthropic:
    headers = {}
    if settings.anthropic_workspace_id:
        headers["anthropic-workspace-id"] = settings.anthropic_workspace_id
    return anthropic.Anthropic(max_retries=3, timeout=240, default_headers=headers or None)


def _ts(t: float) -> str:
    t = int(t)
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def _tool_input(resp, name: str) -> dict:
    for block in resp.content:
        if block.type == "tool_use" and block.name == name:
            return block.input
    raise RuntimeError(f"AI {name} natijasini qaytarmadi")


def _loud_moments(loudness: list[float], limit_end: float, top: int = 25) -> list[tuple[int, float]]:
    """Ovoz keskin ko'tarilgan soniyalar (qichqiriq, portlash, musiqa avji)."""
    if not loudness:
        return []
    arr = np.array(loudness[: int(limit_end)], dtype=float)
    if len(arr) < 10:
        return []
    base = np.convolve(arr, np.ones(15) / 15, mode="same")
    jump = arr - base
    picks: list[tuple[int, float]] = []
    for i in np.argsort(-jump):
        if jump[i] < 6:
            break
        if all(abs(int(i) - p) > 20 for p, _ in picks):
            picks.append((int(i), round(float(jump[i]), 1)))
        if len(picks) >= top:
            break
    return sorted(picks)


def _dense_cut_windows(an: Analysis, limit_end: float, top: int = 12) -> list[tuple[float, int]]:
    """Sahna almashinuvi eng zich 10 soniyalik oynalar (harakat, jang, quvish)."""
    dens = cut_density(an.cuts, min(an.duration, limit_end), 10)
    if not dens:
        return []
    med = float(np.median([d for d in dens if d > 0] or [0]))
    out = []
    for i in np.argsort(dens)[::-1]:
        if dens[i] < max(4, med * 2):
            break
        out.append((i * 10 + 5.0, int(dens[i])))
        if len(out) >= top:
            break
    return sorted(out)


def _transcript_text(segments: list[Segment], limit_end: float, max_chars: int = 160_000) -> str:
    lines = [f"[{_ts(s.start)} | {s.start:.0f}s] {s.text}" for s in segments if s.end <= limit_end]
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    step = int(np.ceil(len(text) / max_chars))
    merged = []
    segs = [s for s in segments if s.end <= limit_end]
    for i in range(0, len(segs), step):
        chunk = segs[i:i + step]
        merged.append(f"[{_ts(chunk[0].start)} | {chunk[0].start:.0f}s] " + " ".join(c.text for c in chunk))
    return "\n".join(merged)


# ----------------------------------------------------------------- 0. ma'lumot
def research_film(title: str) -> str:
    if not title or title.lower() in ("film", "video", "kino"):
        return ""
    prompt = (
        f"\"{title}\" nomli film haqida internetdan qisqa ma'lumot top va o'zbek tilida yoz:\n"
        "1) Janri, yili, davlati, rejissyori.\n"
        "2) Syujet boshlanishi (spoylersiz, 2-3 jumla).\n"
        "3) Tomoshabinni nima ushlab turadi: asosiy sir, xavf yoki savol.\n"
        "4) Tanqidchilar va tomoshabinlar eng ko'p eslaydigan kuchli sahnalar (yakunni aytmasdan).\n"
        "5) Alohida 'SPOYLER:' qatorida - yakuni va asosiy burilish (twist). Bu faqat tizerga KIRITMASLIK uchun.\n"
        "Agar bunday film topilmasa, faqat 'TOPILMADI' deb yoz."
    )
    messages: list[dict] = [{"role": "user", "content": prompt}]
    try:
        client = _client()
        resp = None
        for _ in range(4):
            resp = client.messages.create(
                model=settings.claude_model, max_tokens=1500,
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
                messages=messages)
            usage.record("research", resp)
            if resp.stop_reason != "pause_turn":
                break
            messages = [messages[0], {"role": "assistant", "content": resp.content}]
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        return "" if "TOPILMADI" in text.upper()[:60] else text[:4000]
    except Exception as e:
        log.warning("Film ma'lumotini topib bo'lmadi: %s", e)
        return ""


# ----------------------------------------------------------------- 1. tushunish
STORY_TOOL = {
    "name": "submit_story",
    "description": "Film tahlili va tizer uchun nomzod lahzalar.",
    "input_schema": {
        "type": "object",
        "properties": {
            "genre": {"type": "string"},
            "protagonist": {"type": "string", "description": "asosiy qahramon(lar) va ularning maqsadi, 1 jumla"},
            "hook_question": {"type": "string", "description": "tomoshabinni filmga bog'lab turadigan asosiy savol"},
            "tone": {"type": "string", "description": "filmning kayfiyati: qo'rqinchli, hazil, drama..."},
            "spoiler_notes": {"type": "string", "description": "qaysi vaqtlar/faktlar spoyler - tizerga kirmasin"},
            "candidates": {
                "type": "array",
                "description": "14-18 ta nomzod lahza, har xil turdagi: sirli dialog, keskin harakat, hissiy lahza, hazil, xavf",
                "items": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "number", "description": "soniya"},
                        "end": {"type": "number", "description": "soniya, start dan 3-12 soniya keyin"},
                        "why": {"type": "string", "description": "nima uchun kuchli, 1 jumla"},
                    },
                    "required": ["start", "end", "why"],
                },
            },
        },
        "required": ["genre", "protagonist", "hook_question", "tone", "spoiler_notes", "candidates"],
    },
}

STORY_SYSTEM = """Sen kino treylerlari ustasi va ssenariy tahlilchisisan. Senga filmning to'liq nutq matni
(vaqt belgilari bilan), ovoz avjlari va sahna almashinuvi zich joylari beriladi.

Vazifang:
1. Filmni tushun: janr, qahramon, asosiy savol/sir, kayfiyat.
2. Tizer uchun 14-18 ta eng kuchli NOMZOD lahzani top. Har xil turda bo'lsin:
   - sirli yoki keskin DIALOG (eng yaxshi gaplar, savollar, tahdidlar)
   - HARAKAT lahzalari (baland ovoz, zich montaj - nutqsiz ham bo'ladi)
   - HISSIY lahzalar (qo'rquv, yig'i, muhabbat, hazil - janrga qarab)
   - XAVF yoki to'qnashuv ko'rinadigan joylar
3. Vaqtlarni matndagi gap chegaralariga moslab ol - gap o'rtasidan boshlanmasin/tugamasin.
4. SPOYLER taqiqlanadi: yakun, asosiy burilish, kim o'lishi, sirning javobi. Agar ma'lumotda
   SPOYLER qatori bo'lsa, o'sha narsalarni ko'rsatadigan lahzalarni OLMA.
5. Nomzodlar filmning turli qismlaridan bo'lsin (hammasi bir joydan emas).
Javobni faqat submit_story vositasi orqali ber."""


def understand_story(an: Analysis, title: str, limit_end: float, info: str) -> Story:
    loud = _loud_moments(an.loudness, limit_end)
    loud_txt = "\n".join(f"{_ts(t)} ({t}s): +{d} dB" for t, d in loud) or "(yo'q)"
    dense = _dense_cut_windows(an, limit_end)
    dense_txt = "\n".join(f"{_ts(t)} ({t:.0f}s): {n} ta kesim/10s" for t, n in dense) or "(yo'q)"
    user = f"""Film nomi: {title}
Davomiyligi: {_ts(an.duration)} ({an.duration:.0f}s). Spoyler xavfi tufayli faqat 0 - {limit_end:.0f}s oralig'idan foydalan.

Film haqida internetdan topilgan ma'lumot:
{info or "(topilmadi - faqat matn asosida ishla)"}

Ovoz keskin ko'tarilgan lahzalar (qichqiriq, portlash, musiqa avji):
{loud_txt}

Sahna almashinuvi zich joylar (harakat, jang, quvish):
{dense_txt}

To'liq nutq matni:
{_transcript_text(an.segments, limit_end) or "(nutq topilmadi - ovoz va sahna xaritasidan foydalan)"}"""

    resp = _client().messages.create(
        model=settings.claude_model, max_tokens=4000, system=STORY_SYSTEM,
        tools=[STORY_TOOL], tool_choice={"type": "tool", "name": "submit_story"},
        messages=[{"role": "user", "content": user}])
    usage.record("story", resp)
    data = _tool_input(resp, "submit_story")
    story = Story(genre=str(data.get("genre", ""))[:100], protagonist=str(data.get("protagonist", ""))[:300],
                  hook_question=str(data.get("hook_question", ""))[:300], tone=str(data.get("tone", ""))[:100],
                  spoiler_notes=str(data.get("spoiler_notes", ""))[:500])
    for c in data.get("candidates", []):
        try:
            s, e = float(c["start"]), float(c["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= s < limit_end and e > s:
            e = min(e, s + MAX_CLIP, limit_end)
            if e - s >= MIN_CLIP:
                story.candidates.append(Candidate((s + e) / 2, s, e, str(c.get("why", ""))[:200], "ai"))
    return story


def _add_signal_candidates(story: Story, an: Analysis, limit_end: float) -> None:
    """AI nomzodlariga ovoz va sahna signallaridan bir nechta nutqsiz lahzalarni qo'shamiz."""
    def far(t: float) -> bool:
        return all(abs(t - c.t) > 15 for c in story.candidates)
    for t, d in _loud_moments(an.loudness, limit_end, top=10):
        if far(t) and len(story.candidates) < MAX_CANDIDATES + 4:
            story.candidates.append(Candidate(t, max(0, t - 3), min(limit_end, t + 5), f"ovoz avji +{d} dB", "loud"))
    for t, n in _dense_cut_windows(an, limit_end, top=6):
        if far(t) and len(story.candidates) < MAX_CANDIDATES + 6:
            story.candidates.append(Candidate(t, max(0, t - 4), min(limit_end, t + 5), f"zich montaj ({n} kesim)", "cuts"))
    story.candidates.sort(key=lambda c: c.t)


# ----------------------------------------------------------------- 2. rejissyor (vision)
DIRECT_TOOL = {
    "name": "submit_teaser",
    "description": "Yakuniy tizer montaj rejasi.",
    "input_schema": {
        "type": "object",
        "properties": {
            "clips": {
                "type": "array",
                "description": "Montaj tartibida (rolikda shu tartibda). Har biri nomzod raqamiga asoslanadi, vaqtni aniqlashtirish mumkin.",
                "items": {
                    "type": "object",
                    "properties": {
                        "candidate": {"type": "integer", "description": "nomzod raqami (1 dan)"},
                        "start": {"type": "number"},
                        "end": {"type": "number"},
                        "why": {"type": "string"},
                    },
                    "required": ["candidate", "start", "end", "why"],
                },
            },
            "hook": {"type": "string", "description": "rolik boshidagi 3-7 so'zli yozuv, o'zbekcha, sirli/qiziqtiruvchi"},
            "ending": {"type": "string", "description": "oxirgi kadr: 3-9 so'zli javobsiz savol yoki chaqiriq, o'zbekcha"},
            "summary": {"type": "string", "description": "sizga yuboriladigan 2-3 jumlali spoylersiz izoh: film nima haqida va nega ko'rishga arziydi, o'zbekcha"},
            "rejected": {"type": "string", "description": "qaysi nomzodlar nega olinmadi (qisqa)"},
        },
        "required": ["clips", "hook", "ending", "summary"],
    },
}

DIRECT_SYSTEM = """Sen Hollywood treyler montajchisisan. Oldingi bosqichda film tahlil qilingan va nomzod lahzalar
tanlangan. Endi har bir nomzodning KADRI (rasmi) va matni senga ko'rsatiladi.

Kadrlarga qarab baholab, yakuniy tizerni tuz. Tizerning maqsadi: tomoshabin "bu filmni hoziroq ko'rmasam
bo'lmaydi" deb qolsin.

Dramaturgiya:
1. ILMOQ (birinchi bo'lak, 3-6 s): eng sirli yoki eng keskin lahza. Savol tug'dirsin.
2. DUNYO (1-2 bo'lak): qahramon kim, qayerda, nima istaydi.
3. XAVF (2-3 bo'lak): to'qnashuv, tahdid, muammo kuchayadi. Harakat va dialogni aralashtir, sur'at tezlashsin.
4. CLIFFHANGER (oxirgi bo'lak): javobsiz savol yoki eng keskin lahzaning boshlanishi - davomi ko'rsatilmaydi.

Qoidalar:
- Rasmi zerikarli (qora ekran, bo'sh devor, titrlar, logotip) bo'lgan nomzodlarni OLMA.
- Rasmi kuchli (yuz ifodasi, harakat, qo'rquv, go'zal kadr) bo'lganlarini afzal ko'r.
- Har bir bo'lak 3-12 soniya, jami so'ralgan uzunlikka yaqin. Bo'laklar qoplanmasin.
- Gap o'rtasidan kesma: start/end ni berilgan nomzod oralig'i ichida yoki uning gap chegaralarida saqla.
- Spoyler yo'q. Tartib xronologik bo'lishi shart emas - eng ta'sirli tartib.
Javobni faqat submit_teaser orqali ber."""


def direct_teaser(video: Path, workdir: Path, an: Analysis, story: Story, title: str,
                  seconds: int, info: str, avoid: list[Clip], variant: int,
                  progress: Callable[[str], None] | None = None) -> dict:
    frames_dir = workdir / "frames"
    frames_dir.mkdir(exist_ok=True)
    content: list[dict] = []
    n_target = f"{max(4, seconds // 9)}-{max(5, seconds // 6)}"
    header = f"""Film: {title} | Janr: {story.genre} | Kayfiyat: {story.tone}
Qahramon: {story.protagonist}
Asosiy savol: {story.hook_question}
Spoyler eslatmalari: {story.spoiler_notes}
Kerakli uzunlik: {seconds} soniya ({n_target} ta bo'lak). Variant: {variant}.
{"Oldingi variantlardagi oraliqlarni takrorlama, boshqacha tizer tuz: " + ", ".join(f"{c.start:.0f}-{c.end:.0f}s" for c in avoid) if avoid else ""}

Quyida nomzodlar: raqami, vaqti, nega tanlangani, o'sha paytdagi gaplar va KADRI."""
    content.append({"type": "text", "text": header})

    for i, c in enumerate(story.candidates, 1):
        if progress and i % 5 == 1:
            progress(f"{i}/{len(story.candidates)}")
        if c.frame is None:
            c.frame = extract_frame(video, c.t, frames_dir / f"c{i:02}.jpg")
        said = " ".join(s.text for s in an.segments if s.end > c.start - 1 and s.start < c.end + 1)[:400]
        content.append({"type": "text", "text":
                        f"\n--- Nomzod {i} | {_ts(c.start)}-{_ts(c.end)} ({c.start:.1f}-{c.end:.1f}s) | {c.kind}\n"
                        f"Nega: {c.why}\nGaplar: {said or '(nutq yo‘q)'}"})
        if c.frame:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                         "data": frame_to_b64(c.frame)}})
    resp = _client().messages.create(
        model=settings.claude_model, max_tokens=3000, system=DIRECT_SYSTEM,
        tools=[DIRECT_TOOL], tool_choice={"type": "tool", "name": "submit_teaser"},
        messages=[{"role": "user", "content": content}])
    usage.record("direct", resp, images=sum(1 for c in content if c.get("type") == "image"))
    return _tool_input(resp, "submit_teaser")


# ----------------------------------------------------------------- 3. tekshirish
def _snap(t: float, edges: list[float]) -> float:
    if not edges:
        return t
    idx = int(np.searchsorted(edges, t))
    near = [edges[i] for i in (idx - 1, idx) if 0 <= i < len(edges)]
    best = min(near, key=lambda e: abs(e - t))
    return best if abs(best - t) <= SNAP else t


def sanitize(raw: list[dict], an: Analysis, seconds: int, limit_end: float,
             candidates: list[Candidate] | None = None) -> list[Clip]:
    starts = sorted(s.start for s in an.segments)
    ends = sorted(s.end for s in an.segments)
    clips: list[Clip] = []
    for item in raw:
        try:
            s, e = float(item["start"]), float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        # Nomzod oralig'idan juda uzoqlashib ketmasin
        if candidates and "candidate" in item:
            try:
                c = candidates[int(item["candidate"]) - 1]
                s = min(max(s, c.start - 4), c.end)
                e = max(min(e, c.end + 4), s + MIN_CLIP)
            except (IndexError, ValueError, TypeError):
                pass
        s, e = _snap(s, starts), _snap(e, ends)
        s = max(0.0, s)
        e = min(e, limit_end)
        if e - s > MAX_CLIP:
            e = s + MAX_CLIP
        if e - s < MIN_CLIP:
            continue
        if any(not (e <= c.start or s >= c.end) for c in clips):
            continue
        clips.append(Clip(round(s, 2), round(e, 2), str(item.get("why", ""))[:200]))

    target_max = seconds * 1.1
    while len(clips) > 2 and sum(c.dur for c in clips) > target_max:
        mid = clips[1:-1]
        longest = max(mid, key=lambda c: c.dur)
        excess = sum(c.dur for c in clips) - seconds
        if longest.dur - excess >= MIN_CLIP + 1:
            longest.end = round(longest.end - excess, 2)
            break
        clips.remove(longest)
    total = sum(c.dur for c in clips)
    if total > target_max and clips:
        scale = seconds / total
        for c in clips:
            c.end = round(c.start + max(MIN_CLIP, c.dur * scale), 2)
    return clips


# ----------------------------------------------------------------- 4. zaxira
def fallback_plan(an: Analysis, seconds: int, limit_end: float, avoid: list[Clip]) -> list[Clip]:
    n = max(4, seconds // 8)
    each = min(MAX_CLIP, seconds / n)
    cands = [t for t, _ in _loud_moments(an.loudness, limit_end, top=n * 3)]
    cands += [t for t, _ in _dense_cut_windows(an, limit_end, top=n)]
    cands.sort()
    if len(cands) < n:
        step = limit_end / (n + 1)
        cands += [step * (i + 1) for i in range(n)]
    clips: list[Clip] = []
    for t in cands:
        s = max(0.0, t - each / 2)
        e = min(limit_end, s + each)
        if e - s < MIN_CLIP:
            continue
        if any(not (e <= c.start or s >= c.end) for c in clips + avoid):
            continue
        clips.append(Clip(round(s, 2), round(e, 2), "signal"))
        if len(clips) >= n:
            break
    return sorted(clips, key=lambda c: c.start)


# ----------------------------------------------------------------- asosiy
def plan_teaser(video: Path, workdir: Path, an: Analysis, title: str, seconds: int, info: str,
                avoid: list[Clip] | None = None, variant: int = 1,
                progress: Callable[[str], None] | None = None) -> TeaserPlan:
    avoid = avoid or []
    guard = settings.spoiler_guard if an.duration > 600 else 0.95
    limit_end = max(an.duration * guard, min(an.duration, seconds + 5))
    seconds = int(min(seconds, max(10, an.duration * 0.5)))

    try:
        if progress:
            progress("story")
        story = understand_story(an, title, limit_end, info)
        _add_signal_candidates(story, an, limit_end)
        story.candidates = story.candidates[:MAX_CANDIDATES + 6]
        log.info("Nomzodlar: %d (janr: %s)", len(story.candidates), story.genre)

        if progress:
            progress("frames")
        data = direct_teaser(video, workdir, an, story, title, seconds, info, avoid, variant,
                             lambda s: progress and progress(f"frames {s}"))
        clips = sanitize(data.get("clips", []), an, seconds, limit_end, story.candidates)
        if len(clips) >= 2 and sum(c.dur for c in clips) >= seconds * 0.4:
            summary = str(data.get("summary", ""))[:600]
            if story.hook_question and story.hook_question not in summary:
                summary = (summary + f"\n\n❓ {story.hook_question}")[:700]
            return TeaserPlan(clips, str(data.get("hook", ""))[:80], str(data.get("ending", ""))[:90],
                              summary, "ai+vision")
        log.warning("AI rejasi yaroqsiz (%d bo'lak), zaxira usul", len(clips))
    except anthropic.APIStatusError:
        raise
    except Exception as e:
        log.exception("AI rejissyor xatosi: %s", e)

    clips = fallback_plan(an, seconds, limit_end, avoid)
    return TeaserPlan(clips, title[:60], "Davomini filmda ko'ring", "", "fallback")
