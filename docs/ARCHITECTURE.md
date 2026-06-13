# Architecture Specification: Semantic Air-Gap
**Project:** Predictive Maintenance Orchestrator  
**Design Principle:** Absolute State Transparency via Deterministic Interlocks

---

## 1. Summary & Core Value
The **Predictive Maintenance Orchestrator** is a deterministic software harness designed to cage a non-deterministic AI agent. Traditional Condition-Based Maintenance (CBM) software relies on brittle, predefined `IF/THEN` rules that fail when confronted with unstructured human inputs or nuanced operator notes (e.g., "the pump sounds like grinding gravel"). 

This system uses a Large Language Model (LLM) as a semantic bridge to interpret messy, unstructured industrial telemetry and draft maintenance work orders. However, to prevent catastrophic failures like hallucinated part numbers or unauthorized expenditures, the **Harness** acts as a strict safety interlock. It physically blocks the AI from writing to enterprise systems until all logic, schemas, financial limits, and relational constraints are programmatically verified against local database states.

---

## 2. Technology Stack
The architecture explicitly rejects opaque multi-agent frameworks (such as LangGraph, CrewAI, or AutoGen) in favor of explicit, audible control logic:
* **Orchestrator:** Pure Python 3.12+ implementing a synchronous `while` loop state machine.
* **Validation:** `Pydantic v2` for strict, static JSON schema enforcement.
* **State Grounding:** Local embedded `sqlite3` simulating relational Enterprise Resource Planning (ERP) and Computerized Maintenance Management System (CMMS) database states.
* **Configuration:** `PyYAML` for decoupled, declarative guardrails that can be altered without altering core execution code.
* **Inference Client:** `httpx` connecting to the OpenRouter API (`openai/gpt-oss-20b` or `qwen/qwen3-coder`).

---

## 3. Execution Flow & State Machine
The system orchestrates a strict, unidirectional execution loop. If a constraint fails, the state rolls back, execution is blocked, and the failure context is fed back to the model for a bounded number of self-correction attempts.

```text
[Messy SCADA / Operator Notes]
             │
             ▼
+─────────────────────────────────────────────────────────────+
|                     HARNESS EVENT LOOP                      |
|                                                             |
|  1. INGEST & PARSE (material.py)                            |
|     Enforces strict Pydantic JSON schema structure.         |
|            │                                                |
|            ▼                                                |
|  2. EVALUATE REASONING (engine.py)                          |
|     LLM maps text to structured operational intents.        |
|            │                                                |
|            ▼                                [FAIL: Rollback |
|  3. ENFORCE GUARDRAILS (guardrails.yaml)    & Feed Error    |
|     Pre-execution check on costs & safety. ─── To Agent]    |
|            │                                       ▲        |
|            ▼                                       │        |
|  4. VERIFY TOOLS (tools.py)                        │        |
|     Validates inventory & checks duplicates. ──────┘        |
|            │                                                |
|            ▼ [PASSES ALL INTERLOCKS]                        |
+────────────│────────────────────────────────────────────────+
             ▼
 [Validated ERP Work Order Emitted]
             │
             ▼
 [OBSERVABILITY]: Appends transaction lifecycle to .jsonl audit trail.
 [HITL ESCALATION]: 3 consecutive loop failures trigger a CRITICAL freeze.