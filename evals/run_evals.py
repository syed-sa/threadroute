"""Live conversation evals.   python -m threadroute eval [--scenario ID]

Runs each scenario in a fresh session against the real Gemini model (needs GOOGLE_API_KEY), scores
the agent trace + reply against the expectations in scenarios.yaml, prints a table and writes
reports/eval_results.json. `run_scenarios` is also unit-tested with a scripted model."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path

import yaml

from threadroute.config import REPO_ROOT

SCENARIOS_FILE = Path(__file__).parent / "scenarios.yaml"
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def load_scenarios(only: str | None = None) -> list[dict]:
    scenarios = yaml.safe_load(SCENARIOS_FILE.read_text(encoding="utf-8"))
    return [s for s in scenarios if not only or s["id"] == only]


def score(scenario: dict, result) -> list[str]:
    """Returns a list of failure reasons (empty = pass)."""
    exp, fails = scenario.get("expect", {}), []
    agents = {t["agent"] for t in result.trace}
    tools = {t["name"] for t in result.trace if t["type"] == "tool_call"}
    kinds = {a["kind"] for a in result.new_actions}
    if result.error:
        fails.append(f"runtime error: {result.error}")
    if exp.get("agents") and not agents & set(exp["agents"]):
        fails.append(f"none of agents {exp['agents']} acted (saw {sorted(agents)})")
    for tool in exp.get("tools", []):
        if tool not in tools:
            fails.append(f"tool '{tool}' was not called (called {sorted(tools)})")
    for kind in exp.get("actions", []):
        if kind not in kinds:
            fails.append(f"action '{kind}' was not proposed (proposed {sorted(kinds)})")
    if exp.get("no_actions") and kinds:
        fails.append(f"unexpected actions proposed: {sorted(kinds)}")
    if exp.get("reply_any") and not any(w.lower() in result.reply.lower() for w in exp["reply_any"]):
        fails.append(f"reply mentions none of {exp['reply_any']}")
    return fails


async def run_scenarios(runtime, scenarios: list[dict]) -> list[dict]:
    results = []
    for sc in scenarios:
        images = []
        if sc.get("image"):
            path = REPO_ROOT / sc["image"]
            images.append((path.read_bytes(), MIME[path.suffix.lower()]))
        res = await runtime.chat(f"eval-{sc['id']}-{uuid.uuid4().hex[:6]}", sc["prompt"], images)
        fails = score(sc, res)
        results.append({"id": sc["id"], "passed": not fails, "failures": fails, "agent": res.agent,
                        "tools": [t["name"] for t in res.trace if t["type"] == "tool_call"],
                        "actions": [a["kind"] for a in res.new_actions], "reply": res.reply[:500]})
    return results


async def _run(only: str | None) -> int:
    os.environ["THREADROUTE_HOME"] = tempfile.mkdtemp(prefix="threadroute-eval-")
    os.environ["THREADROUTE_LIVE_APIS"] = "false"  # deterministic data for scoring
    from threadroute.agents.runtime import MissingCredentials, ThreadRouteRuntime
    from threadroute.config import get_settings

    try:
        runtime = ThreadRouteRuntime()
    except MissingCredentials as exc:
        print(exc)
        return 2
    scenarios = load_scenarios(only)
    if not scenarios:
        print(f"No scenario '{only}'.")
        return 2
    print(f"Running {len(scenarios)} scenario(s) against {get_settings().model} ...\n")
    results = await run_scenarios(runtime, scenarios)
    await runtime.close()
    for r in results:
        print(f"[{'PASS' if r['passed'] else 'FAIL'}] {r['id']:<26} agent={r['agent']:<24} tools={r['tools']}")
        for f in r["failures"]:
            print(f"        - {f}")
    passed = sum(r["passed"] for r in results)
    print(f"\n{passed}/{len(results)} scenarios passed")
    out = get_settings().reports_dir / "eval_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Details: {out}")
    return 0 if passed == len(results) else 1


def main(only: str | None = None) -> int:
    return asyncio.run(_run(only))
