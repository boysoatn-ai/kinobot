"""Barcha sozlamalar .env faylidan o'qiladi."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # Telegram
    bot_token: str = field(default_factory=lambda: os.environ["BOT_TOKEN"])
    admin_ids: frozenset[int] = field(default_factory=lambda: frozenset(
        int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x))
    local_api_url: str = os.getenv("LOCAL_API_URL", "http://telegram-bot-api:8081")

    # AI
    claude_model: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
    anthropic_workspace_id: str = os.getenv("ANTHROPIC_WORKSPACE_ID", "")
    web_research: bool = os.getenv("WEB_RESEARCH", "1") == "1"

    # Narxlar (USD, 1 mln token uchun) - statistika uchun. Standart: Sonnet 5.5
    price_in: float = float(os.getenv("PRICE_IN", "2"))
    price_out: float = float(os.getenv("PRICE_OUT", "10"))
    price_search: float = float(os.getenv("PRICE_SEARCH", "10"))  # 1000 ta qidiruv uchun
    whisper_model: str = os.getenv("WHISPER_MODEL", "small")
    whisper_beam: int = _int("WHISPER_BEAM", 1)
    language: str | None = (None if os.getenv("LANGUAGE", "auto").lower() in ("", "auto")
                            else os.getenv("LANGUAGE"))

    # Tizer
    default_teaser_sec: int = _int("DEFAULT_TEASER_SEC", 60)
    spoiler_guard: float = float(os.getenv("SPOILER_GUARD", "0.8"))  # filmning shu qismidan keyingisi olinmaydi
    max_width: int = _int("MAX_WIDTH", 1280)
    film_ttl_hours: int = _int("FILM_TTL_HOURS", 48)  # filmlar shuncha soatdan keyin o'chiriladi

    # Papkalar
    data_dir: Path = Path(os.getenv("DATA_DIR", "data")).resolve()

    @property
    def films_dir(self) -> Path:
        return self.data_dir / "films"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.db"

    def ensure_dirs(self) -> None:
        for d in (self.films_dir, self.work_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
