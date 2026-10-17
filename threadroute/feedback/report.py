"""Feedback + self-healing report (markdown and JSON)."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone

from threadroute import db
from threadroute.approvals import service as approvals
from threadroute.feedback import store
from threadroute.resilience import store as rstore


def _rate(a: int, b: int) -> str:
    return f"{100 * a / b:.0f}%" if b else "n/a"


def build_report_data() -> dict:
    store.ensure()
    actions = approvals.list_actions(limit=1000)
    decided = [a for a in actions if a["status"] in ("executed", "failed", "rejected", "approved")]
    approved = [a for a in decided if a["status"] != "rejected"]
    by_kind: dict[str, dict] = defaultdict(lambda: {"proposed": 0, "approved": 0, "rejected": 0})
    by_day: dict[str, dict] = defaultdict(lambda: {"approved": 0, "rejected": 0})
    with_g = {"n": 0, "approved": 0}
    without_g = {"n": 0, "approved": 0}
    for a in actions:
        by_kind[a["kind"]]["proposed"] += 1
        if a["status"] == "rejected":
            by_kind[a["kind"]]["rejected"] += 1
        elif a["status"] != "pending":
            by_kind[a["kind"]]["approved"] += 1
    for a in decided:
        day = (a["decided_at"] or a["created_at"])[:10]
        by_day[day]["rejected" if a["status"] == "rejected" else "approved"] += 1
        bucket = with_g if a["guidance_ids"] else without_g
        bucket["n"] += 1
        bucket["approved"] += 0 if a["status"] == "rejected" else 1

    conn = rstore.app_conn()
    try:
        store.ensure()
        thumbs = db.rows(conn.execute("SELECT rating, COUNT(*) AS n FROM feedback WHERE kind='thumb' GROUP BY rating"))
        fb_total = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    finally:
        conn.close()
    guidance = store.list_guidance(active_only=False)
    events = rstore.recent_events(minutes=60 * 24 * 30, limit=1000)
    kinds = Counter(e["kind"] for e in events)
    failing = Counter(f"{e['component']}:{e['provider']}" for e in events if e["kind"] in ("failure", "timeout", "schema"))
    remedies = [e for e in events if e["kind"] == "remedy"][:10]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "proposals": {"total": len(actions), "pending": len(actions) - len(decided), "approved": len(approved),
                      "rejected": len(decided) - len(approved), "approval_rate": _rate(len(approved), len(decided))},
        "by_kind": dict(by_kind), "by_day": dict(sorted(by_day.items())),
        "guidance_impact": {"with_guidance": with_g, "without_guidance": without_g},
        "thumbs": {("up" if t["rating"] > 0 else "down"): t["n"] for t in thumbs},
        "feedback_total": fb_total, "guidance": guidance,
        "healing": {"events_by_kind": dict(kinds), "top_failing": failing.most_common(5),
                    "recent_remedies": remedies},
    }


def build_report_markdown() -> str:
    d = build_report_data()
    p = d["proposals"]
    out = ["# ThreadRoute - feedback and self-healing report", "", f"_Generated {d['generated_at']}_", "",
           "## Human decisions on agent proposals", "",
           f"- Proposals: **{p['total']}** (pending {p['pending']}, approved {p['approved']}, rejected {p['rejected']})",
           f"- Approval rate: **{p['approval_rate']}**", "", "| Action | Proposed | Approved | Rejected |", "|---|---|---|---|"]
    for kind, v in sorted(d["by_kind"].items()):
        out.append(f"| {kind} | {v['proposed']} | {v['approved']} | {v['rejected']} |")
    if d["by_day"]:
        out += ["", "Trend by day:", "", "| Day | Approved | Rejected |", "|---|---|---|"]
        out += [f"| {day} | {v['approved']} | {v['rejected']} |" for day, v in d["by_day"].items()]
    gi = d["guidance_impact"]
    out += ["", "## Does learned guidance help?", "",
            f"- Proposals made **with** learned guidance applied: {gi['with_guidance']['n']} "
            f"(approval {_rate(gi['with_guidance']['approved'], gi['with_guidance']['n'])})",
            f"- Proposals made **without** guidance: {gi['without_guidance']['n']} "
            f"(approval {_rate(gi['without_guidance']['approved'], gi['without_guidance']['n'])})",
            "", f"Message thumbs: {d['thumbs'] or 'none yet'} - total feedback records: {d['feedback_total']}", "",
            "## Learned guidance (rules taught by your team)", ""]
    if d["guidance"]:
        out += ["| # | Active | Weight | Used | Helpful | Unhelpful | Scenario | Rule |", "|---|---|---|---|---|---|---|---|"]
        for g in d["guidance"][:15]:
            out.append(f"| {g['id']} | {'yes' if g['active'] else 'retired'} | {g['weight']:.1f} | {g['uses']} | "
                       f"{g['helpful']} | {g['unhelpful']} | {g['scenario']} | {g['text']} |")
    else:
        out.append("_No guidance learned yet. Reject a proposal with a reason, or use /teach._")
    h = d["healing"]
    out += ["", "## Self-healing activity (last 30 days)", ""]
    if h["events_by_kind"]:
        out += [f"- {k}: {v}" for k, v in sorted(h["events_by_kind"].items())]
        if h["top_failing"]:
            out += ["", "Most failing providers: " + ", ".join(f"{t} ({n})" for t, n in h["top_failing"])]
        if h["recent_remedies"]:
            out += ["", "Recent automatic remedies:"] + [f"- {r['ts']}: {r['component']}/{r['provider']} - {r['detail']}"
                                                          for r in h["recent_remedies"]]
    else:
        out.append("_No healing events recorded yet._")
    return "\n".join(out) + "\n"
