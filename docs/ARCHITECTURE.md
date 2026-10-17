# ThreadRoute architecture

## 1. System overview

```mermaid
flowchart LR
    U([Ops manager]) -->|text + photos| UI[Web UI / CLI]
    UI --> RT[Chat runtime<br/>Google ADK Runner]
    RT --> ORC{{Orchestrator agent}}
    ORC -->|transfer| IN[Intake agent<br/>Gemini vision]
    ORC -->|transfer| TR[Tracking agent]
    ORC -->|transfer| AL[Allocation agent]
    ORC -->|transfer| EX[Exceptions and claims agent]
    ORC -->|transfer| RE[Returns agent]
    ORC -->|transfer| CO[Comms agent]
    ORC -->|transfer| WD[Watchdog agent]

    IN & TR & AL & EX & RE & CO -- "read-only MCP (stdio)" --> MCP[(WMS/OMS MCP server<br/>read mode)]
    TR & EX --> API[Resilient API layer]
    API --> CAR[Carrier tracking API]
    API --> MAP[Google Maps Routes API]
    API --> WX[Open-Meteo weather]

    AL & EX & RE & CO -- "propose_*" --> PEND[(Pending actions)]
    PEND --> UI
    U -- "Approve / Reject / Edit" --> APPR[Approval service]
    APPR --> EXE[Executor]
    EXE -- "write MCP (stdio, full mode)" --> MCPW[(WMS/OMS MCP server<br/>full mode)]
    EXE --> GM[Gmail API]
    EXE --> SH[Google Sheets API]
    MCP & MCPW --- DB[(SQLite WMS)]

    APPR -- "decisions, reasons" --> FB[(Feedback + guidance store)]
    UI -- "thumbs, /teach" --> FB
    FB -- "relevant rules injected into prompts" --> ORC
    WD <--> HEAL[(Healing log, breakers, chaos rules)]
    API --> HEAL
```

## 2. One request, step by step

```mermaid
sequenceDiagram
    participant U as User
    participant O as Orchestrator
    participant A as Allocation agent
    participant M as MCP server (read)
    participant P as Pending actions
    participant H as Human (UI)
    participant X as Executor
    participant W as MCP server (full)

    U->>O: "Bengaluru is out of Oxford shirts in L"
    Note over O: instruction = base prompt + retrieved learned rules
    O->>A: transfer_to_agent(allocation_agent)
    A->>M: get_inventory / find_donor_stores
    M-->>A: stock, velocity, donors with surplus
    A->>P: propose_reallocation (validated, deduplicated)
    P-->>A: ACT-1001 pending_approval
    A-->>U: "ACT-1001 is waiting for your approval"
    H->>X: Approve (optionally edit qty, add comment)
    X->>W: reallocate_stock (retry, fallback)
    W-->>X: ok, TRF-3001
    X->>X: log to Sheets, record feedback, credit/penalise rules
```

## 3. Self-healing layers

| Layer | Failure | Automatic response | Where |
|---|---|---|---|
| Transport | HTTP 429/5xx from Gemini | exponential-backoff retries | `agents/models.py` |
| Model | primary model unavailable or unknown | walk the fallback model list, then degrade to a plain-language message | `agents/callbacks.py::on_model_error` |
| Tool call | exception inside a tool | convert to a structured error with a hint; the model corrects itself or reports it | `agents/callbacks.py::on_tool_error` |
| Model output | wrong enum / impossible quantity | validation error returned to the model so it repairs its own arguments; nothing is queued | `agents/tools.py::_propose` |
| External API | timeout, error, malformed payload | retry with backoff, then next provider in the chain (primary -> secondary -> cache/estimate); result flagged `degraded` | `resilience/resilient.py` |
| Repeated failure | provider keeps failing | circuit breaker opens; watchdog demotes it for 2 minutes and may raise timeouts or retries | `resilience/breaker.py`, `watchdog.py` |
| MCP subprocess | write server cannot start | fall back to the same domain function in-process (the human already approved) | `approvals/executor.py` |
| Recovery | the outage ends | demotion expires / `chaos clear` restores order; a half-open breaker probes the primary | `breaker.py` |

Every healing action is written to `healing_events`, shown in the UI "Self-healing" tab and counted in the report.

## 4. Feedback loop

```mermaid
flowchart LR
    A[Agent proposes action] --> H{Human decides}
    H -- approve --> E[Execute] --> O1[+1 decision, rules used gain weight]
    H -- "approve with edit + comment" --> E --> N[learn new rule from comment]
    H -- "reject + reason" --> N2[learn rule from reason] --> O2[-1 decision, rules used lose weight]
    T[Thumbs down + comment, /teach] --> N
    N & N2 --> G[(Guidance store)]
    G -- "retrieve by scenario, tags, text, weight, recency" --> P[Injected into next agent prompt]
    P --> A
    O2 -- "weight below 0.3" --> R[Rule retired]
```

* A rule is created from any rejection reason, any edit that comes with a comment, a thumbs-down comment, or `/teach`.
* Near-duplicate rules are merged and their weight grows (max 5).
* Retrieval scores each rule by scenario match, shared tags (carrier, category, mode, city, weather terms), text overlap, weight and age.
* The report compares approval rate with and without learned guidance applied.

## 5. Safety design

* **Approval gate.** The LLM has no tool that approves or executes. `propose_*` only writes a pending row; only the REST endpoint / CLI command (a person) can approve.
* **Read-only MCP for agents.** Agent toolsets connect to a server started with `THREADROUTE_MCP_MODE=read`, in which write tools are not even registered. Only the executor starts the server in `full` mode.
* **Business rules live in one place** (`domain/wms.py`) and are re-checked at execution time, so stock that changed after a proposal cannot be over-allocated.
* **Idempotent proposals.** Identical pending proposals in a session collapse into one.
* **No secrets in code.** Keys come from `.env` / environment; `.env`, tokens and databases are git-ignored.
* **Untrusted content.** Text read from photos is treated as data; the agents verify ids against the WMS before proposing anything, and anything with side effects still needs a human.

## 6. Guardrails: the egress choke point

Every boundary calls the same function, `guardrails.egress.protect(destination, obj)`, so there is one place to audit and one place that can be wrong.

```mermaid
flowchart LR
    U([User]) -->|text| IN["protect(user_in)<br/>PII redacted"]
    IMG[Photo] --> SAN["sanitize_image<br/>decode, strip EXIF, downscale"]
    IN & SAN --> M[(Gemini)]
    T[Tool / MCP result] --> N["neutralise injections<br/>redact PII, compact"] --> M
    M -->|output| R["redact"] --> V{{"grounding check<br/>vs tool evidence + database"}}
    V -- ok --> OUT["protect(user_reply)"] --> U
    V -- unsupported claim --> RETRY["1 corrective retry"] --> V2{{check again}}
    V2 -- ok --> OUT
    V2 -- still bad --> STRIP["strip unverifiable sentences"] --> OUT
    P[propose_email / sheets / A2A] --> E["protect(email | sheets | a2a_out)<br/>recipient allow-list"]
```

Redaction is one-way (placeholders like `[EMAIL_1]`; the vault keeps hashes only), so nothing downstream can reconstruct a value. The audit counters record *kinds and counts*, never values.

## 7. A2A: two agents, two owners

```mermaid
sequenceDiagram
    participant EX as exceptions_claims_agent (ThreadRoute)
    participant C as CarrierPeer (client)
    participant H as Carrier-desk agent (A2A server :8101)
    EX->>C: ask_carrier_agent("quote air for SH-1001")
    C->>C: redact outbound text
    C->>H: A2A message + Bearer token (agent card is public)
    H->>H: its own tools: status / quote / claim assessment
    H-->>C: reply + reference ids
    C->>C: neutralise injections, redact inbound
    C-->>EX: {reply, transport, references, degraded}
```

Transport chain with the same resilience treatment as every other dependency: `a2a_http` → `a2a_inprocess` (same protocol objects over an ASGI transport) → `local_policy` (flagged `degraded`). ThreadRoute can also be served as an A2A agent (`a2a-serve`).

## 8. Evaluation architecture

* `evaluation/claims.py` extracts checkable claims (ids, numbers + units, money in INR/lakh/crore, dates, "I did X" statements).
* `evidence.py` records what the model was actually shown by tools in the current turn (post-redaction, post-compaction).
* `evaluation/grounding.py::check` compares claims with (a) the evidence and the user's text → **groundedness**, (b) the database through `evaluation/oracle.py` → **truthfulness**, (c) the approval log → **false action claims**, and flags **fabricated ids** and **PII**. Allowed derivations (sums, differences, day counts, rounding) are accounted for.
* `agents/runtime.py::_verify_and_repair` is the runtime guard (check → one retry → strip).
* `evaluation/runner.py` is the offline/live harness; `evaluation/selftest.py` measures the checker itself; `evaluation/judge.py` is an optional LLM second opinion.

## 9. Performance and token flow

* **Hot path has no I/O beyond the model and tools:** timings go to bounded in-memory deques; resilience events/stats are queued to a background writer (flushed every 1.5 s, at exit and before any read); config and chaos probes are cached.
* **Parallelism:** independent lookups run under `asyncio.gather` (reroute comparison); the MCP server is started once and shared by all agents, each keeping its own tool allow-list.
* **Caching:** `perf/cache.py` caches only good carrier/weather/route answers for a short TTL and is cleared on every state change.
* **Context size:** `optimize.py` normalises MCP results, drops nulls, truncates long lists and compacts history (old images and bulky old tool results) before each model call; `before_model` enforces the session token budget and the per-turn call cap.
* **Accounting:** `perf/tokens.py` records real usage when the API reports it (chars/4 estimate otherwise, flagged) and prices it from `config/pricing.yaml`.
