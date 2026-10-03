#!/usr/bin/env bash
# Yangilash: bash /root/kinobot-src/update.sh  (GitHub dan oladi va botni qayta yig'adi)
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
APP=/root/kinobot

# GitHub'dan olingan bo'lsa - eng yangi versiyani tortib olish
if [ -d "$SRC/.git" ] && [ "${1:-}" != "--pulled" ]; then
  echo ">> GitHub'dan yangilanmoqda..."
  git -C "$SRC" pull --ff-only
  exec bash "$SRC/update.sh" --pulled   # skriptning o'zi ham yangilangan bo'lishi mumkin
fi

echo ">> Yangi kod o'rnatilmoqda..."
# Xavfsizlik: /root/kinobot da boshqa loyiha (masalan slaydbot) turgan bo'lsa - hech narsaga tegmaymiz
if [ -f "$APP/docker-compose.yml" ] && ! grep -q "telegram-bot-api" "$APP/docker-compose.yml"; then
  echo "!! $APP papkasida boshqa loyiha turibdi - to'xtatildi, hech narsa o'zgartirilmadi."
  exit 1
fi
if [ ! -f "$APP/.env" ]; then
  echo "!! $APP/.env topilmadi (bot tokeni va kalitlar). Avval setup.sh bilan sozlang."
  exit 1
fi
cd "$APP"
# Eski versiyaning keraksiz fayllari
rm -f instagram.py pipeline.py Caddyfile .env.example README.md
cp "$SRC"/*.py "$SRC"/requirements.txt "$SRC"/Dockerfile "$SRC"/docker-compose.yml "$SRC"/.dockerignore "$APP"/

# .env ni yangilash (mavjud kalitlar saqlanadi)
setenv() {
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi
}
setenv ANTHROPIC_WORKSPACE_ID "${ANTHROPIC_WORKSPACE_ID:-wrkspc_01HtZVvSff9utBJPtBuyb872}"
setenv LANGUAGE auto
setenv CLAUDE_MODEL claude-sonnet-5-5
setenv DEFAULT_TEASER_SEC 60
setenv WEB_RESEARCH 1
setenv PRICE_IN 2
setenv PRICE_OUT 10
setenv PRICE_SEARCH 10
# Endi kerak bo'lmagan Instagram sozlamalari
sed -i '/^IG_\|^PUBLIC_BASE_URL\|^DOMAIN\|^POST_TIMES\|^CAPTION_FOOTER\|^GRAPH_VERSION\|^CLIPS_PER_FILM\|^CLIP_M\|^BURN_SUBTITLES/d' .env

mkdir -p data
echo ">> Bot qayta yig'ilmoqda (1-3 daqiqa)..."
docker compose up -d --build --remove-orphans

sleep 8
echo
echo "=========== Bot logi (oxirgi qatorlar) ==========="
docker compose logs --tail 15 bot
echo "=================================================="
echo " TAYYOR! Botga /start yozing va film yuboring."
