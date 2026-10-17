"""Deterministic grounding / truthfulness / hallucination checker.

Definitions used throughout the project:
  GROUNDEDNESS   every factual claim in the answer is supported by what the tools returned in this turn
                 (or by what the user said). Score = supported claims / checkable claims.
  TRUTHFULNESS   claims are consistent with the database (the ground truth) - checked even if the model got
                 the data from somewhere else; a contradiction is worse than an unsupported claim.
  HALLUCINATION  the answer invents things that exist nowhere: ids that are not in the database, quantities with
                 no source, or actions claimed as done that never happened.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from itertools import combinations

from threadroute.evaluation import claims as cl
from threadroute.evaluation.oracle import Catalog, shipment_facts
from threadroute.guardrails import pii

HIGH, MEDIUM, LOW = "high", "medium", "low"


@dataclass
class Issue:
    kind: str       # fabricated_entity | unsupported_entity | unsupported_number | unsupported_date |
                    # false_action_claim | contradiction | pii_leak
    severity: str
    detail: str
    text: str = ""  # the offending fragment (used by the repair step)


@dataclass
class Verification:
    grounded: bool
    issues: list[Issue] = field(default_factory=list)
    checked_claims: int = 0
    supported_claims: int = 0
    retried: bool = False

    @property
    def groundedness(self) -> float:
        return 1.0 if self.checked_claims == 0 else round(self.supported_claims / self.checked_claims, 3)

    @property
    def hallucinated(self) -> bool:
        return any(i.kind in ("fabricated_entity", "false_action_claim") or
                   (i.kind == "unsupported_number" and i.severity == HIGH) for i in self.issues)

    @property
    def contradicted(self) -> bool:
        return any(i.kind == "contradiction" for i in self.issues)

    def to_dict(self) -> dict:
        return {"grounded": self.grounded, "groundedness": self.groundedness, "hallucinated": self.hallucinated,
                "contradicted": self.contradicted, "retried": self.retried,
                "checked_claims": self.checked_claims, "supported_claims": self.supported_claims,
                "issues": [f"{i.kind}: {i.detail}" for i in self.issues], "issue_details": [asdict(i) for i in self.issues]}


def _flatten(obj) -> str:
    try:
        return json.dumps(obj, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(obj)


def _evidence_numbers(text: str) -> set[float]:
    vals: set[float] = set()
    for m in re.finditer(r"(?<![\w\-])-?\d[\d,]*(?:\.\d+)?", text):
        try:
            vals.add(float(m.group(0).replace(",", "")))
        except ValueError:
            pass
    return vals


def _evidence_dates(text: str) -> list[date]:
    out = []
    for m in cl.ISO_DATE.finditer(text):
        try:
            out.append(date.fromisoformat(m.group(0)))
        except ValueError:
            pass
    return out


def _derived(values: set[float], limit: int = 220) -> set[float]:
    """Sums, differences, products and ratios of pairs of evidence numbers (the model may legitimately compute)."""
    base = sorted(v for v in values if abs(v) >= 1)[:limit]
    out: set[float] = set()
    for a, b in combinations(base, 2):
        out.update((a + b, abs(a - b), a * b))
        if b:
            out.add(a / b)
        if a:
            out.add(b / a)
    return out


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.51, 0.006 * abs(b))


def _supported(value: float, direct: set[float], derived: set[float]) -> str | None:
    if any(_close(value, v) for v in direct):
        return "direct"
    if any(_close(value, v) for v in derived):
        return "derived"
    return None


def check(reply: str, evidence: list[dict], user_text: str = "", session_actions: list[dict] | None = None,
          catalog: Catalog | None = None, today: date | None = None) -> Verification:
    """Verify `reply` against `evidence` (tool results this turn), `user_text`, and the database."""
    catalog = catalog or Catalog()
    today = today or date.today()
    session_actions = session_actions or []
    ev_text = _flatten(evidence)
    base_text = ev_text + " " + (user_text or "")
    issues: list[Issue] = []
    checked = supported = 0

    # ---- 1. entities (ids) ------------------------------------------------------------------------------------
    action_ids = {a["id"] for a in session_actions}
    for kind, ids in cl.entities(reply).items():
        for ident in sorted(ids):
            if kind == "action" and ident in action_ids:
                continue
            checked += 1
            if ident in base_text or ident in action_ids:
                supported += 1
            elif catalog.exists(kind, ident):
                issues.append(Issue("unsupported_entity", LOW,
                                    f"{kind} {ident} exists but no tool returned it this turn", ident))
            else:
                issues.append(Issue("fabricated_entity", HIGH, f"{kind} {ident} does not exist in the system", ident))

    # ---- 2. numbers -------------------------------------------------------------------------------------------
    direct = _evidence_numbers(base_text)
    ev_dates = _evidence_dates(base_text)
    derived = _derived(direct)
    for a, b in combinations(sorted(set(ev_dates))[:12], 2):  # day differences ("4 days late")
        derived.add(abs((a - b).days))
    for d in ev_dates:
        derived.add(abs((d - today).days))
    for claim in cl.numbers(reply):
        checked += 1
        how = _supported(claim.value, direct, derived)
        if how is None and claim.kind == "money":  # money may be quoted in lakh/crore or rounded
            how = _supported(claim.value, {v for v in direct}, set())
        if how:
            supported += 1
        else:
            sev = HIGH if claim.kind in ("unit", "money") and claim.value > 12 else MEDIUM
            issues.append(Issue("unsupported_number", sev, f"'{claim.text}' appears in no tool result", claim.text))

    # ---- 3. dates ---------------------------------------------------------------------------------------------
    known_md = cl.dates(base_text) | {((today + timedelta(days=n)).month, (today + timedelta(days=n)).day)
                                      for n in range(-1, 2)}
    for md in sorted(cl.dates(reply)):
        checked += 1
        if md in known_md:
            supported += 1
        else:
            issues.append(Issue("unsupported_date", MEDIUM, f"date {md[1]}/{md[0]} appears in no tool result",
                                f"{md[1]}/{md[0]}"))

    # ---- 4. actions claimed as done --------------------------------------------------------------------------
    executed = [a for a in session_actions if a.get("status") == "executed"]
    for sentence in cl.action_claims(reply):
        checked += 1
        if executed:
            supported += 1
        else:
            issues.append(Issue("false_action_claim", HIGH,
                                "claims something was done, but agents only propose and nothing was approved",
                                sentence))

    # ---- 5. truthfulness vs the database (contradictions) ---------------------------------------------------
    issues += _contradictions(reply, catalog)

    # ---- 6. personal data in the answer ----------------------------------------------------------------------
    for f in pii.scan(reply):
        issues.append(Issue("pii_leak", HIGH, f"{f.kind} present in the answer", f.text))

    grounded = not any(i.severity == HIGH or i.kind == "contradiction" for i in issues)
    return Verification(grounded=grounded, issues=issues, checked_claims=checked, supported_claims=supported)


_STATUS_WORDS = {"delivered": ["delivered"], "delayed": ["delayed", "running late", "behind schedule"],
                 "held": ["held", "on hold"], "cancelled": ["cancelled", "canceled"],
                 "in_transit": ["in transit", "on its way", "on schedule", "on time"],
                 "pending_pickup": ["awaiting pickup", "pending pickup", "not yet picked up"]}


def _contradictions(reply: str, catalog: Catalog) -> list[Issue]:
    out: list[Issue] = []
    for sid in sorted(cl.entities(reply).get("shipment", set())):
        facts = shipment_facts(sid)
        if not facts:
            continue
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", reply):
            if sid not in sentence:
                continue
            low = sentence.lower()
            tail = low[low.index(sid.lower()) + len(sid):][:90]
            asserted = [st for st, words in _STATUS_WORDS.items()
                        if any(re.search(rf"\b(?:is|was|has been|are|now|status[: ]+)\W*(?:\w+\W+){{0,2}}{re.escape(w)}", tail)
                               for w in words)]
            true_status = facts["status"]
            if asserted and true_status not in asserted:
                out.append(Issue("contradiction", HIGH,
                                 f"{sid} is '{true_status}' in the system but the answer says '{asserted[0]}'",
                                 sentence.strip()[:160]))
            m = re.search(r"(?:carrier is|carried by|shipped (?:with|by)|moving with)\s+([A-Z][A-Za-z ]{3,25}?)(?:[,.;]|\s+(?:and|from|to|via)\b|$)", sentence)
            if m and m.group(1).strip() in catalog.carriers and m.group(1).strip() != facts["carrier"]:
                out.append(Issue("contradiction", HIGH, f"{sid} is carried by {facts['carrier']}, not {m.group(1).strip()}",
                                 sentence.strip()[:160]))
            for um in re.finditer(r"([\d,]+)\s+units", sentence):
                n = int(um.group(1).replace(",", ""))
                allowed = {facts["total_units"]} | {i["qty"] for i in facts["items"]}
                if n not in allowed and re.search(r"\b(?:contains?|carries|carrying|holds?|shipment of|consists? of|has)\b",
                                                  sentence, re.IGNORECASE) and n > 12:
                    out.append(Issue("contradiction", HIGH,
                                     f"{sid} carries {facts['total_units']} units, answer says {n}", um.group(0)))
    return out


# ------------------------------------------------------------------ repair
def repair(reply: str, issues: list[Issue]) -> str:
    """Drop the sentences containing unverifiable high-severity claims; never show them to the user."""
    bad = [i.text for i in issues if (i.severity == HIGH or i.kind == "contradiction") and i.text]
    if not bad:
        return reply
    keep, removed = [], 0
    for sentence in re.split(r"(?<=[.!?])\s+|(?<=\n)", reply):
        if any(b and b in sentence for b in bad):
            removed += 1
        else:
            keep.append(sentence)
    text = " ".join(s.strip("\n") for s in keep).strip()
    note = ("\n\n(I removed " + ("a detail" if removed == 1 else f"{removed} details") +
            " I could not verify against our systems.)")
    return (text or "I can't confirm that from our systems.") + note
