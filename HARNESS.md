# Semantic Air-Gap Harness

## What This Is

A deterministic Python harness that encapsulates a non-deterministic agent (LLM or rule-based) for industrial predictive maintenance. The harness physically prevents the agent from writing to any ERP/CMMS system until its output passes every checkpoint, guardrail, and schema contract defined in the system.

The agent proposes. The harness decides.

---

## The Four Pillars

| Pillar | File | What It Does |
|---|---|---|
| **Schema** | [material.py](material.py) | Pydantic v2 contracts for all inputs and outputs. Garbage-in is rejected here before any agent sees it. |
| **Checkpoints** | [tools.py](tools.py) | SQL queries against the live ERP/CMMS database. Binary PASS/FAIL per constraint. |
| **Guardrails** | [guardrails.yaml](guardrails.yaml) | Declarative config-driven hard limits (cost ceiling, part count, shutdown restriction). No code change needed to tighten a limit. |
| **Alarms** | [alarms.py](alarms.py) | Named structured events emitted whenever the harness blocks, escalates, or detects a failure. Written to `audit.jsonl` and printed to the operator terminal. |

Supporting infrastructure:

| File | Role |
|---|---|
| [engine.py](engine.py) | Synchronous state machine — the orchestration loop |
| [observability.py](observability.py) | Append-only `audit.jsonl` forensic trail |
| [hitl.py](hitl.py) | Human-In-The-Loop review layer for blocked work orders |
| [agent_interface.py](agent_interface.py) | `AgentProtocol` + two swappable implementations |

---

## State Machine

Every run transitions through these phases:

```
INTAKE ──► INFERENCE ──► VALIDATION ──► APPROVED
                │               │
                │          (failure)
                │               │
                └─ (retry, up to max_llm_iterations)
                                │
                           BLOCKED ──► HITL_REVIEW ──► HITL_APPROVED
                                                   └──► HITL_REJECTED
ERROR (abort on unrecoverable fault)
```

**INTAKE** — Raw SCADA telemetry is validated by `SCADAReading` (Pydantic). Physical bounds are enforced (e.g. temperature ge −40°C le 300°C). Any invalid reading raises `ERROR` immediately.

**INFERENCE** — The agent receives a structured prompt containing the SCADA values and the approved parts catalogue for this specific asset. It returns a work order JSON.

**VALIDATION** — Three SQL checkpoints run in sequence, then all guardrail rules:
1. `check_asset_exists` — is this asset in the ERP register?
2. `check_active_tickets` — is there already an open work order on this asset?
3. `check_inventory` — are all required parts in the approved catalogue and in stock?

If all pass → `APPROVED`. If any fail → correction context is injected back and the loop repeats.

**BLOCKED** — All `max_llm_iterations` consumed without a passing draft. The run is frozen and routed to HITL.

**HITL_REVIEW** — Human operator is presented with the last draft and all failure reasons. They choose APPROVE / REJECT / EDIT. The EDIT path re-runs all checkpoints on the corrected draft before approving.

---

## Alarm Types

All alarms are written to `audit.jsonl` with `"event": "ALARM"` and printed to the terminal in a visually distinct block.

| Alarm Type | Severity | Trigger |
|---|---|---|
| `GUARDRAIL_BREACH` | WARNING | A `guardrails.yaml` rule fired (cost, priority, shutdown, parts count, safety prefix) |
| `DUPLICATE_TICKET` | WARNING | Active `OPEN` or `IN_PROGRESS` work order already exists for this asset |
| `UNKNOWN_ASSET` | CRITICAL | Asset ID not found in the ERP/CMMS register |
| `INVENTORY_SHORTAGE` | WARNING | Part is approved for this asset but quantity is zero — PO auto-raised |
| `PART_NOT_APPROVED` | CRITICAL | Agent used a part number not in the approved catalogue for this asset |
| `LLM_PARSE_FAILURE` | WARNING | Agent returned non-JSON or schema-invalid output (self-correction will retry) |
| `LLM_API_ERROR` | CRITICAL | HTTP error from the OpenRouter API |
| `VALIDATION_EXHAUSTED` | CRITICAL | All `max_llm_iterations` used without a passing draft |
| `HITL_ESCALATION` | CRITICAL | Run routed to human operator for binding decision |

---

## Swappable Agent Interface

The harness decouples the agent from the validation pipeline via `AgentProtocol`:

```python
class AgentProtocol(Protocol):
    def generate(
        self,
        messages: List[Dict[str, str]],
        model: str,
    ) -> tuple[str, int, int]:
        """Returns (content, prompt_tokens, completion_tokens)."""
```

**Provided implementations:**

| Class | File | Description |
|---|---|---|
| `OpenRouterAgent` | [agent_interface.py](agent_interface.py) | Production. Calls OpenRouter API via httpx. Requires `OPENROUTER_API_KEY`. |
| `RuleBasedAgent` | [agent_interface.py](agent_interface.py) | Deterministic. Applies ISO 10816-3 vibration zones and API 670 temperature limits directly. No API key, no network call. Requires structured JSON input. |

**Swap at runtime via environment variable:**

```bash
# Default — LLM via OpenRouter
python engine.py

# Deterministic rule engine (no API call, no cost)
AGENT_TYPE=rule-based python engine.py
```

**Add your own agent** by implementing `AgentProtocol` and passing an instance:

```python
from engine import run_harness_loop
from agent_interface import AgentProtocol

class MyCustomAgent:
    def generate(self, messages, model):
        # your logic here
        return json.dumps(my_work_order), 0, 0

state = run_harness_loop(raw_input, agent=MyCustomAgent())
```

---

## How to Add a Guardrail

Guardrails are declared in [guardrails.yaml](guardrails.yaml) and enforced in `engine.enforce_guardrails()`.

**Step 1 — Add to `guardrails.yaml`:**

```yaml
approval:
  max_approval_usd: 1000.0
  # add your new limit here, e.g.:
  max_estimated_days: 14
```

**Step 2 — Add a check block to `enforce_guardrails()` in [engine.py](engine.py):**

```python
max_days = approval.get("max_estimated_days", 14)
if draft.estimated_days > max_days:
    detail = f"estimated_days {draft.estimated_days} exceeds max {max_days}"
    obs.log_guardrail_block(run_id, "max_estimated_days", detail, draft.estimated_days)
    alarms.fire_alarm(alarms.guardrail_breach(run_id, draft.asset_id, "max_estimated_days", detail, draft.estimated_days))
    failures.append(f"Duration guardrail: {detail}.")
```

**Step 3 — Add the field to `WorkOrderRequest` in [material.py](material.py) if needed.**

That's it. The self-correction loop will automatically inject the new failure reason back to the agent on the next iteration.

---

## How to Add a Checkpoint

Checkpoints are SQL queries in [tools.py](tools.py) that return a `ValidationResult`.

**Step 1 — Write the function:**

```python
def check_my_constraint(asset_id: str, run_id: str) -> ValidationResult:
    conn = _get_connection()
    row = conn.execute("SELECT ... FROM ... WHERE asset_id = ?", (asset_id,)).fetchone()
    conn.close()

    if row is None or <failure_condition>:
        alarms.fire_alarm(alarms.guardrail_breach(run_id, asset_id, "my_constraint", "detail", value))
        return ValidationResult(passed=False, check_name="check_my_constraint",
                                detail="Human-readable failure reason.", blocking=True)

    return ValidationResult(passed=True, check_name="check_my_constraint",
                            detail="Constraint satisfied.", blocking=True)
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

No changes needed in engine.py — the engine iterates `tool_results` generically.

---

## Quick Start

```bash
# 1. Copy environment template
cp .env.example .env
# Edit .env: set OPENROUTER_API_KEY

# 2. Create virtualenv and install dependencies
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Run the harness (seeds the database on first run)
python engine.py

# 4. Paste a JSON SCADA reading at the prompt:
# {"asset_id": "MOL-PUMP-001", "timestamp": "2026-06-13T06:00:00Z",
#  "vibration_mm_s": 8.4, "temperature_c": 82.1, "pressure_bar": 19.2,
#  "flow_rate_l_min": 476.0, "fault_codes": ["VIB-HIGH", "BRG-TEMP-WARN"]}

# 5. Run with the deterministic rule-based agent (no API key needed)
AGENT_TYPE=rule-based python engine.py
```

---

## Audit Log

Every state transition, tool result, guardrail block, alarm, purchase order, and human decision is appended to `audit.jsonl`. The log is never truncated or modified — only appended.

```bash
# Pretty-print last 10 events
tail -10 audit.jsonl | jq .

# Show all alarms from the last run
grep '"event": "ALARM"' audit.jsonl | jq .

# Show all approved work orders
grep '"event": "WORK_ORDER_APPROVED"' audit.jsonl | jq .
```

---

## Asset Model

The bundled database models a **Main Oil Line (MOL) Centrifugal Pump** — a Flowserve PVXM 12×10-17 API 610 OH2 process pump (200 kW / 6 kV) at Well Pad 3, Oil Gathering Station. The database includes:

- Full asset register with specs (rated flow 650 m³/h, head 120 m, power 200 kW)
- 20 inventory items across 6 categories (bearings, seals, couplings, sensors, filters, gaskets)
- 10 part compatibility records linking parts to the MOL-PUMP-001 asset
- 7 historical work orders (1 open — triggers duplicate-ticket guardrail)
- 4 purchase order records (pre-seeded for demo)
