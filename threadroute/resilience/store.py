"""Persistence for healing events, call statistics, runtime config and chaos rules (app.db)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict

from threadroute import db
from threadroute.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS healing_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, epoch REAL, component TEXT, provider TEXT,
  kind TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS call_stats (
  id INTEGER PRIMARY KEY AUTOINCREMENT, epoch REAL, target TEXT, ok INTEGER,
  latency_ms REAL, error TEXT
);
CREATE TABLE IF NOT EXISTS healing_config (component TEXT PRIMARY KEY, json TEXT);
CREATE TABLE IF NOT EXISTS chaos_rules (
  target TEXT PRIMARY KEY, mode TEXT, remaining INTEGER, rate REAL, delay_s REAL
);
"""


def app_conn():
    path = get_settings().app_db
    conn = db.connect(path)
    conn.executescript(SCHEMA)
    return conn


@dataclass
class ComponentConfig:
    max_retries: int = 2
    base_delay: float = 0.3
    timeout: float = 8.0
    disabled: list[str] = field(default_factory=list)
    demoted: dict[str, float] = field(default_factory=dict)  # provider -> epoch when demotion expires

    def active_demotions(self, now: float | None = None) -> list[str]:
        now = now or time.time()
        return [p for p, until in self.demoted.items() if until > now]


def get_config(component: str) -> ComponentConfig:
    conn = app_conn()
    try:
        row = conn.execute("SELECT json FROM healing_config WHERE component=?", (component,)).fetchone()
    finally:
        conn.close()
    cfg = ComponentConfig()
    if row:
        data = json.loads(row["json"])
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
    return cfg


def save_config(component: str, cfg: ComponentConfig) -> None:
    conn = app_conn()
    try:
        conn.execute("INSERT INTO healing_config (component, json) VALUES (?, ?) "
                     "ON CONFLICT(component) DO UPDATE SET json=excluded.json",
                     (component, json.dumps(asdict(cfg))))
        conn.commit()
    finally:
        conn.close()


def set_config(component: str, **updates) -> ComponentConfig:
    cfg = get_config(component)
    for k, v in updates.items():
        if not hasattr(cfg, k):
            raise ValueError(f"Unknown config field '{k}'")
        setattr(cfg, k, v)
    save_config(component, cfg)
    return cfg


def clear_demotions() -> None:
    """Forget all provider demotions and disabled flags (used when a failure is known to be over)."""
    conn = app_conn()
    try:
        for row in conn.execute("SELECT component, json FROM healing_config").fetchall():
            data = json.loads(row["json"])
            data["demoted"], data["disabled"] = {}, []
            conn.execute("UPDATE healing_config SET json=? WHERE component=?", (json.dumps(data), row["component"]))
        conn.commit()
    finally:
        conn.close()


def log_event(component: str, provider: str | None, kind: str, detail: str = "") -> None:
    conn = app_conn()
    try:
        conn.execute("INSERT INTO healing_events (ts, epoch, component, provider, kind, detail) VALUES (?,?,?,?,?,?)",
                     (db.now_iso(), time.time(), component, provider, kind, detail[:500]))
        conn.commit()
    finally:
        conn.close()


def record_call(target: str, ok: bool, latency_ms: float, error: str = "") -> None:
    conn = app_conn()
    try:
        conn.execute("INSERT INTO call_stats (epoch, target, ok, latency_ms, error) VALUES (?,?,?,?,?)",
                     (time.time(), target, 1 if ok else 0, round(latency_ms, 1), error[:300]))
        conn.commit()
    finally:
        conn.close()


def recent_events(minutes: float = 60, limit: int = 50, component: str | None = None) -> list[dict]:
    since = time.time() - minutes * 60
    sql, params = "SELECT * FROM healing_events WHERE epoch>=?", [since]
    if component:
        sql += " AND component=?"; params.append(component)
    sql += " ORDER BY id DESC LIMIT ?"; params.append(limit)
    conn = app_conn()
    try:
        return db.rows(conn.execute(sql, params))
    finally:
        conn.close()


def recent_stats(minutes: float = 60) -> list[dict]:
    since = time.time() - minutes * 60
    conn = app_conn()
    try:
        return db.rows(conn.execute("SELECT * FROM call_stats WHERE epoch>=? ORDER BY id", (since,)))
    finally:
        conn.close()


def reset_all() -> None:
    """Wipe healing history, stats, config and chaos rules (used by tests and `chaos reset`)."""
    conn = app_conn()
    try:
        for t in ("healing_events", "call_stats", "healing_config", "chaos_rules"):
            conn.execute(f"DELETE FROM {t}")
        conn.commit()
    finally:
        conn.close()
