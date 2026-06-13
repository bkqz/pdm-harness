"""
hitl.py — Human-In-The-Loop review layer

Intercepts work orders that exhaust the automatic validation loop (BLOCKED phase)
and presents them to an operator for a binding manual decision. The operator may:
  - APPROVE: override all automated failures and emit the last AI draft as-is.
  - REJECT:  kill the work order; no ERP action is taken.
  - EDIT:    paste a corrected draft JSON, which is re-validated through tools and
             guardrails (no further LLM call). If re-validation fails, the order
             is escalated to REJECT — the harness never silently approves.

All decisions are written to the immutable audit log before this module returns,
providing a legally attributable record for every human override.
"""

from __future__ import annotations

import json
import textwrap
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError

from . import observability as obs
from .engine import enforce_guardrails
from .material import EngineState, WorkOrderRequest
from .tools import run_all_checkpoints

_BORDER = "=" * 64


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _print_freeze_header(state: EngineState) -> None:
    print(f"\n{_BORDER}")
    print("  !! HITL CRITICAL FREEZE !!")
    print(f"  Automatic validation loop exhausted after {state.iteration} iteration(s).")
    print("  A binding human decision is required before this run can close.")
    print(_BORDER)
    print(f"  Run ID  : {state.run_id}")
    print(f"  Asset   : {state.asset_id}")


def _print_last_draft(draft: WorkOrderRequest) -> None:
    print("\n-- LAST AI DRAFT " + "-" * 47)
    print(f"  Fault description : {draft.fault_description}")
    print(f"  Recommended action: {draft.recommended_action}")
    print(f"  Priority          : {draft.priority}")
    print(f"  Estimated cost    : ${draft.estimated_cost_usd:,.2f}")
    print(f"  Required parts    : {', '.join(draft.required_parts) or 'None'}")
    print(f"  Requires shutdown : {'Yes' if draft.requires_shutdown else 'No'}")
    print(f"  Confidence score  : {draft.confidence_score:.0%}")


def _print_failures(state: EngineState) -> None:
    print("\n-- VALIDATION FAILURES " + "-" * 41)
    if state.error_message:
        parts = state.error_message.split("Last failures:\n", 1)
        block = parts[1] if len(parts) == 2 else state.error_message
        for line in block.splitlines():
            print(f"  {line}")
    else:
        print("  (No failure detail recorded.)")


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------

def _prompt_decision() -> Tuple[str, str]:
    """Prompts for operator ID and decision. Loops until valid input is given."""
    print("\n-- REQUIRED ACTION " + "-" * 45)
    print("  [A] APPROVE — override all failures and emit this work order")
    print("  [R] REJECT  — kill this work order; no action taken")
    print("  [E] EDIT    — paste a corrected draft for re-validation")
    print()

    operator_id = input("  Operator ID        > ").strip() or "UNKNOWN"

    while True:
        raw = input("  Decision [A/R/E]   > ").strip().upper()
        if raw in ("A", "R", "E"):
            return raw, operator_id
        print("  Invalid input. Enter A, R, or E.")


def _collect_corrected_draft(current: WorkOrderRequest) -> Optional[WorkOrderRequest]:
    """
    Shows the current draft and asks the operator to paste a corrected JSON.
    Returns a validated WorkOrderRequest, or None if the input cannot be parsed.
    """
    print("\n-- EDIT MODE " + "-" * 51)
    print("  Current draft (read-only reference):")
    print(textwrap.indent(current.model_dump_json(indent=2), "    "))
    print()
    print("  Paste corrected work order JSON below.")
    print("  Press Enter on a blank line to submit.")

    lines: List[str] = []
    while True:
        line = input()
        if line == "" and lines:
            break
        lines.append(line)

    raw_json = "\n".join(lines).strip()

    try:
        return WorkOrderRequest(**json.loads(raw_json))
    except (json.JSONDecodeError, ValidationError) as exc:
        print(f"\n  [ERROR] Could not parse corrected draft: {exc}")
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_review(state: EngineState, guardrails: Dict[str, Any]) -> EngineState:
    """
    Presents a BLOCKED work order to a human operator and captures their decision.
    Transitions the EngineState to HITL_APPROVED or HITL_REJECTED before returning.

    For EDIT: re-runs tools.run_all_checkpoints and engine.enforce_guardrails on the
    corrected draft. If any check still fails, the order is automatically escalated to
    HITL_REJECTED — the harness never silently bypasses a failing constraint.
    """
    state.phase = "HITL_REVIEW"
    obs.log_state_transition(state)

    _print_freeze_header(state)

    if state.work_order_draft:
        _print_last_draft(state.work_order_draft)
    else:
        print("\n  [No valid draft was produced — the LLM never emitted parseable JSON.]")

    _print_failures(state)

    try:
        choice, operator_id = _prompt_decision()
    except KeyboardInterrupt:
        print("\n\n  [HITL] Interrupted — logging REJECTED and returning to intake.")
        state.phase = "HITL_REJECTED"
        state.hitl_operator_notes = "HITL interrupted by operator (Ctrl+C)"
        obs.log_hitl_decision(state.run_id, "REJECTED", state.hitl_operator_notes)
        obs.log_state_transition(state)
        return state

    operator_notes = f"operator={operator_id} | decision={choice}"

    # -----------------------------------------------------------------------
    # APPROVE — human overrides all automated failures
    # -----------------------------------------------------------------------
    if choice == "A":
        if state.work_order_draft is None:
            print("\n  [ERROR] Cannot APPROVE: no draft exists. Defaulting to REJECT.")
            choice = "R"
            operator_notes += " | APPROVE rejected — no draft available"
        else:
            state.phase = "HITL_APPROVED"
            state.hitl_operator_notes = operator_notes
            obs.log_hitl_decision(state.run_id, "APPROVED", operator_notes)
            obs.log_state_transition(state)
            obs.log_approval(
                state.run_id,
                state.work_order_draft.asset_id,
                state.work_order_draft.estimated_cost_usd,
            )
            print("\n  [OK] Work order APPROVED by operator. Emitting to ERP.")
            return state

    # -----------------------------------------------------------------------
    # EDIT — operator provides a corrected draft for re-validation
    # -----------------------------------------------------------------------
    if choice == "E":
        if state.work_order_draft is None:
            print("\n  [ERROR] Cannot EDIT: no draft to base corrections on. Defaulting to REJECT.")
            operator_notes += " | EDIT attempted — no draft available"
        else:
            try:
                corrected = _collect_corrected_draft(state.work_order_draft)
            except KeyboardInterrupt:
                print("\n\n  [HITL] Edit interrupted — defaulting to REJECT.")
                corrected = None

            if corrected is None:
                operator_notes += " | EDIT parse failed"
                print("  Defaulting to REJECT.")
            else:
                # Re-run the full validation suite (tools + guardrails) on the corrected draft.
                # No LLM is consulted — the operator's judgement replaces the model at this step.
                tool_results = run_all_checkpoints(corrected.asset_id, corrected.required_parts, state.run_id)
                guardrail_failures = enforce_guardrails(corrected, guardrails, state.run_id)

                for result in tool_results:
                    obs.log_tool_result(state.run_id, result)

                all_failures = [r.detail for r in tool_results if not r.passed] + guardrail_failures

                if all_failures:
                    print("\n-- RE-VALIDATION FAILURES " + "-" * 39)
                    for failure in all_failures:
                        print(f"  - {failure}")
                    print("\n  Corrected draft still fails validation.")
                    print("  Escalating to REJECT. Contact the maintenance manager.")
                    operator_notes += f" | EDIT re-validation failed ({len(all_failures)} issue(s))"
                else:
                    state.work_order_draft = corrected
                    state.phase = "HITL_APPROVED"
                    state.hitl_operator_notes = operator_notes + " | EDIT passed re-validation"
                    obs.log_hitl_decision(state.run_id, "EDIT->APPROVED", state.hitl_operator_notes)
                    obs.log_state_transition(state)
                    obs.log_approval(state.run_id, corrected.asset_id, corrected.estimated_cost_usd)
                    print("\n  [OK] Corrected draft passed all checks. Emitting to ERP.")
                    return state

        # Fall through to REJECT if EDIT could not be completed
        choice = "R"

    # -----------------------------------------------------------------------
    # REJECT — work order is killed; audit trail is preserved
    # -----------------------------------------------------------------------
    state.phase = "HITL_REJECTED"
    state.hitl_operator_notes = operator_notes
    obs.log_hitl_decision(state.run_id, "REJECTED", operator_notes)
    obs.log_state_transition(state)
    print("\n  [--] Work order REJECTED. No ERP action taken.")

    return state
