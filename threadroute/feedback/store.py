"""Feedback + learned-guidance store.

How the loop works
  1. A person approves / rejects / edits an agent proposal, gives a thumbs up/down, or types /teach.
  2. The signal is saved in `feedback`. Any free-text reason becomes (or reinforces) a `guidance` rule.
  3. Before each agent turn, the most relevant active rules are retrieved and added to the agent's
     instructions ("Learned operating guidance from your ops managers ...").
  4. When a proposal that used a rule is approved the rule gains weight; when it is rejected anyway
     the rule loses weight and is retired below a threshold. So the rule set self-corrects.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from threadroute import db
from threadroute.guardrails import audit, egress, injection
from threadroute.resilience.store import app_conn

SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, session_id TEXT, kind TEXT, target_type TEXT,
  target_id TEXT, agent TEXT, scenario TEXT, rating INTEGER, comment TEXT, edit_json TEXT
);
CREATE TABLE IF NOT EXISTS guidance (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, updated_at TEXT, scenario TEXT, tags TEXT,
  text TEXT, weight REAL, uses INTEGER DEFAULT 0, helpful INTEGER DEFAULT 0, unhelpful INTEGER DEFAULT 0,
  active INTEGER DEFAULT 1, source_feedback_id INTEGER
);
"""

KIND_TO_SCENARIO = {
    "reallocate_stock": "allocation", "file_claim": "claims", "reroute_shipment": "exceptions",
    "set_return_disposition": "returns", "send_email": "comms",
}
AGENT_SCENARIOS = {
    "orchestrator": ["global"], "intake_agent": ["claims", "returns"], "tracking_agent": ["tracking", "exceptions"],
    "allocation_agent": ["allocation"], "exceptions_claims_agent": ["exceptions", "claims"],
    "returns_agent": ["returns"], "comms_agent": ["comms"], "watchdog_agent": ["watchdog"],
}

_STOP = set("the and for with that this from have has had not are was were will would should could into over "
            "than then them they their there when while which what your you our out per via use using more most "
            "any all can may also but only just because always never".split())
_VOCAB = {
    "swiftship", "roadrunner", "airlink", "road", "rail", "air", "monsoon", "rain", "flood", "festive", "wedding",
    "shirt", "shirts", "trouser", "trousers", "chinos", "denim", "jeans", "blazer", "blazers", "suit", "suits",
    "jacket", "jackets", "polo", "hanging", "folded", "delhi", "mumbai", "bengaluru", "hyderabad", "chennai",
    "kolkata", "pune", "north", "south", "east", "west", "claim", "claims", "return", "returns", "stain",
    "liquidate", "restock", "refurbish", "reallocation", "transfer", "carrier", "email",
}


def ensure() -> None:
    conn = app_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _conn():
    ensure()
    return app_conn()


def tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", (text or "").lower()) if w not in _STOP}


def extract_tags(text: str) -> set[str]:
    return {w for w in tokens(text) if w in _VOCAB}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


_DIRECTIVE = re.compile(r"\b(always|never|prefer|avoid|instead|must|should|shouldn't|don't|do not|only|first|"
                        r"rather|not\s+from|use\s+\w+\s+first)\b", re.I)


def looks_directive(text: str | None) -> bool:
    """True if a comment reads like a standing instruction rather than praise ('good call')."""
    return bool(text and len(text) >= 12 and _DIRECTIVE.search(text))


def record_feedback(kind: str, rating: int, comment: str | None = None, session_id: str | None = None,
                    target_type: str | None = None, target_id: str | None = None, agent: str | None = None,
                    scenario: str | None = None, edit: dict | None = None) -> int:
    comment = egress.protect("store", comment) if comment else comment
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO feedback (ts, session_id, kind, target_type, target_id, agent, scenario, rating, comment, edit_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (db.now_iso(), session_id, kind, target_type, target_id, agent, scenario, rating, comment,
             db.dumps(edit) if edit else None))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def learn_guidance(text: str, scenario: str = "global", source_feedback_id: int | None = None,
                   extra_tags: set[str] | None = None) -> dict:
    """Create a rule, or reinforce a near-duplicate one."""
    text = " ".join(egress.protect("store", text or "").split())
    if len(text) < 8:
        return {"created": False, "reason": "too short"}
    if injection.find_injections(text):  # a rule is injected into prompts later: never let it carry instructions
        audit.record("guidance", "PROMPT_INJECTION", 1, "rejected")
        return {"created": False, "reason": "rule rejected: it looks like an instruction to override safety rules"}
    tags = extract_tags(text) | (extra_tags or set())
    new_tokens = tokens(text)
    conn = _conn()
    try:
        for row in conn.execute("SELECT * FROM guidance WHERE active=1").fetchall():
            if _jaccard(new_tokens, tokens(row["text"])) >= 0.55:
                weight = min(5.0, row["weight"] + 1.0)
                conn.execute("UPDATE guidance SET weight=?, updated_at=? WHERE id=?", (weight, db.now_iso(), row["id"]))
                conn.commit()
                return {"created": False, "reinforced": True, "id": row["id"], "weight": weight}
        cur = conn.execute(
            "INSERT INTO guidance (created_at, updated_at, scenario, tags, text, weight, source_feedback_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (db.now_iso(), db.now_iso(), scenario, ",".join(sorted(tags)), text, 1.0, source_feedback_id))
        conn.commit()
        return {"created": True, "id": int(cur.lastrowid), "weight": 1.0}
    finally:
        conn.close()


def _age_factor(updated_at: str) -> float:
    try:
        age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(updated_at)).total_seconds() / 86400
    except ValueError:
        return 1.0
    return 1.0 if age_days < 30 else max(0.5, 1.0 - (age_days - 30) / 180)


def retrieve(scenarios: list[str] | None = None, context_text: str = "", tags: set[str] | None = None,
             k: int = 5, min_score: float = 0.9) -> list[dict]:
    scenarios = set(scenarios or []) | {"global"}
    ctx_tags = extract_tags(context_text) | (tags or set())
    ctx_tokens = tokens(context_text)
    conn = _conn()
    try:
        rows = db.rows(conn.execute("SELECT * FROM guidance WHERE active=1"))
    finally:
        conn.close()
    scored = []
    for g in rows:
        g_tags = {t for t in (g["tags"] or "").split(",") if t}
        score = 0.0
        if g["scenario"] in scenarios:
            score += 2.0 if g["scenario"] != "global" else 1.0
        score += 1.5 * len(g_tags & ctx_tags)
        score += 0.4 * min(5, len(tokens(g["text"]) & ctx_tokens))
        score *= g["weight"] * _age_factor(g["updated_at"])
        if score >= min_score:
            scored.append({**g, "score": round(score, 2)})
    scored.sort(key=lambda g: g["score"], reverse=True)
    return scored[:k]


def mark_applied(guidance_ids: list[int]) -> None:
    if not guidance_ids:
        return
    conn = _conn()
    try:
        conn.executemany("UPDATE guidance SET uses=uses+1 WHERE id=?", [(i,) for i in guidance_ids])
        conn.commit()
    finally:
        conn.close()


def record_outcome(guidance_ids: list[int], accepted: bool) -> None:
    """Credit or penalise the rules that were in play when a human judged a proposal."""
    if not guidance_ids:
        return
    conn = _conn()
    try:
        for gid in guidance_ids:
            row = conn.execute("SELECT * FROM guidance WHERE id=?", (gid,)).fetchone()
            if not row:
                continue
            if accepted:
                conn.execute("UPDATE guidance SET helpful=helpful+1, weight=MIN(5.0, weight+0.2), updated_at=? WHERE id=?",
                             (db.now_iso(), gid))
            else:
                weight = row["weight"] - 0.5
                conn.execute("UPDATE guidance SET unhelpful=unhelpful+1, weight=?, active=? WHERE id=?",
                             (weight, 1 if weight >= 0.3 else 0, gid))
        conn.commit()
    finally:
        conn.close()


def list_guidance(active_only: bool = True) -> list[dict]:
    conn = _conn()
    try:
        sql = "SELECT * FROM guidance" + (" WHERE active=1" if active_only else "") + " ORDER BY weight DESC, id"
        return db.rows(conn.execute(sql))
    finally:
        conn.close()


def delete_guidance(guidance_id: int) -> bool:
    conn = _conn()
    try:
        cur = conn.execute("UPDATE guidance SET active=0 WHERE id=?", (guidance_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def format_guidance_block(rules: list[dict]) -> str:
    if not rules:
        return ""
    lines = [f"- {r['text']}" for r in rules]
    return ("\n\nLEARNED OPERATING GUIDANCE (from corrections by your ops managers - follow these unless the user "
            "explicitly overrides them, and mention when a rule changed your recommendation):\n" + "\n".join(lines))
