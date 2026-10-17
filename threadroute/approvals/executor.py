"""Executes an approved action. WMS writes go through the MCP server in `full` mode (the only
place write tools exist). If the MCP subprocess fails, resilient_call falls back to calling the same
domain function directly - the human already approved, so the action must not be lost to an outage."""

from __future__ import annotations

import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, ConfigDict

from threadroute import integrations
from threadroute.guardrails import egress
from threadroute.config import REPO_ROOT, get_settings, mcp_errlog
from threadroute.domain import wms
from threadroute.resilience.resilient import AllProvidersFailed, Provider, resilient_call


class WriteResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    ok: bool


def _parse(result) -> dict:
    if result.isError:
        raise RuntimeError("; ".join(getattr(c, "text", "") for c in result.content) or "MCP tool error")
    if result.structuredContent and isinstance(result.structuredContent, dict) and "ok" in result.structuredContent:
        return result.structuredContent
    text = "".join(getattr(c, "text", "") for c in result.content)
    return json.loads(text)


async def _via_mcp(tool: str, args: dict) -> dict:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "threadroute.mcp_server"], cwd=str(REPO_ROOT),
        env={**os.environ, "THREADROUTE_MCP_MODE": "full", "THREADROUTE_HOME": str(get_settings().home)},
    )
    async with stdio_client(params, errlog=mcp_errlog()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return _parse(await session.call_tool(tool, args))


async def _via_domain(tool: str, args: dict) -> dict:
    try:
        return getattr(wms, tool)(**args)
    except wms.WmsError as exc:
        return exc.to_dict()


async def wms_write(tool: str, args: dict) -> dict:
    return await resilient_call(
        "mcp", [Provider("mcp_stdio", _via_mcp), Provider("direct_domain", _via_domain, degraded=True)],
        tool, args, schema=WriteResult,
    )


def _claim_email(shipment: dict, claim: dict, p: dict) -> tuple[str, str]:
    subject = f"Claim {claim['claim_id']} - {p['claim_type']} on {shipment['shipment_id']} ({shipment['carrier']})"
    body = (
        f"Dear {shipment['carrier']} Claims Team,\n\n"
        f"We are filing claim {claim['claim_id']} for shipment {shipment['shipment_id']} "
        f"(PO {shipment['po_id']}) from {shipment['origin']} to {shipment['destination']}.\n\n"
        f"Claim type: {p['claim_type']}\nDamage type: {p['damage_type']}\nSeverity: {p['severity']}\n"
        f"Affected units: {p['affected_units']} of {shipment['total_units']}\n"
        f"Claim value: {claim['currency']} {claim['claim_value']:,.2f}\n\n"
        f"Evidence: {p['evidence_summary']}\n\n"
        "Please acknowledge receipt within 3 business days and confirm the inspection/settlement process.\n\n"
        "Regards,\nAurelio Menswear - Logistics Control Tower"
    )
    return subject, body


async def execute(kind: str, payload: dict) -> dict:
    """Returns {"status": "executed"|"failed", "steps": [...], "result": {...}}."""
    steps: list[dict] = []

    def step(name: str, ok: bool, detail) -> None:
        steps.append({"step": name, "ok": ok, "detail": detail})

    try:
        if kind == "reallocate_stock":
            res = await wms_write("reallocate_stock", payload)
            step("reallocate_stock", res["ok"], res)
            if res["ok"]:
                log = await integrations.log_action("transfers", [res["transfer_id"], payload["from_location"],
                                                                  payload["to_location"], payload["sku"],
                                                                  f"{payload['size']} x{payload['qty']}"])
                step("log_to_sheet", True, log["location"])
        elif kind == "file_claim":
            args = {k: payload[k] for k in ("shipment_id", "claim_type", "damage_type", "severity", "affected_units")}
            args["details"] = {"evidence_summary": payload["evidence_summary"]}
            res = await wms_write("create_claim", args)
            step("create_claim", res["ok"], res)
            if res["ok"]:
                shipment = wms.get_shipment(payload["shipment_id"])
                subject, body = _claim_email(shipment, res, payload)
                mail = await integrations.send_email(get_settings().claims_email_to, subject, body)
                step("email_carrier", True, {"status": mail["status"], "to": mail["to"], "path": mail.get("path")})
                log = await integrations.log_action("claims", [res["claim_id"], payload["shipment_id"],
                                                               payload["claim_type"], res["claim_value"]])
                step("log_to_sheet", True, log["location"])
        elif kind == "reroute_shipment":
            res = await wms_write("reroute_shipment", payload)
            step("reroute_shipment", res["ok"], res)
            if res["ok"]:
                log = await integrations.log_action("reroutes", [payload["shipment_id"], payload["new_carrier"],
                                                                 payload["new_mode"], res["new_eta"]])
                step("log_to_sheet", True, log["location"])
        elif kind == "set_return_disposition":
            res = await wms_write("set_return_disposition", payload)
            step("set_return_disposition", res["ok"], res)
            if res["ok"]:
                log = await integrations.log_action("returns", [payload["return_id"], payload["grade"],
                                                                payload["disposition"]])
                step("log_to_sheet", True, log["location"])
        elif kind == "send_email":
            mail = await integrations.send_email(payload["to"], payload["subject"], payload["body"])
            res = {"ok": True, **mail}
            step("send_email", True, {"status": mail["status"], "path": mail.get("path")})
        else:
            res = {"ok": False, "error": {"code": "UNKNOWN_KIND", "message": f"Unknown action kind '{kind}'"}}
            step("dispatch", False, res)
    except AllProvidersFailed as exc:
        res = {"ok": False, "error": {"code": "SERVICE_UNAVAILABLE", "message": str(exc)}}
        step("execute", False, res)
    except egress.RecipientBlocked as exc:
        res = {"ok": False, "error": {"code": "RECIPIENT_BLOCKED", "message": str(exc)}}
        step("send_email", False, res)
    return {"status": "executed" if res.get("ok") else "failed", "steps": steps, "result": res}
