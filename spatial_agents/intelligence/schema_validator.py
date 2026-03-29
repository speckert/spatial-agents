"""
Schema Validator — Validate FM structured outputs against expected schemas.

Ensures that Foundation Model guided generation produces output matching
the @Generable struct definitions used in the Swift client. Catches
schema drift before it reaches production.

Version History:
    0.1.0  2026-03-28  Initial schema validator
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Type

from pydantic import BaseModel, ValidationError

from spatial_agents.models import SituationReport

logger = logging.getLogger(__name__)

# Registry of expected output schemas, keyed by name
SCHEMA_REGISTRY: dict[str, Type[BaseModel]] = {
    "SituationReport": SituationReport,
}


@dataclass
class ValidationResult:
    """Result of a schema validation check."""
    valid: bool
    schema_name: str
    errors: list[str]
    warnings: list[str]
    raw_output: str

    @property
    def summary(self) -> str:
        status = "PASS" if self.valid else "FAIL"
        error_str = f" — {len(self.errors)} error(s)" if self.errors else ""
        return f"[{status}] {self.schema_name}{error_str}"


class SchemaValidator:
    """
    Validate FM outputs against registered Pydantic schemas.

    Usage:
        validator = SchemaValidator()
        result = validator.validate(fm_output_json, "SituationReport")
        if not result.valid:
            for error in result.errors:
                print(error)
    """

    def __init__(self) -> None:
        self._schemas = dict(SCHEMA_REGISTRY)
        self._validation_count = 0
        self._failure_count = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "validations": self._validation_count,
            "failures": self._failure_count,
        }

    def register_schema(self, name: str, schema: Type[BaseModel]) -> None:
        """Register an additional output schema."""
        self._schemas[name] = schema

    def validate(self, raw_output: str | dict, schema_name: str) -> ValidationResult:
        """
        Validate FM output against a named schema.

        Args:
            raw_output: JSON string or parsed dict from FM response
            schema_name: Key in the schema registry

        Returns:
            ValidationResult with errors and warnings
        """
        self._validation_count += 1

        schema = self._schemas.get(schema_name)
        if schema is None:
            self._failure_count += 1
            return ValidationResult(
                valid=False,
                schema_name=schema_name,
                errors=[f"Unknown schema: {schema_name}"],
                warnings=[],
                raw_output=str(raw_output),
            )

        errors: list[str] = []
        warnings: list[str] = []

        # Parse if string
        if isinstance(raw_output, str):
            import orjson
            try:
                parsed = orjson.loads(raw_output)
            except Exception as exc:
                self._failure_count += 1
                return ValidationResult(
                    valid=False,
                    schema_name=schema_name,
                    errors=[f"JSON parse error: {exc}"],
                    warnings=[],
                    raw_output=raw_output,
                )
        else:
            parsed = raw_output

        # Validate against Pydantic schema
        try:
            instance = schema.model_validate(parsed)

            # Additional semantic checks
            warnings.extend(self._semantic_checks(instance, schema_name))

        except ValidationError as exc:
            self._failure_count += 1
            for err in exc.errors():
                loc = " → ".join(str(l) for l in err["loc"])
                errors.append(f"{loc}: {err['msg']} ({err['type']})")

        valid = len(errors) == 0
        if not valid:
            self._failure_count += 1

        return ValidationResult(
            valid=valid,
            schema_name=schema_name,
            errors=errors,
            warnings=warnings,
            raw_output=str(raw_output),
        )

    def _semantic_checks(self, instance: BaseModel, schema_name: str) -> list[str]:
        """Run domain-specific semantic validation beyond schema structure."""
        warnings: list[str] = []

        if schema_name == "SituationReport" and isinstance(instance, SituationReport):
            # Check for suspiciously short summaries
            if len(instance.summary) < 20:
                warnings.append("Summary is very short — may lack detail")

            # Check observation count
            if len(instance.key_observations) == 0:
                warnings.append("No key observations generated")

            # Check confidence range
            if instance.confidence < 0.1:
                warnings.append("Very low confidence — model may be uncertain")

        return warnings

    def validate_batch(
        self,
        outputs: list[str | dict],
        schema_name: str,
    ) -> list[ValidationResult]:
        """Validate a batch of FM outputs."""
        return [self.validate(output, schema_name) for output in outputs]

    def batch_summary(self, results: list[ValidationResult]) -> dict:
        """Summarize a batch of validation results."""
        total = len(results)
        passed = sum(1 for r in results if r.valid)
        failed = total - passed
        all_errors = [e for r in results for e in r.errors]

        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": passed / total if total > 0 else 0.0,
            "unique_errors": list(set(all_errors)),
        }
