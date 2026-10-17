# ThreadRoute — multi-agent logistics control tower for men's apparel

ThreadRoute is a team of Gemini-powered agents (built with the **Google Agent Development Kit**) that
handles day-to-day logistics for a men's-apparel brand: delayed inbound shipments, store stock-outs,
damaged cartons, receiving shortfalls and returned garments. It talks to a warehouse system over **MCP**,
calls carrier / maps / weather / Gmail / Sheets **APIs**, reads **photos and packing lists**, learns from
human **feedback**, and **heals itself** when a dependency breaks.

Agents never change inventory on their own. They *propose* actions; a human approves them in the
web UI (or terminal), and only then are changes executed.

- [1. Requirement → implementation map](#1-requirement--implementation-map)
- [2. What you can ask it](#2-what-you-can-ask-it)
- [3. Architecture](#3-architecture)
- [4. Prerequisites](#4-prerequisites)
- [5. Setup (Windows / macOS / Linux)](#5-setup)
- [6. Get a Gemini API key and configure `.env`](#6-get-a-gemini-api-key-and-configure-env)
- [7. Run it](#7-run-it)
- [8. Guided walkthrough (copy-paste prompts)](#8-guided-walkthrough)
- [9. Feedback loop](#9-feedback-loop)
- [10. Self-healing drill](#10-self-healing-drill)
- [11. MCP server details](#11-mcp-server-details)
- [12. Going live with Google APIs (optional)](#12-going-live-with-google-apis-optional)
- [13. Configuration reference](#13-configuration-reference)
- [14. CLI reference](#14-cli-reference)
- [15. REST API reference](#15-rest-api-reference)
- [16. Tests and evals](#16-tests-and-evals)
- [17. Docker and Cloud Run](#17-docker-and-cloud-run)
- [18. Project structure](#18-project-structure)
- [19. Safety design](#19-safety-design)
- [20. Verification status and limitations](#20-verification-status-and-limitations)
- [21. Troubleshooting](#21-troubleshooting)
- [22. License](#22-license)

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
| A–Z instructions | This README, plus `docs/ARCHITECTURE.md` | — |
| Works without keys for review | Offline demo and 89 tests run the real ADK stack with a scripted model | `threadroute/testing.py`, `demo.py` |

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
 Browser / terminal
        │
   ThreadRouteRuntime (ADK Runner + sessions)
        │
   orchestrator ──transfer──▶ intake · tracking · allocation · exceptions_claims · returns · comms · watchdog
        │                          │
        │               read-only MCP tools (WMS)         API tools (carrier, maps, weather)
        │                          │                           │  wrapped in resilient_call
        ▼                          ▼                           ▼
 propose_* tools ──▶ pending_actions (SQLite) ──human approves──▶ executor ──▶ MCP server (write mode) ──▶ WMS
                                                                           └─▶ Gmail / Sheets (outbox/CSV fallback)
 feedback ◀── thumbs / edits / rejections / teach ──▶ guidance rules ──▶ injected into prompts
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

Start with `python -m threadroute doctor`, then `python -m threadroute demo`, then `web`.

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

## 13. Configuration reference

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
| `THREADROUTE_DEBUG` | `false` | verbose traces |

## 14. CLI reference

`seed [--reset]`, `doctor`, `chat [--quiet]`, `web [--host H] [--port P]`, `mcp`, `demo`,
`chaos set|clear|list [component:provider] [mode] [--count N] [--delay S]`, `watchdog`, `report [--out FILE]`,
`eval [--scenario ID]`, `google-auth`. All as `python -m threadroute <command>`.

Chat slash commands: `/image <path>`, `/approve <id> [key=value…] [-- comment]`, `/reject <id> <reason>`, `/actions`,
`/up`, `/down [comment]`, `/teach <rule>`, `/guidance`, `/health`, `/report`, `/new`, `/help`, `/quit`.

## 15. REST API reference

`GET /api/status` · `POST /api/chat` · `GET /api/actions` · `POST /api/actions/{id}/approve` · `POST /api/actions/{id}/reject` ·
`POST /api/feedback` · `POST /api/teach` · `GET /api/guidance` · `DELETE /api/guidance/{id}` · `GET /api/health` ·
`POST /api/watchdog/heal` · `POST|DELETE /api/chaos` · `GET /api/report` · `GET /api/samples`.
Mock carrier API: `/mock-carrier/v1/track`, `/mock-carrier/v2/legacy`. Interactive docs at `/docs`.

## 16. Tests and evals

```bash
python -m pytest            # 89 tests, no API key, ~ real ADK stack with a scripted model
python -m threadroute demo  # narrated offline run of all features
python -m threadroute eval  # 12 live scenarios against Gemini (needs a key; costs a few API calls)
```

CI (`.github/workflows/ci.yml`) runs tests and the demo on Ubuntu (3.11, 3.13) and Windows (3.12).

## 17. Docker and Cloud Run

```bash
docker compose up --build            # http://localhost:8000
```

Cloud Run (single instance, because state is SQLite and in memory):

```bash
gcloud run deploy threadroute --source . --region asia-south1 --max-instances 1 \
  --set-env-vars GOOGLE_API_KEY=... --no-allow-unauthenticated
```

State is ephemeral on Cloud Run. Put it behind authentication; the UI has no login of its own.

## 18. Project structure

```
threadroute/
  agents/        orchestrator + sub-agents, tools, prompts, callbacks, runtime
  approvals/     proposal models, human approval service, MCP executor
  data/          deterministic seed data
  domain/        WMS business logic
  feedback/      feedback store, guidance retrieval, report
  integrations/  carriers, maps, weather, gmail, sheets, google auth
  mcp_server/    WMS MCP server
  resilience/    resilient_call, breaker, chaos, watchdog
  ui/            FastAPI app + static page
  cli.py demo.py testing.py config.py db.py
evals/  tests/  samples/  scripts/  docs/
```

## 19. Safety design

Agents have no approve tool and only a read-only MCP connection. Writes happen only after a human approves in the UI, terminal or REST,
through a separate write-mode MCP process. Proposals are validated by Pydantic schemas, and stock moves respect safety cover.

## 20. Verification status and limitations

Verified automatically: the 89 tests and offline demo exercise the real ADK agent hierarchy, MCP read and write paths, approvals,
feedback injection, chaos and healing, multimodal plumbing, REST API and CLI, using a scripted model.

Not verifiable without credentials, so run `doctor` and `eval` yourself: real Gemini responses and the default model names in `.env.example`,
live Maps / Gmail / Sheets / Open-Meteo calls, the Docker image build and Cloud Run deployment. The carrier API and warehouse data are mock.
Model wording varies between runs, so the exact text in section 8 will differ; the facts and proposals should match.

## 21. Troubleshooting

- **"Missing credentials" in chat:** set `GOOGLE_API_KEY` in `.env`, restart, run `doctor`.
- **Model not found / 404:** change `THREADROUTE_MODEL` to a model listed in AI Studio.
- **`setup.ps1` blocked:** `Set-ExecutionPolicy -Scope Process Bypass`.
- **`database is locked` on `seed --reset`:** stop the web UI first.
- **Port in use:** `python -m threadroute web --port 8010`.
- **Garbled characters on Windows:** use Windows Terminal; the CLI forces UTF-8.
- **Everything says "degraded":** a chaos rule is active; run `python -m threadroute chaos clear`.

## 22. License

See [LICENSE](LICENSE).
