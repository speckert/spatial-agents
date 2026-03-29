"""
Intelligence — Foundation Models prompt evaluation and token management.

Provides tools for developing, testing, and validating prompts against
Apple's on-device Foundation Model using the Python FM SDK (macOS).

Version History:
    0.1.0  2026-03-28  Initial intelligence package with prompt library,
                       token budget manager, schema validator, and eval harness
"""

from spatial_agents.intelligence.token_budget import TokenBudgetManager
from spatial_agents.intelligence.prompt_templates import PromptLibrary
from spatial_agents.intelligence.schema_validator import SchemaValidator
from spatial_agents.intelligence.eval_harness import EvalHarness

__all__ = ["TokenBudgetManager", "PromptLibrary", "SchemaValidator", "EvalHarness"]
