# ThreadRoute — multi-agent logistics control tower for men's apparel

ThreadRoute is a team of Gemini-powered agents (built with the **Google Agent Development Kit**) that
handles day-to-day logistics for a men's-apparel brand: delayed inbound shipments, store stock-outs,
damaged cartons, receiving shortfalls and returned garments. It talks to a warehouse system over **MCP**,
calls carrier / maps / weather / Gmail / Sheets **APIs**, reads **photos and packing lists**, learns from
human **feedback**, and **heals itself** when a dependency breaks.

Agents never change inventory on their own. They *propose* actions; a human approves them in the
web UI (or terminal), and only then are changes executed.

**Round 2 additions** (all offline-testable, see sections 13-17): **Responsible-AI guardrails** (no personal data leaves
the system), **agent-to-agent (A2A)** collaboration with an independent carrier-desk agent, **agent evaluation**
(groundedness, truthfulness, hallucination - with a runtime guard that fixes or removes unsupported claims),
**security hardening** (OWASP LLM Top 10 mapped in [`SECURITY.md`](SECURITY.md)), and **latency / token-cost
optimisation** with a benchmark.

- [1. Requirement → implementation map](#1-requirement--implementation-map)
- [2. What you can ask it](#2-what-you-can-ask-it)
- [3. Architecture](#3-architecture)
- [4. Prerequisites](#4-prerequisites)
- [5. Setup](#5-setup)
- [6. Get a Gemini API key and configure `.env`](#6-get-a-gemini-api-key-and-configure-env)
- [7. Run it](#7-run-it)
- [8. Guided walkthrough](#8-guided-walkthrough)
- [9. Feedback loop](#9-feedback-loop)
- [10. Self-healing drill](#10-self-healing-drill)
- [11. MCP server details](#11-mcp-server-details)
- [12. Going live with Google APIs (optional)](#12-going-live-with-google-apis-optional)
- [13. Responsible AI: guardrails (no personal data goes out)](#13-responsible-ai-guardrails-no-personal-data-goes-out)
- [14. Agent-to-agent (A2A)](#14-agent-to-agent-a2a)
- [15. Agent evaluation: groundedness, truthfulness, hallucination](#15-agent-evaluation-groundedness-truthfulness-hallucination)
- [16. Security](#16-security)
- [17. Latency and token economics](#17-latency-and-token-economics)
- [18. Configuration reference](#18-configuration-reference)
- [19. CLI reference](#19-cli-reference)
- [20. REST API reference](#20-rest-api-reference)
- [21. Tests and evals](#21-tests-and-evals)
- [22. Docker and Cloud Run](#22-docker-and-cloud-run)
- [23. Project structure](#23-project-structure)
- [24. Safety design](#24-safety-design)
- [25. Verification status and limitations](#25-verification-status-and-limitations)
- [26. Troubleshooting](#26-troubleshooting)
- [27. License](#27-license)

---

## 1. Requirement → implementation map

| Requirement | How ThreadRoute meets it | Where |
|---|---|---|
| Agent SDK | Google ADK: `LlmAgent` orchestrator + 7 specialist sub-agents with `transfer_to_agent` routing, `Runner`, sessions | `threadroute/agents/` |
| Google products | Gemini models (AI Studio or Vertex AI), Google Maps Routes API, Gmail API, Google Sheets API, Cloud Run deploy | `agents/factory.py`, `integrations/` |
| Multi-agent | orchestrator, intake, tracking, allocation, exceptions & claims, returns, comms, watchdog | `agents/factory.py` |
| Multi-modal | Photos (damaged cartons, returned garments) and packing-list images are sent to Gemini as image parts; intake agent stores structured findings that claims/returns agents reuse | `agents/runtime.py`, `agents/tools.py` |
| Conversational | Multi-turn chat with session memory in web UI and terminal (`chat`) | `ui/`, `cli.py` |
| API calls | Carrier tracking REST API (mock, with a legacy secondary endpoint), Google Maps Routes, Open-Meteo weather, Gmail, Sheets | `integrations/` |
| MCP integration | Custom WMS/OMS MCP server (stdio) consumed through ADK `McpToolset`; separate read-only and write modes | `mcp_server/`, `agents/factory.py`, `approvals/executor.py` |
| Feedback loop | Thumbs, edits, rejections and `/teach` become guidance rules injected into agent prompts; rule weights move with outcomes; report compares approval rate with vs without guidance | `feedback/` |
| Self-healing | Retry/backoff, timeouts, circuit breakers, provider fallback, schema validation, model fallback chain, tool-error recovery, watchdog agent, chaos injection | `resilience/`, `agents/callbacks.py` |
| Guardrails / Responsible AI (no PII out) | Redaction at every egress: model input, tool results, model output, A2A, e-mail, sheets, stored feedback, UI reply; e-mail recipient allow-list; image metadata stripped; injection neutralisation | `guardrails/`, section 13 |
| A2A | The claims/tracking agents consult an independent carrier-desk agent over the A2A protocol (HTTP server, in-process, or local-policy fallback); ThreadRoute can itself be called as an A2A agent | `a2a/`, section 14 |
| Agent evaluation | Groundedness, truthfulness, hallucination: deterministic checker + oracle (the database) + runtime guard + labelled self-test + 37-case live golden set + optional LLM judge | `evaluation/`, `evals/golden.yaml`, section 15 |
| Security | Bearer-token auth, host allow-list, strict CSP and headers, rate/size limits, validated inputs, least-privilege tools, non-root container, release gate, CI scanners | `ui/security.py`, `SECURITY.md`, section 16 |
| Latency & token economics | Parallel lookups, TTL caches, batched writes, shared MCP session, context compaction, output caps, model tiering, token/cost ledger, budgets, benchmark | `perf/`, `optimize.py`, section 17 |
| A–Z instructions | This README, plus `docs/ARCHITECTURE.md` and `SECURITY.md` | — |
| Works without keys for review | Offline demo, offline evaluation and 145 tests run the real ADK stack with a scripted model | `threadroute/testing.py`, `demo.py` |

## 2. What you can ask it

The mock company ("ThreadRoute Menswear") has 2 distribution centres (Bhiwandi, Gurugram), 8 stores,
24 SKUs (chinos, Oxford shirts, blazers, jackets…), about 35 shipments and 9 pending returns.

- "Where is shipment SH-1001 and what should we do about the delay?" → tracking agent, carrier API, weather, reroute options, proposed reroute
- "Bengaluru Indiranagar has no white Oxford shirts in L. Where can we get stock?" → allocation agent finds donor stores and proposes a transfer
- Upload a photo of a damaged carton → intake agent reads it, claims agent proposes a claim and comms agent drafts the carrier email
- Upload a packing-list image → shortfall detected against the PO
- Upload a photo of a returned jacket → returns agent proposes a disposition (restock / grade B / grade C / liquidate)

## 3. Architecture

```
 Browser / terminal ── token auth, CSP, rate limits ──┐
        │                                              │ every boundary below passes through guardrails/egress
   ThreadRouteRuntime (ADK Runner + sessions) ── redact in · grounding check + guard out
        │
   orchestrator ──transfer──▶ intake · tracking · allocation · exceptions_claims · returns · comms · watchdog
        │                          │
        │               read-only MCP tools (WMS)         API tools (carrier, maps, weather)
        │                          │                           │  wrapped in resilient_call
        ▼                          ▼                           ▼
 propose_* tools ──▶ pending_actions (SQLite) ──human approves──▶ executor ──▶ MCP server (write mode) ──▶ WMS
                                                                           └─▶ Gmail / Sheets (outbox/CSV fallback)
 feedback ◀── thumbs / edits / rejections / teach ──▶ guidance rules ──▶ injected into prompts

 tracking / exceptions agents ◀──A2A──▶ carrier-desk agent (own server, own policy; quotes + claim pre-assessment)
```

Diagrams (system, sequence, feedback loop, self-healing table) are in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## 4. Prerequisites

- Python **3.11 or newer** (3.12 recommended)
- git
- For live agent chat: a free Gemini API key (section 6). The offline demo, tests and mock data need no key.
- Optional: Docker, a Google Cloud project (Maps / Gmail / Sheets / Cloud Run)

## 5. Setup

### Windows (PowerShell)

```powershell
git clone <your-repo-url> threadroute
cd threadroute
# If scripts are blocked:  Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1
.\.venv\Scripts\Activate.ps1
```

Manual equivalent:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt -r requirements-dev.txt
copy .env.example .env
python -m threadroute seed
```

### macOS / Linux

```bash
git clone <your-repo-url> threadroute
cd threadroute
bash scripts/setup.sh
source .venv/bin/activate
```

Manual equivalent:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
python -m threadroute seed
```

Local state (database, outbox, logs) lives in `.threadroute/` inside the repo (git-ignored). Override with `THREADROUTE_HOME`.

## 6. Get a Gemini API key and configure `.env`

1. Open <https://aistudio.google.com/apikey>, sign in, click **Create API key**.
2. Open `.env` in the repo folder and set `GOOGLE_API_KEY=<your key>`.
3. Run `python -m threadroute doctor`. It checks Python version, packages, database, MCP server start-up, key presence, and whether your configured model names resolve. If a model name is rejected, pick a currently available Gemini Flash model from AI Studio and set `THREADROUTE_MODEL` (and `THREADROUTE_FALLBACK_MODELS`, comma separated) in `.env`.

Vertex AI instead of an API key: set `GOOGLE_GENAI_USE_VERTEXAI=TRUE`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, and run `gcloud auth application-default login`.

Never commit `.env`; it is git-ignored.

## 7. Run it

| Goal | Command | Needs key |
|---|---|---|
| Offline end-to-end demo (scripted model, real ADK/MCP/approvals/healing) | `python -m threadroute demo` | no |
| Web UI at <http://127.0.0.1:8000> | `python -m threadroute web` | yes, for chat |
| Terminal chat | `python -m threadroute chat` | yes |
| Reset mock data | `python -m threadroute seed --reset` (stop the web UI first) | no |
| **Evaluation** (groundedness / truthfulness / hallucination) | `python -m threadroute evaluate` | live part only |
| Speed and token benchmark | `python -m threadroute benchmark` | no |
| Carrier-desk agent as its own A2A server | `python -m threadroute a2a-carrier` | yes |
| Pre-push release gate | `python scripts/release_check.py --tests` | no |

Start with `python -m threadroute doctor`, then `python -m threadroute demo`, then `evaluate`, then `web`.

**Opening the web UI:** the server prints a URL that ends in `#token=...`. Open *that* URL - the token after `#` never
leaves your browser, and the page keeps it for the session. Without it the API answers `401`. To use a fixed token set
`THREADROUTE_AUTH_TOKEN` in `.env`; `THREADROUTE_AUTH_TOKEN=disabled` turns authentication off for local experiments only.
To reach the UI under another hostname add it to `THREADROUTE_ALLOWED_HOSTS` (comma separated).

## 8. Guided walkthrough

Open the web UI. Use the **Samples** buttons (or attach files from `samples/`). Each scenario below
ends in a proposal; open the **Approvals** tab to review it.

**Scenario 1 — delayed shipment.** Prompt: `Where is shipment SH-1001 and what should we do about the delay?`
Expected: 400 chinos (value at risk ₹288,000) held at Nagpur because NH44 is closed, about 4 days late. The
tracking agent shows carrier status and weather risk and compares reroute options; a reroute proposal appears.
Approve it, and the MCP write server updates the shipment.

**Scenario 2 — stock-out.** Prompt: `Bengaluru Indiranagar is out of white Oxford shirts in size L. Fix it.`
Expected: donor stores ST-HYD-BJH (52 units) and ST-CHN-TNG (30) are considered, keeping a safety cover;
a reallocation proposal is created.

**Scenario 3 — damaged carton.** Attach `samples/damaged_carton_SH-1002.png`, say `This blazer carton from SH-1002 arrived damaged at Delhi CP.`
Expected: findings are recorded, a claim proposal (CL-2001 once approved) and a carrier email draft. After approval
the email is written to `.threadroute/outbox/` (or sent via Gmail in live mode) and a row appended to `action_log.csv`.

**Scenario 4 — receiving shortfall.** Attach `samples/packing_list_SH-1003.png`, say `Check this packing list against the PO for SH-1003.`
Expected: 140 units listed versus 150 expected, 10 short, with a claim proposal.

**Scenario 5 — returned garment.** Attach `samples/returned_jacket_RT-9001.png`, say `Customer return RT-9001, what do we do with it?`
Expected: stain noted, "too tight at shoulders" reason read from the WMS, a disposition proposal (most likely grade C or liquidate).

**Scenario 6 — low stock on a staple.** Prompt: `Which Delhi stores are running low on khaki chinos?`
Expected: ST-DEL-CP and ST-DEL-SKT are low (sizes 32 and 34), DC-GGN is the nearest source.

## 9. Feedback loop

1. Every agent reply has 👍/👎 buttons (and `/up` / `/down [comment]` in the terminal).
2. Rejecting a proposal requires a reason; editing a proposal before approval records the edit.
3. `/teach <rule>` (or the **Learned rules** tab) adds guidance directly, e.g. `Never reroute by air if value at risk is below 2 lakh.`
4. Rules are tagged by scenario and injected into the prompts of the relevant agents next time.
5. When an action guided by a rule is approved, its weight rises; when rejected, it falls and rules under 0.3 are retired.
6. The **Report** tab (`python -m threadroute report`) shows approval rate with and without guidance.

Try it: reject the SH-1001 reroute with the reason `Prefer rail for apparel above 300 units`, start a new chat, ask again, and see the new proposal reflect the rule.
Generic approval comments like "good call" are intentionally not turned into rules.

## 10. Self-healing drill

```powershell
python -m threadroute chaos set carrier:primary_api fail
python -m threadroute chat      # ask about SH-1001; answer still works via fallback and is marked degraded
python -m threadroute watchdog  # diagnosis + auto heal
python -m threadroute chaos clear
```

Chaos modes: `fail`, `timeout`, `slow`, `malformed`, `flaky` (use `--count N` / `--delay S`). Targets are `component:provider`:
`carrier:primary_api`, `carrier:secondary_api`, `maps:google_routes`, `weather:open_meteo`, `email:gmail_api`, `sheets:sheets_api`.
The same controls are in the **Self-healing** tab. What happens: retries with backoff, circuit breaker opens, the
provider is demoted for 120 s, the next provider (secondary API, then cached data) serves the answer, and a
`healing_note` explains it. Model errors fall through the `THREADROUTE_FALLBACK_MODELS` chain, tool exceptions return
structured errors to the model, and invalid proposals are returned to the model so it can fix and resubmit.

## 11. MCP server details

Run `python -m threadroute mcp` for the stdio server (`threadroute-wms`).
Read mode (default) exposes 11 read tools: `list_locations`, `get_sku`, `list_shipments`, `get_shipment`, `get_purchase_order`, `get_inventory`,
`get_stock_cover`, `find_donor_stores`, `list_returns`, `get_return`, `list_claims` (see `mcp_server/server.py` for the exact list).
Write mode (`THREADROUTE_MCP_MODE=full`) additionally registers `reallocate_stock`, `create_claim`, `reroute_shipment`, `set_return_disposition`,
and is only spawned by the approval executor. To inspect interactively: `npx @modelcontextprotocol/inspector python -m threadroute mcp`.

## 12. Going live with Google APIs (optional)

Everything works with mocks. To go live set `THREADROUTE_LIVE_APIS=true`; each integration then tries the real API first and falls back to mock/outbox/CSV if it is not configured or fails.

- **Maps Routes API:** enable it in Google Cloud, create an API key, set `GOOGLE_MAPS_API_KEY`.
- **Gmail and Sheets:** create an OAuth client (Desktop app) in Google Cloud, enable Gmail API and Sheets API, download JSON, set `GOOGLE_OAUTH_CREDENTIALS=<path>`, `GOOGLE_SHEET_ID=<sheet id>`, `CLAIMS_EMAIL_TO`, then run `python -m threadroute google-auth` once.
- **Weather:** Open-Meteo needs no key.

## 13. Responsible AI: guardrails (no personal data goes out)

**Principle:** personal data is *redacted*, not tokenised - there is no mapping back, so nothing downstream (model, logs,
other agents, e-mail) can recover it. Redaction replaces values with placeholders such as `[EMAIL_1]` or `[PHONE_2]`.

| Boundary | What is applied |
|---|---|
| User message → agents | PII redaction; the user is told when something was masked |
| Uploaded image → model | decode, strip EXIF/GPS and all metadata, downscale to 1568 px, re-encode (non-images rejected) |
| Tool / MCP result → model | normalise, **neutralise embedded instructions** (adds a `security_notice`), redact PII, compact |
| Model output | redaction again before anything is stored or shown |
| Agent ↔ agent (A2A) | outbound redacted; inbound neutralised and redacted |
| E-mail / Sheets | subject, body and cells redacted; **recipient must be on an allow-listed domain** (`CLAIMS_EMAIL_TO`, `OPS_EMAIL`, `THREADROUTE_ALLOWED_EMAIL_DOMAINS`) |
| Feedback / guidance | comments redacted; rules that look like instructions to ignore safeguards are rejected |
| Final reply and trace → UI | last gate (`egress.protect("user_reply", ...)`) |

Detected: e-mail, phone, postal address, PIN code, date of birth, Aadhaar (checksum-validated), PAN, payment cards (Luhn),
API-key/secret shapes, and known customer names. The **Privacy** tab of the UI (`GET /api/guardrails`) shows counts by kind -
never the values. The seed data contains *synthetic* customer contacts and two planted prompt-injection payloads
(`RT-9003` note, `SH-1016` hold reason) so you can see the defences work: ask "Summarise the notes on return RT-9003" and
"Give me the customer's e-mail for RT-9001".

Limits: detection is pattern- and list-based (see [`SECURITY.md`](SECURITY.md) section 7).

## 14. Agent-to-agent (A2A)

ThreadRoute's tracking and exceptions/claims agents call `ask_carrier_agent`, which talks - over the **A2A protocol** - to an
independent **carrier-desk agent** that owns its own policy (shipment status view, reroute quotes by mode, claim
pre-assessment: acceptance odds by severity). References returned by the peer (quote ids, case ids) are quoted in proposals.

Transports, tried in order, each with the usual retry / breaker / fallback treatment (`resilience`, component `a2a`):

1. `a2a_http` - the carrier agent running as a **separate server** (`python -m threadroute a2a-carrier`, port 8101;
   `web` starts it automatically when a Gemini key exists and sets `THREADROUTE_A2A_URL`).
2. `a2a_inprocess` - the same A2A request/response objects over an in-process ASGI transport.
3. `local_policy` - a deterministic snapshot, clearly flagged `degraded=true` in the answer.

Auth: the Agent Card (`/.well-known/agent-card.json`) is public; every other route needs `Authorization: Bearer <THREADROUTE_A2A_TOKEN>`
(random per run if unset). The other direction also works: `python -m threadroute a2a-serve` (port 8102) exposes ThreadRoute's
orchestrator so another agent can call it. The A2A support in ADK is marked experimental; it is pinned via `google-adk[a2a]`.

## 15. Agent evaluation: groundedness, truthfulness, hallucination

```bash
python -m threadroute evaluate                  # self-test + guard drill (offline) + live golden set if a key exists
python -m threadroute evaluate --offline        # no key needed
python -m threadroute evaluate --live --judge   # live set plus an LLM-as-judge second opinion
python -m threadroute evaluate --case g_status  # one live case
```

Writes `reports/evaluation_latest.md` and `.json` (use `--out docs/evidence` to keep a copy you can commit).
Exit code is non-zero if a gate fails: checker recall < 0.9, false-positive rate > 0.1, a guard-drill scenario fails, or the live run shows a PII leak,
a followed injection, or a hallucination that survives the guard.

**Definitions used**

* **Groundedness** - share of an answer's checkable claims (ids, numbers, money, dates, "I did X" statements) that are supported by what the tools
  returned *in that turn* or by the user's own words.
* **Truthfulness** - claims are consistent with the WMS database (the *oracle*), whatever the model's source. A contradiction (wrong status, carrier, unit count) is worse than an unsupported claim.
* **Hallucination** - ids that do not exist, quantities with no source, or actions claimed as done when agents only ever *propose*.

**Three layers**

1. **Self-test of the evaluator** (`evaluation/selftest.py`): 14 planted faults (fabricated ids, wrong status/carrier, invented units/money/dates, false
   "I sent it", leaked e-mail/phone) and 5 good answers, generated from the live database. Reports recall per fault type and false-positive rate.
2. **Runtime guard** (`agents/runtime.py`): every answer is checked before the user sees it. On a problem the model gets **one** corrective retry with the
   specific unsupported claims; if it still fails the offending sentences are removed with a visible note. The guard drill replays a scripted *hallucinating*
   model through the real stack to prove no fabrication reaches the user. The UI shows the verification status on each answer.
3. **Live golden set** (`evals/golden.yaml`, 37 cases): grounded lookups, unknown ids (must abstain), false premises (must correct), data that does not exist
   (must say so), action honesty, PII extraction attempts, prompt injection in data, multimodal. Reported: groundedness, truthfulness, hallucination rate
   **before and after** the guard, abstention accuracy, PII leaks, injections followed, latency per case.

`THREADROUTE_VERIFY=0` disables the runtime guard (for baselines). The older `eval` command (12 behavioural scenarios) is unchanged.

## 16. Security

Summary (details, threat model and **what is not covered** in [`SECURITY.md`](SECURITY.md)): per-run bearer token; Host-header allow-list; strict CSP with no inline script/style;
security headers; chat 20 req/min and API 240 req/min limits; 64 KB JSON and 9 MB upload caps; Pydantic validation with unknown fields rejected; no OpenAPI/Swagger
unless `THREADROUTE_DEBUG=1`; generic error messages; parameterised SQL only; the mock carrier API is no longer exposed over HTTP; agents have read-only tools and
no approve tool; non-root container with health check; compose refuses to start without a token; `scripts/release_check.py` and CI (`pip-audit`, `bandit`, `gitleaks`) before publishing.
The app speaks plain HTTP: put a TLS proxy in front of anything that is not localhost.

## 17. Latency and token economics

Timings are **in memory** (bounded ring buffers) and read on demand - nothing is logged or written per request.

| Technique | Effect |
|---|---|
| Parallel `asyncio.gather` for weather, tracking and every route estimate in a reroute comparison | latency = slowest call, not the sum |
| TTL cache for carrier / weather / route lookups (`THREADROUTE_CACHE_TTL`, default 20 s; cleared on chaos, healing and every executed action; errors and degraded answers never cached) | repeated lookups cost microseconds |
| Batched background SQLite writer, cached config, cached chaos probe | no per-call disk I/O on the hot path |
| One shared MCP session for all agents (separate tool allow-lists kept) | one server process instead of six |
| Context compaction: null-stripping, list truncation, old images and bulky old tool results dropped from history (`THREADROUTE_OPTIMIZE=0` to disable) | fewer prompt tokens |
| `max_output_tokens` cap (`THREADROUTE_MAX_OUTPUT_TOKENS`, default 1500), optional `THREADROUTE_THINKING_BUDGET` | bounded cost and latency |
| Model tiering: `THREADROUTE_ROUTER_MODEL` runs only the orchestrator on a cheaper model | cheaper routing |
| Budgets: `THREADROUTE_SESSION_TOKEN_BUDGET` (250k) and `THREADROUTE_MAX_MODEL_CALLS_PER_TURN` (14) | runaway loops stop |
| Progress streaming (`POST /api/chat/stream`, server-sent events) | the user sees which agent/tool is working; the final answer appears only after guardrails and verification |

**Measure it:** `python -m threadroute benchmark` replays three multi-turn conversations through the real stack with optimisation off and on and writes
`reports/benchmark_latest.md`. Token numbers there are *estimates* (characters / 4) because the scripted model reports no usage; with a Gemini key the **Cost & speed**
tab (`GET /api/perf`) shows the API's real counts and a cost estimate from `config/pricing.yaml` - those prices are **placeholders; confirm them**.
Measured on the offline benchmark in the build environment: 16% fewer prompt tokens across three multi-turn conversations (44,473 -> 37,211 estimated tokens; the saving is concentrated in conversations with bulky tool results and is 0% where results are already small, because most of the prompt is the fixed instructions and tool definitions), and a reroute comparison with 120 ms simulated provider latency taking about 130 ms instead of a calculated ~600 ms sequential equivalent. These are offline, estimated figures - not live Gemini measurements.

## 18. Configuration reference

| Variable | Default | Meaning |
|---|---|---|
| `GOOGLE_API_KEY` | — | Gemini API key (AI Studio) |
| `GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` | — | Vertex AI auth alternative |
| `THREADROUTE_MODEL` | see `.env.example` | primary model |
| `THREADROUTE_FALLBACK_MODELS` | see `.env.example` | comma-separated fallback chain |
| `THREADROUTE_LIVE_APIS` | `false` | try real Google/weather APIs first |
| `GOOGLE_MAPS_API_KEY` | — | Routes API key |
| `GOOGLE_OAUTH_CREDENTIALS` | — | OAuth client JSON path |
| `GOOGLE_SHEET_ID` | — | sheet for the action log |
| `CARRIER_API_BASE_URL` | built-in mock | external carrier API |
| `CLAIMS_EMAIL_TO`, `OPS_EMAIL` | — | claim email recipients |
| `THREADROUTE_HOME` | `./.threadroute` | state directory |
| `THREADROUTE_DEBUG` | `false` | verbose traces; also enables `/docs` |
| `THREADROUTE_AUTH_TOKEN` | random per run | UI/API bearer token; `disabled` turns auth off (local only) |
| `THREADROUTE_ALLOWED_HOSTS` | localhost | extra hostnames allowed in the Host header |
| `THREADROUTE_ALLOWED_EMAIL_DOMAINS` | - | extra e-mail recipient domains (besides `CLAIMS_EMAIL_TO`, `OPS_EMAIL`) |
| `THREADROUTE_A2A_URL`, `THREADROUTE_A2A_TOKEN` | auto | external carrier-agent URL and shared bearer token |
| `THREADROUTE_VERIFY` | `1` | runtime grounding guard on/off |
| `THREADROUTE_OPTIMIZE` | `1` | context compaction on/off |
| `THREADROUTE_CACHE_TTL` | `20` | seconds for integration lookups; `0` disables |
| `THREADROUTE_MCP_SHARED` | `1` | share one MCP session across agents |
| `THREADROUTE_ROUTER_MODEL` | - | cheaper model for the orchestrator only |
| `THREADROUTE_MAX_OUTPUT_TOKENS`, `THREADROUTE_THINKING_BUDGET` | `1500`, unset | generation limits |
| `THREADROUTE_SESSION_TOKEN_BUDGET`, `THREADROUTE_MAX_MODEL_CALLS_PER_TURN` | `250000`, `14` | per-session / per-turn limits |
| `THREADROUTE_PRICING` | `config/pricing.yaml` | token price table (placeholders) |

## 19. CLI reference

`seed [--reset]`, `doctor`, `chat [--quiet]`, `web [--host H] [--port P]`, `mcp`, `demo`,
`chaos set|clear|list [component:provider] [mode] [--count N] [--delay S]`, `watchdog`, `report [--out FILE]`,
`eval [--scenario ID]`, `evaluate [--offline|--live] [--live-only] [--judge] [--case ID] [--out DIR]`, `benchmark [--out DIR]`,
`a2a-carrier [--port 8101]`, `a2a-serve [--port 8102]`, `google-auth`; `web` also takes `--carrier-agent-port` (0 = do not start it).
All as `python -m threadroute <command>`.

Chat slash commands: `/image <path>`, `/approve <id> [key=value…] [-- comment]`, `/reject <id> <reason>`, `/actions`,
`/up`, `/down [comment]`, `/teach <rule>`, `/guidance`, `/health`, `/report`, `/new`, `/help`, `/quit`.

## 20. REST API reference

`GET /api/status` · `POST /api/chat` · `GET /api/actions` · `POST /api/actions/{id}/approve` · `POST /api/actions/{id}/reject` ·
`POST /api/feedback` · `POST /api/teach` · `GET /api/guidance` · `DELETE /api/guidance/{id}` · `GET /api/health` ·
`POST /api/watchdog/heal` · `POST|DELETE /api/chaos` · `GET /api/report` · `GET /api/samples` · `POST /api/chat/stream` (SSE) ·
`GET /api/guardrails` · `GET /api/perf` · `GET /healthz` (public). Everything except `/`, static files and `/healthz` needs `Authorization: Bearer <token>`.
The mock carrier API is internal only (no HTTP route). OpenAPI docs exist only with `THREADROUTE_DEBUG=1`.

## 21. Tests and evals

```bash
python -m pytest                       # 145 tests, no API key, real ADK stack with a scripted model
python -m threadroute demo             # narrated offline run of all features
python -m threadroute evaluate         # evaluator self-test + hallucination guard drill (+ live golden set with a key)
python -m threadroute eval             # 12 live behavioural scenarios against Gemini (needs a key)
python -m threadroute benchmark        # offline speed/token benchmark
python scripts/release_check.py --tests  # run before every push
```

CI workflows live in `docs/ci/` (copy them to `.github/workflows/`, see `docs/ci/README.md`): tests on Ubuntu (3.11, 3.13) and Windows (3.12), demo, offline evaluation,
benchmark, release gate, plus `pip-audit` / `bandit` / `gitleaks`.

## 22. Docker and Cloud Run

```bash
# .env must contain GOOGLE_API_KEY; choose a token for the UI:
export THREADROUTE_AUTH_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(24))")
docker compose up --build            # http://localhost:8000/#token=<your token>
```
The container runs as a non-root user with a read-only filesystem, dropped capabilities and a health check; compose refuses to start without a token.

Cloud Run (single instance, because state is SQLite and in memory):

```bash
gcloud run deploy threadroute --source . --region asia-south1 --max-instances 1 \
  --set-env-vars GOOGLE_API_KEY=...,THREADROUTE_AUTH_TOKEN=...,THREADROUTE_ALLOWED_HOSTS=... --no-allow-unauthenticated
```

State is ephemeral on Cloud Run. Set `THREADROUTE_AUTH_TOKEN` and `THREADROUTE_ALLOWED_HOSTS=<your-service-host>`; Cloud Run terminates TLS for you. There are no user accounts, only the shared token.

## 23. Project structure

```
threadroute/
  a2a/           carrier-desk agent, A2A client/server, peer policy
  agents/        orchestrator + sub-agents, tools, prompts, callbacks, runtime
  approvals/     proposal models, human approval service, MCP executor
  data/          deterministic seed data
  domain/        WMS business logic
  evaluation/    grounding checker, oracle, claim extraction, judge, selftest, runner
  feedback/      feedback store, guidance retrieval, report
  guardrails/    PII redaction, injection neutralisation, image sanitising, egress gate, audit
  integrations/  carriers, maps, weather, gmail, sheets, google auth
  mcp_server/    WMS MCP server
  perf/          timing metrics, token ledger and cost model, TTL cache, benchmark
  resilience/    resilient_call, breaker, chaos, watchdog
  ui/            FastAPI app + static page
  cli.py demo.py testing.py config.py db.py optimize.py evidence.py
evals/  tests/  samples/  scripts/  docs/  config/pricing.yaml  SECURITY.md
```

## 24. Safety design

Agents have no approve tool and only a read-only MCP connection. Writes happen only after a human approves in the UI, terminal or REST,
through a separate write-mode MCP process. Proposals are validated by Pydantic schemas, and stock moves respect safety cover.
On top of that: personal data is redacted at every boundary (section 13), untrusted text is neutralised, e-mail goes only to allow-listed domains,
answers are verified against tool data and the database (section 15), and the web layer is authenticated and hardened (section 16, [`SECURITY.md`](SECURITY.md)).

## 25. Verification status and limitations

**Verified automatically** (clean virtual environment built from `requirements.txt`; 145 tests pass; demo, offline evaluation and benchmark run): the real ADK agent hierarchy, MCP read and
write paths, approvals, feedback injection, chaos and healing, guardrails (PII, injection, images), the web security layer, A2A in all three transports including a real HTTP server,
the grounding checker and the runtime guard, caches and parallelism, the REST API and CLI - using a scripted model.

**Not verifiable without credentials / outside the build environment - run `doctor` and `evaluate` yourself:**
real Gemini responses and the default model names in `.env.example` (the live golden set has *not* been run), real token counts and latency, live Maps / Gmail / Sheets / Open-Meteo calls,
the Docker image build and Cloud Run deployment, a Windows run. The carrier API, the carrier-desk agent's policy and the warehouse data are mock/synthetic.
Token prices in `config/pricing.yaml` are placeholders. ADK's A2A support is experimental.

**Known limitations:** PII detection is pattern/list based; the grounding checker is conservative and can both miss subtle falsehoods and flag unusual-but-correct derivations
(use `--judge`); one shared token, no user accounts, plain HTTP; in-process rate limiting; SQLite and in-memory sessions mean a single instance. See [`SECURITY.md`](SECURITY.md) section 7.
Model wording varies between runs, so the exact text in section 8 will differ; the facts and proposals should match.

## 26. Troubleshooting

- **"Missing credentials" in chat:** set `GOOGLE_API_KEY` in `.env`, restart, run `doctor`.
- **Model not found / 404:** change `THREADROUTE_MODEL` to a model listed in AI Studio.
- **`setup.ps1` blocked:** `Set-ExecutionPolicy -Scope Process Bypass`.
- **`database is locked` on `seed --reset`:** stop the web UI first.
- **Port in use:** `python -m threadroute web --port 8010`.
- **Garbled characters on Windows:** use Windows Terminal; the CLI forces UTF-8.
- **`401 Authentication required`:** open the URL the server printed (it ends in `#token=...`), or send `Authorization: Bearer <token>`.
- **`400 Invalid host header`:** add the hostname to `THREADROUTE_ALLOWED_HOSTS`.
- **My answer says "I removed a detail I could not verify":** the runtime guard stripped an unsupported claim; see the verification badge, or set `THREADROUTE_VERIFY=0` to compare.
- **E-mail proposal rejected "recipient domain not allowed":** add the domain to `THREADROUTE_ALLOWED_EMAIL_DOMAINS`.
- **`No module named 'a2a'`:** reinstall with `pip install -r requirements.txt` (it includes `google-adk[mcp,a2a]`).
- **Everything says "degraded":** a chaos rule is active; run `python -m threadroute chaos clear`.

## 27. License

See [LICENSE](LICENSE).
