# ThreadRoute – Security & Responsible-AI notes

This document says what the system protects, how, where the code lives, and – just as important – what it does **not** protect against. Every control below has a test in `tests/` unless marked *(not tested)*.

## 1. Assets and trust boundaries

| Asset | Where it lives | Who may touch it |
|---|---|---|
| Customer personal data (name, phone, e-mail, address) | `return_contacts` table of the mock WMS (synthetic data only) | Never leaves the process unredacted: not to the model, the UI, logs, e-mail, sheets, other agents |
| API keys / OAuth tokens | `.env`, `~/.threadroute` | Never logged, never sent to the model; secret-shaped strings in any text are redacted |
| Warehouse state (stock, shipments) | SQLite WMS | Agents have **read-only** MCP tools; writes only through the human approval gate |
| The approval gate | `approvals/` | Humans only; no agent tool can approve, reject or execute |

Trust boundaries: browser → web app; web app → agents; agents → model (Gemini); agents → tools / MCP server; agents ↔ carrier-desk agent (A2A); app → Gmail / Sheets / carrier APIs. **Everything coming back across a boundary (model output, tool results, peer-agent replies, database free-text fields, uploaded images) is treated as untrusted data.**

## 2. Threat model mapped to the OWASP Top 10 for LLM Applications (2025)

| # | Risk | Control in ThreadRoute | Code | Test |
|---|---|---|---|---|
| LLM01 | Prompt injection (direct, indirect via data, via peer agent) | Untrusted text is scanned and neutralised before the model sees it (`security_notice` added); a standing system rule says data is never instructions; the agent cannot take any real action without a human approval; e-mail recipients must be on an allow-listed domain; injected fixtures (`RT-9003` note, `SH-1016` hold reason) are part of the golden evaluation | `guardrails/injection.py`, `agents/callbacks.py::after_tool`, `agents/prompts.py` | `test_guardrails.py`, `test_a2a.py`, golden cases `i_*` |
| LLM02 | Sensitive-information disclosure | Redaction (not reversible): e-mail, phone, address, PIN code, DOB, Aadhaar (Verhoeff-checked), PAN, cards (Luhn-checked), secrets, known customer names → placeholders such as `[EMAIL_1]`. Applied at input, before the model, on tool results, on model output, on A2A messages, on e-mail/sheet writes, on stored feedback, on the final reply and trace. Audit trail stores **kinds and counts only, never values** | `guardrails/pii.py`, `guardrails/egress.py` | `test_guardrails.py`, golden cases `p_*` |
| LLM03 | Supply chain | Dependency ranges pinned to major versions in `requirements.txt`; CI runs `pip-audit` and a secret scan (`docs/ci/`); container runs as non-root | `requirements.txt`, `Dockerfile`, `docs/ci/security.yml` | CI (not run locally) |
| LLM04 | Data / model poisoning | Learned guidance (feedback → rules) is redacted, length-limited and rejected if it looks like an instruction to ignore rules; rules are visible and removable in the UI | `feedback/store.py` | `test_feedback.py`, `test_guardrails.py` |
| LLM05 | Improper output handling | Model output is never executed or interpolated into SQL/HTML/shell; the UI renders text through an escaping Markdown subset; proposals are validated by strict Pydantic models and business rules before they can be queued | `agents/tools.py::_precheck`, `ui/static/app.js` | `test_agents.py`, `test_ui.py` |
| LLM06 | Excessive agency | Least privilege: each agent has its own MCP allow-list of read-only tools; write tools exist only on a separate server mode used by the executor; `propose_*` tools only queue; per-turn model-call cap | `agents/factory.py`, `mcp_server/server.py` | `test_agents.py::test_agents_cannot_reach_write_tools` |
| LLM07 | System-prompt leakage | Prompts contain no secrets or credentials; a golden case asks the agent to print its prompt and fails if internals appear. (Prompts are not confidential – do not put secrets in them.) | – | golden `i_prompt` |
| LLM08 | Vector / embedding weaknesses | Not applicable – no vector store or RAG | – | – |
| LLM09 | Misinformation / hallucination | Answers are checked against what the tools returned this turn **and** against the database; one corrective retry, then unverifiable sentences are removed; verification result is shown in the UI | `evaluation/`, `agents/runtime.py::_verify_and_repair` | `test_evaluation.py`, `evaluate` |
| LLM10 | Unbounded consumption | Session token budget, max model calls per turn, output-token cap, request-size limits, per-client rate limits, bounded in-memory structures | `perf/tokens.py`, `agents/callbacks.py`, `ui/security.py` | `test_perf.py`, `test_web_security.py` |

## 3. Web application hardening

* **Authentication:** a random per-run bearer token (or `THREADROUTE_AUTH_TOKEN`). `python -m threadroute web` prints a URL ending in `#token=…`; the fragment is never sent to the server, and the page moves it into `sessionStorage` and strips it from the address bar. Comparison is constant-time. `/healthz` and static assets are the only public paths.
* **Host header validation** (`TrustedHostMiddleware`): localhost by default; add deployment hosts with `THREADROUTE_ALLOWED_HOSTS`.
* **Strict CSP** (`default-src 'none'; script-src 'self'; style-src 'self'`), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, COOP/CORP, `Cache-Control: no-store` on API responses. No inline scripts or styles anywhere.
* **Input validation:** Pydantic models with `extra=forbid`, length limits, enum fields, regex-checked ids; 422 on violations. Uploads: type allow-list, 8 MB cap, then re-decoded and re-encoded server side (see §4).
* **Limits:** chat 20 requests/min, other API 240/min per client; 64 KB JSON bodies; 9 MB uploads; `Content-Length` required on POSTs.
* **No information leaks:** generic 500 messages, no stack traces, OpenAPI/Swagger disabled unless `THREADROUTE_DEBUG=1`. The mock carrier API is **not** exposed over HTTP (in-process only).
* **SQL:** every query is parameterised. The three places that interpolate identifiers (`approvals/service.py::_update`, `data/seed.py::summary`, `resilience/store.py::reset_all`) take them from code-owned allow-lists, never from input.

## 4. Images
Uploaded images are validated (Pillow decode, pixel-count cap), stripped of all metadata (EXIF/GPS), downscaled to max side 1568 px and re-encoded as JPEG before any model sees them. A file that does not decode is rejected (`invalid_image`).

## 5. Agent-to-agent (A2A) security
* The carrier-desk agent is a separate agent that can run as its own server (`python -m threadroute a2a-carrier`). Its Agent Card is public; **every other route requires a bearer token** (`THREADROUTE_A2A_TOKEN`; a random one is generated and shared in-process when not set).
* Outbound messages to the peer are redacted; inbound replies are injection-neutralised and redacted before the calling agent sees them.
* The card URL must be `https` or loopback. If the peer is unreachable or misbehaves, the call degrades to a local policy snapshot and says so (`degraded=true`) instead of failing silently.

## 6. Deployment hardening
* Docker image runs as an unprivileged user with a health check; `docker-compose.yml` uses a read-only root filesystem, `no-new-privileges`, dropped capabilities, a tmpfs for `/tmp`, a volume only for `/data`, and **refuses to start without `THREADROUTE_AUTH_TOKEN`**.
* Secrets come from the environment only; `.env` is git-ignored; `scripts/release_check.py` scans the tree for secret-shaped strings and personal-data patterns and fails if `.env` is present.

## 7. What is NOT covered (be honest with reviewers)
* **TLS and public exposure.** The app speaks plain HTTP. For anything beyond localhost put it behind a TLS-terminating proxy; the token travels in a header.
* **Single shared token, no user accounts.** There is no per-user identity, RBAC or audit-by-user. Approvals are attributed to "ops".
* **Rate limiting is in-process** (per instance); behind several replicas use a gateway limiter.
* **PII detection is pattern- and list-based.** Free-text names that are not in the known-contacts list are only caught if they look like names next to a trigger phrase; unusual formats can slip through. For production add Cloud DLP / a trained detector *(optional hook, not implemented)*.
* **Redaction is lossy by design**, so an agent can never quote a customer's contact details back – that is the intended behaviour, but it also means legitimate workflows that need the real e-mail must be done by a human in the source system.
* **The grounding checker is deterministic and conservative.** It can miss a subtle falsehood that reuses a valid id or number, and it can flag a correct but unusually derived figure. Use `evaluate --judge` for a second opinion.
* **Prompt-injection defence is layered, not absolute.** Neutralisation is heuristic; the hard guarantees come from the architecture (read-only tools, human approval, recipient allow-list), not from the model behaving.
* **Gemini, Gmail, Sheets, Maps behaviour** was not exercised in the build environment (no credentials). Live paths are covered by unit tests with mocks only.
* Dependencies are version-ranged, not hash-locked.

## 8. Reporting
This is a hackathon prototype; report issues to the repository owner.
