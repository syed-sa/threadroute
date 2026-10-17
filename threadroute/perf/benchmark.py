"""Offline benchmark:  python -m threadroute benchmark [--out DIR]

Runs the same multi-turn conversations through the REAL agent stack (ADK runner, MCP servers, callbacks) with a
scripted model, twice: baseline (THREADROUTE_OPTIMIZE=0) and optimized. What is measured:
  * prompt tokens sent to the model across the conversation - an ESTIMATE (characters / 4) because the scripted
    model reports no usage; with a live Gemini key the app records the API's real counts in the same ledger;
  * model calls, tool calls and wall time per conversation;
  * a simulated-latency micro-benchmark of the parallel reroute comparison.
Reading the output: the token numbers show how much less *context* the optimizer feeds the model; they do not
include the cost of model replies. Real latency/cost depend on the live model and network - run
`python -m threadroute web` with a key and open the 'Cost & speed' tab for real numbers."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

from threadroute.perf import tokens as tk
from threadroute.perf.metrics import metrics

LONG_ANSWER = "Here is the summary based on the data above. " * 3


def _conversations():
    from threadroute.testing import call, say, transfer

    return {
        "tracking_delay": (
            ["List all delayed or held shipments.", "Why is SH-1001 late?", "And what about SH-1009?"],
            [transfer("tracking_agent"), call("list_shipments", status="delayed"), say(LONG_ANSWER),
             call("get_shipment", shipment_id="SH-1001"), call("get_carrier_tracking", shipment_id="SH-1001"),
             say(LONG_ANSWER),
             call("get_shipment", shipment_id="SH-1009"), call("get_carrier_tracking", shipment_id="SH-1009"),
             say(LONG_ANSWER)]),
        "reroute_comparison": (
            ["SH-1001 is stuck. Compare reroute options.", "Which is cheapest?"],
            [transfer("exceptions_claims_agent"), call("get_shipment", shipment_id="SH-1001"),
             call("get_reroute_options", shipment_id="SH-1001"), say(LONG_ANSWER), say(LONG_ANSWER)]),
        "stock_check": (
            ["Which stores are low on Oxford white shirts size L?", "Can any donor store cover Bengaluru?"],
            [transfer("allocation_agent"), call("get_stock_cover", sku="SH-OXF-WHT"), say(LONG_ANSWER),
             call("find_donor_stores", sku="SH-OXF-WHT", size="L", to_location_id="ST-BLR-IND", needed_qty=30),
             say(LONG_ANSWER)]),
    }


async def _run_all(optimize: bool) -> dict:
    from threadroute.agents.runtime import ThreadRouteRuntime
    from threadroute.testing import ScriptedLlm

    os.environ["THREADROUTE_OPTIMIZE"] = "1" if optimize else "0"
    os.environ["THREADROUTE_VERIFY"] = "0"  # benchmark the context pipeline, not the verifier's extra retries
    out = {}
    for name, (turns, script) in _conversations().items():
        tk.ledger.reset()
        llm = ScriptedLlm(script=list(script), seen=[])
        rt = ThreadRouteRuntime(model=llm)
        t0 = time.perf_counter()
        tool_calls = 0
        try:
            for i, text in enumerate(turns):
                res = await rt.chat(f"bench-{name}-{optimize}", text)
                tool_calls += sum(1 for t in res.trace if t["type"] == "tool_call")
        finally:
            await rt.close()
        snap = tk.ledger.snapshot()["total"]
        out[name] = {"prompt_tokens_est": snap["prompt_tokens"], "model_calls": snap["calls"],
                     "tool_calls": tool_calls, "seconds": round(time.perf_counter() - t0, 2)}
    return out


async def _reroute_micro(latency_ms: int = 120) -> dict:
    from threadroute.agents import tools as T

    calls = {"n": 0}

    async def slow_route(origin, destination, mode="road"):
        calls["n"] += 1
        await asyncio.sleep(latency_ms / 1000)
        return {"ok": True, "distance_km": 500, "duration_hours": 10}

    original = T.integrations.estimate_route
    T.integrations.estimate_route = slow_route
    try:
        t0 = time.perf_counter()
        await T.get_reroute_options("SH-1001")
        parallel_ms = (time.perf_counter() - t0) * 1000
    finally:
        T.integrations.estimate_route = original
    # the sequential equivalent is the arithmetic sum of the same simulated calls (one per route estimate)
    return {"simulated_latency_ms_per_call": latency_ms, "route_calls": calls["n"],
            "measured_parallel_ms": round(parallel_ms), "sequential_equivalent_ms": latency_ms * (calls["n"] + 2)}


async def run_benchmark() -> dict:
    os.environ["THREADROUTE_HOME"] = tempfile.mkdtemp(prefix="threadroute-bench-")
    os.environ["THREADROUTE_LIVE_APIS"] = "false"
    os.environ["THREADROUTE_CACHE_TTL"] = "0"
    saved = {k: os.environ.get(k) for k in ("THREADROUTE_OPTIMIZE", "THREADROUTE_VERIFY")}
    metrics.enabled = True
    try:
        base = await _run_all(False)
        opt = await _run_all(True)
        micro = await _reroute_micro()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    rows = []
    for name in base:
        b, o = base[name]["prompt_tokens_est"], opt[name]["prompt_tokens_est"]
        rows.append({"conversation": name, "baseline_tokens_est": b, "optimized_tokens_est": o,
                     "saved_pct": round(100 * (b - o) / b, 1) if b else 0.0,
                     "baseline_s": base[name]["seconds"], "optimized_s": opt[name]["seconds"],
                     "model_calls": opt[name]["model_calls"], "tool_calls": opt[name]["tool_calls"]})
    tb, to = sum(r["baseline_tokens_est"] for r in rows), sum(r["optimized_tokens_est"] for r in rows)
    return {"rows": rows, "total_baseline_tokens_est": tb, "total_optimized_tokens_est": to,
            "total_saved_pct": round(100 * (tb - to) / tb, 1) if tb else 0.0, "reroute_micro": micro,
            "estimate_note": "Token counts are character/4 estimates of the prompts sent (scripted model reports no usage)."}


def to_markdown(r: dict) -> str:
    L = ["# ThreadRoute - speed & token benchmark", "",
         "| conversation | baseline prompt tokens (est.) | optimized (est.) | saved | wall time baseline -> optimized |",
         "|---|---:|---:|---:|---:|"]
    for x in r["rows"]:
        L.append(f"| {x['conversation']} | {x['baseline_tokens_est']:,} | {x['optimized_tokens_est']:,} | "
                 f"{x['saved_pct']}% | {x['baseline_s']}s -> {x['optimized_s']}s |")
    L += [f"| **total** | **{r['total_baseline_tokens_est']:,}** | **{r['total_optimized_tokens_est']:,}** | "
          f"**{r['total_saved_pct']}%** | |", "", f"_{r['estimate_note']}_", ""]
    m = r["reroute_micro"]
    L += ["## Parallel reroute comparison (simulated latency)",
          f"With {m['simulated_latency_ms_per_call']} ms per provider call, comparing options for one shipment takes "
          f"**{m['measured_parallel_ms']} ms** run concurrently versus about **{m['sequential_equivalent_ms']} ms** "
          "if the same calls ran one after another (calculated, not measured).", "",
          "Other optimizations are structural and not part of this table: short-lived caches for carrier/weather/route "
          "lookups, batched background writes (no per-request disk I/O), in-memory timing (no logging on the hot path), "
          "bounded output tokens, and optional cheaper model for the routing agent."]
    return "\n".join(L) + "\n"


def main(out: Path | None = None) -> int:
    r = asyncio.run(run_benchmark())
    from threadroute.config import get_settings

    out = out or get_settings().reports_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "benchmark_latest.json").write_text(json.dumps(r, indent=2), encoding="utf-8")
    md = to_markdown(r)
    (out / "benchmark_latest.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"Saved: {out / 'benchmark_latest.md'}")
    return 0
