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


def wms_toolset(tool_names: list[str]) -> McpToolset:
    """A read-only MCP connection to the WMS server (spawned as a stdio subprocess)."""
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "threadroute.mcp_server"], cwd=str(REPO_ROOT),
        env={**os.environ, "THREADROUTE_MCP_MODE": "read", "THREADROUTE_HOME": str(get_settings().home)},
    )
    return McpToolset(connection_params=StdioConnectionParams(server_params=server, timeout=30),
                      tool_filter=tool_names, errlog=mcp_errlog())


def _agent(name: str, description: str, prompt: str, model, tool_list: list, **kwargs) -> LlmAgent:
    base = prompt.replace("{today}", date.today().isoformat())
    return LlmAgent(
        name=name, description=description, model=model,
        instruction=callbacks.make_instruction(name, base),
        tools=tool_list,
        generate_content_config=types.GenerateContentConfig(temperature=0.2),
        on_model_error_callback=callbacks.on_model_error,
        on_tool_error_callback=callbacks.on_tool_error,
        after_tool_callback=callbacks.after_tool,
        before_model_callback=callbacks.before_model,
        after_model_callback=callbacks.after_model,
        **kwargs,
    )


def build_root_agent(model=None) -> LlmAgent:
    """`model` may be a Gemini/BaseLlm instance or a model-name string; defaults to settings.model."""
    if model is None:
        model = build_model(get_settings().model)
    elif isinstance(model, str):
        model = build_model(model)

    intake = _agent("intake_agent", "Reads attached photos (damaged cartons, packing lists, labels, returned garments) "
                    "and extracts structured findings.", prompts.INTAKE, model,
                    [wms_toolset(MCP_BY_AGENT["intake_agent"]), T.record_image_findings])
    tracking = _agent("tracking_agent", "Answers where a shipment is, why it is late, and its ETA (read-only).",
                      prompts.TRACKING, model,
                      [wms_toolset(MCP_BY_AGENT["tracking_agent"]), T.get_carrier_tracking, T.estimate_route,
                       T.get_weather_risk, T.ask_carrier_agent])
    allocation = _agent("allocation_agent", "Fixes stock-outs and size-curve imbalances with inter-store transfers.",
                        prompts.ALLOCATION, model,
                        [wms_toolset(MCP_BY_AGENT["allocation_agent"]), T.propose_reallocation])
    exceptions = _agent("exceptions_claims_agent", "Resolves delayed shipments (reroute options) and files carrier "
                        "claims for damage or shortage.", prompts.EXCEPTIONS, model,
                        [wms_toolset(MCP_BY_AGENT["exceptions_claims_agent"]), T.get_carrier_tracking,
                         T.get_weather_risk, T.estimate_route, T.get_reroute_options, T.get_image_findings,
                         T.ask_carrier_agent, T.propose_reroute, T.propose_file_claim])
    returns = _agent("returns_agent", "Grades returned garments and decides restock, refurbish, liquidate or recycle.",
                     prompts.RETURNS, model,
                     [wms_toolset(MCP_BY_AGENT["returns_agent"]), T.get_image_findings, T.propose_return_disposition])
    comms = _agent("comms_agent", "Drafts emails to stores, carriers and suppliers for approval.", prompts.COMMS, model,
                   [wms_toolset(MCP_BY_AGENT["comms_agent"]), T.propose_email])
    watchdog = _agent("watchdog_agent", "Monitors API/provider health and applies self-healing remedies.",
                      prompts.WATCHDOG, model,
                      [T.get_health_report, T.diagnose_failures, T.apply_remedy, T.run_auto_heal, T.get_chaos_rules])

    return _agent("orchestrator", "ThreadRoute orchestrator: routes requests to specialist logistics agents.",
                  prompts.ORCHESTRATOR, model, [],
                  sub_agents=[intake, tracking, allocation, exceptions, returns, comms, watchdog])
