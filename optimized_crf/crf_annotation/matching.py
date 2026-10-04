"""Tiered matching of CRF text to EDC field definitions.

Exact matching handles most of a well-built study. The residue — a label that
wrapped across two lines, a CRF that says "DOB" where the build says "Date of
Birth", a translated form — is where a run quietly loses annotations.

The tiers run cheapest-first and each one only ever sees what the previous
tier could not resolve:

    1. exact      normalised string equality           (in `merge.match_fields`)
    2. fuzzy      difflib ratio over sorted tokens     deterministic, stdlib
    3. resolver   an injected strategy, e.g. Claude    optional, off by default

Every match records the tier that produced it and a confidence, and both land
in the config workbook. A reviewer sees exactly which annotations were guessed
at, and the rerun path replays their decision deterministically — no model is
consulted twice for the same study.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Protocol, Sequence

import pandas as pd

from .logging_utils import get_logger

logger = get_logger()

METHOD_EXACT = "exact"
METHOD_FUZZY = "fuzzy"
UNRESOLVED = ""

# Below this, a fuzzy hit is more likely noise than a wrapped label.
DEFAULT_FUZZY_THRESHOLD = 0.88


def _tokens(text: str) -> str:
    """Sorted alphanumeric tokens — order-insensitive comparison key."""
    return " ".join(sorted(re.findall(r"[a-z0-9]+", str(text).lower())))


@dataclass(frozen=True)
class Candidate:
    """One field a piece of CRF text might refer to."""

    var_oid: str
    label: str


@dataclass(frozen=True)
class MatchRequest:
    """A CRF text item that no earlier tier could resolve."""

    row_index: int
    page_no: int
    crf_text: str
    form_oid: str
    form_label: str
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True)
class MatchResult:
    """A tier's answer for one request. `var_oid` empty means 'no match'."""

    row_index: int
    var_oid: str
    confidence: float
    method: str
    rationale: str = ""


class LabelResolver(Protocol):
    """A strategy that resolves the residue left by the deterministic tiers."""

    name: str

    def resolve(self, requests: Sequence[MatchRequest]) -> Sequence[MatchResult]:
        ...


@dataclass
class FuzzyResolver:
    """Deterministic near-match on sorted tokens. No dependencies, no network."""

    threshold: float = DEFAULT_FUZZY_THRESHOLD
    name: str = METHOD_FUZZY

    def resolve(self, requests: Sequence[MatchRequest]) -> list[MatchResult]:
        results: list[MatchResult] = []
        for request in requests:
            target = _tokens(request.crf_text)
            if not target:
                continue
            best, best_score = None, 0.0
            for candidate in request.candidates:
                score = difflib.SequenceMatcher(None, target, _tokens(candidate.label)).ratio()
                if score > best_score:
                    best, best_score = candidate, score
            if best is not None and best_score >= self.threshold:
                results.append(
                    MatchResult(
                        row_index=request.row_index,
                        var_oid=best.var_oid,
                        confidence=round(best_score, 3),
                        method=self.name,
                        rationale=f"token similarity {best_score:.2f} to '{best.label}'",
                    )
                )
        return results


def build_requests(
    merged: pd.DataFrame,
    form_fields: pd.DataFrame,
    max_candidates: int = 400,
) -> list[MatchRequest]:
    """Collect the rows still unmatched, each with its form's field catalogue."""
    if merged.empty or form_fields.empty:
        return []

    unresolved = merged[merged["VAR_OID"].isna() & (merged["FORM_OID"].astype(str) != "")]
    # Text already claimed by an exact match elsewhere on the form is not a
    # candidate target - one field, one annotation.
    claimed = set(merged.loc[merged["VAR_OID"].notna(), "VAR_OID"].dropna())

    catalogue: dict[str, list[Candidate]] = {}
    for form_oid, rows in form_fields.groupby("DS_OID"):
        candidates = [
            Candidate(var_oid=str(row["VAR_OID"]), label=str(row["VAR_TXT"]))
            for _, row in rows.iterrows()
            if str(row["VAR_OID"]) not in claimed and str(row["VAR_TXT"]).strip()
        ]
        if candidates:
            catalogue[str(form_oid)] = candidates[:max_candidates]

    requests: list[MatchRequest] = []
    for index, row in unresolved.iterrows():
        form_oid = str(row["FORM_OID"])
        candidates = catalogue.get(form_oid)
        if not candidates:
            continue
        text = str(row.get("ITM_TEXT") or "").strip()
        if len(text) < 2:
            continue
        requests.append(
            MatchRequest(
                row_index=int(index),
                page_no=int(row["PAGE_NO"]),
                crf_text=text,
                form_oid=form_oid,
                form_label=str(row.get("FORM_LABEL") or ""),
                candidates=tuple(candidates),
            )
        )
    return requests


def refine(
    merged: pd.DataFrame,
    form_fields: pd.DataFrame,
    resolvers: Sequence[LabelResolver] = (),
    min_confidence: float = 0.0,
) -> pd.DataFrame:
    """Run each resolver over the still-unmatched rows, in order."""
    result = merged.copy()
    for column, default in (("MATCH_METHOD", ""), ("MATCH_CONFIDENCE", 0.0),
                            ("MATCH_NOTE", "")):
        if column not in result.columns:
            result[column] = default
    result.loc[result["VAR_OID"].notna() & (result["MATCH_METHOD"] == ""), "MATCH_METHOD"] = METHOD_EXACT
    result.loc[result["MATCH_METHOD"] == METHOD_EXACT, "MATCH_CONFIDENCE"] = 1.0

    if not resolvers:
        return result

    labels = dict(zip(form_fields["VAR_OID"].astype(str), form_fields["VAR_TXT"].astype(str)))

    for resolver in resolvers:
        requests = build_requests(result, form_fields)
        if not requests:
            logger.info("Nothing left for resolver '%s'", resolver.name)
            break

        logger.info("Resolver '%s': %d unmatched CRF item(s) to consider",
                    resolver.name, len(requests))
        try:
            results = list(resolver.resolve(requests))
        except Exception as exc:  # noqa: BLE001 - a resolver is an enhancement
            logger.error("Resolver '%s' failed, continuing without it: %s", resolver.name, exc)
            continue

        applied = 0
        for match in results:
            if not match.var_oid or match.confidence < min_confidence:
                continue
            if match.row_index not in result.index:
                continue
            result.loc[match.row_index, "VAR_OID"] = match.var_oid
            result.loc[match.row_index, "VAR_TXT"] = labels.get(match.var_oid, "")
            result.loc[match.row_index, "MATCH_METHOD"] = match.method
            result.loc[match.row_index, "MATCH_CONFIDENCE"] = round(float(match.confidence), 3)
            result.loc[match.row_index, "MATCH_NOTE"] = match.rationale
            applied += 1

        logger.info("Resolver '%s' resolved %d of %d (min confidence %.2f)",
                    resolver.name, applied, len(requests), min_confidence)

    inexact = int((~result["MATCH_METHOD"].isin(["", METHOD_EXACT])).sum())
    if inexact:
        logger.warning(
            "%d annotation(s) come from a non-exact match and are flagged in the "
            "config workbook for review", inexact,
        )
    return result
