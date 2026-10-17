"""Command-line interface:  python -m threadroute <command>

Commands: seed | doctor | chat | web | mcp | demo | chaos | watchdog | report | eval | google-auth
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import tempfile
import uuid
import warnings
from pathlib import Path

from threadroute.config import REPO_ROOT, get_settings, load_env


def _quiet_logs() -> None:
    warnings.filterwarnings("ignore")
    for name in ("google_adk", "google.adk", "google_genai", "httpx", "mcp", "asyncio"):
        logging.getLogger(name).setLevel(logging.ERROR)
    logging.getLogger().setLevel(logging.ERROR)


def _line(char: str = "-", n: int = 72) -> str:
    return char * n


def format_action(a: dict) -> str:
    p = ", ".join(f"{k}={v}" for k, v in a["payload"].items() if v not in (None, ""))
    out = [f"  [{a['id']}] {a['kind']}  status={a['status']}  by {a['agent']}", f"      {a['summary']}", f"      {p}"]
    if a.get("guidance_ids"):
        out.append(f"      learned rules applied: {a['guidance_ids']}")
    return "\n".join(out)


# ---------------------------------------------------------------- simple commands
def cmd_seed(args) -> int:
    from threadroute.data.seed import seed_database

    summary = seed_database(get_settings().wms_db, reset=args.reset)
    print("WMS database ready:", ", ".join(f"{k}={v}" for k, v in summary.items()))
    print(f"Location: {get_settings().wms_db}")
    return 0


def cmd_mcp(_args) -> int:
    from threadroute.mcp_server.server import build_server

    build_server().run()
    return 0


def cmd_report(args) -> int:
    from threadroute.feedback.report import build_report_markdown

    md = build_report_markdown()
    if args.out:
        path = Path(args.out)
    else:
        path = get_settings().reports_dir / "feedback_report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md, encoding="utf-8")
    print(md)
    print(f"(saved to {path})")
    return 0


def cmd_chaos(args) -> int:
    from threadroute.resilience import chaos

    if args.action == "set":
        if not args.target or not args.mode:
            print("usage: chaos set <component:provider> <fail|timeout|slow|malformed|flaky> [--count N] [--delay S]")
            return 2
        chaos.set_rule(args.target, args.mode, args.count, delay_s=args.delay)
        print(f"Chaos rule set: {args.target} -> {args.mode}" + (f" (next {args.count} calls)" if args.count else ""))
    elif args.action == "clear":
        chaos.clear(args.target)
        print("Chaos cleared" + (f" for {args.target}" if args.target else "") + ".")
    else:
        rules = chaos.list_rules()
        print("No chaos rules active." if not rules else "\n".join(
            f"  {r['target']}: {r['mode']} (remaining={r['remaining']})" for r in rules))
    return 0


def cmd_watchdog(_args) -> int:
    from threadroute.resilience import watchdog

    recs = watchdog.diagnose()
    print("Recommendations:" if recs else "No unhealthy providers detected.")
    for r in recs:
        print(f"  - {r['component']}/{r['provider']}: {r['issue']} -> {r['action']}")
    if recs:
        print("Applying remedies...")
        for a in watchdog.auto_heal():
            print(f"  applied {a['action']} on {a['component']}/{a['provider']}: {a['result']['ok']}")
    return 0


def cmd_google_auth(_args) -> int:
    from threadroute.integrations.google_auth import get_credentials

    get_credentials(interactive=True)
    print(f"Google OAuth token saved to {get_settings().home / 'token.json'}")
    return 0


def cmd_doctor(_args) -> int:
    s = get_settings()
    ok = True
    print("ThreadRoute doctor\n" + _line())
    print(f"Python            {sys.version.split()[0]}")
    print(f"Data folder       {s.home}")
    for mod in ("google.adk", "google.genai", "mcp", "fastapi", "uvicorn", "httpx", "PIL", "yaml"):
        try:
            __import__(mod)
            print(f"  [ok] import {mod}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  [!!] import {mod}: {exc}")
    print(f"Live Google APIs  {'ON' if s.live_apis else 'off (mock providers, no keys needed)'}")
    print(f"Maps key          {'set' if s.maps_api_key else 'not set'}")
    print(f"Gemini creds      {'found' if s.has_gemini_credentials else 'MISSING - set GOOGLE_API_KEY in .env'}")
    print(f"Models            primary={s.model}  fallbacks={list(s.fallback_models)}")
    from threadroute.domain import wms

    wms.ensure_db()
    print(f"WMS database      OK ({len(wms.list_shipments(limit=50))} shipments)")
    if s.has_gemini_credentials:
        try:
            from google import genai

            client = genai.Client()
            available = {m.name.split('/')[-1] for m in client.models.list()}
            for name in (s.model, *s.fallback_models):
                flag = "available" if name in available else "NOT FOUND for this key - change THREADROUTE_MODEL(S) in .env"
                ok = ok and (name in available or name != s.model)
                print(f"  model {name}: {flag}")
            reply = client.models.generate_content(model=s.model, contents="Reply with the single word: ready")
            print(f"  live call to {s.model}: {reply.text.strip()[:40]!r}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  [!!] Gemini check failed: {type(exc).__name__}: {str(exc)[:200]}")
    print(_line())
    print("All checks passed." if ok else "Some checks failed - see above and the README troubleshooting section.")
    return 0 if ok else 1


# ---------------------------------------------------------------- chat
HELP = """Commands:
  /image <path> [message]      attach a photo (damaged carton, packing list, returned garment...)
  /approve <id> [k=v ...] [-- comment]   approve a proposal (optionally edit fields, add a comment)
  /reject <id> <reason>        reject a proposal - the reason is learned as guidance
  /actions                     list proposals for this session
  /up | /down [comment]        thumbs on the last reply
  /teach <rule>                teach the agents a standing rule
  /guidance                    show learned rules
  /health                      self-healing status (providers, breakers, chaos)
  /report                      feedback + healing report
  /new                         start a fresh conversation
  /help, /quit
Samples to try: /image samples/damaged_carton_SH-1002.png   (also: packing_list_SH-1003.png, returned_jacket_RT-9001.png)"""


def _parse_edits(tokens: list[str]) -> tuple[dict, str | None]:
    edits: dict = {}
    comment = None
    if "--" in tokens:
        i = tokens.index("--")
        comment = " ".join(tokens[i + 1:]) or None
        tokens = tokens[:i]
    for tok in tokens:
        if "=" in tok:
            k, v = tok.split("=", 1)
            edits[k] = int(v) if v.isdigit() else v
    return edits, comment


async def _chat(args) -> int:
    from threadroute.agents.runtime import MissingCredentials, ThreadRouteRuntime
    from threadroute.approvals import service as approvals
    from threadroute.feedback import store as fb
    from threadroute.feedback.report import build_report_markdown
    from threadroute.resilience import breaker, chaos

    try:
        runtime = ThreadRouteRuntime()
    except MissingCredentials as exc:
        print(f"\n{exc}\n")
        return 2
    session_id = f"cli-{uuid.uuid4().hex[:8]}"
    last_agent, last_action_ids = "orchestrator", []
    print("ThreadRoute control tower - type a question, or /help. Ctrl+C to exit.\n")
    while True:
        try:
            raw = (await asyncio.to_thread(input, "you> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            continue
        if raw.startswith("/"):
            cmd, _, rest = raw.partition(" ")
            if cmd in ("/quit", "/exit"):
                break
            elif cmd == "/help":
                print(HELP)
            elif cmd == "/new":
                session_id = f"cli-{uuid.uuid4().hex[:8]}"
                print("Started a new conversation.")
            elif cmd == "/actions":
                acts = approvals.list_actions(session_id=session_id)
                print("\n".join(format_action(a) for a in acts) if acts else "No proposals in this session.")
            elif cmd == "/approve":
                toks = rest.split()
                if not toks:
                    print("usage: /approve <id> [k=v ...] [-- comment]")
                    continue
                edits, comment = _parse_edits(toks[1:])
                res = await approvals.approve(toks[0], comment, edits or None)
                if not res["ok"]:
                    print("  could not approve:", res["error"])
                else:
                    out = res["outcome"]
                    print(f"  {res['action']['id']}: {out['status'].upper()}")
                    for st in out["steps"]:
                        print(f"    - {st['step']}: {'ok' if st['ok'] else 'FAILED'}")
                    if out["status"] == "failed":
                        print("   ", out["result"].get("error"))
                    if res.get("learned") and res["learned"].get("created"):
                        print("  (learned a new rule from your comment)")
            elif cmd == "/reject":
                toks = rest.split(maxsplit=1)
                if len(toks) < 2:
                    print("usage: /reject <id> <reason>   (the reason teaches the agents)")
                    continue
                res = await approvals.reject(toks[0], toks[1])
                print("  rejected - rule learned." if res["ok"] and res["learned"] and res["learned"].get("created")
                      else ("  rejected - existing rule reinforced." if res["ok"] else f"  {res['error']}"))
            elif cmd in ("/up", "/down"):
                fb.record_feedback("thumb", 1 if cmd == "/up" else -1, rest or None, session_id, "message", None, last_agent)
                if cmd == "/down" and rest:
                    fb.learn_guidance(rest, "global")
                print("  thanks - feedback recorded.")
            elif cmd == "/teach":
                res = fb.learn_guidance(rest, "global")
                print("  learned." if res.get("created") else f"  {res.get('reason') or 'reinforced an existing rule'}")
            elif cmd == "/guidance":
                for g in fb.list_guidance():
                    print(f"  #{g['id']} w={g['weight']:.1f} used={g['uses']} [{g['scenario']}] {g['text']}")
            elif cmd == "/health":
                print("  breakers:", breaker.all_states() or "all closed")
                print("  chaos rules:", chaos.list_rules() or "none")
            elif cmd == "/report":
                print(build_report_markdown())
            elif cmd == "/image":
                toks = rest.split(maxsplit=1)
                if not toks:
                    print("usage: /image <path> [message]")
                    continue
                path = Path(toks[0].strip('"'))
                if not path.is_absolute() and not path.exists():
                    path = REPO_ROOT / path
                if not path.exists():
                    print(f"  file not found: {path}")
                    continue
                mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                        ".webp": "image/webp"}.get(path.suffix.lower())
                if not mime:
                    print("  only png/jpg/webp images are supported")
                    continue
                result = await runtime.chat(session_id, toks[1] if len(toks) > 1 else "Please analyse this image.",
                                            [(path.read_bytes(), mime)])
                last_agent = _show(result, args.quiet)
            else:
                print("Unknown command. /help lists commands.")
            continue
        result = await runtime.chat(session_id, raw)
        last_agent = _show(result, args.quiet)
    await runtime.close()
    return 0


def _show(result, quiet: bool) -> str:
    if not quiet:
        for t in result.trace:
            if t["type"] in ("tool_call", "transfer"):
                print(f"   [{t['agent']}] {t['name']} {t['args'][:90]}")
    print(f"\n{result.agent}> {result.reply}\n")
    for note in result.degraded[-3:]:
        print(f"   (degraded data) {note}")
    for a in result.new_actions:
        print(format_action(a))
        print(f"      -> /approve {a['id']}   or   /reject {a['id']} <reason>")
    if result.new_actions:
        print()
    return result.agent


def cmd_chat(args) -> int:
    _quiet_logs()
    return asyncio.run(_chat(args))


def cmd_web(args) -> int:
    _quiet_logs()
    import uvicorn

    from threadroute.ui.app import resolve_auth_token

    token = resolve_auth_token()  # picked before the app module is imported so the app uses the same one
    if token is not None:
        os.environ["THREADROUTE_AUTH_TOKEN"] = token
        print(f"ThreadRoute UI - open this URL (it carries your access token):\n"
              f"  http://{args.host}:{args.port}/#token={token}")
    else:
        print(f"ThreadRoute UI on http://{args.host}:{args.port}  (AUTH DISABLED - local experiments only)")
    if args.carrier_agent_port and get_settings().has_gemini_credentials:
        try:  # run the carrier agent as its own A2A server so agent-to-agent traffic crosses a real network hop
            from threadroute.a2a.client import a2a_token
            from threadroute.a2a.peer import build_carrier_app, serve_in_thread

            serve_in_thread(build_carrier_app(None, token=a2a_token(), port=args.carrier_agent_port),
                            "127.0.0.1", args.carrier_agent_port)
            os.environ["THREADROUTE_A2A_URL"] = f"http://127.0.0.1:{args.carrier_agent_port}"
            print(f"Carrier-desk agent (A2A) running on http://127.0.0.1:{args.carrier_agent_port}")
        except Exception as exc:  # noqa: BLE001 - falls back to the in-process A2A peer
            print(f"(carrier agent server not started: {type(exc).__name__}; using the in-process A2A peer)")
    uvicorn.run("threadroute.ui.app:app", host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_a2a_carrier(args) -> int:
    _quiet_logs()
    import uvicorn

    from threadroute.a2a.client import a2a_token
    from threadroute.a2a.peer import build_carrier_app
    from threadroute.config import load_env

    load_env()
    token = a2a_token()
    print(f"Carrier-desk agent (A2A) on http://{args.host}:{args.port}\n"
          f"  Agent Card: http://{args.host}:{args.port}/.well-known/agent-card.json\n"
          f"  To let ThreadRoute use it, start the web app with:\n"
          f"    THREADROUTE_A2A_URL=http://{args.host}:{args.port}  THREADROUTE_A2A_TOKEN=<same token>\n"
          f"  (token is taken from THREADROUTE_A2A_TOKEN; this run's token: {token})")
    uvicorn.run(build_carrier_app(None, token=token, host=args.host, port=args.port), host=args.host,
                port=args.port, log_level="warning")
    return 0


def cmd_a2a_serve(args) -> int:
    _quiet_logs()
    import uvicorn

    from threadroute.a2a.client import a2a_token
    from threadroute.a2a.peer import build_threadroute_app
    from threadroute.config import load_env

    load_env()
    token = a2a_token()
    print(f"ThreadRoute as an A2A server on http://{args.host}:{args.port}\n"
          f"  Agent Card: http://{args.host}:{args.port}/.well-known/agent-card.json\n"
          f"  Auth: Authorization: Bearer <THREADROUTE_A2A_TOKEN>  (this run's token: {token})")
    uvicorn.run(build_threadroute_app(None, token=token, host=args.host, port=args.port), host=args.host,
                port=args.port, log_level="warning")
    return 0


def cmd_eval(args) -> int:
    _quiet_logs()
    from evals.run_evals import main as run

    return run(args.scenario)


def cmd_demo(_args) -> int:
    _quiet_logs()
    from threadroute.demo import run_demo

    return asyncio.run(run_demo())


# ---------------------------------------------------------------- entry point
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m threadroute", description="ThreadRoute logistics control tower")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("seed", help="create/reset the mock WMS database")
    s.add_argument("--reset", action="store_true")
    s.set_defaults(fn=cmd_seed)
    sub.add_parser("doctor", help="check installation, credentials and models").set_defaults(fn=cmd_doctor)
    c = sub.add_parser("chat", help="terminal chat with the agents")
    c.add_argument("--quiet", action="store_true", help="hide agent/tool trace")
    c.set_defaults(fn=cmd_chat)
    w = sub.add_parser("web", help="start the web UI")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    w.add_argument("--carrier-agent-port", type=int, default=8101,
                   help="run the carrier-desk A2A agent on this port (0 = use the in-process peer only)")
    w.set_defaults(fn=cmd_web)
    ac = sub.add_parser("a2a-carrier", help="run the carrier-desk agent as its own A2A server")
    ac.add_argument("--host", default="127.0.0.1")
    ac.add_argument("--port", type=int, default=8101)
    ac.set_defaults(fn=cmd_a2a_carrier)
    asv = sub.add_parser("a2a-serve", help="expose ThreadRoute's orchestrator as an A2A server for other agents")
    asv.add_argument("--host", default="127.0.0.1")
    asv.add_argument("--port", type=int, default=8102)
    asv.set_defaults(fn=cmd_a2a_serve)
    sub.add_parser("mcp", help="run the WMS MCP server on stdio").set_defaults(fn=cmd_mcp)
    sub.add_parser("demo", help="offline end-to-end demo (no API key needed)").set_defaults(fn=cmd_demo)
    ch = sub.add_parser("chaos", help="inject failures to watch self-healing")
    ch.add_argument("action", choices=["set", "clear", "list"])
    ch.add_argument("target", nargs="?", help="component:provider, e.g. carrier:primary_api")
    ch.add_argument("mode", nargs="?", help="fail|timeout|slow|malformed|flaky")
    ch.add_argument("--count", type=int, default=None)
    ch.add_argument("--delay", type=float, default=0.0)
    ch.set_defaults(fn=cmd_chaos)
    sub.add_parser("watchdog", help="diagnose and heal now").set_defaults(fn=cmd_watchdog)
    r = sub.add_parser("report", help="feedback + self-healing report")
    r.add_argument("--out")
    r.set_defaults(fn=cmd_report)
    e = sub.add_parser("eval", help="run live conversation evals against Gemini")
    e.add_argument("--scenario", help="run a single scenario id")
    e.set_defaults(fn=cmd_eval)
    sub.add_parser("google-auth", help="one-time OAuth for live Gmail/Sheets").set_defaults(fn=cmd_google_auth)
    return p


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles may default to a legacy code page
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    load_env()
    args = build_parser().parse_args(argv)
    sys.exit(args.fn(args))
