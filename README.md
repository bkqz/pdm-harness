# Semantic Air-Gap: Predictive Maintenance Orchestrator

A deterministic Python harness that lets an LLM diagnose industrial faults and draft maintenance work orders — while physically blocking it from touching the ERP until every schema contract, SQL checkpoint, and YAML guardrail has passed.

**The agent proposes. The harness decides.**

---

## The Problem

Industrial LLM deployments fail when non-deterministic models are granted direct execution access to deterministic enterprise systems. A model that hallucinates a part number, invents a cost estimate, or misclassifies priority can trigger a cascade of incorrect purchase orders, duplicated work tickets, or unsafe maintenance actions.

## The Solution: Semantic Air-Gap

This system interposes a strict software boundary between the AI and the ERP:

```
SCADA telemetry  ──►  LLM diagnoses fault  ──►  HARNESS validates  ──►  ERP write
                       (fault category only)      (schema + SQL + YAML)   (if all pass)
```

The model is structurally prevented from hallucinating part numbers or costs — those fields do not exist in its output schema. The harness looks up parts from a maintenance BOM and computes costs from live ERP inventory prices. Only a 7-value fault category label crosses the air-gap.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Web UI | FastAPI + HTMX + Jinja2 (no JS framework, no build step) |
| Validation | Pydantic v2 — strict schema on all inputs and outputs |
| State Grounding | SQLite — simulates ERP/CMMS relational tables |
| Guardrails | PyYAML — declarative operational limits |
| LLM Client | httpx → OpenRouter API |
| Models | `openai/gpt-oss-20b:free` (default) · `qwen/qwen3-coder:free` |
| Observability | Append-only `.jsonl` audit trail |

---

## UI Overview

Three-column dark industrial console served at `http://localhost:8000`:

- **Left** — SCADA Feed form (structured telemetry) OR Operator Report tab (free-text NLP)
- **Centre** — Live pipeline phases: INTAKE → INFERENCE → ERP LOOKUP → VALIDATION → RESULT
- **Right** — Work order panel with HITL approve / reject / edit controls

Model selector in the header switches between GPT-OSS-20B, Qwen3-Coder, and Offline (rule-based) modes. ERP guardrails are always active regardless of model.

---

## Quick Start

```bash
# 1. Clone and set up environment
git clone <repo-url>
cd pdm-harness

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure API key
cp .env.example .env
# Edit .env: set OPENROUTER_API_KEY=sk-or-v1-...

# 3. Start the console
uvicorn api:app --reload
# Open http://localhost:8000
```

No database setup needed — for demo purposes,`bootstrap_db()` seeds a complete ERP state on every startup.

---

## Demo Scenarios

Use the preset buttons in the UI or paste these into the SCADA Feed form.

### 1. Normal operation (auto-approved)
```json
{"asset_id": "PUMP-042", "timestamp": "2026-06-13T08:00:00Z",
 "vibration_mm_s": 2.1, "temperature_c": 62.0, "pressure_bar": 8.1,
 "flow_rate_l_min": 215.0, "fault_codes": []}
```

### 2. Zone C vibration — bearing fault (HITL: exceeds auto-approval or requires shutdown)
```json
{"asset_id": "PUMP-042", "timestamp": "2026-06-13T08:00:00Z",
 "vibration_mm_s": 8.4, "temperature_c": 82.1, "pressure_bar": 8.2,
 "flow_rate_l_min": 210.0, "fault_codes": ["VIB-HIGH", "BRG-TEMP-WARN"]}
```

### 3. Trip-level CRITICAL — HITL with badge 001 only
```json
{"asset_id": "PUMP-042", "timestamp": "2026-06-13T08:00:00Z",
 "vibration_mm_s": 12.4, "temperature_c": 97.2, "pressure_bar": 7.8,
 "flow_rate_l_min": 158.0, "fault_codes": ["VIB-TRIP", "BRG-TEMP-TRIP", "OVERTEMP"]}
```

### 4. Duplicate ticket guardrail (MOL-PUMP-001 has an OPEN ticket seeded in DB)
```json
{"asset_id": "MOL-PUMP-001", "timestamp": "2026-06-13T08:00:00Z",
 "vibration_mm_s": 8.4, "temperature_c": 82.1, "pressure_bar": 19.2,
 "flow_rate_l_min": 476.0, "fault_codes": ["VIB-HIGH"]}
```

### 5. Unknown asset
```json
{"asset_id": "PHANTOM-99", "timestamp": "2026-06-13T08:00:00Z",
 "vibration_mm_s": 3.5, "temperature_c": 55.0, "pressure_bar": 6.0,
 "flow_rate_l_min": null, "fault_codes": []}
```

### 6. NLP operator report (use the Operator Report tab)
```
The pump on well pad 3 MOL-PUMP-001 has been making a grinding noise since
yesterday. Bearing temp is reading around 83 degrees and vibration feels rough.
Fault codes showing VIB-HIGH and BRG-TEMP-WARN.
```

---

## HITL Badge Authorisation

Human-In-The-Loop decisions require a badge ID. Configured in `guardrails.yaml`:

| Badge | Authorisation |
|---|---|
| `001` | All decisions — CRITICAL and standard |
| `002` | Standard decisions (LOW / MEDIUM / HIGH) |
| `003` | Standard decisions (LOW / MEDIUM / HIGH) |

CRITICAL work orders can only be approved or rejected by badge `001`. Any other badge is blocked. Empty badge is always rejected.

---

## Deployment (Render Free Tier)

1. Push your code to GitHub
2. Create a **Web Service** on Render, connect the repo
3. Set **Build Command**: `pip install -r requirements.txt`
4. Set **Start Command**: `uvicorn api:app --host 0.0.0.0 --port $PORT`
5. Add environment variable: `OPENROUTER_API_KEY` = your key
6. Deploy

The SQLite database is ephemeral on Render's free tier — it reseeds cleanly on every restart via `bootstrap_db()`. The audit log (`audit.jsonl`) is also ephemeral by design.

---

## Project Structure

```
pdm-harness/
├── api.py                  # FastAPI app — routes, HTMX handlers, HITL badge checks
├── run.py                  # CLI entry point
├── guardrails.yaml         # Declarative operational limits
├── requirements.txt
├── Procfile                # Render start command
├── .env.example
│
├── harness/
│   ├── engine.py           # Orchestration loop (state machine)
│   ├── material.py         # Pydantic v2 schemas — SCADAReading, AgentDiagnosis, WorkOrderRequest
│   ├── tools.py            # SQL checkpoints — check_inventory, check_active_tickets, create_work_order
│   ├── observability.py    # Append-only audit.jsonl writer
│   ├── agent_interface.py  # AgentProtocol + OpenRouterAgent + RuleBasedAgent
│   ├── alarms.py           # Named structured alarm events
│   └── hitl.py             # CLI HITL review flow
│
└── templates/
    ├── console.html         # Three-column operator console
    ├── _result.html         # Pipeline + work order panel (HTMX target)
    ├── _decisions.html      # Recent decisions sidebar
    ├── _audit.html          # Audit log modal
    └── _workorders.html     # Work order calendar modal
```

---

## Further Reading

- [HARNESS.md](HARNESS.md) — Developer reference: guardrails, checkpoints, agent interface, how to extend
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — Full architecture specification with data flow
- [guardrails.yaml](guardrails.yaml) — Operational limits (edit to tighten constraints without touching code)

---

## License

MIT — see [LICENSE](LICENSE).
