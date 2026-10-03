"""API xarajatlari hisobi: har bir Claude so'rovining tokenlari va narxi SQLite'ga yoziladi.

Anthropic oddiy API kalit orqali balansni bermaydi, shuning uchun bot xarajatni o'zi hisoblaydi:
  qolgan balans = /balance orqali kiritilgan boshlang'ich summa - shu paytgacha sarflangan.
Narxlar (1 mln token uchun, USD) .env dan olinadi; standart: Sonnet 5.5 - $2 kirish / $10 chiqish.
"""
from __future__ import annotations

import logging
import threading
from contextvars import ContextVar

import db
from config import settings

log = logging.getLogger(__name__)

# Qaysi ish (job) uchun so'rov ketayotganini bilish uchun
current_job: ContextVar[int | None] = ContextVar("current_job", default=None)
_lock = threading.Lock()
on_record = None  # bot tomonidan o'rnatiladi: har so'rovdan keyin jonli hisobni yangilash uchun


def _usage_fields(resp) -> dict:
    u = getattr(resp, "usage", None)
    if u is None:
        return {}
    g = lambda k: int(getattr(u, k, 0) or 0)  # noqa: E731
    searches = 0
    stu = getattr(u, "server_tool_use", None)
    if stu is not None:
        searches = int(getattr(stu, "web_search_requests", 0) or 0)
    return {"input": g("input_tokens"), "output": g("output_tokens"),
            "cache_read": g("cache_read_input_tokens"), "cache_write": g("cache_creation_input_tokens"),
            "searches": searches}


def cost_usd(input_t: int, output_t: int, cache_read: int = 0, cache_write: int = 0, searches: int = 0) -> float:
    p = settings
    return (input_t * p.price_in + output_t * p.price_out
            + cache_read * p.price_in * 0.1 + cache_write * p.price_in * 1.25) / 1_000_000 \
        + searches * p.price_search / 1000


def record(stage: str, resp, images: int = 0) -> float:
    """So'rov natijasidagi usage ni yozib, narxini qaytaradi."""
    f = _usage_fields(resp)
    if not f:
        return 0.0
    c = cost_usd(f["input"], f["output"], f["cache_read"], f["cache_write"], f["searches"])
    try:
        with _lock:
            db.add_usage(current_job.get(), stage, getattr(resp, "model", settings.claude_model),
                         f["input"], f["output"], f["cache_read"], f["cache_write"], f["searches"], images, c)
    except Exception:
        log.exception("Usage yozilmadi")
    log.info("API %s: in=%d out=%d search=%d img=%d -> $%.4f", stage, f["input"], f["output"],
             f["searches"], images, c)
    if on_record is not None:
        try:
            on_record(current_job.get())
        except Exception:
            pass
    return c
