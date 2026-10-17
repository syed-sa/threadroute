"""Persistence for healing events, call statistics, runtime config and chaos rules (app.db)."""

from __future__ import annotations

import atexit
import copy
import json
import threading
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


_schema_ready: set[str] = set()
_config_cache: dict[tuple[str, str], tuple[float, "ComponentConfig"]] = {}
CONFIG_TTL_S = 2.0


def app_conn():
    path = get_settings().app_db
    conn = db.connect(path)
    if str(path) not in _schema_ready:  # create tables once per database file, not on every call
        conn.executescript(SCHEMA)
        _schema_ready.add(str(path))
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
    key = (str(get_settings().app_db), component)
    hit = _config_cache.get(key)
    if hit and time.monotonic() - hit[0] < CONFIG_TTL_S:
        return copy.deepcopy(hit[1])
    cfg = _load_config(component)
    _config_cache[key] = (time.monotonic(), cfg)
    return copy.deepcopy(cfg)


def invalidate_config_cache() -> None:
    _config_cache.clear()


# Components whose calls legitimately take longer than the 8s default (an LLM agent answers inside the call).
COMPONENT_DEFAULTS = {"a2a": {"timeout": 60.0, "max_retries": 1}}


def _load_config(component: str) -> ComponentConfig:
    conn = app_conn()
    try:
        row = conn.execute("SELECT json FROM healing_config WHERE component=?", (component,)).fetchone()
    finally:
        conn.close()
    cfg = ComponentConfig()
    for k, v in COMPONENT_DEFAULTS.get(component, {}).items():
        setattr(cfg, k, v)
    if row:
        data = json.loads(row["json"])
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
    return cfg


def save_config(component: str, cfg: ComponentConfig) -> None:
    invalidate_config_cache()
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
    invalidate_config_cache()
    conn = app_conn()
    try:
        for row in conn.execute("SELECT component, json FROM healing_config").fetchall():
            data = json.loads(row["json"])
            data["demoted"], data["disabled"] = {}, []
            conn.execute("UPDATE healing_config SET json=? WHERE component=?", (json.dumps(data), row["component"]))
        conn.commit()
    finally:
        conn.close()


class _BatchWriter:
    """Healing events and call stats are queued in memory and written in batches by a background thread
    (every ~1.5s, at exit, and before any read) so the request path never waits on SQLite."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[tuple] = []  # (db_path, sql, params)
        self._thread: threading.Thread | None = None

    def add(self, sql: str, params: tuple) -> None:
        path = str(get_settings().app_db)
        with self._lock:
            self._events.append((path, sql, params))
            if len(self._events) >= 200:
                pass  # the flusher will pick it up on its next tick
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="tr-batch-writer", daemon=True)
                self._thread.start()

    def _loop(self) -> None:
        while True:
            time.sleep(1.5)
            self.flush()

    def flush(self) -> None:
        with self._lock:
            batch, self._events = self._events, []
        by_path: dict[str, list[tuple]] = {}
        for path, sql, params in batch:
            by_path.setdefault(path, []).append((sql, params))
        for path, items in by_path.items():
            try:
                conn = db.connect(path)
                try:
                    conn.executescript(SCHEMA)
                    for sql, params in items:
                        conn.execute(sql, params)
                    conn.commit()
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001 - telemetry must never break the app
                pass


_writer = _BatchWriter()
atexit.register(_writer.flush)


def flush() -> None:
    _writer.flush()


def log_event(component: str, provider: str | None, kind: str, detail: str = "") -> None:
    _writer.add("INSERT INTO healing_events (ts, epoch, component, provider, kind, detail) VALUES (?,?,?,?,?,?)",
                (db.now_iso(), time.time(), component, provider, kind, detail[:500]))


def record_call(target: str, ok: bool, latency_ms: float, error: str = "") -> None:
    _writer.add("INSERT INTO call_stats (epoch, target, ok, latency_ms, error) VALUES (?,?,?,?,?)",
                (time.time(), target, 1 if ok else 0, round(latency_ms, 1), error[:300]))


def recent_events(minutes: float = 60, limit: int = 50, component: str | None = None) -> list[dict]:
    flush()
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
    flush()
    since = time.time() - minutes * 60
    conn = app_conn()
    try:
        return db.rows(conn.execute("SELECT * FROM call_stats WHERE epoch>=? ORDER BY id", (since,)))
    finally:
        conn.close()


def reset_all() -> None:
    """Wipe healing history, stats, config and chaos rules (used by tests and `chaos reset`)."""
    flush()
    invalidate_config_cache()
    conn = app_conn()
    try:
        for t in ("healing_events", "call_stats", "healing_config", "chaos_rules"):
            conn.execute(f"DELETE FROM {t}")
        conn.commit()
    finally:
        conn.close()
