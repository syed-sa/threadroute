import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from threadroute.config import REPO_ROOT, get_settings
from threadroute.mcp_server.server import READ_TOOLS, WRITE_TOOLS


async def _session(mode):
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "threadroute.mcp_server"], cwd=str(REPO_ROOT),
        env={**os.environ, "THREADROUTE_MCP_MODE": mode, "THREADROUTE_HOME": str(get_settings().home)})
    return stdio_client(params, errlog=open(os.devnull, "w"))


async def _call(session, name, args):
    res = await session.call_tool(name, args)
    return json.loads(res.content[0].text)


async def test_read_mode_exposes_only_read_tools():
    async with await _session("read") as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
    assert names == set(READ_TOOLS)
    assert not names & set(WRITE_TOOLS)


async def test_full_mode_adds_write_tools_and_they_work():
    async with await _session("full") as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            assert names == set(READ_TOOLS) | set(WRITE_TOOLS)
            shipment = await _call(s, "get_shipment", {"shipment_id": "SH-1001"})
            assert shipment["status"] == "delayed"
            bad = await _call(s, "reallocate_stock", {"from_location": "ST-HYD-BJH", "to_location": "ST-BLR-IND",
                                                       "sku": "SH-OXF-WHT", "size": "L", "qty": 500})
            assert bad["ok"] is False and bad["error"]["code"] == "INSUFFICIENT_SURPLUS"
            good = await _call(s, "reallocate_stock", {"from_location": "ST-HYD-BJH", "to_location": "ST-BLR-IND",
                                                        "sku": "SH-OXF-WHT", "size": "L", "qty": 5})
            assert good["ok"] is True


async def test_state_is_shared_between_server_processes():
    async with await _session("full") as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            await _call(s, "reallocate_stock", {"from_location": "ST-HYD-BJH", "to_location": "ST-BLR-IND",
                                                 "sku": "SH-OXF-WHT", "size": "L", "qty": 7})
    async with await _session("read") as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            rows = await _call(s, "get_inventory", {"location_id": "ST-BLR-IND", "sku": "SH-OXF-WHT", "size": "L"})
    row = rows[0] if isinstance(rows, list) else rows
    assert row["qty"] == 7
