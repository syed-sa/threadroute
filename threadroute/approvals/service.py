"""Pending-action lifecycle: pending -> (approved ->) executed | failed, or rejected.

Only a person (via the CLI, the web UI buttons or the REST API) can approve - the LLM has no tool
that approves, so it cannot bypass the gate."""

from __future__ import annotations

from pydantic import ValidationError

from threadroute import db
from threadroute.approvals import executor
from threadroute.approvals.models import PAYLOAD_MODELS
from threadroute.feedback import store as fb
from threadroute.resilience.store import app_conn

SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_actions (
  id TEXT PRIMARY KEY, created_at TEXT, session_id TEXT, agent TEXT, kind TEXT, payload_json TEXT,
  summary TEXT, rationale TEXT, guidance_ids_json TEXT, status TEXT, decided_at TEXT,
  decision_comment TEXT, result_json TEXT
);
"""


def _conn():
    conn = app_conn()
    conn.executescript(SCHEMA)
    return conn


def _view(row) -> dict:
    d = dict(row)
    d["payload"] = db.loads(d.pop("payload_json"), {})
    d["guidance_ids"] = db.loads(d.pop("guidance_ids_json"), [])
    d["result"] = db.loads(d.pop("result_json"), None)
    return d


def create(session_id: str, agent: str, kind: str, payload: dict, summary: str, rationale: str = "",
           guidance_ids: list[int] | None = None) -> dict:
    conn = _conn()
    try:
        n = conn.execute("SELECT COUNT(*) FROM pending_actions").fetchone()[0]
        action_id = f"ACT-{1001 + n}"
        conn.execute("INSERT INTO pending_actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (action_id, db.now_iso(), session_id, agent, kind, db.dumps(payload), summary, rationale,
                      db.dumps(guidance_ids or []), "pending", None, None, None))
        conn.commit()
    finally:
        conn.close()
    fb.mark_applied(guidance_ids or [])
    return get(action_id)


def get(action_id: str) -> dict | None:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM pending_actions WHERE id=?", (action_id.upper(),)).fetchone()
        return _view(row) if row else None
    finally:
        conn.close()


def list_actions(session_id: str | None = None, status: str | None = None, limit: int = 50) -> list[dict]:
    sql, params = "SELECT * FROM pending_actions WHERE 1=1", []
    if session_id:
        sql += " AND session_id=?"; params.append(session_id)
    if status:
        sql += " AND status=?"; params.append(status)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"; params.append(limit)
    conn = _conn()
    try:
        return [_view(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _update(action_id: str, **fields) -> None:
    conn = _conn()
    try:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE pending_actions SET {sets} WHERE id=?", (*fields.values(), action_id))
        conn.commit()
    finally:
        conn.close()


async def approve(action_id: str, comment: str | None = None, edits: dict | None = None) -> dict:
    action = get(action_id)
    if not action:
        return {"ok": False, "error": f"No action '{action_id}'."}
    if action["status"] != "pending":
        return {"ok": False, "error": f"{action['id']} is already {action['status']}."}
    payload = dict(action["payload"])
    if edits:
        payload.update({k: v for k, v in edits.items() if k in payload or k in PAYLOAD_MODELS[action["kind"]].model_fields})
        try:
            payload = PAYLOAD_MODELS[action["kind"]].model_validate(payload).model_dump()
        except ValidationError as exc:
            return {"ok": False, "error": "Edited values are invalid: "
                    + "; ".join(f"{e['loc'][0]}: {e['msg']}" for e in exc.errors())}
    scenario = fb.KIND_TO_SCENARIO.get(action["kind"], "global")
    outcome = await executor.execute(action["kind"], payload)
    _update(action["id"], status=outcome["status"], decided_at=db.now_iso(), decision_comment=comment,
            payload_json=db.dumps(payload), result_json=db.dumps(outcome))
    diff = {k: {"from": action["payload"].get(k), "to": v} for k, v in payload.items()
            if action["payload"].get(k) != v}
    fid = fb.record_feedback("decision", +1, comment, action["session_id"], "action", action["id"],
                             action["agent"], scenario, diff or None)
    fb.record_outcome(action["guidance_ids"], True)
    learned = (fb.learn_guidance(comment, scenario, fid)
               if comment and (diff or fb.looks_directive(comment)) else None)
    return {"ok": True, "action": get(action["id"]), "outcome": outcome, "learned": learned}


async def reject(action_id: str, reason: str) -> dict:
    action = get(action_id)
    if not action:
        return {"ok": False, "error": f"No action '{action_id}'."}
    if action["status"] != "pending":
        return {"ok": False, "error": f"{action['id']} is already {action['status']}."}
    scenario = fb.KIND_TO_SCENARIO.get(action["kind"], "global")
    _update(action["id"], status="rejected", decided_at=db.now_iso(), decision_comment=reason)
    fid = fb.record_feedback("decision", -1, reason, action["session_id"], "action", action["id"],
                             action["agent"], scenario)
    fb.record_outcome(action["guidance_ids"], False)
    learned = fb.learn_guidance(reason, scenario, fid) if reason else None
    return {"ok": True, "action": get(action["id"]), "learned": learned}
