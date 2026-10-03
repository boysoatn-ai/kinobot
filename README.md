# Kino tizer boti

Shaxsiy Telegram bot: film yuborasiz, AI uni to'liq tinglab, eng qiziq lahzalardan
spoylersiz 30–60 soniyalik tizer (treylerga o'xshash rolik) yasab, faqat sizga yuboradi.

## Qanday ishlaydi
1. **Ko'rish** – ffmpeg sahna almashinuvlarini topadi (zich montaj = harakat, jang, quvish).
2. **Eshitish** – Whisper butun nutqni vaqt belgilari bilan matnga aylantiradi; har soniyaning ovoz
   balandligi o'lchanadi (portlash, qichqiriq, musiqa avjlari). Natija keshlanadi.
3. **Ma'lumot** – Claude internetdan film haqida qidiradi: janr, syujet, kuchli sahnalar, qaysi joylar spoyler.
4. **Tushunish** – Claude to'liq matn + ovoz + sahna xaritasi asosida filmni tushunadi (qahramon, asosiy sir,
   kayfiyat) va 14–18 ta nomzod lahza tanlaydi; nutqsiz harakat lahzalari signal bo'yicha qo'shiladi.
5. **Rejissyor** – har bir nomzodning kadri ajratib olinadi va Claude ularni **ko'rib** yakuniy montajni tuzadi:
   ilmoq → dunyo → xavf → cliffhanger. Zerikarli kadrlar (qora ekran, titrlar) chiqarib tashlanadi.
   Filmning oxirgi 20% qismi hech qachon ishlatilmaydi.
6. **Tekshiruv** – kod AI rejasini qat'iy tekshiradi: gap o'rtasidan kesmaydi, qoplanishlarni olib tashlaydi,
   uzunlikni moslaydi. AI ishlamasa – zaxira usul (ovoz avjlari + sahna zichligi).
7. **Montaj** – bo'laklar yumshoq o'tishlar bilan ulanadi, boshida qiziqtiruvchi yozuv, oxirida savol kadri.

## Foydalanish
- Film yuboring (2 GB gacha). Izohga film nomini yozing – AI internetdan ma'lumot topadi.
- Izohga `30` qo'shsangiz – 30 soniyalik tizer.
- Tayyor roliк ostidagi tugmalar: **30 soniyalik**, **1 daqiqalik**, **Boshqa variant** (boshqa lahzalar bilan).
- `/stats` – to'liq statistika (tugmali menyu): API xarajati va tokenlar (bugun / oy / jami), hisoblangan balans,
  o'rtacha tizer narxi va nechtaga yetishi, bosqichlar bo'yicha xarajat, oxirgi ishlar, server (disk, CPU).
- `/balance 5` – Anthropic hisobidagi hozirgi summani kiritish; bot har so'rov narxini ayirib, qolganini ko'rsatadi.
  Anthropic balansni API orqali bermaydi, shuning uchun bu bot hisobi; aniq raqam platform.claude.com → Billing.
- Har tayyor tizer ostida: shu tizerga ketgan $ , tokenlar, qidiruvlar, ko'rilgan kadrlar, qolgan balans.
- `/test` – bot, Claude API kaliti, ffmpeg, Whisper, disk va xotirani tekshiradi.
- `/logs` – oxirgi 40 qator log (`/logs 100` – ko'proq). `/queue` – hozir nima bajarilyapti. `/cancel` – ishlarni bekor qilish.
- `/status` – navbat holati, `/cleanup` – eski filmlarni o'chirish.
- Bot har ishga tushganda o'zini tekshirib, natijani sizga yuboradi.

Ikkinchi va keyingi variantlar tez tayyorlanadi, chunki film tahlili saqlanib qoladi.
Filmlar 48 soatdan keyin serverdan avtomatik o'chiriladi.

## Fayllar
| Fayl | Vazifasi |
|---|---|
| `bot.py` | Telegram bot, navbat, holat xabarlari |
| `analyze.py` | Nutqni matnga aylantirish, ovoz xaritasi, sahna almashinuvlari, kesh |
| `visual.py` | Sahna aniqlash, kadr ajratish |
| `highlights.py` | Ma'lumot qidirish, filmni tushunish, kadrlar bilan rejissyorlik, tekshiruv, zaxira |
| `render.py` | Kesish, o'tishlar, yozuvlar, yig'ish |
| `media.py` | ffmpeg/ffprobe yordamchilari |
| `db.py` | SQLite – ishlar, API usage, sozlamalar |
| `usage.py` | Har so'rov tokenlari va narxini yozish |
| `stats.py` | /stats hisobotlari |
| `config.py` | Sozlamalar (`.env`) |

## Sozlamalar (`.env`)
`WHISPER_MODEL` (small/medium), `LANGUAGE` (auto/uz/ru/en), `DEFAULT_TEASER_SEC`, `SPOILER_GUARD` (0.8),
`WEB_RESEARCH` (1/0), `FILM_TTL_HOURS`, `ANTHROPIC_WORKSPACE_ID`,
`PRICE_IN` / `PRICE_OUT` (USD per 1M token, standart Sonnet 5.5: 2 / 10), `PRICE_SEARCH` (USD per 1000 qidiruv).

Loglar: `cd /root/kinobot && docker compose logs -f bot`

## Serverga o'rnatish / yangilash
Birinchi marta (serverda `.env` allaqachon bor bo'lsa):
```
apt-get install -y git
git clone https://github.com/boysoatn-ai/kinobot /root/kinobot-src
bash /root/kinobot-src/update.sh
```
Keyingi yangilanishlar – bitta buyruq:
```
bash /root/kinobot-src/update.sh
```
