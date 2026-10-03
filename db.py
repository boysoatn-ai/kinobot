"""SQLite: filmlar va tizer buyurtmalari holati (qayta ishga tushganda davom etish uchun)."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS films (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    title       TEXT    NOT NULL,
    path        TEXT    NOT NULL,
    created_at  TEXT    DEFAULT CURRENT_TIMESTAMP,
    deleted     INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    film_id     INTEGER NOT NULL REFERENCES films(id),
    seconds     INTEGER NOT NULL,
    variant     INTEGER NOT NULL DEFAULT 1,
    status      TEXT    NOT NULL DEFAULT 'queued',   -- queued / running / done / failed
    error       TEXT,
    result_path TEXT,
    created_at  TEXT    DEFAULT CURRENT_TIMESTAMP,
    started_at  TEXT,
    finished_at TEXT,
    t_analyze   REAL,      -- soniya
    t_ai        REAL,
    t_render    REAL,
    source      TEXT,      -- ai+vision / fallback
    clips       INTEGER,
    out_seconds REAL
);
CREATE TABLE IF NOT EXISTS usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER,
    stage       TEXT NOT NULL,        -- research / story / direct
    model       TEXT,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read    INTEGER DEFAULT 0,
    cache_write   INTEGER DEFAULT 0,
    searches      INTEGER DEFAULT 0,
    images        INTEGER DEFAULT 0,
    cost_usd      REAL DEFAULT 0,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_MIGRATIONS = [
    "ALTER TABLE jobs ADD COLUMN started_at TEXT",
    "ALTER TABLE jobs ADD COLUMN finished_at TEXT",
    "ALTER TABLE jobs ADD COLUMN t_analyze REAL",
    "ALTER TABLE jobs ADD COLUMN t_ai REAL",
    "ALTER TABLE jobs ADD COLUMN t_render REAL",
    "ALTER TABLE jobs ADD COLUMN source TEXT",
    "ALTER TABLE jobs ADD COLUMN clips INTEGER",
    "ALTER TABLE jobs ADD COLUMN out_seconds REAL",
    "ALTER TABLE jobs ADD COLUMN attempts INTEGER DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN stage TEXT",
]


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(settings.db_path, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init() -> None:
    with connect() as c:
        c.executescript(SCHEMA)
        for m in _MIGRATIONS:  # eski bazani yangi ustunlar bilan to'ldirish
            try:
                c.execute(m)
            except sqlite3.OperationalError:
                pass
        # Bot o'chib qolganda "running" bo'lib qolgan ishlar. 2 marta qulagan ish qayta urinilmaydi -
        # aks holda (masalan xotira yetmasa) bot cheksiz qayta ishga tushib qolishi mumkin.
        c.execute("UPDATE jobs SET status='failed', error='Bot ishlash paytida 2 marta to''xtab qoldi"
                  " (ehtimol xotira yoki disk yetmadi). /logs ni tekshiring.' "
                  "WHERE status='running' AND COALESCE(attempts,0) >= 2")
        c.execute("UPDATE jobs SET status='queued' WHERE status='running'")


def add_film(chat_id: int, title: str, path: str) -> int:
    with connect() as c:
        return c.execute("INSERT INTO films(chat_id, title, path) VALUES (?,?,?)",
                         (chat_id, title, path)).lastrowid


def get_film(film_id: int) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute("SELECT * FROM films WHERE id=?", (film_id,)).fetchone()


def add_job(film_id: int, seconds: int, variant: int) -> int:
    with connect() as c:
        return c.execute("INSERT INTO jobs(film_id, seconds, variant) VALUES (?,?,?)",
                         (film_id, seconds, variant)).lastrowid


def get_job(job_id: int) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def set_job(job_id: int, status: str, error: str | None = None, result_path: str | None = None) -> None:
    with connect() as c:
        c.execute("UPDATE jobs SET status=?, error=?, result_path=COALESCE(?, result_path),"
                  " started_at=CASE WHEN ?='running' THEN CURRENT_TIMESTAMP ELSE started_at END,"
                  " finished_at=CASE WHEN ? IN ('done','failed') THEN CURRENT_TIMESTAMP ELSE finished_at END"
                  " WHERE id=?", (status, error, result_path, status, status, job_id))


def start_job(job_id: int) -> None:
    with connect() as c:
        c.execute("UPDATE jobs SET status='running', started_at=CURRENT_TIMESTAMP,"
                  " attempts=COALESCE(attempts,0)+1 WHERE id=?", (job_id,))


def set_stage(job_id: int, stage: str) -> None:
    with connect() as c:
        c.execute("UPDATE jobs SET stage=? WHERE id=?", (stage[:200], job_id))


def active_jobs() -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute("SELECT j.*, f.title FROM jobs j JOIN films f ON f.id=j.film_id"
                         " WHERE j.status IN ('queued','running') ORDER BY j.id").fetchall()


def cancel_queued() -> int:
    with connect() as c:
        return c.execute("UPDATE jobs SET status='failed', error='Bekor qilindi' WHERE status='queued'").rowcount


def set_job_metrics(job_id: int, **kw) -> None:
    allowed = {"t_analyze", "t_ai", "t_render", "source", "clips", "out_seconds"}
    kw = {k: v for k, v in kw.items() if k in allowed}
    if not kw:
        return
    with connect() as c:
        c.execute(f"UPDATE jobs SET {', '.join(f'{k}=?' for k in kw)} WHERE id=?", (*kw.values(), job_id))


# ---- usage / statistika ------------------------------------------------------
def add_usage(job_id, stage, model, inp, out, cr, cw, searches, images, cost) -> None:
    with connect() as c:
        c.execute("INSERT INTO usage(job_id, stage, model, input_tokens, output_tokens, cache_read, cache_write,"
                  " searches, images, cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (job_id, stage, model, inp, out, cr, cw, searches, images, cost))


def usage_totals(where: str = "1=1", params: tuple = ()) -> sqlite3.Row:
    with connect() as c:
        return c.execute(
            f"SELECT COUNT(*) calls, COALESCE(SUM(input_tokens),0) inp, COALESCE(SUM(output_tokens),0) out,"
            f" COALESCE(SUM(cache_read),0) cr, COALESCE(SUM(searches),0) searches,"
            f" COALESCE(SUM(images),0) images, COALESCE(SUM(cost_usd),0) cost FROM usage WHERE {where}",
            params).fetchone()


def usage_by_stage() -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute("SELECT stage, COUNT(*) calls, SUM(input_tokens) inp, SUM(output_tokens) out,"
                         " SUM(cost_usd) cost FROM usage GROUP BY stage ORDER BY cost DESC").fetchall()


def job_cost(job_id: int) -> float:
    return float(usage_totals("job_id=?", (job_id,))["cost"])


def recent_jobs(limit: int = 8) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute(
            "SELECT j.*, f.title, (SELECT COALESCE(SUM(cost_usd),0) FROM usage u WHERE u.job_id=j.id) cost"
            " FROM jobs j JOIN films f ON f.id=j.film_id ORDER BY j.id DESC LIMIT ?", (limit,)).fetchall()


def job_averages() -> sqlite3.Row:
    with connect() as c:
        return c.execute(
            "SELECT COUNT(*) n, AVG(t_analyze) ta, AVG(t_ai) tai, AVG(t_render) tr,"
            " AVG((julianday(finished_at)-julianday(started_at))*86400) total,"
            " SUM(source='fallback') fallbacks FROM jobs WHERE status='done'").fetchone()


def kv_get(key: str, default: str | None = None) -> str | None:
    with connect() as c:
        r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def kv_set(key: str, value: str) -> None:
    with connect() as c:
        c.execute("INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))


def queued_jobs() -> list[int]:
    with connect() as c:
        return [r["id"] for r in c.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY id")]


def next_variant(film_id: int, seconds: int) -> int:
    with connect() as c:
        r = c.execute("SELECT COALESCE(MAX(variant),0)+1 FROM jobs WHERE film_id=? AND seconds=?",
                      (film_id, seconds)).fetchone()
        return int(r[0])


def old_films(hours: int) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute(
            "SELECT * FROM films WHERE deleted=0 AND created_at < datetime('now', ?)"
            " AND id NOT IN (SELECT film_id FROM jobs WHERE status IN ('queued','running'))",
            (f"-{hours} hours",)).fetchall()


def mark_film_deleted(film_id: int) -> None:
    with connect() as c:
        c.execute("UPDATE films SET deleted=1 WHERE id=?", (film_id,))


def stats() -> dict[str, int]:
    with connect() as c:
        films = c.execute("SELECT COUNT(*) FROM films").fetchone()[0]
        rows = c.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()
    out = {"films": films}
    out.update({r[0]: r[1] for r in rows})
    return out
