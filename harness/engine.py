"""
engine.py — Orchestration layer (The Loop)

The central synchronous state machine. Controls the full request lifecycle:
  1. Intake: parse and validate raw SCADA telemetry via Pydantic.
  2. Inference: send a structured prompt to the LLM via OpenRouter.
  3. Validation: run SQL checkpoints and guardrail rules against the draft.
  4. Self-correction: if validation fails, inject the error back into the LLM
     context and re-prompt, up to max_llm_iterations.
  5. Approval or escalation: emit the final decision to the audit log.

This is a transparent state machine — no hidden agent logic, no framework magic.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import yaml
from pydantic import ValidationError

from . import alarms
from . import observability as obs
from .agent_interface import AgentProtocol, OpenRouterAgent, RuleBasedAgent
from .material import AgentDiagnosis, EngineState, SCADAReading, WorkOrderRequest
from .tools import (
    bootstrap_db, compute_order_cost, check_asset_exists,
    check_active_tickets, check_inventory, create_work_order,
    get_available_parts, run_all_checkpoints, select_parts_for_fault,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Always resolve relative to project root regardless of working directory
GUARDRAILS_PATH = Path(__file__).parent.parent / "guardrails.yaml"
DEFAULT_MODEL = "openai/gpt-oss-20b:free"


def _load_guardrails() -> Dict[str, Any]:
    with GUARDRAILS_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert predictive maintenance engineer.

Your task is to DIAGNOSE the fault from SCADA telemetry and classify it.
The maintenance management system will handle part selection and costing automatically
from the ERP database — you must NOT include part numbers or costs in your response.

You MUST respond with ONLY valid JSON matching this exact schema:
{
  "fault_category": "<one of the 7 values below>",
  "fault_description": "<plain-English description of what you observed, 10-500 chars>",
  "recommended_action": "<plain-English maintenance action to resolve it, 10-500 chars>",
  "priority": "<LOW|MEDIUM|HIGH|CRITICAL>",
  "requires_shutdown": <true|false>,
  "confidence_score": <0.0-1.0>
}

fault_category MUST be exactly one of these 7 values — no other values are accepted:
  BEARING_WEAR        — vibration elevated, bearing temperature rising, wear pattern
  SEAL_FAILURE        — seal leakage, fluid contamination, pressure loss at seal face
  IMPELLER_WEAR       — reduced flow, efficiency drop, cavitation indicators
  COUPLING_FAULT      — lateral vibration spike, misalignment, coupling fatigue
  VIBRATION_SENSOR    — sensor reading anomaly, probe gap fault, transmitter failure
  LUBRICATION         — lubrication interval due, oil degradation, grease depletion
  ROUTINE_INSPECTION  — all parameters within limits, scheduled maintenance only

STRICT RULES — violations cause immediate rejection:
  1. Do NOT include part numbers, model numbers, or catalogue codes.
  2. Do NOT include cost estimates or pricing.
  3. fault_category must be exactly one of the 7 values above.
  4. Do not include any text before or after the JSON object.

If a previous parse attempt failed, a correction will be shown. Fix only the flagged issue."""


def _build_diagnosis_prompt(
    reading: SCADAReading,
    correction: Optional[str] = None,
    operator_notes: Optional[str] = None,
) -> str:
    """
    Builds the agent prompt. Contains SCADA telemetry only — no part numbers,
    no catalogue data. The agent's job is diagnosis; the harness handles parts.
    Operator notes are included verbatim so the agent can factor in field context
    that sensors alone may not capture (e.g. unusual smells, recent incidents).
    """
    telemetry = (
        f"Asset: {reading.asset_id}\n"
        f"Timestamp: {reading.timestamp.isoformat()}\n"
        f"Vibration: {reading.vibration_mm_s} mm/s\n"
        f"Temperature: {reading.temperature_c} C\n"
        f"Pressure: {reading.pressure_bar} bar\n"
        f"Flow Rate: {reading.flow_rate_l_min} L/min\n"
        f"Active fault codes: {', '.join(reading.fault_codes) or 'None'}"
    )

    base = f"Diagnose the fault from the following SCADA telemetry:\n\n{telemetry}"

    if operator_notes and operator_notes.strip():
        base += f"\n\nOperator field notes: {operator_notes.strip()}"

    if correction:
        return (
            f"{base}\n\n"
            f"CORRECTION REQUIRED — your previous response was rejected:\n{correction}\n\n"
            "Fix only the flagged issue and return corrected JSON."
        )
    return base


# ---------------------------------------------------------------------------
# Guardrail enforcement
# ---------------------------------------------------------------------------

def enforce_guardrails(
    draft: WorkOrderRequest,
    rules: Dict[str, Any],
    run_id: str,
) -> List[str]:
    """
    Applies all static guardrail rules to a work order draft.
    Returns a list of human-readable failure reasons (empty = all passed).
    """
    failures: List[str] = []
    approval = rules.get("approval", {})
    parts_rules = rules.get("parts", {})
    shutdown_rules = rules.get("shutdown", {})
    assets_rules = rules.get("assets", {})

    # Cost ceiling
    max_usd: float = approval.get("max_approval_usd", 1000.0)
    if draft.estimated_cost_usd > max_usd:
        detail = f"estimated_cost_usd {draft.estimated_cost_usd} exceeds max_approval_usd {max_usd}"
        obs.log_guardrail_block(run_id, "max_approval_usd", detail, draft.estimated_cost_usd)
        alarms.fire_alarm(alarms.guardrail_breach(run_id, draft.asset_id, "max_approval_usd", detail, draft.estimated_cost_usd))
        failures.append(f"Cost guardrail: {detail}. Lower the estimate or flag for human review.")

    # Auto-approvable priorities
    approvable: List[str] = approval.get("auto_approvable_priorities", [])
    if draft.priority not in approvable:
        detail = f"priority '{draft.priority}' is not in auto_approvable list {approvable}"
        obs.log_guardrail_block(run_id, "auto_approvable_priorities", detail, draft.priority)
        alarms.fire_alarm(alarms.guardrail_breach(run_id, draft.asset_id, "auto_approvable_priorities", detail, draft.priority))
        failures.append(f"Priority guardrail: {detail}. This order requires human escalation.")

    # Minimum confidence
    min_conf: float = approval.get("min_confidence_score", 0.75)
    if draft.confidence_score < min_conf:
        detail = f"confidence_score {draft.confidence_score} is below minimum {min_conf}"
        obs.log_guardrail_block(run_id, "min_confidence_score", detail, draft.confidence_score)
        alarms.fire_alarm(alarms.guardrail_breach(run_id, draft.asset_id, "min_confidence_score", detail, draft.confidence_score))
        failures.append(f"Confidence guardrail: {detail}. Re-analyse the telemetry.")

    # Shutdown restriction
    allow_shutdown: bool = shutdown_rules.get("allow_auto_approve_shutdown", False)
    if draft.requires_shutdown and not allow_shutdown:
        detail = "work order requires asset shutdown; auto-approval of shutdowns is disabled"
        obs.log_guardrail_block(run_id, "allow_auto_approve_shutdown", detail, draft.requires_shutdown)
        alarms.fire_alarm(alarms.guardrail_breach(run_id, draft.asset_id, "allow_auto_approve_shutdown", detail, draft.requires_shutdown))
        failures.append(f"Shutdown guardrail: {detail}. Escalate to operations manager.")

    # Part count
    max_parts: int = parts_rules.get("max_parts_per_order", 5)
    if len(draft.required_parts) > max_parts:
        detail = f"{len(draft.required_parts)} parts exceed max_parts_per_order {max_parts}"
        obs.log_guardrail_block(run_id, "max_parts_per_order", detail, len(draft.required_parts))
        alarms.fire_alarm(alarms.guardrail_breach(run_id, draft.asset_id, "max_parts_per_order", detail, len(draft.required_parts)))
        failures.append(f"Parts guardrail: {detail}. Simplify or split the work order.")

    # Safety-critical asset prefixes
    safety_prefixes: List[str] = assets_rules.get("safety_critical_prefixes", [])
    if any(draft.asset_id.startswith(p) for p in safety_prefixes):
        detail = f"asset '{draft.asset_id}' matches a safety-critical prefix"
        obs.log_guardrail_block(run_id, "safety_critical_prefixes", detail, draft.asset_id)
        alarms.fire_alarm(alarms.guardrail_breach(run_id, draft.asset_id, "safety_critical_prefixes", detail, draft.asset_id))
        failures.append(f"Safety guardrail: {detail}. Route to safety officer.")

    return failures


# ---------------------------------------------------------------------------
# Main engine entry point
# ---------------------------------------------------------------------------

def run(
    raw_scada: Dict[str, Any],
    agent: AgentProtocol,
    model: str = DEFAULT_MODEL,
    operator_notes: Optional[str] = None,
) -> EngineState:
    """
    Executes the Semantic Air-Gap loop for a single SCADA reading.

    Flow:
      INTAKE    — validate SCADAReading; confirm asset exists in ERP (fail-fast).
      INFERENCE — agent produces AgentDiagnosis (fault category, priority, shutdown flag).
                  Self-correction only for parse errors; no part numbers ever cross this boundary.
      ERP LOOKUP — harness looks up maintenance_bom for (asset_id, fault_category).
                   Parts and quantities come entirely from the database.
      VALIDATION — check_active_tickets, check_inventory (raises POs for out-of-stock).
                   Cost computed from inventory.unit_cost_usd × BOM quantities.
      GUARDRAILS — declarative rules from guardrails.yaml (cost ceiling, priority gate,
                   shutdown restriction, safety prefixes).
      APPROVED / BLOCKED — final state logged to audit.jsonl.
    """
    run_id = str(uuid.uuid4())
    guardrails = _load_guardrails()
    max_iterations: int = guardrails.get("iteration", {}).get("max_llm_iterations", 3)
    labor_usd: float = float(guardrails.get("costs", {}).get("labor_usd_per_order", 150.0))

    # ── INTAKE: validate SCADA telemetry ────────────────────────────────────
    try:
        reading = SCADAReading(**raw_scada)
    except ValidationError as exc:
        state = EngineState(run_id=run_id, asset_id=raw_scada.get("asset_id", "UNKNOWN"), phase="ERROR")
        state.error_message = f"SCADA validation failed: {exc}"
        obs.log_state_transition(state)
        obs.log_error(run_id, "SCADAValidationError", state.error_message)
        return state

    state = EngineState(run_id=run_id, asset_id=reading.asset_id, scada_reading=reading, phase="INTAKE")
    obs.log_state_transition(state)

    # Fail-fast: confirm asset exists before calling the agent
    asset_check = check_asset_exists(reading.asset_id, run_id)
    obs.log_tool_result(run_id, asset_check)
    if not asset_check.passed:
        state.phase = "BLOCKED"
        state.error_message = f"Asset not in ERP register. Last failures:\n- {asset_check.detail}"
        obs.log_state_transition(state)
        obs.log_error(run_id, "UnknownAsset", asset_check.detail)
        alarms.fire_alarm(alarms.hitl_escalation(run_id, reading.asset_id, asset_check.detail))
        return state

    # ── INFERENCE: agent diagnoses fault category ────────────────────────────
    messages: List[Dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    correction_context: Optional[str] = None
    diagnosis: Optional[AgentDiagnosis] = None

    for iteration in range(1, max_iterations + 1):
        state.iteration = iteration
        state.phase = "INFERENCE"
        obs.log_state_transition(state)

        user_msg = _build_diagnosis_prompt(reading, correction=correction_context, operator_notes=operator_notes)
        messages.append({"role": "user", "content": user_msg})
        obs.log_llm_exchange(run_id, "user", user_msg)

        try:
            llm_text, prompt_tok, comp_tok = agent.generate(messages, model)
        except httpx.HTTPStatusError as exc:
            state.phase = "ERROR"
            state.error_message = f"LLM API error on iteration {iteration}: {exc}"
            obs.log_state_transition(state)
            obs.log_error(run_id, "LLMHTTPError", state.error_message)
            alarms.fire_alarm(alarms.llm_api_error(run_id, reading.asset_id, exc.response.status_code, str(exc)))
            return state

        state.llm_prompt_tokens += prompt_tok
        state.llm_completion_tokens += comp_tok
        obs.log_llm_exchange(run_id, "assistant", llm_text, tokens=comp_tok)
        messages.append({"role": "assistant", "content": llm_text})

        try:
            diagnosis = AgentDiagnosis(**json.loads(llm_text))
            break  # valid diagnosis — exit the self-correction loop
        except (json.JSONDecodeError, ValidationError) as exc:
            alarms.fire_alarm(alarms.llm_parse_failure(run_id, reading.asset_id, exc))
            correction_context = (
                f"JSON parse / schema error: {exc}\n"
                "fault_category must be exactly one of: BEARING_WEAR, SEAL_FAILURE, "
                "IMPELLER_WEAR, COUPLING_FAULT, VIBRATION_SENSOR, LUBRICATION, ROUTINE_INSPECTION"
            )
            continue

    if diagnosis is None:
        state.phase = "BLOCKED"
        state.error_message = (
            f"Agent failed to produce a valid diagnosis after {max_iterations} attempt(s). "
            f"Last failures:\n{correction_context}"
        )
        obs.log_state_transition(state)
        obs.log_error(run_id, "DiagnosisParseFailure", state.error_message)
        alarms.fire_alarm(alarms.validation_exhausted(run_id, reading.asset_id, max_iterations))
        alarms.fire_alarm(alarms.hitl_escalation(run_id, reading.asset_id, state.error_message))
        return state

    state.diagnosis = diagnosis
    obs.log_diagnosis(run_id, reading.asset_id, diagnosis.model_dump())

    # ── ERP LOOKUP: select parts from maintenance BOM ───────────────────────
    # The agent's fault_category is the only key used here.
    # Part numbers and quantities come entirely from the database.
    parts_qty: Dict[str, int] = select_parts_for_fault(reading.asset_id, diagnosis.fault_category)
    part_list: List[str] = list(parts_qty.keys())

    erp_cost, cost_breakdown = compute_order_cost(parts_qty, labor_usd)
    state.cost_breakdown = cost_breakdown

    # ── VALIDATION: duplicate tickets + inventory ────────────────────────────
    state.phase = "VALIDATION"

    ticket_check = check_active_tickets(reading.asset_id, run_id)
    obs.log_tool_result(run_id, ticket_check)

    inventory_check = check_inventory(part_list, reading.asset_id, run_id)
    obs.log_tool_result(run_id, inventory_check)

    state.validation_results = [asset_check, ticket_check, inventory_check]
    obs.log_state_transition(state)

    tool_failures = [
        r.detail for r in [ticket_check, inventory_check] if not r.passed
    ]

    # ── BUILD WORK ORDER from diagnosis + ERP data ───────────────────────────
    draft = WorkOrderRequest(
        asset_id=reading.asset_id,
        fault_description=diagnosis.fault_description,
        recommended_action=diagnosis.recommended_action,
        priority=diagnosis.priority,
        estimated_cost_usd=erp_cost,
        required_parts=part_list,
        requires_shutdown=diagnosis.requires_shutdown,
        confidence_score=diagnosis.confidence_score,
    )
    state.work_order_draft = draft

    # ── GUARDRAILS: declarative rules from guardrails.yaml ──────────────────
    guardrail_failures = enforce_guardrails(draft, guardrails, run_id)

    all_failures = tool_failures + guardrail_failures

    if not all_failures:
        ticket_id = create_work_order(draft, run_id)
        state.phase = "APPROVED"
        obs.log_state_transition(state)
        obs.log_approval(run_id, draft.asset_id, draft.estimated_cost_usd, ticket_id=ticket_id)
        return state

    # Validation or guardrail failures → BLOCKED for human review.
    # ERP state failures (duplicate ticket, out-of-stock) and guardrail failures
    # cannot be resolved by re-prompting the agent — they require operator action.
    failure_text = "\n".join(f"- {f}" for f in all_failures)
    state.phase = "BLOCKED"
    state.error_message = f"Work order blocked by validation. Last failures:\n{failure_text}"
    obs.log_state_transition(state)
    obs.log_error(run_id, "ValidationBlocked", state.error_message)
    alarms.fire_alarm(alarms.hitl_escalation(run_id, reading.asset_id, failure_text))
    return state


# ---------------------------------------------------------------------------
# Unstructured input parser
# ---------------------------------------------------------------------------

SCADA_PARSE_SYSTEM = """You extract SCADA telemetry from unstructured operator notes.
Output ONLY valid JSON with exactly these fields:
{
  "asset_id": "<UPPERCASE-ID e.g. PUMP-042>",
  "timestamp": "<ISO8601 UTC>",
  "vibration_mm_s": <number>,
  "temperature_c": <number>,
  "pressure_bar": <number>,
  "flow_rate_l_min": <number or null>,
  "fault_codes": ["<code>", ...]
}
If a value is not mentioned, use a physically plausible default.
Asset IDs follow the pattern UPPERCASE-digits (e.g. PUMP-042, COMP-017)."""


def _parse_unstructured_to_scada(
    raw_text: str, agent: AgentProtocol, model: str = DEFAULT_MODEL
) -> Dict[str, Any]:
    """
    Uses the agent as a semantic bridge to extract structured SCADA fields from
    unstructured operator prose (e.g. "the pump on line 3 sounds like grinding gravel").
    This is the only legitimate use of the LLM outside the main validation loop.
    Note: RuleBasedAgent does not support prose extraction — use structured JSON input
    when AGENT_TYPE=rule-based.
    """
    content, _, _ = agent.generate(
        [
            {"role": "system", "content": SCADA_PARSE_SYSTEM},
            {"role": "user", "content": f"Extract SCADA fields from this operator note:\n{raw_text}"},
        ],
        model,
    )
    return json.loads(content)


# ---------------------------------------------------------------------------
# Public harness entry point
# ---------------------------------------------------------------------------

def run_harness_loop(
    raw_input: str,
    agent: Optional[AgentProtocol] = None,
) -> EngineState:
    """
    Public entry point for the Semantic Air-Gap. Accepts either structured JSON
    (pasted from a SCADA export) or unstructured operator prose. Prose is routed
    through a preliminary LLM extraction step to produce a SCADAReading before
    entering the main validation loop.

    The agent parameter is optional — if None, an OpenRouterAgent is created from
    the OPENROUTER_API_KEY environment variable. Pass a RuleBasedAgent to run
    without any API calls (structured JSON input only).
    """
    model = os.getenv("LLM_MODEL", DEFAULT_MODEL)

    if agent is None:
        api_key = os.environ["OPENROUTER_API_KEY"]
        agent = OpenRouterAgent(api_key)

    # Attempt direct JSON parse (structured SCADA input path)
    try:
        raw_scada: Dict[str, Any] = json.loads(raw_input)
    except json.JSONDecodeError:
        # Unstructured prose path: use agent to extract SCADA fields
        raw_scada = _parse_unstructured_to_scada(raw_input, agent, model)

    state = run(raw_scada, agent, model)

    # Route automatic validation failures to human review
    if state.phase == "BLOCKED":
        from harness import hitl  # late import — avoids circular dependency at module level
        state = hitl.run_review(state, _load_guardrails())

    return state


# ---------------------------------------------------------------------------
# CLI entry point (called by run.py)
# ---------------------------------------------------------------------------

def main() -> None:
    """Persistent operator console — runs until the operator types 'exit' or Ctrl+C."""
    from dotenv import load_dotenv
    load_dotenv()

    agent_type_env = os.getenv("AGENT_TYPE", "openrouter").lower()
    active_model = os.getenv("LLM_MODEL", DEFAULT_MODEL)

    if agent_type_env == "rule-based":
        active_agent: AgentProtocol = RuleBasedAgent()
        agent_label = "RuleBasedAgent (deterministic, no API — ISO 10816 / API 670)"
    else:
        if not os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_API_KEY") == "your_key_here":
            print("ERROR: Please configure a valid OPENROUTER_API_KEY in your local .env file.")
            raise SystemExit(1)
        api_key = os.environ["OPENROUTER_API_KEY"]
        active_agent = OpenRouterAgent(api_key)
        agent_label = f"OpenRouterAgent ({active_model})"

    bootstrap_db()

    print("=== HARNESS ACTIVE: SEMANTIC AIR-GAP ===")
    print(f"    Agent  : {agent_label}")
    if agent_type_env != "rule-based":
        print(f"    Model  : {active_model}")
    print("    Type 'exit' or press Ctrl+C to shut down.")

    _DIVIDER = "-" * 64

    while True:
        print(f"\n{_DIVIDER}")
        print("Awaiting raw telemetry or operator notes...")
        if agent_type_env == "rule-based":
            print("  [rule-based mode: structured JSON input required]")

        try:
            scada_anomaly = input("\n[SCADA INGEST] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n[HARNESS] Shutdown signal received. Exiting.")
            break

        if scada_anomaly.lower() in ("exit", "quit", "q"):
            print("[HARNESS] Operator logout. Exiting.")
            break

        if not scada_anomaly:
            print("[HARNESS] Empty input — awaiting next feed.")
            continue

        print("\n--- INITIATING EVENT LOOP ---")

        try:
            final_payload = run_harness_loop(scada_anomaly, agent=active_agent)

            if final_payload.phase in ("APPROVED", "HITL_APPROVED"):
                print("\n=== VALIDATED WORK ORDER EMITTED ===")
            elif final_payload.phase == "HITL_REJECTED":
                print("\n=== WORK ORDER REJECTED — AUDIT TRAIL PRESERVED ===")
            else:
                print(f"\n=== ENGINE HALTED [phase={final_payload.phase}] ===")

            print(final_payload.model_dump_json(indent=2))

        except KeyboardInterrupt:
            print("\n\n[HARNESS] Run interrupted mid-flight. Returning to intake.")
            continue
        except Exception as exc:
            print(f"\n[HARNESS ERROR] {type(exc).__name__}: {exc}")
            print("[HARNESS] Run aborted. Returning to intake for next feed.")


if __name__ == "__main__":
    main()
