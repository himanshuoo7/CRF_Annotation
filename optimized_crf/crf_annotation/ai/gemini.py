"""Gemini as a drop-in for the one call shape this package makes.

`ClaudeClient` hands a request here when the configured model is a Gemini
model (`ACRF_AI_MODEL=gemini-3.8-flash`), so a cheap model can stand in while
the try-it app is in testing and Claude comes back with one setting. Same
structured JSON contract, same cache, same "never fail a run" rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

REQUEST_TIMEOUT_MS = 120_000

# Gemini's thinking levels stop at "high"; the deeper Claude efforts map onto it.
_THINKING_LEVELS = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


def is_gemini(model: str) -> bool:
    return model.startswith("gemini")


def thinking_level(effort: str) -> str:
    return _THINKING_LEVELS.get(effort, "medium")


@dataclass
class GeminiReply:
    text: str
    input_tokens: int
    output_tokens: int
    blocked: Optional[str] = None


def make_client(project: Optional[str], region: str) -> Any:
    """Vertex AI with Application Default Credentials when a project is set;
    otherwise the Gemini API with GEMINI_API_KEY / GOOGLE_API_KEY."""
    from google import genai
    from google.genai import types

    # a hung request must not hang the run: give up after two minutes
    http_options = types.HttpOptions(timeout=REQUEST_TIMEOUT_MS)
    if project:
        return genai.Client(vertexai=True, project=project, location=region, http_options=http_options)
    return genai.Client(http_options=http_options)


def _system_text(system: Sequence[Mapping[str, Any]] | str) -> str:
    if isinstance(system, str):
        return system
    return "\n\n".join(str(block.get("text", "")) for block in system)


def request(
    client: Any,
    *,
    model: str,
    max_tokens: int,
    effort: str,
    system: Sequence[Mapping[str, Any]] | str,
    user: str,
    schema: Mapping[str, Any],
) -> GeminiReply:
    from google.genai import types

    response = client.models.generate_content(
        model=model,
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=_system_text(system),
            max_output_tokens=max_tokens,
            response_mime_type="application/json",
            response_json_schema=dict(schema),
            thinking_config=types.ThinkingConfig(thinking_level=thinking_level(effort)),
            # no tools are sent, so the SDK's automatic function calling is noise
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )
    usage = getattr(response, "usage_metadata", None)
    feedback = getattr(response, "prompt_feedback", None)
    blocked = getattr(feedback, "block_reason", None)
    return GeminiReply(
        text=(response.text or "") if not blocked else "",
        input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
        output_tokens=(getattr(usage, "candidates_token_count", 0) or 0)
        + (getattr(usage, "thoughts_token_count", 0) or 0),
        blocked=str(blocked) if blocked else None,
    )
