"""/stats menyusi uchun hisobotlar: API xarajati, balans, ishlar, server holati."""
from __future__ import annotations

import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import db
from config import settings

STARTED = time.time()


def _fmt_tokens(n: int) -> str:
    return f"{n / 1_000_000:.2f}M" if n >= 1_000_000 else f"{n / 1000:.1f}K" if n >= 1000 else str(n)


def _fmt_dur(sec: float | None) -> str:
    if not sec:
        return "-"
    sec = int(sec)
    return f"{sec // 60} daq {sec % 60} s" if sec >= 60 else f"{sec} s"


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def balance_info() -> tuple[float | None, float, float]:
    """(boshlang'ich balans yoki None, shu paytgacha sarf, qolgan)."""
    start = db.kv_get("balance_start")
    since = db.kv_get("balance_since")
    spent = float(db.usage_totals("created_at >= ?", (since,))["cost"]) if since else \
        float(db.usage_totals()["cost"])
    if start is None:
        return None, spent, 0.0
    return float(start), spent, float(start) - spent


def overview() -> str:
    tot = db.usage_totals()
    today = db.usage_totals("date(created_at)=date('now')")
    month = db.usage_totals("strftime('%Y-%m',created_at)=strftime('%Y-%m','now')")
    start, spent, left = balance_info()
    s = db.stats()
    avg = db.job_averages()
    done = s.get("done", 0)

    lines = ["📊 <b>STATISTIKA</b>", ""]
    lines.append("💰 <b>Balans (hisob-kitob)</b>")
    if start is None:
        lines.append("Boshlang'ich balans kiritilmagan. Masalan: <code>/balance 5</code>")
        lines.append(f"Jami sarflangan: <b>${spent:.3f}</b>")
    else:
        pct = (left / start * 100) if start else 0
        bar = "█" * max(0, min(10, round(pct / 10))) + "░" * max(0, 10 - round(pct / 10))
        lines.append(f"{bar} {pct:.0f}%")
        lines.append(f"Boshlang'ich: ${start:.2f} · Sarflangan: ${spent:.3f} · <b>Qoldi: ${left:.3f}</b>")
        if done and spent > 0:
            per = spent / done
            lines.append(f"O'rtacha 1 tizer: ${per:.3f} → yana taxminan <b>{int(left / per)}</b> ta tizerga yetadi")
    lines.append("<i>Anthropic balansni API orqali bermaydi; bu bot hisobi. Aniq raqam: platform.claude.com → Billing</i>")
    lines.append("")

    lines.append("🤖 <b>API ishlatilishi</b>")
    lines.append(f"Bugun: ${today['cost']:.3f} ({today['calls']} so'rov) · Bu oy: ${month['cost']:.3f}")
    lines.append(f"Jami: {tot['calls']} so'rov · kirish {_fmt_tokens(tot['inp'])} · chiqish {_fmt_tokens(tot['out'])} tok")
    lines.append(f"Internet qidiruvlari: {tot['searches']} · Ko'rilgan kadrlar: {tot['images']}")
    lines.append(f"Model: {settings.claude_model} (${settings.price_in:.0f}/${settings.price_out:.0f} per 1M tok)")
    lines.append("")

    lines.append("🎬 <b>Ishlar</b>")
    lines.append(f"Filmlar: {s.get('films', 0)} · Tayyor tizerlar: {done} · Xato: {s.get('failed', 0)}"
                 f" · Navbatda: {s.get('queued', 0) + s.get('running', 0)}")
    if avg and avg["n"]:
        lines.append(f"O'rtacha vaqt: tahlil {_fmt_dur(avg['ta'])} · AI {_fmt_dur(avg['tai'])}"
                     f" · montaj {_fmt_dur(avg['tr'])} · jami {_fmt_dur(avg['total'])}")
        if avg["fallbacks"]:
            lines.append(f"Zaxira usul ishlatilgan: {avg['fallbacks']} marta")
    lines.append("")

    free = shutil.disk_usage(settings.data_dir)
    films_sz = _dir_size(settings.films_dir)
    work_sz = _dir_size(settings.work_dir)
    load = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0
    lines.append("🖥 <b>Server</b>")
    lines.append(f"Disk: bo'sh {free.free / 1024**3:.1f} GB / {free.total / 1024**3:.0f} GB"
                 f" · filmlar {films_sz / 1024**3:.2f} GB · ish fayllari {work_sz / 1024**2:.0f} MB")
    lines.append(f"CPU yuklama: {load:.2f} ({os.cpu_count()} yadro) · Bot ishlayapti: {_fmt_dur(time.time() - STARTED)}")
    lines.append(f"Whisper: {settings.whisper_model} · Til: {settings.language or 'auto'}")
    return "\n".join(lines)


def recent() -> str:
    rows = db.recent_jobs(10)
    if not rows:
        return "Hali birorta ish yo'q."
    lines = ["🗂 <b>Oxirgi ishlar</b>", ""]
    icon = {"done": "✅", "failed": "❌", "running": "⏳", "queued": "🕒"}
    for r in rows:
        total = None
        if r["started_at"] and r["finished_at"]:
            fmt = "%Y-%m-%d %H:%M:%S"
            total = (datetime.strptime(r["finished_at"], fmt) - datetime.strptime(r["started_at"], fmt)).total_seconds()
        line = f"{icon.get(r['status'], '•')} #{r['id']} {r['title'][:28]} · {r['seconds']}s v{r['variant']}"
        details = []
        if r["cost"]:
            details.append(f"${r['cost']:.3f}")
        if total:
            details.append(_fmt_dur(total))
        if r["source"]:
            details.append(r["source"])
        if r["status"] == "failed" and r["error"]:
            details.append(r["error"][:60])
        if details:
            line += "\n   " + " · ".join(details)
        lines.append(line)
    return "\n".join(lines)


def by_stage() -> str:
    rows = db.usage_by_stage()
    if not rows:
        return "Hali API so'rovlari yo'q."
    names = {"research": "🔎 Internet qidiruv", "story": "🧠 Filmni tushunish", "direct": "🎞 Kadrlar bilan montaj"}
    lines = ["🧾 <b>Bosqichlar bo'yicha xarajat</b>", ""]
    for r in rows:
        lines.append(f"{names.get(r['stage'], r['stage'])}: ${r['cost']:.3f}"
                     f" · {r['calls']} so'rov · {_fmt_tokens(r['inp'] or 0)} / {_fmt_tokens(r['out'] or 0)} tok")
    return "\n".join(lines)


def job_report(job_id: int) -> str:
    """Tayyor tizer ostiga qo'shiladigan qisqa hisobot."""
    j = db.get_job(job_id)
    u = db.usage_totals("job_id=?", (job_id,))
    if not j:
        return ""
    parts = [f"💸 ${u['cost']:.3f}"]
    parts.append(f"{_fmt_tokens(u['inp'])}/{_fmt_tokens(u['out'])} tok")
    if u["searches"]:
        parts.append(f"{u['searches']} qidiruv")
    if u["images"]:
        parts.append(f"{u['images']} kadr ko'rildi")
    start, spent, left = balance_info()
    if start is not None:
        parts.append(f"qoldi ${left:.2f}")
    return " · ".join(parts)
