"""
api.py — FastAPI operator console

Three-column control-room UI served via Jinja2 + HTMX.
The engine runs in a thread pool so the async event loop stays free.
HTMX OOB swaps update all three panels from a single POST /dispatch response.

Routes:
  GET  /                       Main console
  POST /dispatch               Run pipeline → HTML result (pipeline + OOB workorder + OOB decisions)
  POST /hitl/{run_id}/approve  HITL approve
  POST /hitl/{run_id}/reject   HITL reject
  POST /hitl/{run_id}/edit     HITL edit + re-validate
  GET  /decisions              Recent decisions fragment (polled every 15 s)
  GET  /audit                  Full audit log fragment
  GET  /health                 Render health check
"""

from __future__ import annotations

import asyncio
import calendar
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from harness import observability as obs
from harness.agent_interface import OpenRouterAgent, RuleBasedAgent
from harness.engine import (
    DEFAULT_MODEL, _load_guardrails, _parse_unstructured_to_scada,
    enforce_guardrails, run,
)
from harness.material import AgentDiagnosis, EngineState, WorkOrderRequest
from harness.tools import (
    bootstrap_db, run_all_checkpoints, compute_order_cost,
    select_parts_for_fault, create_work_order, DB_PATH,
)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="PDM Harness Operator Console")
templates = Jinja2Templates(directory="templates")
_pool = ThreadPoolExecutor(max_workers=4)

# In-memory state store for HITL — single-instance only (fine for Render free tier)
_states: Dict[str, EngineState] = {}

_LOG_PATH = Path(__file__).parent / "audit.jsonl"


@app.on_event("startup")
def _startup() -> None:
    bootstrap_db()


# ---------------------------------------------------------------------------
# Preset scenarios
# ---------------------------------------------------------------------------

PRESETS: Dict[str, Dict[str, Any]] = {
    "Normal operation": {
        "asset_id": "PUMP-042",
        "vibration_mm_s": "2.1",
        "temperature_c": "62.0",
        "pressure_bar": "8.1",
        "flow_rate_l_min": "215",
        "fault_codes": "",
    },
    "Zone C vibration": {
        "asset_id": "PUMP-042",
        "vibration_mm_s": "8.4",
        "temperature_c": "82.1",
        "pressure_bar": "8.2",
        "flow_rate_l_min": "210",
        "fault_codes": "VIB-HIGH,BRG-TEMP-WARN",
    },
    "Trip level — CRITICAL": {
        "asset_id": "PUMP-042",
        "vibration_mm_s": "12.4",
        "temperature_c": "97.2",
        "pressure_bar": "7.8",
        "flow_rate_l_min": "158",
        "fault_codes": "VIB-TRIP,BRG-TEMP-TRIP,OVERTEMP",
    },
    "Duplicate ticket": {
        "asset_id": "MOL-PUMP-001",
        "vibration_mm_s": "8.4",
        "temperature_c": "82.1",
        "pressure_bar": "19.2",
        "flow_rate_l_min": "476",
        "fault_codes": "VIB-HIGH",
    },
    "Low flow / impeller": {
        "asset_id": "PUMP-042",
        "vibration_mm_s": "2.1",
        "temperature_c": "60.0",
        "pressure_bar": "7.5",
        "flow_rate_l_min": "95",
        "fault_codes": "FLOW-LOW",
    },
    "Unknown asset": {
        "asset_id": "PHANTOM-99",
        "vibration_mm_s": "3.5",
        "temperature_c": "55.0",
        "pressure_bar": "6.0",
        "flow_rate_l_min": "",
        "fault_codes": "",
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_badge(priority: str, operator_id: str, guardrails: Dict[str, Any]) -> Optional[str]:
    """
    Enforces badge-level HITL authorisation from guardrails.yaml.
    An empty badge is always rejected — no anonymous decisions permitted.
    CRITICAL orders require a badge in hitl.critical_approver_badges.
    All other priorities require a badge in hitl.standard_approver_badges.
    Returns a human-readable error string, or None if authorised.
    """
    badge = operator_id.strip()

    if not badge:
        return "Operator badge ID is required. Enter your badge number before approving or rejecting."

    hitl_rules = guardrails.get("hitl", {})

    if priority == "CRITICAL":
        allowed: List[str] = hitl_rules.get("critical_approver_badges", [])
        if allowed and badge not in allowed:
            return (
                f"CRITICAL work orders require badge {' or '.join(allowed)}. "
                f"Badge '{badge}' is not authorised."
            )
    else:
        allowed = hitl_rules.get("standard_approver_badges", [])
        if allowed and badge not in allowed:
            return (
                f"{priority} work orders require badge {' or '.join(allowed)}. "
                f"Badge '{badge}' is not authorised."
            )
    return None


def _build_agent(agent_type: str) -> Any:
    if agent_type == "rule-based":
        return RuleBasedAgent()
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    return OpenRouterAgent(api_key)


def _build_phases(state: EngineState) -> List[Dict[str, Any]]:
    """Constructs a display-ready phase list from EngineState for the pipeline column."""
    phases: List[Dict[str, Any]] = []

    def phase(name: str, status: str, detail: str) -> None:
        phases.append({"name": name, "status": status, "detail": detail})

    # INTAKE
    if state.phase == "ERROR" and state.error_message and "SCADA" in state.error_message:
        phase("INTAKE", "fail", state.error_message[:120])
    else:
        phase("INTAKE", "pass", f"SCADA telemetry validated · Asset: {state.asset_id}")

    if state.phase == "ERROR" and "SCADA" in (state.error_message or ""):
        return phases

    # ASSET CHECK
    asset_r = next((r for r in state.validation_results if r.check_name == "check_asset_exists"), None)
    if asset_r:
        detail = asset_r.detail
        if len(detail) > 100:
            detail = detail[:97] + "..."
        phase("ASSET CHECK", "pass" if asset_r.passed else "fail", detail)
        if not asset_r.passed:
            phase("RESULT", "blocked", "Blocked — asset not in ERP register")
            return phases

    # INFERENCE
    if state.diagnosis:
        phase(
            "INFERENCE",
            "pass",
            f"Diagnosis: {state.diagnosis.fault_category} · "
            f"{state.diagnosis.priority} priority · "
            f"{state.diagnosis.confidence_score:.0%} confidence",
        )
    elif state.error_message and "diagnosis" in state.error_message.lower():
        phase("INFERENCE", "fail", "Agent failed to produce a valid diagnosis")
        phase("RESULT", "blocked", "Blocked — parse failure after max iterations")
        return phases

    # ERP LOOKUP
    if state.work_order_draft is not None:
        n = len(state.work_order_draft.required_parts)
        phase(
            "ERP LOOKUP",
            "pass",
            f"Maintenance BOM: {n} part(s) selected · Labour: ${_load_guardrails().get('costs', {}).get('labor_usd_per_order', 150):.0f}",
        )

    # VALIDATION
    ticket_r = next((r for r in state.validation_results if r.check_name == "check_active_tickets"), None)
    inv_r = next((r for r in state.validation_results if r.check_name == "check_inventory"), None)
    checks = [r for r in [ticket_r, inv_r] if r]
    if checks:
        passed = sum(1 for r in checks if r.passed)
        all_ok = all(r.passed for r in checks)
        phase(
            "VALIDATION",
            "pass" if all_ok else "fail",
            f"{passed}/{len(checks)} checkpoints passed"
            + ("" if all_ok else f" — {'; '.join(r.detail[:60] for r in checks if not r.passed)}"),
        )

    # RESULT
    if state.phase == "APPROVED":
        cost = state.work_order_draft.estimated_cost_usd if state.work_order_draft else 0
        phase("RESULT", "approved", f"APPROVED · ${cost:,.0f} · ready to emit")
    elif state.phase == "HITL_APPROVED":
        phase("RESULT", "hitl_approved", f"HITL APPROVED · {state.hitl_operator_notes or ''}")
    elif state.phase == "HITL_REJECTED":
        phase("RESULT", "hitl_rejected", f"HITL REJECTED · {state.hitl_operator_notes or ''}")
    elif state.phase == "BLOCKED":
        failures = ""
        if state.error_message:
            block = state.error_message.split("Last failures:\n", 1)
            failures = block[1][:120] if len(block) == 2 else state.error_message[:120]
        phase("RESULT", "blocked", f"BLOCKED — HITL required · {failures}")
    elif state.phase == "ERROR":
        phase("RESULT", "error", (state.error_message or "Engine error")[:120])

    return phases


def _read_recent_decisions(n: int = 8) -> List[Dict[str, Any]]:
    if not _LOG_PATH.exists():
        return []
    results = []
    for line in reversed(_LOG_PATH.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("event") in ("WORK_ORDER_APPROVED", "HITL_DECISION", "ENGINE_ERROR", "AGENT_DIAGNOSIS"):
            results.append(rec)
            if len(results) >= n:
                break
    return results


def _read_audit(limit: int = 100) -> List[Dict[str, Any]]:
    if not _LOG_PATH.exists():
        return []
    results = []
    for line in reversed(_LOG_PATH.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(results) >= limit:
            break
    return results


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def console(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "console.html", {
        "presets": PRESETS,
        "agent_types": ["rule-based", "openrouter"],
    })


@app.post("/dispatch", response_class=HTMLResponse)
async def dispatch(
    request: Request,
    asset_id: str = Form(...),
    vibration_mm_s: str = Form(...),
    temperature_c: str = Form(...),
    pressure_bar: str = Form(...),
    flow_rate_l_min: str = Form(""),
    fault_codes: str = Form(""),
    operator_notes: str = Form(""),
    agent_type: str = Form("openrouter"),
    model: str = Form(""),
) -> HTMLResponse:
    raw: Dict[str, Any] = {
        "asset_id": asset_id.strip().upper(),
        "timestamp": _now_utc(),
        "vibration_mm_s": float(vibration_mm_s),
        "temperature_c": float(temperature_c),
        "pressure_bar": float(pressure_bar),
        "flow_rate_l_min": float(flow_rate_l_min) if flow_rate_l_min.strip() else None,
        "fault_codes": [c.strip() for c in fault_codes.split(",") if c.strip()],
    }

    agent = _build_agent(agent_type)
    effective_model = model.strip() or os.getenv("LLM_MODEL", DEFAULT_MODEL)

    loop = asyncio.get_event_loop()
    state: EngineState = await loop.run_in_executor(
        _pool, lambda: run(raw, agent, effective_model, operator_notes=operator_notes or None)
    )
    _states[state.run_id] = state

    phases = _build_phases(state)
    decisions = _read_recent_decisions()

    return templates.TemplateResponse(request, "_result.html", {
        "state": state,
        "phases": phases,
        "decisions": decisions,
    })


@app.post("/dispatch/nlp", response_class=HTMLResponse)
async def dispatch_nlp(
    request: Request,
    natural_language: str = Form(...),
    agent_type: str = Form("openrouter"),
    model: str = Form(""),
) -> HTMLResponse:
    agent = _build_agent(agent_type)
    effective_model = model.strip() or os.getenv("LLM_MODEL", DEFAULT_MODEL)

    def _run() -> tuple:
        raw_scada = _parse_unstructured_to_scada(natural_language, agent, effective_model)
        if "timestamp" not in raw_scada or not raw_scada.get("timestamp"):
            raw_scada["timestamp"] = _now_utc()
        state = run(raw_scada, agent, effective_model, operator_notes=natural_language)
        return state, raw_scada

    loop = asyncio.get_event_loop()
    state, raw_scada = await loop.run_in_executor(_pool, _run)
    _states[state.run_id] = state

    phases = _build_phases(state)
    decisions = _read_recent_decisions()

    return templates.TemplateResponse(request, "_result.html", {
        "state": state,
        "phases": phases,
        "decisions": decisions,
        "nlp_extracted": raw_scada,
    })


@app.post("/hitl/{run_id}/approve", response_class=HTMLResponse)
async def hitl_approve(
    run_id: str,
    request: Request,
    operator_id: str = Form(""),
) -> HTMLResponse:
    state = _states.get(run_id)
    if not state:
        return HTMLResponse('<p class="text-red-400 p-4">Run not found in session.</p>')

    priority = state.work_order_draft.priority if state.work_order_draft else "MEDIUM"
    badge_error = _check_badge(priority, operator_id, _load_guardrails())
    if badge_error:
        obs.log_error(run_id, "BadgeAuthFailed", badge_error)
        phases = _build_phases(state)
        return templates.TemplateResponse(request, "_result.html", {
            "state": state, "phases": phases,
            "decisions": _read_recent_decisions(),
            "badge_error": badge_error,
        })

    notes = f"operator={operator_id or 'UNKNOWN'} | decision=APPROVE | via=console"
    state.phase = "HITL_APPROVED"
    state.hitl_operator_notes = notes
    obs.log_hitl_decision(run_id, "APPROVED", notes)
    obs.log_state_transition(state)
    if state.work_order_draft:
        ticket_id = create_work_order(state.work_order_draft, run_id)
        obs.log_approval(run_id, state.work_order_draft.asset_id, state.work_order_draft.estimated_cost_usd, ticket_id=ticket_id)
    phases = _build_phases(state)
    decisions = _read_recent_decisions()
    return templates.TemplateResponse(request, "_result.html", {
        "state": state, "phases": phases, "decisions": decisions,
    })


@app.post("/hitl/{run_id}/reject", response_class=HTMLResponse)
async def hitl_reject(
    run_id: str,
    request: Request,
    operator_id: str = Form(""),
) -> HTMLResponse:
    state = _states.get(run_id)
    if not state:
        return HTMLResponse('<p class="text-red-400 p-4">Run not found in session.</p>')

    priority = state.work_order_draft.priority if state.work_order_draft else "MEDIUM"
    badge_error = _check_badge(priority, operator_id, _load_guardrails())
    if badge_error:
        obs.log_error(run_id, "BadgeAuthFailed", badge_error)
        phases = _build_phases(state)
        return templates.TemplateResponse(request, "_result.html", {
            "state": state, "phases": phases,
            "decisions": _read_recent_decisions(),
            "badge_error": badge_error,
        })

    notes = f"operator={operator_id or 'UNKNOWN'} | decision=REJECT | via=console"
    state.phase = "HITL_REJECTED"
    state.hitl_operator_notes = notes
    obs.log_hitl_decision(run_id, "REJECTED", notes)
    obs.log_state_transition(state)
    phases = _build_phases(state)
    decisions = _read_recent_decisions()
    return templates.TemplateResponse(request, "_result.html", {
        "state": state, "phases": phases, "decisions": decisions,
    })


@app.post("/hitl/{run_id}/edit", response_class=HTMLResponse)
async def hitl_edit(
    run_id: str,
    request: Request,
    corrected_json: str = Form(...),
    operator_id: str = Form(""),
) -> HTMLResponse:
    state = _states.get(run_id)
    if not state:
        return HTMLResponse('<p class="text-red-400 p-4">Run not found in session.</p>')

    try:
        corrected = WorkOrderRequest(**json.loads(corrected_json))
    except (json.JSONDecodeError, ValidationError) as exc:
        state.error_message = f"Edit parse error: {exc}"
        phases = _build_phases(state)
        return templates.TemplateResponse(request, "_result.html", {
            "state": state,
            "phases": phases, "decisions": _read_recent_decisions(),
            "edit_error": str(exc),
        })

    # Re-validate without calling the agent
    guardrails = _load_guardrails()
    labor_usd = float(guardrails.get("costs", {}).get("labor_usd_per_order", 150.0))
    tool_results = run_all_checkpoints(corrected.asset_id, corrected.required_parts, run_id)
    guardrail_failures = enforce_guardrails(corrected, guardrails, run_id)
    for r in tool_results:
        obs.log_tool_result(run_id, r)

    all_failures = [r.detail for r in tool_results if not r.passed] + guardrail_failures

    notes = f"operator={operator_id or 'UNKNOWN'} | decision=EDIT"

    # Badge check applies to the corrected draft's priority
    badge_error = _check_badge(corrected.priority, operator_id, guardrails)
    if badge_error:
        obs.log_error(run_id, "BadgeAuthFailed", badge_error)
        phases = _build_phases(state)
        return templates.TemplateResponse(request, "_result.html", {
            "state": state, "phases": phases,
            "decisions": _read_recent_decisions(),
            "badge_error": badge_error,
        })

    if all_failures:
        state.error_message = "Edit re-validation failed:\n" + "\n".join(f"- {f}" for f in all_failures)
        phases = _build_phases(state)
        return templates.TemplateResponse(request, "_result.html", {
            "state": state,
            "phases": phases, "decisions": _read_recent_decisions(),
            "edit_error": "; ".join(all_failures),
        })

    state.work_order_draft = corrected
    state.phase = "HITL_APPROVED"
    state.hitl_operator_notes = notes + " | EDIT->APPROVED"
    obs.log_hitl_decision(run_id, "EDIT->APPROVED", state.hitl_operator_notes)
    obs.log_state_transition(state)
    ticket_id = create_work_order(corrected, run_id)
    obs.log_approval(run_id, corrected.asset_id, corrected.estimated_cost_usd, ticket_id=ticket_id)

    phases = _build_phases(state)
    decisions = _read_recent_decisions()
    return templates.TemplateResponse(request, "_result.html", {
        "state": state, "phases": phases, "decisions": decisions,
    })


@app.get("/decisions", response_class=HTMLResponse)
async def decisions_fragment(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "_decisions.html", {
        "decisions": _read_recent_decisions(),
    })


@app.get("/audit", response_class=HTMLResponse)
async def audit_fragment(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "_audit.html", {
        "records": _read_audit(80),
    })


@app.get("/workorders", response_class=HTMLResponse)
async def workorders_panel(request: Request) -> HTMLResponse:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM work_orders ORDER BY created_utc DESC"
    ).fetchall()
    conn.close()
    orders = [dict(r) for r in rows]

    # Build calendar data for current month
    today = date.today()
    year, month = today.year, today.month
    first_weekday = date(year, month, 1).weekday()  # Monday=0
    days_in_month = calendar.monthrange(year, month)[1]
    month_prefix = f"{year}-{month:02d}"

    orders_by_day: Dict[int, List[Dict]] = {}
    for o in orders:
        d_str = (o.get("created_utc") or "")[:10]
        if d_str.startswith(month_prefix):
            day = int(d_str[8:10])
            orders_by_day.setdefault(day, []).append(o)

    cal_ctx = {
        "month_name": date(year, month, 1).strftime("%B %Y"),
        "first_weekday": first_weekday,
        "days_in_month": days_in_month,
        "today_day": today.day,
        "orders_by_day": orders_by_day,
    }

    open_orders = [o for o in orders if o["status"] in ("OPEN", "IN_PROGRESS")]
    closed_orders = [o for o in orders if o["status"] == "CLOSED"]

    return templates.TemplateResponse(request, "_workorders.html", {
        "open_orders": open_orders,
        "closed_orders": closed_orders,
        "all_orders": orders,
        "cal": cal_ctx,
    })


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}
