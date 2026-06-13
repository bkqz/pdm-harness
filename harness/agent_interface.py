"""
agent_interface.py — Swappable agent protocol

Defines the AgentProtocol that the engine uses to generate work order drafts.
Two implementations are provided:

  OpenRouterAgent   — production; calls the OpenRouter API via httpx.
                      Requires OPENROUTER_API_KEY. Supports any model on OpenRouter.

  RuleBasedAgent    — deterministic; applies ISO 10816 vibration and API 670 temperature
                      thresholds directly to the SCADA values in the prompt.
                      Requires no API key and makes no network calls.
                      Used to prove harness portability: the same validation pipeline
                      applies regardless of which agent produced the draft.

Swap agents by setting the AGENT_TYPE environment variable:
  AGENT_TYPE=openrouter   (default)
  AGENT_TYPE=rule-based
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

import httpx

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"


# ---------------------------------------------------------------------------
# Protocol — the contract all agents must satisfy
# ---------------------------------------------------------------------------

@runtime_checkable
class AgentProtocol(Protocol):
    def generate(
        self,
        messages: List[Dict[str, str]],
        model: str,
    ) -> tuple[str, int, int]:
        """
        Generate a response for the given message history.

        Returns:
            (content, prompt_tokens, completion_tokens)
            content           — raw text content of the assistant response
            prompt_tokens     — tokens consumed from the prompt (0 if unavailable)
            completion_tokens — tokens in the response (0 if unavailable)
        """
        ...


# ---------------------------------------------------------------------------
# OpenRouterAgent — production LLM via OpenRouter API
# ---------------------------------------------------------------------------

class OpenRouterAgent:
    """
    Calls the OpenRouter chat completions API via httpx.
    Raises httpx.HTTPStatusError on non-2xx responses.
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def generate(
        self,
        messages: List[Dict[str, str]],
        model: str,
    ) -> tuple[str, int, int]:
        response = httpx.post(
            OPENROUTER_API_URL,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": 0.2,
            },
            timeout=60.0,
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        return content, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


# ---------------------------------------------------------------------------
# RuleBasedAgent — deterministic threshold engine (no API call)
# ---------------------------------------------------------------------------

class RuleBasedAgent:
    """
    Deterministic maintenance decision agent based on industry standards:
      - ISO 10816-3 vibration severity zones for process pumps >15 kW
      - API 670 bearing temperature warning and trip limits

    Parses SCADA values from the prompt text injected by the engine and
    applies threshold rules to produce a valid work order JSON without any
    LLM call. The generated JSON passes through the same Pydantic validation,
    SQL checkpoints, and guardrail rules as any LLM-produced draft.

    Part numbers are extracted from the APPROVED PARTS catalogue block
    injected by the engine, so only catalogue-approved parts appear in the output.
    """

    # ISO 10816-3 Zone B/C boundary (vibration in mm/s RMS) — warning threshold
    VIB_WARN_MM_S: float = 4.5
    # ISO 10816-3 Zone C/D boundary — action threshold
    VIB_HIGH_MM_S: float = 7.1
    # ISO 10816-3 trip level
    VIB_TRIP_MM_S: float = 11.2

    # API 670 bearing temperature limits
    TEMP_WARN_C: float = 80.0
    TEMP_TRIP_C: float = 95.0

    def generate(
        self,
        messages: List[Dict[str, str]],
        model: str,
    ) -> tuple[str, int, int]:
        user_msg = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )

        vibration = self._extract_float(user_msg, r"Vibration:\s*([\d.]+)\s*mm/s") or 0.0
        temp      = self._extract_float(user_msg, r"Temperature:\s*([\d.]+)") or 0.0
        codes     = self._extract_str(user_msg, r"Active fault codes:\s*(.+)$") or ""
        flow      = self._extract_float(user_msg, r"Flow Rate:\s*([\d.]+)") or None

        category, fault, action, priority, shutdown, confidence = self._apply_rules(
            vibration, temp, codes, flow
        )

        # AgentDiagnosis only — no part numbers, no cost.
        # The harness resolves parts from the maintenance BOM and costs from inventory.
        diagnosis: Dict[str, Any] = {
            "fault_category": category,
            "fault_description": fault,
            "recommended_action": action,
            "priority": priority,
            "requires_shutdown": shutdown,
            "confidence_score": confidence,
        }
        return json.dumps(diagnosis), 0, 0

    # ------------------------------------------------------------------
    # Threshold logic
    # ------------------------------------------------------------------

    def _apply_rules(
        self, vibration: float, temp: float, codes: str, flow: Optional[float]
    ) -> tuple[str, str, str, str, bool, float]:
        """Returns (fault_category, fault_description, action, priority, shutdown, confidence)."""
        if vibration >= self.VIB_TRIP_MM_S or temp >= self.TEMP_TRIP_C:
            return (
                "BEARING_WEAR",
                f"TRIP-LEVEL: vibration {vibration:.1f} mm/s (ISO 10816 Zone D) / "
                f"bearing temp {temp:.1f} C (API 670 trip). Immediate shutdown required.",
                "Emergency bearing inspection and replacement. Shut down asset immediately "
                "and tag out per lockout-tagout procedure before any maintenance.",
                "CRITICAL",
                True,
                0.95,
            )
        if vibration >= self.VIB_HIGH_MM_S or temp >= self.TEMP_WARN_C:
            return (
                "BEARING_WEAR",
                f"WARNING-ZONE: vibration {vibration:.1f} mm/s (ISO 10816 Zone C) / "
                f"bearing temp {temp:.1f} C (API 670 warning). Trending towards trip.",
                "Schedule bearing inspection within 48 hours. "
                "Increase monitoring frequency to 4-hour intervals until resolved.",
                "HIGH",
                False,
                0.89,
            )
        if vibration >= self.VIB_WARN_MM_S:
            return (
                "BEARING_WEAR",
                f"ELEVATED vibration {vibration:.1f} mm/s (ISO 10816 Zone B/C boundary). "
                "Bearing wear likely — lubrication depletion or early race damage.",
                "Inspect and lubricate bearings. Perform vibration trending analysis. "
                "Schedule bearing replacement within 14 days if trend continues.",
                "MEDIUM",
                False,
                0.83,
            )
        # Low flow with otherwise normal vibration/temp → impeller wear
        if flow is not None and flow < 150.0 and vibration < self.VIB_WARN_MM_S:
            return (
                "IMPELLER_WEAR",
                f"Reduced flow rate {flow:.0f} L/min with normal vibration/temperature. "
                "Suggests progressive impeller or wear-ring degradation.",
                "Inspect impeller and wear rings for erosion or cavitation damage. "
                "Compare differential pressure across pump against design curve.",
                "MEDIUM",
                False,
                0.78,
            )
        if codes and codes.lower() not in ("none", ""):
            return (
                "ROUTINE_INSPECTION",
                f"Active fault codes: {codes}. All analogue parameters within limits.",
                "Inspect components associated with fault codes per OEM procedure. "
                "Clear codes and verify normal operation after service.",
                "MEDIUM",
                False,
                0.80,
            )
        return (
            "ROUTINE_INSPECTION",
            "All parameters within acceptable limits per ISO 10816 and API 670. "
            "Scheduled preventive maintenance recommended.",
            "Perform scheduled inspection: verify lubrication, alignment, coupling, "
            "and seal integrity. Document readings against baseline trend.",
            "LOW",
            False,
            0.87,
        )

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    def _extract_float(self, text: str, pattern: str) -> Optional[float]:
        m = re.search(pattern, text, re.MULTILINE)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
        return None

    def _extract_str(self, text: str, pattern: str) -> Optional[str]:
        m = re.search(pattern, text, re.MULTILINE)
        return m.group(1).strip() if m else None

