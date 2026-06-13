"""
alarms.py — Alarm subsystem

Named, structured alarms that fire whenever the harness detects a constraint
violation, system failure, or required human escalation. Each alarm carries:
  - alarm_type  : machine-readable name (e.g. GUARDRAIL_BREACH)
  - severity    : INFO | WARNING | CRITICAL
  - asset_id    : the asset under evaluation
  - run_id      : links back to the full run in audit.jsonl
  - context     : key-value detail bag — what exactly happened
  - recommended_action : plain-English operator instruction

Alarms are written to audit.jsonl AND printed to the terminal in a visually
distinct block so they are immediately visible during live operation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field

from . import observability as obs

# ---------------------------------------------------------------------------
# Alarm type constants — one per harness condition
# ---------------------------------------------------------------------------

GUARDRAIL_BREACH     = "GUARDRAIL_BREACH"
DUPLICATE_TICKET     = "DUPLICATE_TICKET"
UNKNOWN_ASSET        = "UNKNOWN_ASSET"
INVENTORY_SHORTAGE   = "INVENTORY_SHORTAGE"
PART_NOT_APPROVED    = "PART_NOT_APPROVED"
LLM_PARSE_FAILURE    = "LLM_PARSE_FAILURE"
LLM_API_ERROR        = "LLM_API_ERROR"
VALIDATION_EXHAUSTED = "VALIDATION_EXHAUSTED"
HITL_ESCALATION      = "HITL_ESCALATION"

AlarmSeverity = Literal["INFO", "WARNING", "CRITICAL"]

_SEVERITY_LABEL: Dict[str, str] = {
    "INFO":     "INFO    ",
    "WARNING":  "WARNING ",
    "CRITICAL": "CRITICAL",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Alarm(BaseModel):
    alarm_type: str
    severity: AlarmSeverity
    asset_id: str
    run_id: str
    context: Dict[str, Any]
    recommended_action: str
    timestamp: str = Field(default_factory=_utc_now)


# ---------------------------------------------------------------------------
# Emit — write to audit log and print to terminal
# ---------------------------------------------------------------------------

def fire_alarm(alarm: Alarm) -> None:
    """
    Logs the alarm to the append-only audit trail and prints a visually distinct
    block to the operator terminal. Both happen atomically so no alarm is silent.
    """
    obs.log_alarm(alarm.model_dump())

    label = _SEVERITY_LABEL.get(alarm.severity, alarm.severity)
    print(f"\n!! ALARM [{label}] {alarm.alarm_type}")
    print(f"   Asset  : {alarm.asset_id}")
    for key, value in alarm.context.items():
        print(f"   {key.capitalize():<10}: {value}")
    print(f"   Action : {alarm.recommended_action}")


# ---------------------------------------------------------------------------
# Constructor helpers — one factory function per alarm type
# ---------------------------------------------------------------------------

def guardrail_breach(
    run_id: str,
    asset_id: str,
    rule_name: str,
    detail: str,
    value: Any,
) -> Alarm:
    return Alarm(
        alarm_type=GUARDRAIL_BREACH,
        severity="WARNING",
        asset_id=asset_id,
        run_id=run_id,
        context={"rule": rule_name, "value": str(value), "detail": detail},
        recommended_action=(
            "Review guardrails.yaml limits. Escalate to maintenance manager "
            "or request HITL override if the limit is too conservative."
        ),
    )


def duplicate_ticket(
    run_id: str,
    asset_id: str,
    existing_tickets: str,
) -> Alarm:
    return Alarm(
        alarm_type=DUPLICATE_TICKET,
        severity="WARNING",
        asset_id=asset_id,
        run_id=run_id,
        context={"existing_tickets": existing_tickets},
        recommended_action=(
            "Update or close the existing ticket rather than creating a duplicate. "
            "Check CMMS for the current work order status."
        ),
    )


def unknown_asset(run_id: str, asset_id: str) -> Alarm:
    return Alarm(
        alarm_type=UNKNOWN_ASSET,
        severity="CRITICAL",
        asset_id=asset_id,
        run_id=run_id,
        context={"attempted_asset": asset_id},
        recommended_action=(
            "Verify the asset ID against the ERP register. "
            "Register the asset in the CMMS before raising a work order."
        ),
    )


def inventory_shortage(
    run_id: str,
    asset_id: str,
    part_number: str,
    description: str,
    po_id: str,
    unit_cost_usd: float,
) -> Alarm:
    return Alarm(
        alarm_type=INVENTORY_SHORTAGE,
        severity="WARNING",
        asset_id=asset_id,
        run_id=run_id,
        context={
            "part_number": part_number,
            "description": description,
            "po_raised": po_id,
            "unit_cost_usd": f"${unit_cost_usd:.2f}",
        },
        recommended_action=(
            f"Purchase order {po_id} auto-raised. "
            "Confirm with procurement and plan maintenance after delivery."
        ),
    )


def part_not_approved(
    run_id: str,
    asset_id: str,
    rejected_parts: List[str],
) -> Alarm:
    return Alarm(
        alarm_type=PART_NOT_APPROVED,
        severity="CRITICAL",
        asset_id=asset_id,
        run_id=run_id,
        context={"rejected_parts": ", ".join(rejected_parts)},
        recommended_action=(
            "Only use part numbers from the approved catalogue injected into the prompt. "
            "If a new part is genuinely needed, register it in the ERP first."
        ),
    )


def llm_parse_failure(run_id: str, asset_id: str, error: Exception) -> Alarm:
    return Alarm(
        alarm_type=LLM_PARSE_FAILURE,
        severity="WARNING",
        asset_id=asset_id,
        run_id=run_id,
        context={"error": str(error)[:200]},
        recommended_action=(
            "LLM returned non-JSON output. "
            "The self-correction loop will re-prompt. "
            "If this repeats, check model compatibility or simplify the system prompt."
        ),
    )


def llm_api_error(
    run_id: str,
    asset_id: str,
    status_code: int,
    error: str,
) -> Alarm:
    return Alarm(
        alarm_type=LLM_API_ERROR,
        severity="CRITICAL",
        asset_id=asset_id,
        run_id=run_id,
        context={"status_code": status_code, "error": error[:200]},
        recommended_action=(
            "Check OPENROUTER_API_KEY validity, network connectivity, "
            "and that the configured LLM_MODEL is available on OpenRouter."
        ),
    )


def validation_exhausted(
    run_id: str,
    asset_id: str,
    iterations: int,
) -> Alarm:
    return Alarm(
        alarm_type=VALIDATION_EXHAUSTED,
        severity="CRITICAL",
        asset_id=asset_id,
        run_id=run_id,
        context={"iterations_used": iterations},
        recommended_action=(
            "All automatic self-correction attempts exhausted. "
            "Escalating to human operator for HITL review."
        ),
    )


def hitl_escalation(run_id: str, asset_id: str, reason: str) -> Alarm:
    return Alarm(
        alarm_type=HITL_ESCALATION,
        severity="CRITICAL",
        asset_id=asset_id,
        run_id=run_id,
        context={"reason": reason[:300]},
        recommended_action=(
            "Human review required. Choose APPROVE to override, "
            "REJECT to kill the work order, or EDIT to correct the draft."
        ),
    )
