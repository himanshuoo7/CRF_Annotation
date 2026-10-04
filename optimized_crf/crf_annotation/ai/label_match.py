"""Claude as the last matching tier.

Only ever sees CRF text that exact and fuzzy matching could not resolve, and
only ever chooses from the fields defined on that page's form — it cannot
invent a variable. Every answer carries a confidence and a one-line reason,
both of which land in the config workbook for review.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..logging_utils import get_logger
from ..matching import MatchRequest, MatchResult
from .client import AiSettings, ClaudeClient

logger = get_logger()

METHOD = "claude"
PROMPT_VERSION = "1"

SYSTEM_PROMPT = """\
You match text printed on a clinical trial Case Report Form (CRF) page to the \
field definitions from that study's EDC build.

The CRF text and the field label often differ: the CRF may abbreviate ("DOB" \
for "Date of Birth"), wrap a long label across lines so you only see a \
fragment, add units or formatting hints ("Weight (kg)"), or print a \
translation of the build label.

Rules:
- Choose only from the candidate fields given for that item. Never invent a \
field name.
- Section headings, instructions, page furniture, units on their own, and \
legal text are not fields. Return an empty variable for those.
- If two candidates are equally plausible, return an empty variable rather \
than guessing. A missed annotation is corrected in review; a wrong one may \
not be noticed.
- confidence is your probability that a reviewer would accept the match: \
0.9+ only when the wording differs trivially, 0.7-0.9 for a clear \
abbreviation or a wrapped label, below 0.7 when you are inferring from \
position or context alone.
- reason must be under 15 words and state what made the match."""

RESPONSE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "variable": {"type": "string"},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "variable", "confidence", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["matches"],
    "additionalProperties": False,
}


@dataclass
class ClaudeLabelResolver:
    """A `matching.LabelResolver` backed by Claude."""

    client: ClaudeClient
    batch_size: int = 40
    name: str = METHOD

    @classmethod
    def from_settings(cls, settings: AiSettings, **kwargs: Any) -> ClaudeLabelResolver:
        settings.prompt_version = PROMPT_VERSION
        return cls(client=ClaudeClient(settings), **kwargs)

    def resolve(self, requests: Sequence[MatchRequest]) -> list[MatchResult]:
        results: list[MatchResult] = []
        # Group by form: every item on a form shares one candidate list, which
        # makes the catalogue a stable, cacheable prompt prefix.
        by_form: dict[str, list[MatchRequest]] = {}
        for request in requests:
            by_form.setdefault(request.form_oid, []).append(request)

        for form_oid, form_requests in by_form.items():
            catalogue = self._catalogue(form_requests[0])
            for start in range(0, len(form_requests), self.batch_size):
                batch = form_requests[start:start + self.batch_size]
                results.extend(self._resolve_batch(form_oid, catalogue, batch))

        logger.info("Claude label matching: %s", self.client.usage_summary())
        return results

    # -- internals ----------------------------------------------------------
    @staticmethod
    def _catalogue(request: MatchRequest) -> str:
        lines = [f"Form: {request.form_label or request.form_oid}", "", "Candidate fields:"]
        lines += [f"  {c.var_oid}\t{c.label}" for c in request.candidates]
        return "\n".join(lines)

    def _resolve_batch(
        self,
        form_oid: str,
        catalogue: str,
        batch: Sequence[MatchRequest],
    ) -> list[MatchResult]:
        valid = {c.var_oid for c in batch[0].candidates}
        items = [
            {"id": index, "page": request.page_no, "crf_text": request.crf_text}
            for index, request in enumerate(batch)
        ]
        user = (
            f"{catalogue}\n\n"
            "Unmatched CRF text from this form:\n"
            f"{json.dumps(items, indent=2)}\n\n"
            "Return one entry per id. Use an empty string for variable when the "
            "text is not a field or no candidate clearly fits."
        )
        # The catalogue is the stable prefix across every batch on this form,
        # so it is worth a cache breakpoint.
        system = [
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
        ]

        payload = self.client.json_call(
            system=system, user=user, schema=RESPONSE_SCHEMA, cache_salt=form_oid,
        )
        if not payload:
            return []

        results: list[MatchResult] = []
        for entry in payload.get("matches", []):
            try:
                index = int(entry["id"])
                variable = str(entry.get("variable") or "").strip()
                confidence = float(entry.get("confidence") or 0.0)
            except (KeyError, TypeError, ValueError):
                continue
            if not 0 <= index < len(batch) or not variable:
                continue
            if variable not in valid:
                # The model named something outside the candidate list.
                logger.warning("Claude proposed unknown field %r on form %s; ignored",
                               variable, form_oid)
                continue
            results.append(
                MatchResult(
                    row_index=batch[index].row_index,
                    var_oid=variable,
                    confidence=max(0.0, min(1.0, confidence)),
                    method=METHOD,
                    rationale=str(entry.get("reason") or "")[:120],
                )
            )
        return results
