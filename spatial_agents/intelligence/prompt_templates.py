"""
Prompt Templates — Versioned prompt library for FM evaluation.

Each data domain (maritime, aviation, orbital) has a set of prompt
templates with version tracking. Templates are designed to produce
structured output matching the @Generable SituationReport schema.

Prompts are engineered against the 4096-token context window:
    - System instructions: ~30 tokens
    - Tool schemas: ~80-200 tokens (depending on tool count)
    - Data payload: variable (budget-managed)
    - Response: remaining tokens

Version History:
    0.1.0  2026-03-28  Initial prompt templates
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from spatial_agents.models import DataDomain


@dataclass(frozen=True)
class PromptTemplate:
    """A versioned prompt template."""
    domain: DataDomain
    version: str
    name: str
    system_instructions: str
    user_prompt_template: str
    expected_output_schema: str
    notes: str = ""

    def render_user_prompt(self, **kwargs: Any) -> str:
        """Render the user prompt template with provided variables."""
        return self.user_prompt_template.format(**kwargs)


# ---------------------------------------------------------------------------
# Maritime Prompts
# ---------------------------------------------------------------------------

MARITIME_SITREP_V1 = PromptTemplate(
    domain=DataDomain.MARITIME,
    version="1.0.0",
    name="maritime_situation_report",
    system_instructions=(
        "You are a maritime intelligence analyst. Given vessel activity data "
        "for a geographic area, generate a structured situation report. "
        "Focus on patterns, anomalies, and operationally relevant observations. "
        "Be concise — every token matters."
    ),
    user_prompt_template=(
        "Analyze the following vessel activity in H3 cell {h3_cell} "
        "(resolution {resolution}):\n\n"
        "Vessel count: {vessel_count}\n"
        "Time window: {temporal_bin}\n"
        "Activity summary:\n{activity_summary}\n\n"
        "Provide a situation report with: summary, key observations (max 5), "
        "anomalies (if any), and confidence score (0-1)."
    ),
    expected_output_schema="SituationReport",
    notes="Designed for res 4-5 tiles with 10-50 vessels. "
          "Payload should be pre-summarized to fit token budget.",
)

MARITIME_ANOMALY_V1 = PromptTemplate(
    domain=DataDomain.MARITIME,
    version="1.0.0",
    name="maritime_anomaly_detection",
    system_instructions=(
        "You are a maritime anomaly detection system. Given vessel behavior data, "
        "identify unusual patterns that may indicate: dark vessel activity (AIS gaps), "
        "loitering, unexpected route deviations, or spoofing. "
        "Report findings with confidence levels."
    ),
    user_prompt_template=(
        "Review vessel behavior in H3 cell {h3_cell}:\n\n"
        "{behavior_data}\n\n"
        "Identify anomalies. For each, provide: type, description, "
        "affected vessel(s), and confidence (0-1)."
    ),
    expected_output_schema="AnomalyReport",
)

# ---------------------------------------------------------------------------
# Aviation Prompts
# ---------------------------------------------------------------------------

AVIATION_SITREP_V1 = PromptTemplate(
    domain=DataDomain.AVIATION,
    version="1.0.0",
    name="aviation_situation_report",
    system_instructions=(
        "You are an aviation intelligence analyst. Given aircraft activity data "
        "for a geographic area, generate a structured situation report. "
        "Note flight patterns, altitude distributions, and traffic density."
    ),
    user_prompt_template=(
        "Analyze aircraft activity in H3 cell {h3_cell} "
        "(resolution {resolution}):\n\n"
        "Aircraft count: {aircraft_count}\n"
        "Time window: {temporal_bin}\n"
        "Activity summary:\n{activity_summary}\n\n"
        "Provide a situation report with: summary, key observations (max 5), "
        "anomalies (if any), and confidence score (0-1)."
    ),
    expected_output_schema="SituationReport",
)

# ---------------------------------------------------------------------------
# Causal Narration Prompts
# ---------------------------------------------------------------------------

CAUSAL_NARRATION_V1 = PromptTemplate(
    domain=DataDomain.MARITIME,  # Used across domains
    version="1.0.0",
    name="causal_graph_narration",
    system_instructions=(
        "You are an intelligence analyst explaining causal relationships "
        "in geospatial event data. Given a causal graph (nodes = events, "
        "edges = causal links), generate a clear narrative explaining "
        "why observed patterns occurred. Use the causal structure — "
        "don't just describe correlations."
    ),
    user_prompt_template=(
        "Explain the following causal graph for H3 cell {h3_cell}:\n\n"
        "Nodes (events):\n{nodes}\n\n"
        "Edges (causal links):\n{edges}\n\n"
        "Intervention results:\n{interventions}\n\n"
        "Generate a clear narrative explaining the causal chain."
    ),
    expected_output_schema="SituationReport",
    notes="Causal graphs are pre-built by the Python causal module. "
          "The FM narrates them — it does not construct them.",
)


# ---------------------------------------------------------------------------
# Prompt Library
# ---------------------------------------------------------------------------

class PromptLibrary:
    """
    Registry of all prompt templates, indexed by domain and name.

    Usage:
        library = PromptLibrary()
        template = library.get("maritime", "maritime_situation_report")
        prompt = template.render_user_prompt(
            h3_cell="842831dffffffff",
            resolution=4,
            vessel_count=23,
            temporal_bin="1hour",
            activity_summary="..."
        )
    """

    def __init__(self) -> None:
        self._templates: dict[tuple[str, str], PromptTemplate] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        """Register all built-in templates."""
        for template in [
            MARITIME_SITREP_V1,
            MARITIME_ANOMALY_V1,
            AVIATION_SITREP_V1,
            CAUSAL_NARRATION_V1,
        ]:
            self.register(template)

    def register(self, template: PromptTemplate) -> None:
        """Register a prompt template."""
        key = (template.domain.value, template.name)
        self._templates[key] = template

    def get(self, domain: str, name: str) -> PromptTemplate | None:
        """Retrieve a template by domain and name."""
        return self._templates.get((domain, name))

    def list_templates(self, domain: str | None = None) -> list[PromptTemplate]:
        """List all templates, optionally filtered by domain."""
        templates = list(self._templates.values())
        if domain:
            templates = [t for t in templates if t.domain.value == domain]
        return templates

    def domains(self) -> list[str]:
        """Return all registered domains."""
        return sorted(set(d for d, _ in self._templates.keys()))
