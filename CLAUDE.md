# HARNESS ARCHITECTURE: SEMANTIC AIR-GAP
**Project:** Predictive Maintenance Orchestrator 
**Role:** You are an expert Industrial Systems & Staff Python Software Engineer.

## 1. Project Philosophy
This project is a "Semantic Air-Gap"—a deterministic software harness that encapsulates a non-deterministic AI agent. It allows an LLM to read industrial telemetry and draft work orders, but physically blocks the AI from executing actions until its math and logic are verified against rigid rules and local database states. 

**DO NOT** use opaque multi-agent frameworks (LangGraph, CrewAI, AutoGen). This must be a transparent, observable state machine built in pure Python.

## 2. Technical Stack & Conventions
* **Language:** Pure Python 3.12+ 
* **Type Hinting:** Strict type hinting is REQUIRED on all functions (`typing.Dict`, `typing.List`, `typing.Optional`).
* **Validation:** `Pydantic v2` for strict JSON schema enforcement on all inputs/outputs.
* **State Grounding:** Local `sqlite3` database simulating ERP relational tables.
* **Configuration:** `PyYAML` for decoupled declarative guardrails.
* **Inference Client:** `httpx` connecting to the OpenRouter API.

## 3. Core Architecture Blueprint
When scaffolding or modifying the codebase, adhere to this strict separation of concerns across files:
1. **`engine.py` (The Loop):** A synchronous `while` loop that controls the request lifecycle, intercepts LLM text, and feeds validation errors back to the LLM for self-correction.
2. **`material.py` (The Schema):** Rigid Pydantic v2 models for incoming SCADA data and outgoing Work Order JSONs.
3. **`tools.py` (The Checkpoints):** Relational SQL queries (`check_inventory`, `check_active_tickets`) that return hard binary PASS/FAIL states. Do NOT use semantic/vector search here.
4. **`guardrails.yaml` (The Boundaries):** Static operational limits (e.g., `max_approval_usd: 1000`).
5. **`observability.py` (The Audit Trail):** Appends all state transitions, tool failures, and guardrail blocks to an immutable `.jsonl` log file.

## 4. Coding Rules
* **No "Magic":** Write explicit, readable code. Avoid deep inheritance or complex decorators unless absolutely necessary.
* **Error Handling:** Pydantic validation errors and SQLite constraint failures must be caught cleanly and returned as structured strings so the `engine.py` loop can inject them back into the LLM's prompt window.
* **Comments:** Leave brief, professional docstrings on core classes and loop functions explaining *why* a constraint is placed, using industrial terminology (e.g., "Prevents duplicate work orders on the same asset").