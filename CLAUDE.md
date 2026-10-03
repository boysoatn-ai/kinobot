# Kino tizer boti – loyiha konteksti (Claude uchun)

Foydalanuvchi o'zbek tilida yozadi – javoblar ham o'zbekcha, oddiy tilda bo'lsin (foydalanuvchi dasturchi emas).
Buyruqlarni u PowerShell'ga nusxalab qo'yadi, shuning uchun har bir buyruq to'liq va bitta qatorda bo'lsin.

## Nima bu
Shaxsiy Telegram bot (@kino_yordamchimbot). Foydalanuvchi film yuboradi (2 GB gacha), bot filmni tahlil qilib,
eng qiziq lahzalardan spoylersiz 30–60 soniyalik tizer yasaydi va FAQAT foydalanuvchining o'ziga yuboradi
(shaxsiy ko'rish uchun – Instagram yoki boshqa joyga joylash funksiyasi ataylab olib tashlangan, qaytarilmasin).

## Arxitektura
- `bot.py` – aiogram 3, lokal Telegram Bot API server (2 GB fayllar uchun), navbat/worker, jonli progress
  (tokenlar, $, vaqt), /stats /balance /test /logs /queue /cancel /cleanup, umumiy xato ushlagich.
- `analyze.py` – ffmpeg audio → faster-whisper (CPU, int8, numpy massiv orqali – PyAV `metadata_errors`
  xatosini chetlab o'tadi), har soniya ovoz balandligi, sahna kesimlari. `data/work/<film>/analysis.json` keshi.
  Uzun filmlarda spoyler zonasi (oxirgi 20%) tinglanmaydi.
- `visual.py` – sahna aniqlash (`-skip_frame nokey`, showinfo **stderr**dan o'qiladi), kadr ajratish.
- `highlights.py` – Claude: (0) web_search bilan film haqida ma'lumot, (1) `submit_story` – filmni tushunish
  va 14–18 nomzod, (2) `submit_teaser` – nomzod kadrlarini KO'RIB (vision) montaj rejasi, (3) `sanitize`
  – qat'iy tekshiruv, (4) `fallback_plan`. Barcha so'rovlarga `anthropic-workspace-id` header qo'shiladi
  (foydalanuvchi API kaliti workspace'ga bog'lanmagan).
- `render.py` – ffmpeg: bo'laklar, fade, boshida hook yozuvi, oxirida savol kadri, concat.
- `usage.py` / `stats.py` – har so'rov tokenlari va narxi (Sonnet 5.5: $2/$10 per 1M, qidiruv $10/1000).
  Anthropic balansni API orqali bermaydi – bot /balance dan ayirib hisoblaydi.
- `db.py` – SQLite (`data/bot.db`). Eski v1 sxemasi avtomatik `*_v1` ga arxivlanadi.

## Server
- Hetzner CPX22 (2 vCPU, 4 GB RAM), Ubuntu, Docker Compose. IP va parol foydalanuvchida.
- Ishchi papka: `/root/kinobot` (`.env`, `data/`, docker-compose). Kod manbai: `/root/kinobot-src` (shu repo).
- Yangilash: `ssh root@<IP> "bash /root/kinobot-src/update.sh"` – git pull, fayllarni ko'chiradi,
  `.env` ni yangilaydi, `docker compose up -d --build --remove-orphans`.
- Maxfiy narsalar (BOT_TOKEN, ANTHROPIC_API_KEY, TELEGRAM_API_ID/HASH) faqat serverdagi `.env` da.
  Ularni hech qachon repoga yozma va foydalanuvchidan chatga yuborishni so'rama.

## Ishlash qoidalari
- O'zgartirishdan keyin: `python3 -m py_compile *.py`, imkon bo'lsa stub'lar bilan e2e sinov, so'ng commit + push.
- Foydalanuvchiga faqat bitta yangilash buyrug'ini ber va bot ishga tushganda yuboradigan
  "Bot vX ishga tushdi" hisobotini so'ra. Muammo bo'lsa – botdagi `/logs` natijasini so'ra.
- `bot.py` dagi `VERSION` ni har relizda oshir.
