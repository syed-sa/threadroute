import asyncio
import json

import pytest

from threadroute import cli
from threadroute.agents.runtime import ThreadRouteRuntime
from threadroute.config import get_settings
from threadroute.resilience import chaos
from threadroute.testing import ScriptedLlm, call, say, transfer


def test_parser_covers_all_commands():
    p = cli.build_parser()
    for argv in (["seed", "--reset"], ["doctor"], ["chat", "--quiet"], ["web", "--port", "9000"], ["mcp"], ["demo"],
                 ["chaos", "set", "carrier:primary_api", "fail", "--count", "2"], ["chaos", "clear"], ["chaos", "list"],
                 ["watchdog"], ["report", "--out", "x.md"], ["eval", "--scenario", "a"], ["google-auth"]):
        assert p.parse_args(argv).command == argv[0]


def test_seed_and_chaos_and_report_commands(capsys, tmp_path):
    assert cli.main.__name__ == "main"
    p = cli.build_parser()
    assert cli.cmd_seed(p.parse_args(["seed", "--reset"])) == 0
    assert "shipments=35" in capsys.readouterr().out
    cli.cmd_chaos(p.parse_args(["chaos", "set", "carrier:primary_api", "fail", "--count", "2"]))
    cli.cmd_chaos(p.parse_args(["chaos", "list"]))
    assert "carrier:primary_api: fail" in capsys.readouterr().out
    cli.cmd_chaos(p.parse_args(["chaos", "clear"]))
    assert chaos.list_rules() == []
    out = tmp_path / "r.md"
    assert cli.cmd_report(p.parse_args(["report", "--out", str(out)])) == 0
    assert out.read_text(encoding="utf-8").startswith("# ThreadRoute")


def test_doctor_runs_without_credentials(capsys):
    assert cli.cmd_doctor(None) == 0
    text = capsys.readouterr().out
    assert "MISSING" in text and "WMS database      OK" in text


def test_edit_parsing_for_approve_command():
    assert cli._parse_edits(["qty=15", "to_location=ST-CHN-TNG", "--", "use", "Chennai"]) == (
        {"qty": 15, "to_location": "ST-CHN-TNG"}, "use Chennai")
    assert cli._parse_edits([]) == ({}, None)


async def test_chat_command_loop_end_to_end(monkeypatch, capsys):
    llm = ScriptedLlm(script=[transfer("allocation_agent"),
                              call("propose_reallocation", from_location="ST-HYD-BJH", to_location="ST-BLR-IND",
                                   sku="SH-OXF-WHT", size="L", qty=20, rationale="x"),
                              say("ACT-1001 awaits approval.")], seen=[])
    monkeypatch.setattr("threadroute.agents.runtime.build_root_agent", lambda model=None: __import__(
        "threadroute.agents.factory", fromlist=["x"]).build_root_agent(llm))
    inputs = iter(["Bengaluru needs Oxford shirts", "/actions", "/approve ACT-1001 qty=10 -- Always keep Hyderabad cover.",
                   "/guidance", "/up", "/health", "/help", "/quit"])

    async def fake_to_thread(fn, prompt):
        return next(inputs)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy")
    code = await cli._chat(type("A", (), {"quiet": False})())
    out = capsys.readouterr().out
    assert code == 0 and "ACT-1001" in out and "EXECUTED" in out and "learned a new rule" in out


def test_demo_runs_end_to_end(capsys):
    from threadroute.demo import run_demo

    assert asyncio.run(run_demo()) == 0
    out = capsys.readouterr().out
    for marker in ("STEP 1", "STEP 6", "learned rule", "image part delivered to the model: True",
                   "Bengaluru L stock: 0 -> 18", "source=cache", "fully recovered", "Demo complete"):
        assert marker in out


async def test_eval_scoring_and_runner_with_scripted_model(runtime, scripted):
    from evals import run_evals

    scenarios = run_evals.load_scenarios()
    assert len(scenarios) >= 10 and all("id" in s and "prompt" in s for s in scenarios)
    scripted.script = [transfer("allocation_agent"),
                       call("find_donor_stores", sku="SH-OXF-WHT", size="L", to_location_id="ST-BLR-IND", needed_qty=30),
                       call("propose_reallocation", from_location="ST-HYD-BJH", to_location="ST-BLR-IND", sku="SH-OXF-WHT",
                            size="L", qty=30, rationale="x"),
                       say("Queued ACT-1001, awaiting your approval."),
                       say("Sure, SH-9999 was not found in our system.")]
    chosen = run_evals.load_scenarios("stockout_reallocation") + run_evals.load_scenarios("unknown_id_not_invented")
    results = await run_evals.run_scenarios(runtime, chosen)
    assert [r["passed"] for r in results] == [True, True], results
    # a wrong answer must fail loudly
    scripted.script = [say("Everything is fine.")]
    bad = await run_evals.run_scenarios(runtime, run_evals.load_scenarios("delay_root_cause"))
    assert bad[0]["passed"] is False and any("agents" in f or "tool" in f or "reply" in f for f in bad[0]["failures"])
