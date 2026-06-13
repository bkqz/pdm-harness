"""
observability.py — Audit trail layer (The Audit Trail)

Writes every state transition, tool result, guardrail block, and error to an
append-only .jsonl file. Each line is a self-contained JSON record.
The log is immutable by design: records are only ever appended, never updated
or deleted. This provides a forensic trace for every automated decision.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .material import EngineState, ValidationResult

# Always write to <project-root>/audit.jsonl regardless of CWD
LOG_PATH = Path(__file__).parent.parent / "audit.jsonl"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(record: Dict[str, Any]) -> None:
    """Atomically appends a single JSON record to the audit log."""
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def log_state_transition(state: EngineState) -> None:
    """
    Records a full engine state snapshot whenever the phase changes.
    Provides a complete replay trace for post-incident review.
    """
    _append({
        "event": "STATE_TRANSITION",
        "timestamp": _now_utc(),
        "run_id": state.run_id,
        "asset_id": state.asset_id,
        "iteration": state.iteration,
        "phase": state.phase,
        "llm_prompt_tokens": state.llm_prompt_tokens,
        "llm_completion_tokens": state.llm_completion_tokens,
        "error_message": state.error_message,
    })


def log_tool_result(run_id: str, result: ValidationResult) -> None:
    """
    Records the outcome of every tool checkpoint call.
    A FAIL here means the engine blocked a work order from advancing.
    """
    _append({
        "event": "TOOL_RESULT",
        "timestamp": _now_utc(),
        "run_id": run_id,
        "check_name": result.check_name,
        "passed": result.passed,
        "blocking": result.blocking,
        "detail": result.detail,
    })


def log_guardrail_block(run_id: str, rule_name: str, detail: str, value: Any) -> None:
    """
    Records when a static guardrail rule (from guardrails.yaml) prevents
    approval. This is distinct from tool failures — guardrails are config-driven
    hard limits, not database state checks.
    """
    _append({
        "event": "GUARDRAIL_BLOCK",
        "timestamp": _now_utc(),
        "run_id": run_id,
        "rule_name": rule_name,
        "detail": detail,
        "value_at_block": value,
    })


def log_diagnosis(run_id: str, asset_id: str, diagnosis_dict: Dict[str, Any]) -> None:
    """
    Records the agent's fault diagnosis before ERP lookup.
    Separating this from the final work order provides a clear audit trail
    showing exactly what the agent decided vs. what the harness computed.
    """
    _append({
        "event": "AGENT_DIAGNOSIS",
        "timestamp": _now_utc(),
        "run_id": run_id,
        "asset_id": asset_id,
        **diagnosis_dict,
    })


def log_llm_exchange(run_id: str, role: str, content: str, tokens: Optional[int] = None) -> None:
    """
    Records every message sent to or received from the LLM.
    This provides a full prompt-response audit trail for compliance review.
    """
    _append({
        "event": "LLM_EXCHANGE",
        "timestamp": _now_utc(),
        "run_id": run_id,
        "role": role,
        "content_preview": content[:300],
        "tokens": tokens,
    })


def log_approval(
    run_id: str,
    asset_id: str,
    estimated_cost_usd: float,
    ticket_id: Optional[str] = None,
) -> None:
    """Records a successful work order approval — the happy-path terminal event."""
    rec: Dict[str, Any] = {
        "event": "WORK_ORDER_APPROVED",
        "timestamp": _now_utc(),
        "run_id": run_id,
        "asset_id": asset_id,
        "estimated_cost_usd": estimated_cost_usd,
    }
    if ticket_id:
        rec["ticket_id"] = ticket_id
    _append(rec)


def log_purchase_order(
    run_id: str,
    po_id: str,
    part_number: str,
    asset_id: str,
    quantity: int,
    unit_cost_usd: float,
) -> None:
    """
    Records an automatically raised purchase order for an approved but out-of-stock part.
    This is a committed procurement record — the PO exists in the ERP database from this
    point regardless of whether the triggering work order is ultimately approved or rejected.
    """
    _append({
        "event": "PURCHASE_ORDER_RAISED",
        "timestamp": _now_utc(),
        "run_id": run_id,
        "po_id": po_id,
        "part_number": part_number,
        "asset_id": asset_id,
        "quantity": quantity,
        "unit_cost_usd": unit_cost_usd,
        "total_cost_usd": quantity * unit_cost_usd,
    })


def log_hitl_decision(run_id: str, decision: str, operator_notes: str) -> None:
    """
    Records the human operator's binding decision on a blocked work order.
    This entry is the legal authorisation record for any HITL_APPROVED order.
    """
    _append({
        "event": "HITL_DECISION",
        "timestamp": _now_utc(),
        "run_id": run_id,
        "decision": decision,
        "operator_notes": operator_notes,
    })


def log_error(run_id: str, error_type: str, detail: str) -> None:
    """Records unexpected errors that caused the engine to abort a run."""
    _append({
        "event": "ENGINE_ERROR",
        "timestamp": _now_utc(),
        "run_id": run_id,
        "error_type": error_type,
        "detail": detail,
    })


def log_alarm(alarm_dict: Dict[str, Any]) -> None:
    """
    Records a structured alarm event to the audit trail.
    Called by alarms.fire_alarm() — accepts a plain dict so observability.py
    does not need to import alarms.py (avoids circular dependency).
    """
    _append({
        "event": "ALARM",
        "timestamp": _now_utc(),
        **alarm_dict,
    })
