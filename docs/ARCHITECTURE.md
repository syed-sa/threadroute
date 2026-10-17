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
