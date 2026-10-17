"""Builds the multi-agent system: an orchestrator plus seven specialists, each with its own tools,
MCP toolset (read-only WMS tools) and the shared self-healing callbacks."""

from __future__ import annotations

import os
import sys
from datetime import date

from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.genai import types
from mcp import StdioServerParameters

from threadroute.agents import callbacks, prompts, tools as T
from threadroute.agents.models import build_model
from threadroute.config import REPO_ROOT, get_settings, mcp_errlog

MCP_BY_AGENT = {
    "intake_agent": ["get_shipment", "get_purchase_order", "get_return", "get_sku"],
    "tracking_agent": ["list_shipments", "get_shipment", "list_locations"],
    "allocation_agent": ["get_inventory", "get_stock_cover", "find_donor_stores", "list_locations", "get_sku",
                         "list_shipments"],
    "exceptions_claims_agent": ["get_shipment", "get_purchase_order", "list_shipments", "list_claims"],
    "returns_agent": ["get_return", "list_returns", "get_sku"],
    "comms_agent": ["get_shipment", "list_locations"],
}


def wms_toolset(tool_names: list[str], shared: McpToolset | None = None) -> McpToolset:
    """A read-only MCP connection to the WMS server (spawned as a stdio subprocess).

    Each agent still gets its own toolset with its own tool allow-list, but when `shared` (the first toolset built) is
    given they reuse its single MCP session, so there is ONE server subprocess instead of one per agent - less memory
    and a faster start. Set THREADROUTE_MCP_SHARED=0 to give every agent its own subprocess."""
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "threadroute.mcp_server"], cwd=str(REPO_ROOT),
        env={**os.environ, "THREADROUTE_MCP_MODE": "read", "THREADROUTE_HOME": str(get_settings().home)},
    )
    ts = McpToolset(connection_params=StdioConnectionParams(server_params=server, timeout=30),
                    tool_filter=tool_names, errlog=mcp_errlog())
    if shared is not None and hasattr(shared, "_mcp_session_manager") and hasattr(ts, "_mcp_session_manager"):
        ts._mcp_session_manager = shared._mcp_session_manager  # noqa: SLF001 - one session, many allow-lists
    return ts


def _gen_config() -> types.GenerateContentConfig:
    """Bounded output (cost + latency) and an optional thinking budget. Both are env-tunable:
    THREADROUTE_MAX_OUTPUT_TOKENS (default 1500) and THREADROUTE_THINKING_BUDGET (unset = model default)."""
    kwargs: dict = {"temperature": 0.2}
    try:
        cap = int(os.environ.get("THREADROUTE_MAX_OUTPUT_TOKENS", "1500"))
        if cap > 0:
            kwargs["max_output_tokens"] = cap
    except ValueError:
        pass
    budget = os.environ.get("THREADROUTE_THINKING_BUDGET", "").strip()
    if budget.lstrip("-").isdigit():
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=int(budget))
    return types.GenerateContentConfig(**kwargs)


def _agent(name: str, description: str, prompt: str, model, tool_list: list, **kwargs) -> LlmAgent:
    base = prompt.replace("{today}", date.today().isoformat())
    return LlmAgent(
        name=name, description=description, model=model,
        instruction=callbacks.make_instruction(name, base),
        tools=tool_list,
        generate_content_config=_gen_config(),
        on_model_error_callback=callbacks.on_model_error,
        on_tool_error_callback=callbacks.on_tool_error,
        after_tool_callback=callbacks.after_tool,
        before_model_callback=callbacks.before_model,
        after_model_callback=callbacks.after_model,
        **kwargs,
    )


def build_root_agent(model=None) -> LlmAgent:
    """`model` may be a Gemini/BaseLlm instance or a model-name string; defaults to settings.model."""
    use_default = model is None  # an injected model (tests, scripted demos) is used everywhere, untouched
    if model is None:
        model = build_model(get_settings().model)
    elif isinstance(model, str):
        model = build_model(model)

    share = os.environ.get("THREADROUTE_MCP_SHARED", "1").strip().lower() not in {"0", "false", "off", "no"}
    first: list[McpToolset] = []

    def wms(agent_name: str) -> McpToolset:
        ts = wms_toolset(MCP_BY_AGENT[agent_name], first[0] if (share and first) else None)
        first.append(ts)
        return ts

    intake = _agent("intake_agent", "Reads attached photos (damaged cartons, packing lists, labels, returned garments) "
                    "and extracts structured findings.", prompts.INTAKE, model,
                    [wms("intake_agent"), T.record_image_findings])
    tracking = _agent("tracking_agent", "Answers where a shipment is, why it is late, and its ETA (read-only).",
                      prompts.TRACKING, model,
                      [wms("tracking_agent"), T.get_carrier_tracking, T.estimate_route,
                       T.get_weather_risk, T.ask_carrier_agent])
    allocation = _agent("allocation_agent", "Fixes stock-outs and size-curve imbalances with inter-store transfers.",
                        prompts.ALLOCATION, model,
                        [wms("allocation_agent"), T.propose_reallocation])
    exceptions = _agent("exceptions_claims_agent", "Resolves delayed shipments (reroute options) and files carrier "
                        "claims for damage or shortage.", prompts.EXCEPTIONS, model,
                        [wms("exceptions_claims_agent"), T.get_carrier_tracking,
                         T.get_weather_risk, T.estimate_route, T.get_reroute_options, T.get_image_findings,
                         T.ask_carrier_agent, T.propose_reroute, T.propose_file_claim])
    returns = _agent("returns_agent", "Grades returned garments and decides restock, refurbish, liquidate or recycle.",
                     prompts.RETURNS, model,
                     [wms("returns_agent"), T.get_image_findings, T.propose_return_disposition])
    comms = _agent("comms_agent", "Drafts emails to stores, carriers and suppliers for approval.", prompts.COMMS, model,
                   [wms("comms_agent"), T.propose_email])
    watchdog = _agent("watchdog_agent", "Monitors API/provider health and applies self-healing remedies.",
                      prompts.WATCHDOG, model,
                      [T.get_health_report, T.diagnose_failures, T.apply_remedy, T.run_auto_heal, T.get_chaos_rules])

    # Model tiering: the orchestrator only routes, so it can run on a cheaper/faster model than the specialists
    # (THREADROUTE_ROUTER_MODEL, e.g. a flash-lite model). Unset = same model as everyone else.
    router_name = os.environ.get("THREADROUTE_ROUTER_MODEL", "").strip()
    router_model = build_model(router_name) if router_name and use_default else model
    return _agent("orchestrator", "ThreadRoute orchestrator: routes requests to specialist logistics agents.",
                  prompts.ORCHESTRATOR, router_model, [],
                  sub_agents=[intake, tracking, allocation, exceptions, returns, comms, watchdog])
