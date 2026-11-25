# -*- coding: utf-8 -*-
"""SQLite 存储：运行记录与历史对比。"""
import json
import sqlite3
import threading

import config

_lock = threading.Lock()
_conn = None


def conn():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS runs("
            "ts TEXT PRIMARY KEY, trade_date TEXT, payload TEXT, created REAL)"
        )
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_ts ON runs(ts DESC)")
        _conn.commit()
    return _conn


def save(payload):
    with _lock:
        c = conn()
        c.execute(
            "INSERT OR REPLACE INTO runs(ts, trade_date, payload, created) VALUES(?,?,?,strftime('%s','now'))",
            (payload["generated_at"], payload.get("trade_date"), json.dumps(payload, ensure_ascii=False)),
        )
        c.execute(
            "DELETE FROM runs WHERE ts NOT IN (SELECT ts FROM runs ORDER BY ts DESC LIMIT 500)"
        )
        c.commit()


def latest():
    with _lock:
        row = conn().execute("SELECT payload FROM runs ORDER BY ts DESC LIMIT 1").fetchone()
    return json.loads(row[0]) if row else None


def previous():
    with _lock:
        rows = conn().execute("SELECT payload FROM runs ORDER BY ts DESC LIMIT 2").fetchall()
    if len(rows) < 2:
        return None
    return json.loads(rows[1][0])


def recent_payloads(limit=12):
    with _lock:
        rows = conn().execute(
            "SELECT payload FROM runs ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for (raw,) in rows:
        try:
            out.append(json.loads(raw))
        except Exception:
            continue
    return out


def history(limit=24):
    with _lock:
        rows = conn().execute(
            "SELECT ts, trade_date, payload FROM runs ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for ts, td, raw in rows:
        try:
            p = json.loads(raw)
        except Exception:
            continue
        ai = p.get("ai") or {}
        out.append({
            "ts": ts,
            "trade_date": td,
            "regime": (p.get("market") or {}).get("regime"),
            "risk": ai.get("risk"),
            "top": (p.get("picks") or [{}])[0].get("name") if p.get("picks") else "",
            "count": len(p.get("picks") or []),
            "ai_status": ai.get("status"),
        })
    return out
