"""Thin Claude wrapper with the properties a validated pipeline needs.

Three rules shape everything here:

* **Optional.** `anthropic` is not a hard dependency. Nothing imports this
  module unless AI assist is switched on.
* **Cached.** Every response is stored under a hash of its exact input. A
  rerun of the same study re-reads the cache and calls nothing, so a rerun is
  reproducible and free.
* **Reviewable.** Results carry a confidence and a rationale, and land in the
  config workbook where a human signs them off.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from ..logging_utils import get_logger
from . import gemini

logger = get_logger()

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 16000
CACHE_VERSION = "1"
# `output_config` and adaptive `thinking` need a reasonably current SDK. An
# older one fails per request deep inside the run, which is exactly the kind
# of silent degradation this pipeline refuses — so it is checked once, up
# front, and reported as a single clear warning.
MIN_SDK_VERSION = (0, 40)

# Adaptive thinking and `output_config.effort` exist only on the 4.6-and-later
# families. Sending either to Haiku 4.5 or Sonnet 4.5 is a 400, so the request
# is shaped to the model rather than assuming the newest surface.
_MODERN_FAMILIES = (
    "opus-5", "opus-4-6", "opus-4-7", "opus-4-8",
    "sonnet-5", "sonnet-4-6",
    "fable-5", "mythos-5",
)


def supports_adaptive_thinking(model: str) -> bool:
    return any(family in model for family in _MODERN_FAMILIES)


def supports_effort(model: str) -> bool:
    # Opus 4.5 accepts effort too, but only low/medium/high.
    return supports_adaptive_thinking(model) or "opus-4-5" in model


class AiUnavailable(RuntimeError):
    """The `anthropic` SDK is not installed, or no credentials are configured."""


@dataclass
class AiSettings:
    """Everything that changes an AI result — hashed into the cache key."""

    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    effort: str = "high"
    prompt_version: str = "1"
    cache_dir: Optional[Path] = None
    enabled: bool = True
    """AI assist is on by default. Set ACRF_AI=0, or matching.ai=False in the
    payload, to run with the deterministic tiers only."""
    vertex_project: Optional[str] = None
    """Set to call Claude through Google Vertex AI in this Google Cloud project
    (Application Default Credentials, no Anthropic key) instead of the
    Anthropic API. Same model IDs, same answers, so not part of the cache key."""
    vertex_region: str = "global"

    @classmethod
    def from_env(cls, **overrides: Any) -> AiSettings:
        """Read settings from `ACRF_AI_*` environment variables."""
        settings = cls(
            model=os.getenv("ACRF_AI_MODEL", DEFAULT_MODEL),
            effort=os.getenv("ACRF_AI_EFFORT", "high"),
            cache_dir=Path(os.getenv("ACRF_AI_CACHE", "")) if os.getenv("ACRF_AI_CACHE") else None,
            enabled=os.getenv("ACRF_AI", "1").strip().lower() not in {"0", "false", "no", "off"},
            vertex_project=os.getenv("ACRF_VERTEX_PROJECT") or os.getenv("ANTHROPIC_VERTEX_PROJECT_ID") or None,
            vertex_region=os.getenv("ACRF_VERTEX_REGION") or os.getenv("CLOUD_ML_REGION") or "global",
        )
        for key, value in overrides.items():
            if value is not None:
                setattr(settings, key, value)
        return settings

    def fingerprint(self) -> str:
        return f"{CACHE_VERSION}:{self.model}:{self.effort}:{self.prompt_version}"


class ResponseCache:
    """Content-addressed JSON cache. Absent cache_dir means no caching."""

    def __init__(self, directory: Optional[Path]) -> None:
        self.directory = Path(directory) if directory else None

    @staticmethod
    def key(*parts: Any) -> str:
        digest = hashlib.sha256()
        for part in parts:
            digest.update(json.dumps(part, sort_keys=True, default=str).encode("utf-8"))
            digest.update(b"\x00")
        return digest.hexdigest()

    def get(self, key: str) -> Optional[Any]:
        if not self.directory:
            return None
        path = self.directory / f"{key}.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def put(self, key: str, value: Any) -> None:
        if not self.directory:
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            (self.directory / f"{key}.json").write_text(
                json.dumps(value, indent=2, sort_keys=True, default=str), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("Could not write AI cache entry: %s", exc)


class ClaudeClient:
    """Structured-output JSON calls against the Messages API — or against
    Gemini when the configured model is a Gemini model (see `gemini.py`)."""

    def __init__(self, settings: Optional[AiSettings] = None) -> None:
        self.settings = settings or AiSettings()
        self.cache = ResponseCache(self.settings.cache_dir)
        self._client: Any = None
        self.calls = 0
        self.cache_hits = 0
        self.input_tokens = 0
        self.output_tokens = 0

    # -- lifecycle ----------------------------------------------------------
    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if gemini.is_gemini(self.settings.model):
            try:
                self._client = gemini.make_client(self.settings.vertex_project, self.settings.vertex_region)
            except ImportError as exc:  # pragma: no cover - depends on install
                raise AiUnavailable("Gemini needs the Google Gen AI SDK: pip install google-genai") from exc
            except Exception as exc:  # noqa: BLE001 - surfaces as a clean message
                raise AiUnavailable(f"Could not create a Gemini client: {exc}") from exc
            return self._client
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on install
            raise AiUnavailable(
                "AI assist needs the Anthropic SDK: pip install 'anthropic>=0.40'"
            ) from exc

        version = getattr(anthropic, "__version__", "0")
        try:
            parsed = tuple(int(part) for part in version.split(".")[:2])
        except ValueError:
            parsed = MIN_SDK_VERSION  # unparseable (a dev build): assume current
        if parsed < MIN_SDK_VERSION:
            raise AiUnavailable(
                f"Anthropic SDK {version} is too old for AI assist "
                f"(need >= {'.'.join(str(p) for p in MIN_SDK_VERSION)}): "
                "pip install -U anthropic"
            )

        if self.settings.vertex_project:
            try:
                import google.auth  # noqa: F401 - AnthropicVertex needs it per request
            except ImportError as exc:  # pragma: no cover - depends on install
                raise AiUnavailable(
                    "Claude on Vertex AI needs Google auth: pip install 'anthropic[vertex]'"
                ) from exc
        try:
            if self.settings.vertex_project:
                # Application Default Credentials: the Cloud Run service
                # account, or `gcloud auth application-default login` locally.
                self._client = anthropic.AnthropicVertex(
                    project_id=self.settings.vertex_project, region=self.settings.vertex_region
                )
            else:
                # Resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an
                # `ant auth login` profile, in that order.
                self._client = anthropic.Anthropic()
        except Exception as exc:  # noqa: BLE001 - surfaces as a clean message
            raise AiUnavailable(f"Could not create an Anthropic client: {exc}") from exc
        return self._client

    def available(self) -> bool:
        """Probe once, up front, so a missing SDK or credential is one clear
        warning at startup rather than an error per batch mid-run."""
        try:
            self._ensure_client()
        except AiUnavailable as exc:
            logger.warning("%s", exc)
            return False
        return True

    # -- the one call shape this package makes ------------------------------
    def json_call(
        self,
        *,
        system: Sequence[Mapping[str, Any]] | str,
        user: str,
        schema: Mapping[str, Any],
        cache_salt: Any = None,
    ) -> Optional[dict[str, Any]]:
        """One structured-output request. Returns None if the call failed."""
        key = ResponseCache.key(self.settings.fingerprint(), system, user, schema, cache_salt)
        cached = self.cache.get(key)
        if cached is not None:
            self.cache_hits += 1
            return cached

        client = self._ensure_client()

        model = self.settings.model
        if gemini.is_gemini(model):
            return self._gemini_call(client, key, system=system, user=user, schema=schema)
        # Structured output works on every model; thinking and effort do not.
        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": dict(schema)}
        }
        request: dict[str, Any] = {}
        if supports_effort(model):
            output_config["effort"] = self.settings.effort
        if supports_adaptive_thinking(model):
            request["thinking"] = {"type": "adaptive"}

        try:
            response = client.messages.create(
                model=model,
                max_tokens=self.settings.max_tokens,
                system=system,
                output_config=output_config,
                messages=[{"role": "user", "content": user}],
                **request,
            )
        except Exception as exc:  # noqa: BLE001 - never fail a run over AI assist
            logger.error("Claude request failed: %s", exc)
            return None

        self.calls += 1
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.input_tokens += getattr(usage, "input_tokens", 0) or 0
            self.output_tokens += getattr(usage, "output_tokens", 0) or 0

        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            logger.error("Claude declined the request (%s)", getattr(details, "category", "unknown"))
            return None

        text = next((b.text for b in response.content if b.type == "text"), "")
        if not text:
            logger.error("Claude returned no text content")
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.error("Claude returned unparseable JSON: %s", exc)
            return None

        self.cache.put(key, parsed)
        return parsed

    def _gemini_call(
        self,
        client: Any,
        key: str,
        *,
        system: Sequence[Mapping[str, Any]] | str,
        user: str,
        schema: Mapping[str, Any],
    ) -> Optional[dict[str, Any]]:
        reply = None
        for attempt in range(1 + len(gemini.RETRY_DELAYS)):
            try:
                reply = gemini.request(
                    client, model=self.settings.model, max_tokens=self.settings.max_tokens,
                    effort=self.settings.effort, system=system, user=user, schema=schema,
                )
                break
            except Exception as exc:  # noqa: BLE001 - never fail a run over AI assist
                # A timeout or an overloaded server is worth another try; a
                # whole form left unmapped because one call blinked is not.
                if attempt < len(gemini.RETRY_DELAYS) and gemini.is_transient(exc):
                    logger.warning("Gemini request failed (%s); retrying", str(exc)[:80])
                    time.sleep(gemini.RETRY_DELAYS[attempt])
                    continue
                logger.error("Gemini request failed: %s", exc)
                return None
        if reply is None:
            return None

        self.calls += 1
        self.input_tokens += reply.input_tokens
        self.output_tokens += reply.output_tokens
        if reply.blocked:
            logger.error("Gemini declined the request (%s)", reply.blocked)
            return None
        if not reply.text:
            logger.error("Gemini returned no text content")
            return None
        try:
            parsed = json.loads(reply.text)
        except json.JSONDecodeError as exc:
            logger.error("Gemini returned unparseable JSON: %s", exc)
            return None

        self.cache.put(key, parsed)
        return parsed

    # -- reporting ----------------------------------------------------------
    def usage_summary(self) -> str:
        return (
            f"{self.calls} {'Gemini' if gemini.is_gemini(self.settings.model) else 'Claude'} call(s), {self.cache_hits} cache hit(s), "
            f"{self.input_tokens} input / {self.output_tokens} output tokens"
        )
