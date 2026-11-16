"""Persistência em SQLite (stdlib) para sobreviver a reinícios.

Guarda: conversas, lista de bloqueio (opt-out), contadores diários de envio,
estado do disparo (erros consecutivos) e data do primeiro envio (rampa).
Arquivo configurável por DB_PATH (padrão: data/verona.db).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_conn_path: str | None = None
_seen_inserts = 0

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (phone TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS blocklist (phone TEXT PRIMARY KEY, reason TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS daily_counters (day TEXT PRIMARY KEY, sent INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sent_log (phone TEXT PRIMARY KEY, sheet TEXT, row INTEGER, sent_at REAL NOT NULL, sheet_marked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS processed_messages (msg_id TEXT PRIMARY KEY, seen_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_processed_seen ON processed_messages(seen_at);
CREATE INDEX IF NOT EXISTS idx_daily_sent ON daily_counters(sent);
"""


def db_path() -> str:
    return os.getenv("DB_PATH", "").strip() or str(BASE_DIR / "data" / "verona.db")


def conn() -> sqlite3.Connection:
    global _conn, _conn_path
    path = db_path()
    with _lock:
        if _conn is None or _conn_path != path:
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
            if path != ":memory:":
                _conn.execute("PRAGMA journal_mode=WAL")
                # WAL + NORMAL: durável o bastante e evita fsync a cada commit
                _conn.execute("PRAGMA synchronous=NORMAL")
            _conn.executescript(SCHEMA)
            _conn_path = path
            _migrate_json_conversations(_conn)
        return _conn


def reset_for_tests() -> None:
    global _conn, _conn_path
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn, _conn_path = None, None


def _migrate_json_conversations(c: sqlite3.Connection) -> None:
    """Importa o antigo conversations.json uma única vez."""
    legacy = BASE_DIR / "conversations.json"
    if os.getenv("DB_SKIP_LEGACY_IMPORT") or not legacy.exists():
        return
    if c.execute("SELECT value FROM kv WHERE key='legacy_imported'").fetchone():
        return
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
        for phone, conv in (data or {}).items():
            c.execute("INSERT OR IGNORE INTO conversations VALUES (?,?,?)", (phone, json.dumps(conv, ensure_ascii=False), time.time()))
    except Exception:
        pass
    c.execute("INSERT OR REPLACE INTO kv VALUES ('legacy_imported','1')")
    c.commit()


# ---- conversas ----
def get_conversation(phone: str) -> dict[str, Any] | None:
    with _lock:
        row = conn().execute("SELECT data FROM conversations WHERE phone=?", (phone,)).fetchone()
    return json.loads(row[0]) if row else None


def all_conversations() -> dict[str, Any]:
    with _lock:
        rows = conn().execute("SELECT phone, data FROM conversations").fetchall()
    return {p: json.loads(d) for p, d in rows}


def save_conversation(phone: str, conv: dict[str, Any]) -> None:
    with _lock:
        c = conn()
        c.execute("INSERT OR REPLACE INTO conversations VALUES (?,?,?)", (phone, json.dumps(conv, ensure_ascii=False), time.time()))
        c.commit()


# ---- bloqueio / opt-out ----
def block(phone: str, reason: str = "opt-out") -> None:
    if not phone:
        return
    with _lock:
        c = conn()
        c.execute("INSERT OR IGNORE INTO blocklist VALUES (?,?,?)", (phone, reason, time.time()))
        c.commit()


def unblock(phone: str) -> None:
    with _lock:
        c = conn()
        c.execute("DELETE FROM blocklist WHERE phone=?", (phone,))
        c.commit()


def is_blocked(phone: str) -> bool:
    from evolution_client import phone_variants  # import tardio

    variants = phone_variants(phone) | {phone}
    with _lock:
        c = conn()
        q = "SELECT 1 FROM blocklist WHERE phone IN (%s)" % ",".join("?" * len(variants))
        return c.execute(q, tuple(variants)).fetchone() is not None


def count_blocked() -> int:
    with _lock:
        return int(conn().execute("SELECT COUNT(*) FROM blocklist").fetchone()[0])


def list_blocked() -> list[dict[str, Any]]:
    with _lock:
        rows = conn().execute("SELECT phone, reason, created_at FROM blocklist ORDER BY created_at DESC").fetchall()
    return [{"phone": p, "reason": r, "created_at": t} for p, r, t in rows]


# ---- contadores diários ----
def get_sent(day: str) -> int:
    with _lock:
        row = conn().execute("SELECT sent FROM daily_counters WHERE day=?", (day,)).fetchone()
    return int(row[0]) if row else 0


def incr_sent(day: str) -> int:
    with _lock:
        c = conn()
        c.execute("INSERT INTO daily_counters(day, sent) VALUES (?,1) ON CONFLICT(day) DO UPDATE SET sent=sent+1", (day,))
        row = c.execute("SELECT sent FROM daily_counters WHERE day=?", (day,)).fetchone()
        c.commit()
    return int(row[0])


def active_send_days() -> int:
    """Quantos dias distintos já tiveram envio (usado na rampa de aquecimento)."""
    with _lock:
        row = conn().execute("SELECT COUNT(*) FROM daily_counters WHERE sent > 0").fetchone()
    return int(row[0])


# ---- registro local de envios (evita reenviar se a planilha falhar) ----
def record_sent(phone: str, sheet: str, row: int, sheet_marked: bool) -> None:
    with _lock:
        c = conn()
        c.execute(
            "INSERT OR REPLACE INTO sent_log(phone, sheet, row, sent_at, sheet_marked) VALUES (?,?,?,?,?)",
            (phone, sheet, row, time.time(), 1 if sheet_marked else 0),
        )
        c.commit()


def was_sent(phone: str) -> bool:
    from evolution_client import phone_variants

    variants = phone_variants(phone) | {phone}
    with _lock:
        q = "SELECT 1 FROM sent_log WHERE phone IN (%s)" % ",".join("?" * len(variants))
        return conn().execute(q, tuple(variants)).fetchone() is not None


def set_sheet_marked(phone: str) -> None:
    with _lock:
        c = conn()
        c.execute("UPDATE sent_log SET sheet_marked=1 WHERE phone=?", (phone,))
        c.commit()


def sent_get(phone: str) -> dict[str, Any] | None:
    from evolution_client import phone_variants

    variants = phone_variants(phone) | {phone}
    with _lock:
        q = "SELECT phone, sheet, row, sent_at, sheet_marked FROM sent_log WHERE phone IN (%s)" % ",".join("?" * len(variants))
        row = conn().execute(q, tuple(variants)).fetchone()
    if not row:
        return None
    return {"phone": row[0], "sheet": row[1], "row": row[2], "sent_at": row[3], "sheet_marked": bool(row[4])}


# ---- deduplicação de webhooks (Evolution reenvia em timeout) ----
def mark_message_seen(msg_id: str, ttl_seconds: float = 3 * 86400) -> bool:
    """Registra o id; devolve False se já tinha sido visto (duplicado)."""
    if not msg_id:
        return True
    now = time.time()
    with _lock:
        c = conn()
        cur = c.execute("INSERT OR IGNORE INTO processed_messages(msg_id, seen_at) VALUES (?,?)", (msg_id, now))
        fresh = cur.rowcount == 1
        global _seen_inserts
        _seen_inserts += 1
        if _seen_inserts % 200 == 0:  # poda periódica, sem custo por mensagem
            c.execute("DELETE FROM processed_messages WHERE seen_at < ?", (now - ttl_seconds,))
        c.commit()
    return fresh


# ---- kv ----
def kv_get(key: str, default: Any = None) -> Any:
    with _lock:
        row = conn().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def kv_set(key: str, value: Any) -> None:
    with _lock:
        c = conn()
        c.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))
        c.commit()


def ping() -> bool:
    try:
        with _lock:
            conn().execute("SELECT 1").fetchone()
        return True
    except Exception:
        return False
