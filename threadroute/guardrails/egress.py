"""The single choke point every outbound channel goes through.

Destinations: user_reply, email, sheets, a2a_out, a2a_in, llm_in, store (DB writes), log.
Everything is redacted (PII + secrets). Email recipients must also be on the business allow-list.
"""

from __future__ import annotations

from typing import Any

from threadroute.guardrails import audit, injection, pii


def protect(destination: str, obj: Any, vault: pii.Vault | None = None, sanitize_untrusted: bool = False) -> Any:
    """Return `obj` with all PII/secrets redacted (and instruction-like text neutralised if untrusted)."""
    counts: dict[str, int] = {}
    clean = pii.redact_obj(obj, vault, counts)
    for kind, n in counts.items():
        audit.record(destination, kind, n, "redacted")
    if sanitize_untrusted:
        clean, removed = injection.neutralize_obj(clean)
        audit.record(destination, "PROMPT_INJECTION", removed, "neutralised")
    return clean


def protect_text(destination: str, text: str, vault: pii.Vault | None = None) -> str:
    return protect(destination, text or "", vault)


class RecipientBlocked(ValueError):
    pass


def check_recipient(address: str) -> str:
    """Only business addresses on the allow-list may receive email (stops data exfiltration to strangers)."""
    addr = (address or "").strip()
    if not pii.is_allowed_business_email(addr):
        audit.record("email", "RECIPIENT", 1, "blocked")
        raise RecipientBlocked(
            f"Recipient domain is not on the allow-list (THREADROUTE_ALLOWED_EMAIL_DOMAINS). "
            f"Allowed domains: {', '.join(sorted(pii.allowed_email_domains())) or 'none'}.")
    return addr
