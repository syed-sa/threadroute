"""Deterministic watchdog: reads call statistics, diagnoses sick providers and patches the
runtime healing configuration. The Watchdog LLM agent exposes the same functions as tools."""

from __future__ import annotations

import time
from collections import defaultdict

from threadroute.resilience import breaker, store

DEMOTION_SECONDS = 120
MIN_CALLS = 3


def health_report(window_minutes: float = 60) -> dict:
    stats = store.recent_stats(window_minutes)
    by_target: dict[str, list[dict]] = defaultdict(list)
    for s in stats:
        by_target[s["target"]].append(s)
    targets = {}
    states = breaker.all_states()
    for target, rows in sorted(by_target.items()):
        fails = [r for r in rows if not r["ok"]]
        targets[target] = {
            "calls": len(rows),
            "failures": len(fails),
            "failure_rate": round(len(fails) / len(rows), 2),
            "avg_latency_ms": round(sum(r["latency_ms"] for r in rows) / len(rows), 1),
            "last_error": fails[-1]["error"] if fails else None,
            "breaker": states.get(target, "closed"),
        }
    components = sorted({t.split(":")[0] for t in targets})
    configs = {}
    for c in components:
        cfg = store.get_config(c)
        configs[c] = {"max_retries": cfg.max_retries, "timeout": cfg.timeout, "disabled": cfg.disabled,
                      "demoted_now": cfg.active_demotions()}
    return {"window_minutes": window_minutes, "targets": targets, "config": configs,
            "open_breakers": [t for t, s in states.items() if s != "closed"],
            "recent_events": store.recent_events(window_minutes, limit=15)}


def diagnose(window_minutes: float = 30, component: str | None = None) -> list[dict]:
    """Return recommended remedies. Each has {component, provider, issue, action, params}."""
    report = health_report(window_minutes)
    recs: list[dict] = []
    for target, t in report["targets"].items():
        comp, prov = target.split(":", 1)
        if component and comp != component:
            continue
        if t["calls"] < MIN_CALLS:
            continue
        cfg = store.get_config(comp)
        if prov in cfg.active_demotions():
            continue
        err = (t["last_error"] or "").lower()
        if t["failure_rate"] >= 0.5:
            recs.append({"component": comp, "provider": prov, "action": "demote_provider",
                         "issue": f"{prov} failing {int(t['failure_rate'] * 100)}% of {t['calls']} calls "
                                  f"(last error: {t['last_error']})",
                         "params": {"seconds": DEMOTION_SECONDS}})
        elif "timeout" in err and cfg.timeout < 30:
            recs.append({"component": comp, "provider": prov, "action": "increase_timeout",
                         "issue": f"{prov} timing out (avg latency {t['avg_latency_ms']} ms)",
                         "params": {"timeout": min(30.0, round(cfg.timeout * 1.5, 1))}})
        elif t["failure_rate"] >= 0.2 and cfg.max_retries < 4:
            recs.append({"component": comp, "provider": prov, "action": "increase_retries",
                         "issue": f"{prov} intermittently failing ({int(t['failure_rate'] * 100)}%)",
                         "params": {"max_retries": cfg.max_retries + 1}})
    return recs


def apply_remedy(component: str, action: str, provider: str | None = None, params: dict | None = None) -> dict:
    params = params or {}
    cfg = store.get_config(component)
    if action == "demote_provider" and provider:
        cfg.demoted[provider] = time.time() + float(params.get("seconds", DEMOTION_SECONDS))
    elif action == "restore_provider" and provider:
        cfg.demoted.pop(provider, None)
        if provider in cfg.disabled:
            cfg.disabled.remove(provider)
    elif action == "disable_provider" and provider:
        if provider not in cfg.disabled:
            cfg.disabled.append(provider)
    elif action == "enable_provider" and provider:
        if provider in cfg.disabled:
            cfg.disabled.remove(provider)
    elif action == "increase_timeout":
        cfg.timeout = min(30.0, float(params.get("timeout", cfg.timeout * 1.5)))
    elif action == "increase_retries":
        cfg.max_retries = min(4, int(params.get("max_retries", cfg.max_retries + 1)))
    elif action == "reset_breaker" and provider:
        breaker.get_breaker(f"{component}:{provider}").reset()
    else:
        return {"ok": False, "error": f"Unsupported action '{action}' (or missing provider)."}
    store.save_config(component, cfg)
    store.log_event(component, provider, "remedy", f"{action} {params}")
    return {"ok": True, "component": component, "action": action, "provider": provider, "params": params}


def auto_heal(component: str | None = None) -> list[dict]:
    applied = []
    for rec in diagnose(component=component):
        res = apply_remedy(rec["component"], rec["action"], rec["provider"], rec["params"])
        applied.append({**rec, "result": res})
    return applied
