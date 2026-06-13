"""
material.py — Schema layer (The Schema)

Defines the rigid Pydantic v2 data contracts for all inputs and outputs.
This is the single source of truth for what is "real" data in the system.
Any data that does not conform to these models is rejected before reaching
the engine or the LLM.

Agent output is strictly limited to AgentDiagnosis — a fault classification
with no part numbers, no costs, and no ERP-specific knowledge. The harness
builds the full WorkOrderRequest from ERP data (maintenance_bom + inventory).
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class SCADAReading(BaseModel):
    """
    Represents a single telemetry snapshot from a SCADA-connected asset.
    Enforces physical plausibility constraints before any AI inference occurs.
    This prevents garbage-in from producing plausible-but-wrong work orders.
    """

    asset_id: str = Field(..., description="Unique asset identifier, e.g. PUMP-042")
    timestamp: datetime = Field(..., description="UTC timestamp of the reading")
    vibration_mm_s: float = Field(..., ge=0.0, description="Vibration in mm/s (RMS)")
    temperature_c: float = Field(..., ge=-40.0, le=300.0, description="Asset temperature in Celsius")
    pressure_bar: float = Field(..., ge=0.0, description="Operating pressure in bar")
    flow_rate_l_min: Optional[float] = Field(None, ge=0.0, description="Flow rate in litres/min")
    fault_codes: List[str] = Field(default_factory=list, description="Active IEC fault codes")

    @field_validator("asset_id")
    @classmethod
    def asset_id_must_be_uppercase(cls, v: str) -> str:
        """Asset IDs must match the ERP format: UPPERCASE-digits."""
        if not v.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"asset_id '{v}' contains invalid characters")
        return v.upper()


class AgentDiagnosis(BaseModel):
    """
    The narrow, controlled output of the AI agent — a fault diagnosis only.

    The agent classifies what component is failing and how urgent it is.
    It NEVER specifies part numbers, costs, or asset-specific catalogue data.
    The harness looks up the correct parts and computes costs from the ERP
    maintenance_bom and inventory tables after receiving this diagnosis.

    Keeping the agent output schema this narrow is the primary hallucination
    guard: the agent cannot invent part numbers or prices because those fields
    do not exist in its output contract.
    """

    fault_category: Literal[
        "BEARING_WEAR",
        "SEAL_FAILURE",
        "IMPELLER_WEAR",
        "COUPLING_FAULT",
        "VIBRATION_SENSOR",
        "LUBRICATION",
        "ROUTINE_INSPECTION",
    ] = Field(
        ...,
        description=(
            "Fault classification used by the harness to look up the "
            "maintenance BOM in the ERP. Must be exactly one of the 7 values."
        ),
    )
    fault_description: str = Field(
        ..., min_length=10, max_length=500,
        description="Plain-English description of what was observed in the telemetry."
    )
    recommended_action: str = Field(
        ..., min_length=10, max_length=500,
        description="Plain-English maintenance action to resolve the fault."
    )
    priority: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = Field(
        ..., description="Urgency level. CRITICAL always requires human review."
    )
    requires_shutdown: bool = Field(
        False,
        description="True if the repair cannot be performed with the asset running."
    )
    confidence_score: float = Field(
        ..., ge=0.0, le=1.0,
        description="Agent's self-reported confidence in this diagnosis (0–1)."
    )


class WorkOrderRequest(BaseModel):
    """
    Complete work order built by the harness from AgentDiagnosis + ERP data.
    Never produced directly by the agent — the harness populates required_parts
    from maintenance_bom and estimated_cost_usd from inventory.unit_cost_usd.

    This model is the final artefact that is validated against guardrails and
    written to the ERP if approved.
    """

    asset_id: str = Field(..., description="Target asset identifier")
    fault_description: str = Field(..., min_length=10, max_length=500)
    recommended_action: str = Field(..., min_length=10, max_length=500)
    priority: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = Field(...)
    estimated_cost_usd: float = Field(..., ge=0.0)
    required_parts: List[str] = Field(default_factory=list)
    requires_shutdown: bool = Field(False, description="Whether the repair requires asset downtime")
    confidence_score: float = Field(..., ge=0.0, le=1.0, description="Agent's diagnosis confidence")

    @field_validator("asset_id")
    @classmethod
    def asset_id_uppercase(cls, v: str) -> str:
        return v.upper()


class ValidationResult(BaseModel):
    """
    Structured result from a guardrail or tool checkpoint.
    Used by engine.py to decide whether to approve or block.
    """

    passed: bool
    check_name: str
    detail: str
    blocking: bool = Field(True, description="If True, the engine must not proceed until this passes")


class EngineState(BaseModel):
    """
    Snapshot of the engine's state at any point in the request lifecycle.
    Serialised to the observability log at every transition.
    """

    run_id: str
    asset_id: str
    iteration: int = Field(0, ge=0)
    phase: Literal[
        "INTAKE", "INFERENCE", "VALIDATION",
        "APPROVED", "BLOCKED",
        "HITL_REVIEW", "HITL_APPROVED", "HITL_REJECTED",
        "ERROR",
    ] = "INTAKE"
    scada_reading: Optional[SCADAReading] = None
    diagnosis: Optional[AgentDiagnosis] = None
    work_order_draft: Optional[WorkOrderRequest] = None
    validation_results: List[ValidationResult] = Field(default_factory=list)
    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    error_message: Optional[str] = None
    hitl_operator_notes: Optional[str] = None
    cost_breakdown: Optional[str] = None
