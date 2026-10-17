"""Failure injection ("chaos") so the self-healing behaviour can be demonstrated and tested.

A rule targets "component:provider" (or "component:*") with a mode:
  fail       raise an error every time (or `count` times)
  timeout    raise a timeout
  slow       sleep `delay_s` before the real call (used to trigger timeouts)
  malformed  let the call succeed but corrupt the response so schema validation fails
  flaky      fail with probability `rate`
"""

from __future__ import annotations

import asyncio
import random

from threadroute.resilience.store import app_conn

MODES = {"fail", "timeout", "slow", "malformed", "flaky"}


class InjectedFailure(RuntimeError):
    pass


def set_rule(target: str, mode: str, count: int | None = None, rate: float = 1.0, delay_s: float = 0.0) -> None:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {sorted(MODES)}")
    conn = app_conn()
    try:
        conn.execute("INSERT INTO chaos_rules (target, mode, remaining, rate, delay_s) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(target) DO UPDATE SET mode=excluded.mode, remaining=excluded.remaining, "
                     "rate=excluded.rate, delay_s=excluded.delay_s",
                     (target, mode, -1 if count is None else count, rate, delay_s))
        conn.commit()
    finally:
        conn.close()


def clear(target: str | None = None) -> None:
    conn = app_conn()
    try:
        if target:
            conn.execute("DELETE FROM chaos_rules WHERE target=?", (target,))
        else:
            conn.execute("DELETE FROM chaos_rules")
        conn.commit()
    finally:
        conn.close()
    if not target:  # the experiment is over: let demoted providers take traffic again
        from threadroute.resilience import breaker, store

        store.clear_demotions()
        breaker.reset_all()


def list_rules() -> list[dict]:
    conn = app_conn()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM chaos_rules ORDER BY target")]
    finally:
        conn.close()


def _find_rule(conn, target: str):
    component = target.split(":")[0]
    return (conn.execute("SELECT * FROM chaos_rules WHERE target=?", (target,)).fetchone()
            or conn.execute("SELECT * FROM chaos_rules WHERE target=?", (f"{component}:*",)).fetchone())


async def apply(target: str) -> str | None:
    """Called before a provider runs. Raises for fail/timeout/flaky, sleeps for slow, and returns
    'malformed' so the caller can corrupt the response afterwards."""
    conn = app_conn()
    try:
        rule = _find_rule(conn, target)
        if not rule:
            return None
        if rule["remaining"] == 0:
            return None
        if rule["remaining"] > 0:
            conn.execute("UPDATE chaos_rules SET remaining=remaining-1 WHERE target=?", (rule["target"],))
            conn.commit()
        mode, rate, delay = rule["mode"], rule["rate"], rule["delay_s"]
    finally:
        conn.close()
    if mode == "fail":
        raise InjectedFailure(f"chaos: injected failure on {target}")
    if mode == "timeout":
        raise asyncio.TimeoutError(f"chaos: injected timeout on {target}")
    if mode == "flaky" and random.random() < rate:
        raise InjectedFailure(f"chaos: injected flaky failure on {target}")
    if mode == "slow":
        await asyncio.sleep(delay)
        return None
    if mode == "malformed":
        return "malformed"
    return None


def corrupt(result: dict) -> dict:
    """Drop the first key and null the next to simulate a malformed upstream payload."""
    broken = dict(result)
    for key in list(broken)[:2]:
        broken.pop(key, None)
    broken["__garbage__"] = "<html>502 Bad Gateway</html>"
    return broken
