"""PII + secret detection and redaction (local, deterministic, no network).

Detectors: email, Indian + international phone numbers, Aadhaar (Verhoeff-checked), PAN, payment cards
(Luhn-checked), dates of birth, street addresses / pincodes, person names (honorifics, labelled names and
names already known from the data store), and secrets (API keys, tokens, private keys).

Redaction replaces each value with a stable placeholder such as [EMAIL_1]. The mapping keeps only a hash of
the original in memory, so redaction is NOT reversible - there is deliberately no way to get the value back.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any

PRIORITY = ["SECRET", "CARD", "AADHAAR", "PAN", "EMAIL", "PHONE", "DOB", "ADDRESS", "PINCODE", "PERSON"]


@dataclass(frozen=True)
class Finding:
    kind: str
    start: int
    end: int
    text: str = ""


# ------------------------------------------------------------------ checksums
_D = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
      [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
      [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
      [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]
_P = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
      [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
      [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]


def verhoeff_ok(number: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(number)):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


def verhoeff_make(base11: str) -> str:
    """Append the Verhoeff check digit (used to build synthetic test Aadhaar numbers)."""
    inv = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]
    c = 0
    for i, ch in enumerate(reversed(base11)):
        c = _D[c][_P[(i + 1) % 8][int(ch)]]
    return base11 + str(inv[c])


def luhn_ok(number: str) -> bool:
    total, alt = 0, False
    for ch in reversed(number):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


# ------------------------------------------------------------------ patterns
_NAME = r"[A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{1,20}){0,2}"
PATTERNS: dict[str, re.Pattern] = {
    "SECRET": re.compile(
        r"AIza[0-9A-Za-z_\-]{35}|sk-[A-Za-z0-9_\-]{20,}|ghp_[A-Za-z0-9]{30,}|gh[ousr]_[A-Za-z0-9]{30,}|"
        r"xox[baprs]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16}|ya29\.[0-9A-Za-z_\-]{20,}|"
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
        r"(?i:(?:api[_\- ]?key|secret|token|passw(?:or)?d|bearer)\s*[:=]\s*)[A-Za-z0-9_\-./+]{10,}"),
    "CARD": re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])"),
    "AADHAAR": re.compile(r"(?<!\d)[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?!\d)"),
    "PAN": re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
    "EMAIL": re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}"),
    "PHONE": re.compile(r"(?<![\d.])(?:\+\s?91[\s\-]?|0091[\s\-]?|0)?[6-9]\d{4}[\s\-]?\d{5}(?!\d)|"
                        r"(?<![\w.])\+\d{1,3}[\s\-]?\(?\d{2,4}\)?[\s\-]?\d{3,4}[\s\-]?\d{3,4}(?!\d)"),
    "DOB": re.compile(r"(?i:\b(?:dob|d\.o\.b\.?|date of birth|born on)\b)[:\s\-]*\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}"),
    "ADDRESS": re.compile(
        r"\b(?:(?i:flat|house|plot|door|h\.?\s?no\.?)\s*)?(?:no\.?\s*|#)?\d{1,4}[A-Za-z/]{0,3}\b,?\s+"
        r"(?!(?i:units?|pcs|pieces|days?|hours?|hrs?|km|kg|cartons?|boxes|shipments?|items?|sizes?)\b)"
        r"(?:[A-Za-z.']+,?\s+){0,4}?(?i:road|rd|street|lane|nagar|colony|apartments?|society|sector|block|marg|"
        r"chowk|layout|vihar|enclave)\b(?:,?\s+(?!(?i:pin|zip|postal)\b)[A-Za-z]+){0,3}"),
    "PINCODE": re.compile(r"(?i:\b(?:pin(?:\s?code)?|zip|postal code)\b)[:\s\-]*[1-9]\d{5}\b"),
    "PERSON": re.compile(rf"\b(?:Mr|Mrs|Ms|Miss|Dr|Shri|Smt|Sri|Mx)\.?\s+{_NAME}"),
}
# labelled names: "Customer name: Aarav Menon" -> only the name part is the PII
_LABEL_NAME = re.compile(
    rf"(?i:\b(?:my name is|customer(?: name)?|consignee|recipient|buyer|contact(?: person)?|attn|attention|"
    rf"returned by|ordered by|store manager|manager|name)\s*(?:is|:|-|=)\s*)({_NAME})")

_known_names: set[str] = set()


def register_known_names(names: list[str]) -> None:
    """Names already present in the data store (synthetic contacts) are always treated as PII."""
    for n in names:
        n = (n or "").strip()
        if len(n) >= 4:
            _known_names.add(n.lower())


def allowed_email_domains() -> set[str]:
    raw = os.environ.get("THREADROUTE_ALLOWED_EMAIL_DOMAINS", "")
    domains = {d.strip().lower() for d in raw.split(",") if d.strip()}
    for var, default in (("CLAIMS_EMAIL_TO", "claims@carrier-desk.example.com"),
                         ("OPS_EMAIL", "ops@aurelio-menswear.example.com")):
        addr = os.environ.get(var, default)
        if "@" in addr:
            domains.add(addr.split("@", 1)[1].strip().lower())
    return domains


def is_allowed_business_email(addr: str) -> bool:
    return "@" in addr and addr.rsplit("@", 1)[1].strip().lower() in allowed_email_domains()


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def scan(text: str) -> list[Finding]:
    """All non-overlapping findings, highest-priority detector wins an overlap."""
    if not text or not isinstance(text, str):
        return []
    taken: list[tuple[int, int]] = []
    found: list[Finding] = []

    def free(a: int, b: int) -> bool:
        return all(b <= s or a >= e for s, e in taken)

    def add(kind: str, a: int, b: int) -> None:
        if free(a, b):
            taken.append((a, b))
            found.append(Finding(kind, a, b, text[a:b]))

    for kind in PRIORITY:
        if kind == "PERSON":
            for m in _LABEL_NAME.finditer(text):
                add("PERSON", m.start(1), m.end(1))
            for name in _known_names:
                for m in re.finditer(re.escape(name), text, flags=re.IGNORECASE):
                    add("PERSON", m.start(), m.end())
        pat = PATTERNS.get(kind)
        if pat is None:
            continue
        for m in pat.finditer(text):
            val = m.group(0)
            if kind == "CARD":
                d = _digits(val)
                if not (13 <= len(d) <= 19 and luhn_ok(d)):
                    continue
            elif kind == "AADHAAR":
                d = _digits(val)
                if len(d) != 12 or not verhoeff_ok(d):
                    continue
            elif kind == "EMAIL" and is_allowed_business_email(val):
                continue
            elif kind == "PHONE" and len(_digits(val)) < 10:
                continue
            add(kind, m.start(), m.end())
    found.sort(key=lambda f: f.start)
    return found


class Vault:
    """Stable placeholder numbering per value. Stores only hashes of the originals (not reversible)."""

    def __init__(self) -> None:
        self._index: dict[tuple[str, str], int] = {}
        self._count: dict[str, int] = {}

    def token(self, kind: str, value: str) -> str:
        h = hashlib.sha256(value.strip().lower().encode()).hexdigest()[:16]
        key = (kind, h)
        if key not in self._index:
            self._count[kind] = self._count.get(kind, 0) + 1
            self._index[key] = self._count[kind]
        return f"[{kind}_{self._index[key]}]"


_default_vault = Vault()


def redact(text: str, vault: Vault | None = None) -> tuple[str, list[Finding]]:
    findings = scan(text)
    if not findings:
        return text, []
    v = vault or _default_vault
    out, pos = [], 0
    for f in findings:
        out.append(text[pos:f.start])
        out.append(v.token(f.kind, f.text))
        pos = f.end
    out.append(text[pos:])
    return "".join(out), findings


def redact_obj(obj: Any, vault: Vault | None = None, counts: dict[str, int] | None = None) -> Any:
    """Redact every string inside nested dict/list/tuple structures; `counts` accumulates findings by kind."""
    if isinstance(obj, str):
        clean, found = redact(obj, vault)
        if counts is not None:
            for f in found:
                counts[f.kind] = counts.get(f.kind, 0) + 1
        return clean
    if isinstance(obj, dict):
        return {k: redact_obj(v, vault, counts) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v, vault, counts) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact_obj(v, vault, counts) for v in obj)
    return obj


def contains_pii(obj: Any) -> bool:
    if isinstance(obj, str):
        return bool(scan(obj))
    if isinstance(obj, dict):
        return any(contains_pii(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(contains_pii(v) for v in obj)
    return False
