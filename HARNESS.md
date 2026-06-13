# Semantic Air-Gap Harness — Developer Reference

## What This Is

A deterministic Python harness that encapsulates a non-deterministic AI agent for industrial predictive maintenance. The harness physically prevents the agent from writing to any ERP/CMMS system until its output passes every schema contract, SQL checkpoint, and YAML guardrail.

**The agent proposes. The harness decides.**

---

## The Five Pillars

| Pillar | File | Role |
|---|---|---|
| **Schema** | [harness/material.py](harness/material.py) | Pydantic v2 contracts for all I/O. Garbage-in is rejected before any agent sees it. |
| **Engine** | [harness/engine.py](harness/engine.py) | Synchronous state machine — the orchestration loop. |
| **Checkpoints** | [harness/tools.py](harness/tools.py) | SQL queries returning binary PASS/FAIL. No semantic search. |
| **Guardrails** | [guardrails.yaml](guardrails.yaml) | Declarative hard limits. Tighten any limit without touching Python. |
| **Observability** | [harness/observability.py](harness/observability.py) | Append-only `audit.jsonl` forensic trail. |

Supporting files:

| File | Role |
|---|---|
| [harness/agent_interface.py](harness/agent_interface.py) | `AgentProtocol` + two swappable implementations |
| [harness/alarms.py](harness/alarms.py) | Named structured alarm events |
| [harness/hitl.py](harness/hitl.py) | CLI HITL review flow (used by `run.py`) |
| [api.py](api.py) | FastAPI web console — routes, HTMX handlers, badge HITL enforcement |

---

## State Machine

Every run transitions through these phases in `EngineState.phase`:

```
INTAKE ──► INFERENCE ──► (ERP LOOKUP) ──► VALIDATION ──► APPROVED
               │                               │
           (parse error,                  (tool or guardrail failure)
            retry up to                        │
            max_iterations)               BLOCKED ──► HITL_APPROVED
                                                  └──► HITL_REJECTED

ERROR  (unrecoverable — SCADA validation failure, API error, asset not found)
```

**INTAKE** — `SCADAReading(**raw)` enforces physical bounds (e.g. temperature ≥ −40°C). `check_asset_exists` runs as a fail-fast before any LLM call.

**INFERENCE** — Agent receives SCADA telemetry only (no part numbers, no prices). Returns `AgentDiagnosis` (7-value `fault_category` Literal, priority, shutdown flag, confidence). Self-correction loop retries only on JSON parse or schema errors, up to `max_llm_iterations`.

**ERP LOOKUP** — Harness queries `maintenance_bom` using `fault_category` as the key. `select_parts_for_fault()` returns `{part_number: qty}`. `compute_order_cost()` multiplies by `inventory.unit_cost_usd` and adds the configured labour charge. The agent never sees or influences this step.

**VALIDATION** — `check_active_tickets` blocks if an OPEN/IN_PROGRESS ticket already exists for the asset (duplicate prevention). `check_inventory` verifies parts are in the approved catalogue and in stock; auto-raises a purchase order for approved-but-out-of-stock parts.

**GUARDRAILS** — `enforce_guardrails()` applies all rules from `guardrails.yaml`. Any failure adds a human-readable reason to `all_failures`. ERP and guardrail failures both lead to BLOCKED — neither retries the agent (they require operator action, not model correction).

**APPROVED** — `create_work_order(draft, run_id)` persists an OPEN ticket to the `work_orders` table. This is what causes subsequent dispatches on the same asset to be blocked by `check_active_tickets`.

**BLOCKED** — The web UI presents the last draft and all failure reasons to a human operator. HITL controls require a valid badge ID.

---

## Alarm Types

All alarms are appended to `audit.jsonl` with `"event": "ALARM"`.

| Alarm | Severity | Trigger |
|---|---|---|
| `GUARDRAIL_BREACH` | WARNING | Any `guardrails.yaml` rule fired |
| `DUPLICATE_TICKET` | WARNING | Active OPEN/IN_PROGRESS WO on this asset |
| `UNKNOWN_ASSET` | CRITICAL | Asset ID not in ERP register |
| `INVENTORY_SHORTAGE` | WARNING | Approved part is out of stock — PO auto-raised |
| `PART_NOT_APPROVED` | CRITICAL | Part not in approved catalogue for this asset |
| `LLM_PARSE_FAILURE` | WARNING | Agent returned non-JSON or schema-invalid output |
| `LLM_API_ERROR` | CRITICAL | HTTP error from OpenRouter API |
| `VALIDATION_EXHAUSTED` | CRITICAL | All `max_llm_iterations` used with no passing draft |
| `HITL_ESCALATION` | CRITICAL | Run routed to human operator for binding decision |

---

## Swappable Agent Interface

```python
class AgentProtocol(Protocol):
    def generate(
        self,
        messages: List[Dict[str, str]],
        model: str,
    ) -> tuple[str, int, int]:
        """Returns (content, prompt_tokens, completion_tokens)."""
```

| Class | Description |
|---|---|
| `OpenRouterAgent` | Production. Calls OpenRouter via httpx. Requires `OPENROUTER_API_KEY`. |
| `RuleBasedAgent` | Deterministic. ISO 10816-3 vibration zones + API 670 temperature limits. No API key. Structured JSON input only — cannot extract from prose. |

The UI header selects between three modes:

| Button | Agent | Model |
|---|---|---|
| GPT-OSS-20B | OpenRouterAgent | `openai/gpt-oss-20b:free` |
| Qwen3-Coder | OpenRouterAgent | `qwen/qwen3-coder:free` |
| Offline | RuleBasedAgent | N/A |

Add your own agent by implementing `AgentProtocol`:

```python
class MyAgent:
    def generate(self, messages, model):
        return json.dumps({"fault_category": "LUBRICATION", ...}), 0, 0

state = run(raw_scada, agent=MyAgent(), model="")
```

---

## NLP Operator Report Path

The `POST /dispatch/nlp` route accepts unstructured operator prose. The harness uses the agent as a semantic bridge to extract structured SCADA fields before entering the main validation loop:

```python
def _parse_unstructured_to_scada(
    raw_text: str, agent: AgentProtocol, model: str
) -> Dict[str, Any]:
```

The system prompt instructs the LLM to extract `asset_id`, `vibration_mm_s`, `temperature_c`, `pressure_bar`, `flow_rate_l_min`, and `fault_codes` from the prose. Missing values are filled with physically plausible defaults. The original prose is then passed as `operator_notes` to `engine.run()` and appended to the diagnosis prompt — the agent can factor in field context that sensor readings alone don't capture.

The UI response includes a purple "NLP → SCADA Extraction" box showing exactly what was inferred, so the operator can catch hallucinated values before approving.

**Note:** `RuleBasedAgent` does not support prose extraction. Use structured JSON or switch to an LLM model when using the Operator Report tab.

---

## HITL Badge Guardrail

Configured in `guardrails.yaml` under `hitl`:

```yaml
hitl:
  critical_approver_badges:
    - "001"
  standard_approver_badges:
    - "001"
    - "002"
    - "003"
```

Enforced by `_check_badge()` in `api.py` at the top of all three HITL handlers (`/approve`, `/reject`, `/edit`):

- Empty badge → always blocked (no anonymous decisions)
- CRITICAL priority → badge must be in `critical_approver_badges`
- All other priorities → badge must be in `standard_approver_badges`
- Badge `001` is a superuser — authorised for all decisions

To add a new badge: edit `guardrails.yaml` only. No Python change needed.

---

## How to Add a Guardrail

**Step 1 — Add the limit to `guardrails.yaml`:**

```yaml
approval:
  max_approval_usd: 1000.0
  max_estimated_days: 14   # <-- new
```

**Step 2 — Add a check block to `enforce_guardrails()` in `engine.py`:**

```python
max_days = approval.get("max_estimated_days", 14)
if draft.estimated_days > max_days:
    detail = f"estimated_days {draft.estimated_days} exceeds max {max_days}"
    obs.log_guardrail_block(run_id, "max_estimated_days", detail, draft.estimated_days)
    alarms.fire_alarm(alarms.guardrail_breach(
        run_id, draft.asset_id, "max_estimated_days", detail, draft.estimated_days
    ))
    failures.append(f"Duration guardrail: {detail}.")
```

**Step 3 — Add the field to `WorkOrderRequest` in `material.py` if it's a new work order field.**

The self-correction loop will automatically inject the new failure reason into the agent's next prompt on retry.

---

## How to Add a Checkpoint

Checkpoints are SQL queries in `tools.py` that return a `ValidationResult`.

**Step 1 — Write the function:**

```python
def check_my_constraint(asset_id: str, run_id: str) -> ValidationResult:
    """Prevents <describe what this blocks>."""
    conn = _get_connection()
    row = conn.execute("SELECT ... FROM ... WHERE asset_id = ?", (asset_id,)).fetchone()
    conn.close()

    if row is None or <failure_condition>:
        alarms.fire_alarm(alarms.guardrail_breach(
            run_id, asset_id, "my_constraint", "detail", value
        ))
        return ValidationResult(
            passed=False,
            check_name="check_my_constraint",
            detail="Human-readable failure reason.",
            blocking=True,
        )

    return ValidationResult(
        passed=True,
        check_name="check_my_constraint",
        detail="Constraint satisfied.",
        blocking=True,
    )
```

**Step 2 — Register it in `run_all_checkpoints()`:**

```python
def run_all_checkpoints(asset_id, part_numbers, run_id):
    return [
        check_asset_exists(asset_id, run_id),
        check_active_tickets(asset_id, run_id),
        check_inventory(part_numbers, asset_id, run_id),
        check_my_constraint(asset_id, run_id),   # <-- add here
    ]
```

No changes needed in `engine.py` — the engine iterates `tool_results` generically.

---

## Quick Start

### Web console (recommended)

```bash
cp .env.example .env          # set OPENROUTER_API_KEY
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn api:app --reload
# Open http://localhost:8000
```

### CLI

```bash
python run.py                        # OpenRouter LLM (requires API key)
AGENT_TYPE=rule-based python run.py  # Deterministic, no API call
```

---

## Audit Log

Every state transition, tool result, guardrail block, alarm, PO, and human decision is appended to `audit.jsonl`. Never truncated; only appended.

```bash
# Pretty-print last 10 events
tail -10 audit.jsonl | jq .

# Show all alarms
grep '"event": "ALARM"' audit.jsonl | jq .

# Show all approved work orders
grep '"event": "WORK_ORDER_APPROVED"' audit.jsonl | jq .

# Show all guardrail blocks
grep '"event": "GUARDRAIL_BLOCK"' audit.jsonl | jq .
```

---

## Asset Model

The bundled database models an oil gathering station pump train and supporting equipment:

| Asset | Type | Key Specs |
|---|---|---|
| `MOL-PUMP-001` | Centrifugal pump (Flowserve PVXM 12×10-17) | 320 m³/h · 185 m · 200 kW · API 610 OH2 |
| `MOL-MOTOR-001` | Drive motor (WEG W22) | 200 kW · 6 kV · IE3 |
| `MOL-COUP-001` | Disc coupling (Rexnord Thomas 710) | Flexible disc pack |
| `PUMP-042` | Feed pump (Sulzer CPT-40-200) | 95 m³/h · 62 m · 18.5 kW |
| `COMP-017` | Air compressor (Atlas Copco GA55+ VSD) | 55 kW |
| `FAN-008` | Cooling fan (Howden VAH-1400-6P) | 7.5 kW |

**Inventory:** 30 spare parts across 8 categories (bearings, seals, wear parts, gaskets, coupling, instruments, lubrication, hardware). All SKF, John Crane, Flowserve, Rexnord, Bently Nevada, and other OEM part numbers are realistic and cross-referenced against the asset compatibility table.

**Maintenance BOM:** Covers 7 fault categories × 6 assets. `ROUTINE_INSPECTION` carries no BOM entries — cost is labour only.

**Pre-seeded work order:** `WO-MOL-2026-003` (OPEN, HIGH priority) on `MOL-PUMP-001` — demonstrates the duplicate-ticket guardrail on any new dispatch to that asset.
