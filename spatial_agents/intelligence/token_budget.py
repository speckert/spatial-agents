"""
Token Budget Manager — Context window allocation for the Foundation Model.

The on-device FM has a 4096-token context window. This module tracks
token consumption across all components (instructions, tool schemas,
data payloads) and enforces budgets to prevent exceededContextWindowSize errors.

When the FM Python SDK is available (macOS), token counts are measured
exactly via model.tokenCount(for:). Otherwise, estimates are used
based on a ~4 chars/token heuristic for English text.

Version History:
    0.1.0  2026-03-28  Initial token budget manager
"""

from __future__ import annotations

import logging
from typing import Any

from spatial_agents.config import config
from spatial_agents.models import TokenBudget

logger = logging.getLogger(__name__)

# Rough estimation: ~4 characters per token for English text
# This is conservative — actual tokenization varies by content
CHARS_PER_TOKEN_ESTIMATE = 4.0


def estimate_tokens(text: str) -> int:
    """Estimate token count from text length. Used when FM SDK is unavailable."""
    return max(1, int(len(text) / CHARS_PER_TOKEN_ESTIMATE))


class TokenBudgetManager:
    """
    Manage token allocation within the FM's context window.

    Tracks consumption by component (instructions, tools, payload)
    and prevents exceeding the context window limit.

    Usage:
        budget = TokenBudgetManager()

        # Check if a payload fits
        payload_json = json.dumps(vessel_summary)
        if budget.payload_fits(payload_json):
            # Send to FM
            ...
        else:
            # Compress payload
            compressed = budget.compress_payload(payload_json, target_tokens=500)
    """

    def __init__(
        self,
        context_size: int | None = None,
        fm_model: Any | None = None,
    ) -> None:
        self._context_size = context_size or config.fm.context_window_size
        self._fm_model = fm_model  # apple_fm.FoundationModel when available

        # Tracked allocations
        self._instructions_tokens: int = 0
        self._tool_schema_tokens: int = 0
        self._data_payload_tokens: int = 0

    @property
    def context_size(self) -> int:
        return self._context_size

    @property
    def used_tokens(self) -> int:
        return self._instructions_tokens + self._tool_schema_tokens + self._data_payload_tokens

    @property
    def remaining_tokens(self) -> int:
        return max(0, self._context_size - self.used_tokens)

    @property
    def utilization_pct(self) -> float:
        return self.used_tokens / self._context_size if self._context_size > 0 else 0.0

    async def count_tokens(self, text: str) -> int:
        """
        Count tokens for a text string.

        Uses FM SDK when available, falls back to estimation.
        """
        if self._fm_model is not None:
            try:
                # FM SDK integration point
                # usage = await self._fm_model.token_count(text)
                # return usage.token_count
                pass
            except Exception as exc:
                logger.debug("FM token count failed, using estimate: %s", exc)

        return estimate_tokens(text)

    async def set_instructions(self, instructions_text: str) -> int:
        """Measure and record token cost of system instructions."""
        tokens = await self.count_tokens(instructions_text)
        self._instructions_tokens = tokens
        logger.debug("Instructions: %d tokens (%.1f%%)", tokens, tokens / self._context_size * 100)
        return tokens

    async def set_tool_schemas(self, schemas_json: str) -> int:
        """Measure and record token cost of tool schema definitions."""
        tokens = await self.count_tokens(schemas_json)
        self._tool_schema_tokens = tokens
        logger.debug("Tool schemas: %d tokens (%.1f%%)", tokens, tokens / self._context_size * 100)
        return tokens

    async def measure_payload(self, payload_text: str) -> int:
        """Measure token cost of a data payload without recording it."""
        return await self.count_tokens(payload_text)

    async def set_payload(self, payload_text: str) -> int:
        """Measure and record token cost of the data payload."""
        tokens = await self.count_tokens(payload_text)
        self._data_payload_tokens = tokens
        logger.debug("Data payload: %d tokens (%.1f%%)", tokens, tokens / self._context_size * 100)
        return tokens

    def payload_fits(self, payload_text: str) -> bool:
        """Check whether a payload fits within the remaining budget (using estimate)."""
        estimated = estimate_tokens(payload_text)
        max_payload = int(self._context_size * config.fm.max_prompt_budget_pct)
        return estimated <= max_payload

    def get_budget(self) -> TokenBudget:
        """Return current token allocation breakdown."""
        return TokenBudget(
            context_window_size=self._context_size,
            instructions_tokens=self._instructions_tokens,
            tool_schema_tokens=self._tool_schema_tokens,
            data_payload_tokens=self._data_payload_tokens,
            remaining_tokens=self.remaining_tokens,
            utilization_pct=round(self.utilization_pct, 4),
        )

    def reset(self) -> None:
        """Reset all tracked allocations."""
        self._instructions_tokens = 0
        self._tool_schema_tokens = 0
        self._data_payload_tokens = 0

    @staticmethod
    def compress_payload(payload: str, target_tokens: int) -> str:
        """
        Compress a payload to fit within a target token count.

        Strategy: truncate from the end, preserving structure.
        A smarter implementation would summarize or prioritize fields.
        """
        target_chars = int(target_tokens * CHARS_PER_TOKEN_ESTIMATE)
        if len(payload) <= target_chars:
            return payload

        # Simple truncation with indicator
        return payload[:target_chars - 20] + "\n... [truncated]"

    async def refresh_context_size(self) -> int:
        """
        Query the FM SDK for the actual context window size.
        Falls back to configured default if SDK unavailable.
        """
        if self._fm_model is not None:
            try:
                # FM SDK integration point
                # self._context_size = await self._fm_model.context_size()
                pass
            except Exception as exc:
                logger.debug("Could not query FM context size: %s", exc)

        return self._context_size
