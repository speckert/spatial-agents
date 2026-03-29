"""
Eval Harness — Batch prompt evaluation, scoring, and regression testing.

Runs prompt templates against test fixtures, validates outputs,
and tracks performance across model versions. Critical for catching
regressions when Apple updates the on-device model (e.g. 26.4).

Version History:
    0.1.0  2026-03-28  Initial eval harness
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from spatial_agents.intelligence.prompt_templates import PromptLibrary, PromptTemplate
from spatial_agents.intelligence.schema_validator import SchemaValidator, ValidationResult
from spatial_agents.intelligence.token_budget import TokenBudgetManager

logger = logging.getLogger(__name__)


@dataclass
class EvalCase:
    """A single evaluation test case."""
    name: str
    domain: str
    template_name: str
    template_vars: dict[str, Any]
    expected_schema: str
    description: str = ""


@dataclass
class EvalResult:
    """Result of a single evaluation run."""
    case_name: str
    template_version: str
    rendered_prompt: str
    prompt_tokens: int
    fm_output: str | None
    validation: ValidationResult | None
    latency_ms: float
    error: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def passed(self) -> bool:
        return self.validation is not None and self.validation.valid and self.error is None


@dataclass
class EvalSuite:
    """Collection of eval cases for batch execution."""
    name: str
    cases: list[EvalCase]
    description: str = ""


class EvalHarness:
    """
    Run prompt evaluations against the Foundation Model.

    Supports two modes:
    1. Live mode: actually calls the FM SDK (macOS only)
    2. Dry-run mode: renders prompts, measures tokens, validates structure
       without calling the model. Useful for CI and cross-platform dev.

    Usage:
        harness = EvalHarness()
        suite = harness.load_suite("maritime_basic")
        results = await harness.run_suite(suite)
        report = harness.generate_report(results)
    """

    def __init__(
        self,
        prompt_library: PromptLibrary | None = None,
        schema_validator: SchemaValidator | None = None,
        budget_manager: TokenBudgetManager | None = None,
        fm_model: Any | None = None,
    ) -> None:
        self._library = prompt_library or PromptLibrary()
        self._validator = schema_validator or SchemaValidator()
        self._budget = budget_manager or TokenBudgetManager()
        self._fm_model = fm_model  # None = dry-run mode
        self._results_history: list[EvalResult] = []

    @property
    def is_live(self) -> bool:
        """Whether the harness has a live FM connection."""
        return self._fm_model is not None

    async def run_case(self, case: EvalCase) -> EvalResult:
        """Run a single evaluation case."""
        template = self._library.get(case.domain, case.template_name)
        if template is None:
            return EvalResult(
                case_name=case.name,
                template_version="unknown",
                rendered_prompt="",
                prompt_tokens=0,
                fm_output=None,
                validation=None,
                latency_ms=0,
                error=f"Template not found: {case.domain}/{case.template_name}",
            )

        # Render the prompt
        try:
            rendered = template.render_user_prompt(**case.template_vars)
        except KeyError as exc:
            return EvalResult(
                case_name=case.name,
                template_version=template.version,
                rendered_prompt="",
                prompt_tokens=0,
                fm_output=None,
                validation=None,
                latency_ms=0,
                error=f"Template render error — missing variable: {exc}",
            )

        # Measure tokens
        prompt_tokens = await self._budget.measure_payload(rendered)
        instructions_tokens = await self._budget.measure_payload(template.system_instructions)

        start = time.monotonic()
        fm_output: str | None = None
        validation: ValidationResult | None = None
        error: str | None = None

        if self._fm_model is not None:
            # Live FM evaluation
            try:
                # FM SDK integration point:
                # session = LanguageModelSession(
                #     model=self._fm_model,
                #     instructions=Instructions(template.system_instructions),
                # )
                # response = await session.respond(to=Prompt(rendered))
                # fm_output = response.content
                pass
            except Exception as exc:
                error = f"FM evaluation error: {exc}"
        else:
            # Dry-run mode — no FM output to validate
            logger.info(
                "Dry-run: %s — %d prompt tokens, %d instruction tokens",
                case.name, prompt_tokens, instructions_tokens,
            )

        latency_ms = (time.monotonic() - start) * 1000

        # Validate output if we have one
        if fm_output is not None:
            validation = self._validator.validate(fm_output, case.expected_schema)

        result = EvalResult(
            case_name=case.name,
            template_version=template.version,
            rendered_prompt=rendered,
            prompt_tokens=prompt_tokens,
            fm_output=fm_output,
            validation=validation,
            latency_ms=latency_ms,
            error=error,
        )

        self._results_history.append(result)
        return result

    async def run_suite(self, suite: EvalSuite) -> list[EvalResult]:
        """Run all cases in an evaluation suite."""
        logger.info("Running eval suite: %s (%d cases)", suite.name, len(suite.cases))
        results: list[EvalResult] = []

        for case in suite.cases:
            result = await self.run_case(case)
            results.append(result)
            status = "PASS" if result.passed else "FAIL"
            logger.info("  [%s] %s (%.1fms, %d tokens)", status, case.name, result.latency_ms, result.prompt_tokens)

        return results

    def generate_report(self, results: list[EvalResult]) -> dict[str, Any]:
        """Generate a summary report from evaluation results."""
        total = len(results)
        passed = sum(1 for r in results if r.passed)
        failed = total - passed
        errors = [r.error for r in results if r.error]
        avg_tokens = sum(r.prompt_tokens for r in results) / total if total else 0
        avg_latency = sum(r.latency_ms for r in results) / total if total else 0

        return {
            "summary": {
                "total": total,
                "passed": passed,
                "failed": failed,
                "pass_rate": passed / total if total else 0,
                "mode": "live" if self.is_live else "dry-run",
            },
            "performance": {
                "avg_prompt_tokens": round(avg_tokens, 1),
                "avg_latency_ms": round(avg_latency, 1),
            },
            "errors": errors,
            "results": [
                {
                    "case": r.case_name,
                    "passed": r.passed,
                    "tokens": r.prompt_tokens,
                    "latency_ms": round(r.latency_ms, 1),
                    "error": r.error,
                    "validation_summary": r.validation.summary if r.validation else None,
                }
                for r in results
            ],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def save_report(self, report: dict, path: Path) -> None:
        """Save evaluation report to JSON file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, default=str))
        logger.info("Report saved: %s", path)


# ---------------------------------------------------------------------------
# Built-in Test Suites
# ---------------------------------------------------------------------------

MARITIME_BASIC_SUITE = EvalSuite(
    name="maritime_basic",
    description="Basic maritime situation report evaluation",
    cases=[
        EvalCase(
            name="oakland_harbor_busy",
            domain="maritime",
            template_name="maritime_situation_report",
            template_vars={
                "h3_cell": "842831dffffffff",
                "resolution": 4,
                "vessel_count": 23,
                "temporal_bin": "1hour",
                "activity_summary": (
                    "Vessel types: 12 cargo, 5 tanker, 3 tug, 2 passenger, 1 fishing. "
                    "Avg speed: 4.2 knots. 3 vessels stationary (likely at berth). "
                    "1 vessel showing erratic course changes. "
                    "Traffic density: above average for this time period."
                ),
            },
            expected_schema="SituationReport",
        ),
        EvalCase(
            name="open_ocean_sparse",
            domain="maritime",
            template_name="maritime_situation_report",
            template_vars={
                "h3_cell": "832830fffffffff",
                "resolution": 3,
                "vessel_count": 2,
                "temporal_bin": "1day",
                "activity_summary": (
                    "2 cargo vessels transiting. Both on standard shipping lane. "
                    "Speeds: 14.5, 16.2 knots. No anomalies detected."
                ),
            },
            expected_schema="SituationReport",
        ),
        EvalCase(
            name="sf_bay_mixed",
            domain="maritime",
            template_name="maritime_situation_report",
            template_vars={
                "h3_cell": "842830dffffffff",
                "resolution": 4,
                "vessel_count": 45,
                "temporal_bin": "1hour",
                "activity_summary": (
                    "Vessel types: 8 cargo, 3 tanker, 15 sailing, 12 pleasure, 4 tug, 3 fishing. "
                    "High recreational traffic. 2 large cargo vessels inbound. "
                    "1 AIS gap detected: tanker MMSI 367000001 silent for 45 minutes. "
                    "Wind: 18 knots WSW. Ebb tide."
                ),
            },
            expected_schema="SituationReport",
        ),
    ],
)
