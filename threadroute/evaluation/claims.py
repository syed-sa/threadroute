"""Extract the checkable claims from an answer: entity ids, numbers with units, money, dates, action claims."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from threadroute.evaluation.oracle import ID_PATTERNS

_NUM = r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?"
UNIT_NUMBER = re.compile(
    rf"(?P<num>{_NUM})\s*(?P<unit>units?|pcs|pieces|shirts|trousers|jackets|blazers|days?|hours?|hrs?|km|kg|%|percent|"
    rf"weeks?|cartons?|boxes)\b", re.IGNORECASE)
MONEY = re.compile(rf"(?:₹|INR|Rs\.?)\s*(?P<num>{_NUM})(?:\s*(?P<mult>lakhs?|crores?|k)\b)?|"
                   rf"(?P<num2>{_NUM})\s*(?P<mult2>lakhs?|crores?)?\s*INR\b", re.IGNORECASE)
BARE_BIG = re.compile(rf"(?<![\w\-./:])(?P<num>{_NUM})(?![\w\-./:])")
ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
TEXT_DATE = re.compile(r"\b(?:(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|"
                       r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?)\b", re.IGNORECASE)

ACTION_DONE = re.compile(
    r"\b(?:i(?:'ve| have)|we(?:'ve| have)|i|we)\s+(?:just\s+|already\s+|successfully\s+)?"
    r"(approved|executed|completed|processed|sent|filed|rerouted|re-routed|transferred|booked|moved|shipped|"
    r"updated|cancelled|graded|refunded)\b|"
    r"\b(?:has|have)\s+(?:now\s+|already\s+|successfully\s+)?been\s+(approved|executed|completed|processed|sent|filed|"
    r"rerouted|re-routed|transferred|booked|moved|shipped|updated|cancelled|graded|refunded)\b|"
    r"\b(?:action|transfer|claim|reroute|email)\s+\S+\s+(?:is|was)\s+(?:now\s+)?(approved|executed|completed|done)\b",
    re.IGNORECASE)
PENDING_OK = re.compile(r"await|pending|queued|proposal|proposed|needs your approval|for approval|once approved|"
                        r"after approval|if approved|will be", re.IGNORECASE)


@dataclass(frozen=True)
class NumberClaim:
    value: float
    text: str
    kind: str  # unit | money | bare


def _to_float(s: str) -> float:
    return float(s.replace(",", ""))


def _mult(m: str | None) -> float:
    if not m:
        return 1.0
    m = m.lower()
    return {"k": 1e3, "lakh": 1e5, "lakhs": 1e5, "crore": 1e7, "crores": 1e7}.get(m, 1.0)


def entities(text: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    consumed: list[tuple[int, int]] = []
    for kind in ("shipment", "po", "return", "claim", "order", "action", "transfer", "sku", "location"):
        for m in ID_PATTERNS[kind].finditer(text):
            if kind == "location" and any(s <= m.start() < e for s, e in consumed):
                continue  # e.g. 'SH-OXF-WHT' must not also count as something else
            out.setdefault(kind, set()).add(m.group(0))
            consumed.append((m.start(), m.end()))
    # a SKU like TR-CHN-KHK can look like a location pattern only for DC-/ST- prefixes, so no overlap remains
    return out


def numbers(text: str) -> list[NumberClaim]:
    """Numbers that make a factual claim: with units, money, or bare numbers > 12 (ids/dates/list markers excluded)."""
    claims: list[NumberClaim] = []
    taken: list[tuple[int, int]] = []

    def free(a: int, b: int) -> bool:
        return all(b <= s or a >= e for s, e in taken)

    masked = text
    for pat in ID_PATTERNS.values():  # blank out ids so their digits are not read as quantities
        masked = pat.sub(lambda m: " " * len(m.group(0)), masked)
    masked = ISO_DATE.sub(lambda m: " " * len(m.group(0)), masked)
    masked = re.sub(r"(?m)^\s*\d+[.)]\s", lambda m: " " * len(m.group(0)), masked)  # '1. ' list markers
    for m in MONEY.finditer(masked):
        num = m.group("num") or m.group("num2")
        mult = m.group("mult") or m.group("mult2")
        claims.append(NumberClaim(_to_float(num) * _mult(mult), m.group(0).strip(), "money"))
        taken.append((m.start(), m.end()))
    for m in UNIT_NUMBER.finditer(masked):
        if free(m.start(), m.end()):
            claims.append(NumberClaim(_to_float(m.group("num")), m.group(0), "unit"))
            taken.append((m.start(), m.end()))
    for m in BARE_BIG.finditer(masked):
        v = _to_float(m.group("num"))
        if free(m.start(), m.end()) and v > 12 and not (1990 <= v <= 2100 and float(v).is_integer()):
            claims.append(NumberClaim(v, m.group(0), "bare"))
    return claims


def dates(text: str, today: date | None = None) -> set[tuple[int, int]]:
    """Dates mentioned, as (month, day) so 'Oct 20' and 2026-10-20 compare equal."""
    found: set[tuple[int, int]] = set()
    for m in ISO_DATE.finditer(text):
        try:
            d = date.fromisoformat(m.group(0))
            found.add((d.month, d.day))
        except ValueError:
            pass
    for m in TEXT_DATE.finditer(text):
        day = m.group(1) or m.group(4)
        mon = (m.group(2) or m.group(3))[:3].lower()
        found.add((MONTHS[mon], int(day)))
    return found


def action_claims(reply: str) -> list[str]:
    """Sentences asserting that something was actually done (agents can only *propose*)."""
    hits = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", reply):
        if ACTION_DONE.search(sentence) and not PENDING_OK.search(sentence):
            hits.append(sentence.strip()[:160])
    return hits
