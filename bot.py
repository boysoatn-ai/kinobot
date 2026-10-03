"""Kino tizer boti: film yuborasiz - AI uni tahlil qilib, eng qiziq lahzalardan 30-60 soniyalik tizer yasaydi."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from dataclasses import asdict
from logging.handlers import RotatingFileHandler
from pathlib import Path

import anthropic
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, ErrorEvent, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

import db
import stats
import usage
from analyze import analyze
from config import settings
from highlights import Clip, plan_teaser, research_film
from media import MediaError
from render import output_size, render_teaser

VERSION = "2.5.1"

settings.ensure_dirs()
LOG_FILE = settings.data_dir / "bot.log"
_fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=_fmt)
_fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
_fh.setFormatter(logging.Formatter(_fmt, "%m-%d %H:%M:%S"))
logging.getLogger().addHandler(_fh)
logging.getLogger("aiogram.event").setLevel(logging.WARNING)  # har bir update haqidagi shovqinni o'chirish
log = logging.getLogger("bot")

MAX_FILE = 2000 * 1024 * 1024  # Telegram botlari uchun chegara

session = AiohttpSession(api=TelegramAPIServer.from_base(settings.local_api_url, is_local=True))
bot = Bot(settings.bot_token, session=session)
router = Router()
router.message.filter(F.from_user.id.in_(settings.admin_ids))
router.callback_query.filter(F.from_user.id.in_(settings.admin_ids))

queue: asyncio.Queue[int] = asyncio.Queue()
progress_msgs: dict[int, tuple[int, int]] = {}  # job_id -> (chat_id, message_id)
MAIN_LOOP: asyncio.AbstractEventLoop | None = None
progress_state: dict[int, tuple[str, float, bool]] = {}  # job_id -> (bosqich matni, boshlanish vaqti, tugadimi)
cancelled: set[int] = set()


class Cancelled(Exception):
    pass


def check_cancel(job_id: int) -> None:
    if job_id in cancelled:
        raise Cancelled()


# ------------------------------------------------------------------ yordamchilar
def keyboard(film_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="30 soniyalik", callback_data=f"t:{film_id}:30"),
        InlineKeyboardButton(text="1 daqiqalik", callback_data=f"t:{film_id}:60"),
    ], [
        InlineKeyboardButton(text="🔄 Boshqa variant", callback_data=f"t:{film_id}:same"),
    ]])


def live_footer(job_id: int, started: float) -> str:
    """Jonli hisob: shu ish uchun hozirgacha ketgan tokenlar, pul va vaqt."""
    u = db.usage_totals("job_id=?", (job_id,))
    elapsed = int(time.monotonic() - started)
    parts = [f"⏱ {elapsed // 60:02d}:{elapsed % 60:02d}"]
    if u["calls"]:
        parts.append(f"🪙 {stats._fmt_tokens(u['inp'])} kirish / {stats._fmt_tokens(u['out'])} chiqish")
        parts.append(f"💸 ${u['cost']:.4f}")
        if u["searches"]:
            parts.append(f"🔎 {u['searches']}")
        if u["images"]:
            parts.append(f"🖼 {u['images']}")
    else:
        parts.append("🪙 0 tok · 💸 $0.0000")
    start, spent, left = stats.balance_info()
    if start is not None:
        parts.append(f"balans ${left:.2f}")
    return " · ".join(parts)


async def set_progress(job_id: int, text: str, final: bool = False) -> None:
    ref = progress_msgs.get(job_id)
    if not ref:
        return
    st = progress_state.get(job_id)
    started = st[1] if st else time.monotonic()
    if not st or st[0] != text:
        log.info("Job %s: %s", job_id, text.splitlines()[0][:150])
        try:
            db.set_stage(job_id, text.splitlines()[0])
        except Exception:
            pass
    progress_state[job_id] = (text, started, final)
    body = text if final else f"{text}\n\n{live_footer(job_id, started)}"
    try:
        await bot.edit_message_text(body, chat_id=ref[0], message_id=ref[1])
    except TelegramBadRequest:
        pass  # matn o'zgarmagan yoki xabar o'chirilgan


def _refresh_from_thread(job_id: int | None) -> None:
    """API so'rovi tugashi bilan (boshqa oqimdan) jonli hisobni darhol yangilash."""
    st = progress_state.get(job_id) if job_id is not None else None
    if st and not st[2] and MAIN_LOOP is not None:
        asyncio.run_coroutine_threadsafe(set_progress(job_id, st[0]), MAIN_LOOP)


async def live_ticker(job_id: int) -> None:
    """Har 15 soniyada jarayon xabaridagi vaqt va xarajatni yangilab turadi."""
    while True:
        await asyncio.sleep(15)
        st = progress_state.get(job_id)
        if not st or st[2] or job_id not in progress_msgs:
            return
        await set_progress(job_id, st[0])


def friendly_error(e: Exception) -> str:
    if isinstance(e, anthropic.AuthenticationError):
        return "AI kaliti noto'g'ri yoki bekor qilingan. Anthropic API kalitini tekshiring."
    if isinstance(e, anthropic.PermissionDeniedError):
        return "AI kalitining ruxsati yetarli emas (workspace sozlamasini tekshiring)."
    if isinstance(e, anthropic.BadRequestError) and "credit" in str(e).lower():
        return "Anthropic hisobida mablag' tugagan. console.anthropic.com da Billing bo'limini to'ldiring."
    if isinstance(e, anthropic.APIStatusError):
        return f"AI xizmati xatosi ({e.status_code}): {str(e)[:300]}"
    if isinstance(e, anthropic.APIConnectionError):
        return "AI xizmatiga ulanib bo'lmadi. Birozdan keyin qayta urinib ko'ring."
    if isinstance(e, MediaError):
        return f"Videoni qayta ishlashda xato: {str(e)[:300]}"
    return f"Kutilmagan xato: {type(e).__name__}: {str(e)[:300]}"


def film_workdir(film_id: int) -> Path:
    return settings.work_dir / str(film_id)


def previous_clips(workdir: Path) -> list[Clip]:
    clips: list[Clip] = []
    for f in workdir.glob("plan_*.json"):
        try:
            clips += [Clip(**c) for c in json.loads(f.read_text(encoding="utf-8"))["clips"]]
        except Exception:
            continue
    return clips


# ------------------------------------------------------------------ asosiy ish
async def process_job(job_id: int) -> None:
    job = db.get_job(job_id)
    film = db.get_film(job["film_id"]) if job else None
    if not job or not film:
        return
    chat_id = film["chat_id"]
    video = Path(film["path"])
    if film["deleted"] or not video.exists():
        db.set_job(job_id, "failed", "film fayli topilmadi")
        await bot.send_message(chat_id, f"«{film['title']}» fayli serverdan o'chirilgan. Filmni qayta yuboring.")
        return

    if job_id not in progress_msgs:
        m = await bot.send_message(chat_id, "⏳ Navbatdan olindi...")
        progress_msgs[job_id] = (chat_id, m.message_id)

    db.start_job(job_id)
    usage.current_job.set(job_id)
    log.info("Job %s boshlandi: film #%s «%s», %ss, variant %s, fayl %.2f GB", job_id, film["id"], film["title"],
             job["seconds"], job["variant"], video.stat().st_size / 1024 ** 3)
    progress_state[job_id] = ("⏳ Boshlanmoqda...", time.monotonic(), False)
    ticker = asyncio.create_task(live_ticker(job_id))
    loop = asyncio.get_running_loop()
    workdir = film_workdir(film["id"])
    workdir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    try:
        # 1. Tahlil (uzun filmlarda eng ko'p vaqt oladi, natija keshlanadi)
        last = [0.0]
        t_speech = [0.0]

        def on_progress(stage: str, frac: float) -> None:
            now = time.monotonic()
            if stage == "scenes":
                asyncio.run_coroutine_threadsafe(set_progress(
                    job_id, "👁 1/4 Sahnalar va montaj tezligi tahlil qilinmoqda..."), loop)
                return
            if t_speech[0] == 0:
                t_speech[0] = now
            if now - last[0] < 20 and frac < 1:
                return
            last[0] = now
            eta = ""
            if 0.05 < frac < 1:
                remaining = (now - t_speech[0]) / frac * (1 - frac)
                eta = f" · taxminan {int(remaining // 60) + 1} daqiqa qoldi"
            asyncio.run_coroutine_threadsafe(set_progress(
                job_id, f"🎧 2/4 Film tinglanmoqda va matnga aylantirilmoqda: {int(frac * 100)}%{eta}"), loop)

        cached = (workdir / "analysis.json").exists()
        await set_progress(job_id, "👁 1/4 Film tahlil qilinmoqda..." if not cached
                           else "⚡ Tahlil keshdan olindi")
        t0 = time.monotonic()
        an = await asyncio.to_thread(analyze, video, workdir, on_progress)
        db.set_job_metrics(job_id, t_analyze=time.monotonic() - t0)
        log.info("Job %s tahlil tugadi: %d gap, %d sahna kesimi, %.0f s", job_id, len(an.segments), len(an.cuts),
                 time.monotonic() - t0)
        check_cancel(job_id)

        # 2. Film haqida ma'lumot (bir marta, keshlanadi)
        info_file = workdir / "info.txt"
        if info_file.exists():
            info = info_file.read_text(encoding="utf-8")
        elif settings.web_research:
            await set_progress(job_id, "🔎 3/4 Film haqida internetdan ma'lumot qidirilmoqda...")
            info = await asyncio.to_thread(research_film, film["title"])
            info_file.write_text(info, encoding="utf-8")
        else:
            info = ""

        check_cancel(job_id)
        # 3. AI rejissyor: tushunish -> kadrlarni ko'rish -> montaj rejasi
        def on_plan(stage: str) -> None:
            text = {"story": "🧠 3/4 AI filmni tushunib, nomzod lahzalarni tanlamoqda...",
                    "frames": "🎞 3/4 AI nomzod lahzalarning kadrlarini ko'rib chiqmoqda..."}.get(stage)
            if stage.startswith("frames "):
                text = f"🎞 3/4 Kadrlar tayyorlanmoqda ({stage.split()[1]})..."
            if text:
                asyncio.run_coroutine_threadsafe(set_progress(job_id, text), loop)

        avoid = previous_clips(workdir) if job["variant"] > 1 else []
        t0 = time.monotonic()
        plan = await asyncio.to_thread(plan_teaser, video, workdir, an, film["title"], job["seconds"],
                                       info, avoid, job["variant"], on_plan)
        db.set_job_metrics(job_id, t_ai=time.monotonic() - t0, source=plan.source, clips=len(plan.clips))
        (workdir / f"plan_{job_id}.json").write_text(
            json.dumps({"clips": [asdict(c) for c in plan.clips], "hook": plan.hook,
                        "ending": plan.ending, "source": plan.source}, ensure_ascii=False),
            encoding="utf-8")

        check_cancel(job_id)
        # 4. Montaj
        await set_progress(job_id, f"✂️ 4/4 Rolik yig'ilmoqda ({len(plan.clips)} ta lahza)...")
        out = workdir / f"teaser_{job_id}.mp4"
        t0 = time.monotonic()
        await asyncio.to_thread(render_teaser, video, an, plan, out, film["title"])
        db.set_job_metrics(job_id, t_render=time.monotonic() - t0)

        # 5. Yuborish
        await set_progress(job_id, "📤 Yuborilmoqda...")
        total = sum(c.dur for c in plan.clips) + 2.5
        w, h = output_size(an)
        caption_parts = [f"🎬 {film['title']}"]
        if plan.summary:
            caption_parts.append(plan.summary)
        mins = int((time.monotonic() - started) // 60)
        caption_parts.append(f"⏱ {int(total)} soniya · variant {job['variant']} · {mins} daqiqada tayyorlandi"
                             + (" · zaxira usul (AI ishlamadi)" if plan.source == "fallback" else ""))
        db.set_job_metrics(job_id, out_seconds=total)
        caption_parts.append(stats.job_report(job_id))
        await bot.send_video(chat_id, FSInputFile(out), caption="\n\n".join(caption_parts)[:1024],
                             duration=int(total), width=w, height=h, supports_streaming=True,
                             reply_markup=keyboard(film["id"]))
        db.set_job(job_id, "done", result_path=str(out))
        log.info("Job %s tayyor: %.0f s", job_id, time.monotonic() - started)
        ref = progress_msgs.pop(job_id, None)
        if ref:
            try:
                await bot.delete_message(ref[0], ref[1])
            except TelegramBadRequest:
                pass
        out.unlink(missing_ok=True)  # Telegramda saqlanadi, diskni band qilmaymiz
    except Cancelled:
        log.info("Job %s bekor qilindi", job_id)
        db.set_job(job_id, "failed", "Bekor qilindi")
        await set_progress(job_id, "🚫 Bekor qilindi", final=True)
        progress_msgs.pop(job_id, None)
    except Exception as e:
        log.exception("Job %s xato", job_id)
        db.set_job(job_id, "failed", str(e)[:1000])
        st = progress_state.get(job_id)
        footer = live_footer(job_id, st[1]) if st else ""
        await set_progress(job_id, "❌ " + friendly_error(e) + (f"\n\n{footer}" if footer else ""), final=True)
        progress_msgs.pop(job_id, None)
    finally:
        ticker.cancel()
        progress_state.pop(job_id, None)
        cancelled.discard(job_id)


async def worker() -> None:
    while True:
        job_id = await queue.get()
        try:
            j = db.get_job(job_id)
            if not j or j["status"] != "queued":
                continue  # bekor qilingan yoki allaqachon bajarilgan
            await process_job(job_id)
        except Exception:
            log.exception("Worker xatosi")
        finally:
            queue.task_done()


async def cleanup_loop() -> None:
    """Eski filmlarni diskdan o'chiradi (tahlil keshi qoladi)."""
    while True:
        try:
            for f in db.old_films(settings.film_ttl_hours):
                Path(f["path"]).unlink(missing_ok=True)
                db.mark_film_deleted(f["id"])
                log.info("Eski film o'chirildi: #%s", f["id"])
        except Exception:
            log.exception("Tozalash xatosi")
        await asyncio.sleep(3600)


def enqueue(film_id: int, seconds: int) -> int:
    job_id = db.add_job(film_id, seconds, db.next_variant(film_id, seconds))
    queue.put_nowait(job_id)
    return job_id


# ------------------------------------------------------------------ handlerlar
@router.message(Command("start", "help"))
async def cmd_start(m: Message) -> None:
    await m.answer(
        "Salom! Menga film yuboring (video yoki fayl, 2 GB gacha).\n"
        "Izohga film nomini yozsangiz, AI internetdan u haqida ma'lumot topib, yanada aniq tanlaydi.\n\n"
        "Men filmni to'liq tinglab, eng qiziq lahzalardan spoylersiz 30-60 soniyalik tizer yasab beraman.\n"
        "Izohga «30» deb qo'shsangiz - 30 soniyalik bo'ladi.\n\n"
        "Jarayon: sahnalar tahlili → nutqni matnga aylantirish → internetdan ma'lumot → "
        "AI filmni tushunadi va kadrlarni ko'rib montaj qiladi.\n"
        "2 soatlik film uchun 30-70 daqiqa ketadi; keyingi variantlar bir necha daqiqada tayyor.\n\n"
        "/stats - to'liq statistika (API xarajati, balans, ishlar, server)\n"
        "/balance 5 - Anthropic hisobidagi summani kiritish (qolganini hisoblab boradi)\n"
        "/queue - hozir nima bajarilyapti\n/cancel - ishlarni bekor qilish\n"
        "/test - bot, AI kaliti va serverni tekshirish\n/logs - oxirgi loglar\n"
        "/status - navbat holati\n/cleanup - eski filmlarni o'chirish")


# ------------------------------------------------------------------ diagnostika
def _meminfo() -> str:
    try:
        info = dict(l.split(":", 1) for l in Path("/proc/meminfo").read_text().splitlines())
        tot = int(info["MemTotal"].split()[0]) / 1024 ** 2
        av = int(info["MemAvailable"].split()[0]) / 1024 ** 2
        return f"{av:.1f} GB bo'sh / {tot:.1f} GB"
    except Exception:
        return "?"


async def self_test() -> list[str]:
    """Barcha qismlarni tekshiradi va natijani qatorlar ro'yxati sifatida qaytaradi."""
    out: list[str] = []
    # 1. Telegram lokal server
    try:
        me = await bot.get_me()
        out.append(f"✅ Telegram lokal server: @{me.username}")
    except Exception as e:
        out.append(f"❌ Telegram lokal server: {str(e)[:150]}")
    # 2. Anthropic API (juda kichik so'rov, narxi ~$0.0001)
    def _ping() -> str:
        from highlights import _client
        r = _client().messages.create(model=settings.claude_model, max_tokens=5,
                                      messages=[{"role": "user", "content": "Reply with: OK"}])
        usage.record("test", r)
        return "".join(b.text for b in r.content if b.type == "text").strip()
    try:
        reply = await asyncio.to_thread(_ping)
        out.append(f"✅ Claude API: ishlaydi ({settings.claude_model}, javob: {reply[:20]})")
    except Exception as e:
        out.append(f"❌ Claude API: {friendly_error(e)}")
    out.append(f"   workspace: {settings.anthropic_workspace_id or 'yo‘q'}")
    # 3. ffmpeg
    try:
        from media import run
        v = run(["ffmpeg", "-version"], timeout=20).splitlines()[0]
        out.append(f"✅ {v[:60]}")
    except Exception as e:
        out.append(f"❌ ffmpeg: {e}")
    # 4. Whisper kutubxonasi
    try:
        import faster_whisper  # noqa: F401
        out.append(f"✅ Whisper kutubxonasi o'rnatilgan (model: {settings.whisper_model})")
    except Exception as e:
        out.append(f"❌ Whisper: {e}")
    # 5. Resurslar
    du = shutil.disk_usage(settings.data_dir)
    out.append(f"💾 Disk: {du.free / 1024 ** 3:.1f} GB bo'sh · 🧠 RAM: {_meminfo()}")
    return out


@router.message(Command("test"))
async def cmd_test(m: Message) -> None:
    msg = await m.answer("🔧 Tekshirilmoqda...")
    lines = await self_test()
    await msg.edit_text(f"🔧 Bot v{VERSION} tekshiruvi\n\n" + "\n".join(lines))


@router.message(Command("logs"))
async def cmd_logs(m: Message) -> None:
    arg = (m.text or "").split()
    n = int(arg[1]) if len(arg) > 1 and arg[1].isdigit() else 40
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except FileNotFoundError:
        lines = ["(log hali bo'sh)"]
    text = "\n".join(lines)[-3800:]
    await m.answer("📜 Oxirgi loglar:\n\n" + (text or "(bo'sh)"))


@router.message(Command("queue"))
async def cmd_queue(m: Message) -> None:
    rows = db.active_jobs()
    if not rows:
        return await m.answer("Navbat bo'sh, hozir hech qanday ish bajarilmayapti.")
    lines = ["📋 Hozirgi ishlar:"]
    for r in rows:
        icon = "⏳" if r["status"] == "running" else "🕒"
        lines.append(f"{icon} #{r['id']} «{r['title'][:30]}» {r['seconds']}s — {r['stage'] or r['status']}")
    lines.append("\nBekor qilish: /cancel")
    await m.answer("\n".join(lines))


@router.message(Command("cancel"))
async def cmd_cancel(m: Message) -> None:
    n = db.cancel_queued()
    running = [r["id"] for r in db.active_jobs() if r["status"] == "running"]
    cancelled.update(running)
    await m.answer(f"🚫 Navbatdagi {n} ta ish bekor qilindi."
                   + (f" Hozir ishlayotgan #{running[0]} joriy bosqich tugashi bilan to'xtaydi." if running else ""))


@router.message(Command("status"))
async def cmd_status(m: Message) -> None:
    s = db.stats()
    await m.answer(f"Filmlar: {s.get('films', 0)}\nNavbatda: {queue.qsize()}\n"
                   f"Tayyor: {s.get('done', 0)} · Xato: {s.get('failed', 0)}")


def stats_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📊 Umumiy", callback_data="s:overview"),
        InlineKeyboardButton(text="🗂 Oxirgi ishlar", callback_data="s:recent"),
        InlineKeyboardButton(text="🧾 Bosqichlar", callback_data="s:stage"),
    ], [InlineKeyboardButton(text="🔄 Yangilash", callback_data="s:overview")]])


def stats_text(section: str) -> str:
    return {"recent": stats.recent, "stage": stats.by_stage}.get(section, stats.overview)()


@router.message(Command("stats"))
async def cmd_stats(m: Message) -> None:
    await m.answer(stats_text("overview"), parse_mode="HTML", reply_markup=stats_keyboard())


@router.callback_query(F.data.startswith("s:"))
async def on_stats_button(cb: CallbackQuery) -> None:
    section = cb.data.split(":", 1)[1]
    try:
        await cb.message.edit_text(stats_text(section), parse_mode="HTML", reply_markup=stats_keyboard())
    except TelegramBadRequest:
        pass  # matn o'zgarmagan
    await cb.answer()


@router.message(Command("balance"))
async def cmd_balance(m: Message) -> None:
    arg = (m.text or "").split(maxsplit=1)
    if len(arg) < 2:
        start, spent, left = stats.balance_info()
        if start is None:
            return await m.answer("Anthropic hisobingizdagi hozirgi summani kiriting, masalan: <code>/balance 5</code>\n"
                                  "Shundan keyin bot har bir so'rov narxini ayirib, qolganini ko'rsatib boradi.",
                                  parse_mode="HTML")
        return await m.answer(f"Boshlang'ich: ${start:.2f} · Sarflangan: ${spent:.3f} · Qoldi: ${left:.3f}\n"
                              "Yangilash: <code>/balance 10</code>", parse_mode="HTML")
    try:
        value = float(arg[1].replace("$", "").replace(",", "."))
    except ValueError:
        return await m.answer("Raqam kiriting, masalan: /balance 5")
    db.kv_set("balance_start", f"{value:.4f}")
    db.kv_set("balance_since", time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))
    await m.answer(f"✅ Balans ${value:.2f} deb belgilandi. Endi /stats da qolgan summa ko'rinadi.")


@router.message(Command("cleanup"))
async def cmd_cleanup(m: Message) -> None:
    n = 0
    for f in db.old_films(0):
        Path(f["path"]).unlink(missing_ok=True)
        db.mark_film_deleted(f["id"])
        n += 1
    free = shutil.disk_usage(settings.data_dir).free / 1024 ** 3
    await m.answer(f"{n} ta film fayli o'chirildi. Bo'sh joy: {free:.1f} GB.\n"
                   "Tahlil natijalari saqlanib qoldi, lekin yangi variant uchun filmni qayta yuborish kerak bo'ladi.")


@router.message(F.video | F.document)
async def on_film(m: Message) -> None:
    media = m.video or m.document
    mime = (getattr(media, "mime_type", "") or "")
    if m.document and not mime.startswith("video"):
        await m.answer("Bu video fayl emas.")
        return
    if (media.file_size or 0) > MAX_FILE:
        await m.answer(f"Fayl hajmi {media.file_size / 1024 ** 3:.1f} GB. Telegram botlari 2 GB gacha "
                       "faylni qabul qiladi - kichikroq sifatdagi versiyasini yuboring.")
        return

    free = shutil.disk_usage(settings.data_dir).free
    need = (media.file_size or 0) * 2 + 2 * 1024 ** 3  # film + audio + ish fayllari
    if free < need:
        await m.answer(f"Serverda joy yetarli emas: bo'sh {free / 1024 ** 3:.1f} GB, kerak ~{need / 1024 ** 3:.1f} GB.\n"
                       "Eski filmlar 48 soatda o'zi o'chadi yoki /cleanup buyrug'i bilan hozir o'chiring.")
        return

    caption = (m.caption or "").strip()
    seconds = 30 if re.search(r"\b30\b", caption) else settings.default_teaser_sec
    lines = re.sub(r"\s*\b30\b\s*", " ", caption).strip().splitlines()
    title = lines[0].strip()[:100] if lines else ""
    title = title or (media.file_name or "Film").rsplit(".", 1)[0][:100]

    status = await m.answer("📥 Qabul qilindi, yuklab olinmoqda...")
    ext = Path(media.file_name or "video.mp4").suffix or ".mp4"
    dest = settings.films_dir / f"{media.file_unique_id}{ext}"
    size_gb = (media.file_size or 0) / 1024 ** 3
    t_start = time.monotonic()
    log.info("Film qabul qilindi: %s (%.2f GB, file_id=%s)", title, size_gb, media.file_id[:20])

    async def show(stage: str) -> None:
        el = int(time.monotonic() - t_start)
        try:
            await status.edit_text(f"📥 {stage}\n⏱ {el // 60:02d}:{el % 60:02d} · {size_gb:.2f} GB · bot ishlayapti")
        except TelegramBadRequest:
            pass

    async def with_heartbeat(coro, stage: str):
        """Uzoq ishni bajaradi va har 30 soniyada xabarni yangilab turadi (bot tirikligini ko'rsatish uchun)."""
        task = asyncio.ensure_future(coro)
        while True:
            done, _ = await asyncio.wait({task}, timeout=30)
            if done:
                return task.result()
            await show(stage)

    try:
        # Lokal Bot API serveri avval butun faylni Telegram'dan yuklab oladi - katta filmda bu
        # bir necha daqiqa. Shuning uchun uzun timeout va bir necha urinish.
        file = None
        for attempt in range(1, 7):
            try:
                file = await with_heartbeat(bot.get_file(media.file_id, request_timeout=900),
                                            f"Telegram'dan yuklab olinmoqda (urinish {attempt}/6)...")
                break
            except (TelegramNetworkError, asyncio.TimeoutError) as e:
                log.warning("get_file urinish %d: %s", attempt, e)
                await show(f"Telegram javob bermadi, qayta urinish {attempt}/6...")
                await asyncio.sleep(20)
        if file is None:
            raise TimeoutError("Telegram faylni 1 soat ichida bermadi")
        log.info("get_file tayyor: %s (%.0f s)", file.file_path, time.monotonic() - t_start)
        src = Path(file.file_path)
        if src.is_absolute() and src.exists():
            await with_heartbeat(asyncio.to_thread(shutil.move, str(src), str(dest)), "Fayl serverga ko'chirilmoqda...")
        else:
            await with_heartbeat(bot.download_file(file.file_path, destination=dest, timeout=3600),
                                 "Fayl serverga yuklab olinmoqda...")
        if not dest.exists() or dest.stat().st_size < 1024:
            raise MediaError("Fayl serverga to'liq kelmadi")
        log.info("Film saqlandi: %s (%.2f GB, %.0f s)", dest, dest.stat().st_size / 1024 ** 3, time.monotonic() - t_start)
    except Exception as e:
        log.exception("Yuklab olishda xato")
        await status.edit_text(f"❌ Faylni yuklab bo'lmadi: {str(e)[:200]}\n"
                               "Yana bir bor yuborib ko'ring; muammo takrorlansa xabar bering.")
        return

    try:
        film_id = db.add_film(m.chat.id, title, str(dest))
        job_id = enqueue(film_id, seconds)
    except Exception as e:
        log.exception("Filmni navbatga qo'shib bo'lmadi")
        await status.edit_text(f"❌ Filmni navbatga qo'shib bo'lmadi: {type(e).__name__}: {str(e)[:200]}")
        return
    progress_msgs[job_id] = (status.chat.id, status.message_id)
    ahead = queue.qsize() - 1
    await status.edit_text(f"✅ «{title}» qabul qilindi. {seconds} soniyalik tizer tayyorlanadi."
                           + (f"\nNavbatda oldinda: {ahead}" if ahead > 0 else ""))


@router.callback_query(F.data.startswith("t:"))
async def on_button(cb: CallbackQuery) -> None:
    _, film_id_s, sec = cb.data.split(":")
    film_id = int(film_id_s)
    film = db.get_film(film_id)
    if not film or film["deleted"]:
        await cb.answer("Bu film serverdan o'chirilgan, qayta yuboring.", show_alert=True)
        return
    seconds = settings.default_teaser_sec if sec == "same" else int(sec)
    if sec == "same" and cb.message and cb.message.caption:
        found = re.search(r"⏱ (\d+) soniya", cb.message.caption)
        if found:
            seconds = 30 if int(found.group(1)) <= 40 else 60
    job_id = enqueue(film_id, seconds)
    m = await bot.send_message(cb.from_user.id, f"⏳ {seconds} soniyalik yangi variant navbatga qo'shildi...")
    progress_msgs[job_id] = (m.chat.id, m.message_id)
    await cb.answer()


@router.message()
async def fallback(m: Message) -> None:
    await m.answer("Menga film faylini yuboring. Yordam: /start")


# ------------------------------------------------------------------ ishga tushirish
@router.errors()
async def on_error(event: ErrorEvent) -> bool:
    """Har qanday kutilmagan xato - logga yoziladi va sizga darhol xabar qilinadi (jim qolmaydi)."""
    e = event.exception
    log.error("Kutilmagan xato: %s", e, exc_info=e)
    text = f"⚠️ Botda kutilmagan xato:\n{type(e).__name__}: {str(e)[:500]}\n\nBatafsil: /logs"
    for uid in settings.admin_ids:
        try:
            await bot.send_message(uid, text)
        except Exception:
            pass
    return True


async def main() -> None:
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()
    usage.on_record = _refresh_from_thread
    settings.ensure_dirs()
    db.init()
    for job_id in db.queued_jobs():
        queue.put_nowait(job_id)
    asyncio.create_task(worker())
    asyncio.create_task(cleanup_loop())
    dp = Dispatcher()
    dp.include_router(router)
    log.info("Bot v%s ishga tushdi. Adminlar: %s", VERSION, sorted(settings.admin_ids))

    async def announce() -> None:
        await asyncio.sleep(3)
        try:
            lines = await self_test()
            pending = len(db.active_jobs())
            text = (f"🤖 Bot v{VERSION} ishga tushdi\n\n" + "\n".join(lines)
                    + (f"\n\n📋 Navbatda {pending} ta ish davom ettiriladi" if pending else ""))
            for uid in settings.admin_ids:
                await bot.send_message(uid, text)
        except Exception:
            log.exception("Ishga tushish xabari yuborilmadi")
    asyncio.create_task(announce())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
