# Architecture Specification: Semantic Air-Gap
**Project:** Predictive Maintenance Orchestrator
**Design Principle:** Absolute State Transparency via Deterministic Interlocks

---

## 1. Core Concept

The **Semantic Air-Gap** is a boundary between a non-deterministic AI agent and a deterministic enterprise system. The LLM is permitted to reason about telemetry and classify faults. It is structurally prevented from producing part numbers, costs, or ERP-specific data — those fields do not exist in its output schema. The harness performs all ERP interactions.

This contrasts with naive LLM integrations that pass a work order template to the model and trust it to fill in part numbers and prices. Those systems fail when the model hallucinates a part code that doesn't exist in the warehouse or estimates a cost that breaches a financial control.

---

## 2. Technology Stack

| Component | Technology | Rationale |
|---|---|---|
| Web UI | FastAPI + HTMX + Jinja2 | No JS framework, no build step; all state lives server-side |
| Schema enforcement | Pydantic v2 | Strict field-level validation on every input and output boundary |
| ERP state | SQLite via `sqlite3` | Embedded relational DB; simulates assets, inventory, BOM, work orders |
| Guardrails config | PyYAML | Operational limits that operators can tighten without touching Python |
| LLM client | `httpx` → OpenRouter | Synchronous HTTP; avoids framework coupling |
| Async isolation | `ThreadPoolExecutor` | Engine runs synchronously in a thread pool; FastAPI event loop stays free |
| Observability | Append-only `.jsonl` | Immutable forensic audit trail; every state transition is written |

**Explicitly rejected:** LangGraph, CrewAI, AutoGen, and any opaque multi-agent framework. The system is a transparent `while` loop — every decision path is readable in `engine.py`.

---

## 3. Five-Layer Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  LAYER 1: PRESENTATION                                           │
│  FastAPI + HTMX + Jinja2 operator console (api.py)              │
│  Three-column dark UI · SCADA form · NLP tab · HITL panel       │
└────────────────────────────┬─────────────────────────────────────┘
                             │ POST /dispatch or POST /dispatch/nlp
┌────────────────────────────▼─────────────────────────────────────┐
│  LAYER 2: SCHEMA (material.py)                                   │
│  SCADAReading — physical plausibility bounds on all sensor fields│
│  AgentDiagnosis — narrow LLM output (7-value fault Literal only) │
│  WorkOrderRequest — harness-built artefact (no agent input)      │
│  EngineState — serialisable snapshot at every phase transition   │
└────────────────────────────┬─────────────────────────────────────┘
                             │
┌────────────────────────────▼─────────────────────────────────────┐
│  LAYER 3: ENGINE (engine.py)                                     │
│  Synchronous state machine · phases: INTAKE → INFERENCE →        │
│  ERP LOOKUP → VALIDATION → APPROVED | BLOCKED                    │
│  Self-correction loop (max_llm_iterations) for parse errors only │
│  NLP extraction path (_parse_unstructured_to_scada)              │
└──────────────┬─────────────────────────────┬────────────────────┘
               │                             │
┌──────────────▼──────────┐  ┌──────────────▼──────────────────────┐
│  LAYER 4a: TOOLS        │  │  LAYER 4b: GUARDRAILS               │
│  (tools.py)             │  │  (guardrails.yaml + engine.py)       │
│  SQL checkpoint queries │  │  Cost ceiling · Priority gate        │
│  check_asset_exists     │  │  Shutdown restriction                │
│  check_active_tickets   │  │  Part count limit                    │
│  check_inventory        │  │  Safety-critical asset prefixes      │
│  create_work_order      │  │  Declarative — no code change needed │
│  compute_order_cost     │  │  to tighten a limit                  │
│  select_parts_for_fault │  └─────────────────────────────────────┘
└──────────────┬──────────┘
               │
┌──────────────▼─────────────────────────────────────────────────┐
│  LAYER 5: OBSERVABILITY (observability.py)                     │
│  Append-only audit.jsonl · every phase, tool result, approval  │
│  LLM exchange, guardrail block, alarm, PO, HITL decision       │
└────────────────────────────────────────────────────────────────┘
```

---

## 4. Execution Flow

### 4a. Structured SCADA path (primary)

```
Operator fills SCADA form
        │
        ▼
POST /dispatch
        │
        ▼ ThreadPoolExecutor
   ┌─────────────────────────────────────────────────────────┐
   │  engine.run()                                           │
   │                                                         │
   │  1. INTAKE                                              │
   │     SCADAReading(**raw) — Pydantic validates bounds     │
   │     check_asset_exists — fail-fast before LLM call      │
   │                                                         │
   │  2. INFERENCE (up to max_llm_iterations)                │
   │     agent.generate(messages, model)                     │
   │     → AgentDiagnosis (fault_category + 5 other fields)  │
   │     Self-corrects only on JSON parse / schema errors    │
   │     ERP failures never re-prompt the agent              │
   │                                                         │
   │  3. ERP LOOKUP                                          │
   │     select_parts_for_fault(asset_id, fault_category)    │
   │     → {part_number: qty} from maintenance_bom table     │
   │     compute_order_cost(parts_qty, labor_usd)            │
   │     → total USD from inventory.unit_cost_usd × qty      │
   │                                                         │
   │  4. VALIDATION                                          │
   │     check_active_tickets — duplicate prevention         │
   │     check_inventory — catalogue + stock check + auto PO │
   │                                                         │
   │  5. GUARDRAILS                                          │
   │     enforce_guardrails(draft, rules, run_id)            │
   │     Cost ceiling · priority gate · shutdown · parts     │
   │                                                         │
   │  6. APPROVED → create_work_order() → OPEN in DB         │
   │     or                                                  │
   │     BLOCKED → return state for HITL                     │
   └─────────────────────────────────────────────────────────┘
        │
        ▼
HTMX OOB response updates all three panels simultaneously
(pipeline panel + work order panel + decisions panel)
```

### 4b. NLP operator report path

```
Operator types free-text ("the pump sounds like grinding gravel")
        │
        ▼
POST /dispatch/nlp
        │
        ▼
_parse_unstructured_to_scada(raw_text, agent, model)
  LLM extracts: asset_id, vibration_mm_s, temperature_c,
                pressure_bar, flow_rate_l_min, fault_codes
        │
        ▼
engine.run(raw_scada, agent, model, operator_notes=raw_text)
  (same validation pipeline as structured path)
        │
        ▼
_result.html includes purple NLP extraction box
showing exactly what the LLM inferred from the prose
```

### 4c. HITL decision path

```
state.phase == "BLOCKED"
        │
        ▼
Operator console shows work order draft + failure reasons
Operator enters badge ID in the badge input field

POST /hitl/{run_id}/approve  (or /reject or /edit)
        │
        ▼
_check_badge(priority, operator_id, guardrails)
  CRITICAL → must be in hitl.critical_approver_badges
  Others  → must be in hitl.standard_approver_badges
  Empty   → always rejected
        │
        ▼ (if authorised)
APPROVE: create_work_order(draft, run_id) → OPEN in DB
         state.phase = "HITL_APPROVED"
REJECT:  state.phase = "HITL_REJECTED"  (no DB write)
EDIT:    Pydantic re-validates corrected JSON → create_work_order
         state.phase = "HITL_APPROVED"
```

---

## 5. The Air-Gap in Detail

The hallucination prevention mechanism is structural, not prompt-based:

```python
class AgentDiagnosis(BaseModel):
    fault_category: Literal[
        "BEARING_WEAR", "SEAL_FAILURE", "IMPELLER_WEAR",
        "COUPLING_FAULT", "VIBRATION_SENSOR", "LUBRICATION",
        "ROUTINE_INSPECTION",
    ]
    fault_description: str   # plain English, 10–500 chars
    recommended_action: str  # plain English, 10–500 chars
    priority: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    requires_shutdown: bool
    confidence_score: float  # 0.0–1.0
```

The schema has no `part_number`, `cost`, or `catalogue_code` fields. The model cannot hallucinate what it has no field to write to.

After the diagnosis, the harness queries `maintenance_bom` using only the `fault_category` as a lookup key and computes costs from `inventory.unit_cost_usd`. Neither table is ever shown to the model.

---

## 6. ERP Data Model

Six SQLite tables simulate a minimal ERP + CMMS:

```
assets              — asset register (ID, name, location, specs, criticality)
inventory           — spare parts (part_number, qty, unit_cost_usd, min_stock)
part_compatibility  — approved parts per asset (many-to-many)
maintenance_bom     — (asset_id, fault_category) → [part_number, qty] mapping
work_orders         — ticket register (status: OPEN | IN_PROGRESS | CLOSED)
purchase_orders     — auto-raised POs for out-of-stock approved parts
```

### Seeded assets

| Asset ID | Description | Criticality |
|---|---|---|
| `MOL-PUMP-001` | Main Oil Line centrifugal pump (Flowserve PVXM, 200 kW) | A |
| `MOL-MOTOR-001` | 200 kW 6 kV pump drive motor (WEG W22) | A |
| `MOL-COUP-001` | Pump-motor flexible disc coupling (Rexnord Thomas 710) | A |
| `PUMP-042` | Centrifugal feed pump (Sulzer CPT-40-200, 18.5 kW) | B |
| `COMP-017` | Instrument air compressor (Atlas Copco GA55+ VSD) | B |
| `FAN-008` | Cooling tower fan (Howden VAH-1400-6P) | C |

`MOL-PUMP-001` has one `OPEN` work order pre-seeded (`WO-MOL-2026-003`) to demonstrate the duplicate-ticket guardrail. Any new dispatch to this asset will be blocked until that ticket is resolved.

---

## 7. Guardrail Catalogue

All limits live in `guardrails.yaml`. No code change is needed to adjust a threshold.

| Guardrail | Default | Triggered When |
|---|---|---|
| `max_approval_usd` | $1,000 | `estimated_cost_usd` exceeds ceiling |
| `auto_approvable_priorities` | LOW · MEDIUM · HIGH | Priority is CRITICAL |
| `min_confidence_score` | 0.75 | Agent confidence below threshold |
| `allow_auto_approve_shutdown` | false | `requires_shutdown` is true |
| `max_parts_per_order` | 5 | BOM lookup returns more than 5 parts |
| `safety_critical_prefixes` | SAFETY- · EMERG- · FIRE- | Asset ID matches prefix |
| `hitl.critical_approver_badges` | ["001"] | CRITICAL work order HITL decision |
| `hitl.standard_approver_badges` | ["001","002","003"] | Non-CRITICAL HITL decision |

---

## 8. Agent Implementations

Both agents implement `AgentProtocol`:

```python
class AgentProtocol(Protocol):
    def generate(
        self,
        messages: List[Dict[str, str]],
        model: str,
    ) -> tuple[str, int, int]:
        """Returns (content, prompt_tokens, completion_tokens)."""
```

**OpenRouterAgent** — production path. Calls `https://openrouter.ai/api/v1/chat/completions` via `httpx`. Requires `OPENROUTER_API_KEY`. Supports any model available on OpenRouter; `openai/gpt-oss-20b:free` and `qwen/qwen3-coder:free` are wired to the UI preset buttons.

**RuleBasedAgent** — deterministic path. Applies ISO 10816-3 vibration severity zones and API 670 temperature alarm limits directly in Python. No API key, no network call, no token cost. Requires structured JSON input (cannot extract from prose). Selected in UI via the "Offline" model button.

The rule-based agent returns `(0, 0)` for token counts since no LLM call is made. The token metrics display in the UI will show zeroes for offline runs.

---

## 9. Observability

Every event in the system lifecycle is appended to `audit.jsonl` as a newline-delimited JSON record. The file is never truncated or overwritten — only appended.

Event types logged:

| Event | Trigger |
|---|---|
| `STATE_TRANSITION` | Phase change in `EngineState` |
| `LLM_EXCHANGE` | Every user/assistant message pair |
| `TOOL_RESULT` | Each SQL checkpoint result |
| `GUARDRAIL_BLOCK` | Each guardrail that fires |
| `DIAGNOSIS` | Successful `AgentDiagnosis` parse |
| `WORK_ORDER_APPROVED` | Approved work order (auto or HITL) |
| `PURCHASE_ORDER_RAISED` | Auto-raised PO for out-of-stock part |
| `ERROR` | Any non-recoverable engine fault |
| `ALARM` | Named alarm events (see HARNESS.md) |

The web UI exposes a full audit log modal (button: "↓ View full audit log") and a work order calendar view (button: "↓ Work orders / calendar").

---

## 10. Concurrency Model

FastAPI runs in an `asyncio` event loop. The engine is synchronous (blocking SQLite calls + synchronous `httpx` calls). All `engine.run()` calls are dispatched to a `ThreadPoolExecutor(max_workers=4)` via `loop.run_in_executor()`. This means up to 4 concurrent pipeline runs without blocking the web server's event loop.

HITL state (`_states: Dict[str, EngineState]`) is an in-process dictionary — safe for single-instance Render free-tier deployment but not suitable for multi-replica horizontal scaling without an external state store.
