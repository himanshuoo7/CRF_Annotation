"""Optional AI assist. Nothing here is imported unless it is switched on.

Two distinct uses, with very different risk profiles:

* `schema_infer` runs at **design time** and produces a parser profile that a
  human reviews and commits. Its output is configuration, not study data.
* `label_match` runs at **run time** on the residue the deterministic tiers
  could not match. Its output is flagged in the config workbook with a
  confidence and a reason, and a reviewer decides whether it stands.

Neither is on by default; see `crf_annotation.ai.build_resolvers`.
"""

from __future__ import annotations

from typing import Optional

from ..logging_utils import get_logger
from ..matching import FuzzyResolver, LabelResolver
from .client import AiSettings, AiUnavailable, ClaudeClient

logger = get_logger()

__all__ = [
    "AiSettings",
    "AiUnavailable",
    "ClaudeClient",
    "build_resolvers",
]


def build_resolvers(
    settings: Optional[AiSettings] = None,
    fuzzy_threshold: Optional[float] = None,
) -> tuple[LabelResolver, ...]:
    """The resolver chain for a run: fuzzy always, then Claude unless disabled."""
    resolvers: list[LabelResolver] = [
        FuzzyResolver(threshold=fuzzy_threshold) if fuzzy_threshold
        else FuzzyResolver()
    ]

    settings = settings or AiSettings.from_env()
    if not settings.enabled:
        logger.info("AI assist disabled; using deterministic tiers only")
        return tuple(resolvers)

    try:
        from .label_match import ClaudeLabelResolver

        resolver = ClaudeLabelResolver.from_settings(settings)
    except AiUnavailable as exc:
        logger.warning("AI assist unavailable (%s); continuing without it", exc)
        return tuple(resolvers)

    if not resolver.client.available():
        # `available()` has already logged the specific reason (missing SDK,
        # SDK too old, no credential).
        logger.warning("AI assist is enabled but could not start; continuing "
                       "with the deterministic tiers only")
        return tuple(resolvers)

    resolvers.append(resolver)
    # Report the request shape actually sent, not the configured default —
    # effort and thinking are dropped for models that reject them.
    from .client import supports_adaptive_thinking, supports_effort
    from .gemini import is_gemini, thinking_level

    shape = f"thinking level {thinking_level(settings.effort)}" if is_gemini(settings.model) else ", ".join(
        part for part in (
            f"effort={settings.effort}" if supports_effort(settings.model) else "",
            "adaptive thinking" if supports_adaptive_thinking(settings.model) else "",
        ) if part
    ) or "no thinking/effort (model does not support them)"
    logger.info("AI assist enabled: %s [%s], cache: %s",
                settings.model, shape, settings.cache_dir or "disabled")
    return tuple(resolvers)
