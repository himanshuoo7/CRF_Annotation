"""Check an aCRF against the FDA's PDF specifications for eCTD submissions.

Source: FDA "Portable Document Format (PDF) Specifications", Technical
Specifications Document v4.1 (September 2016), incorporated by reference in
the eCTD guidance. Each check below cites the section it comes from. The
report ships beside every aCRF so a submission team can see, rule by rule,
what was verified - and what (like a sponsor's own fonts in the blank CRF)
needs their attention.

A check is ``pass``, ``fail``, ``warn`` (needs a person to look) or ``info``.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import pymupdf

SPEC = "FDA PDF Specifications v4.1 (Sept 2016)"

# Table 1 "List of Standard Fonts" - these need not be embedded.
_STANDARD_FONTS = re.compile(
    r"^(TimesNewRoman|Times-?New-?Roman|Arial|CourierNew|Courier-?New|Symbol|ZapfDingbats)", re.IGNORECASE)
_SUBSET = re.compile(r"^[A-Z]{6}\+")
_LETTER_OR_A4 = (612.0, 842.0)       # widest of 8.5x11in and A4, in points
_FILE_NAME = re.compile(r"^[a-z0-9_-]+\.pdf$")


@dataclass
class Check:
    rule: str
    section: str
    status: str
    detail: str


def _fonts(document: pymupdf.Document) -> dict[str, bool]:
    """Base font name -> embedded?"""
    found: dict[str, bool] = {}
    for page in document:
        for _xref, ext, _type, basefont, _name, _encoding, *_ in page.get_fonts(full=True):
            found[basefont] = found.get(basefont, False) or ext not in ("n/a", "")
    return found


def _objects_with(document: pymupdf.Document, keys: Iterable[str]) -> int:
    hits = 0
    for xref in range(1, document.xref_length()):
        try:
            source = document.xref_object(xref, compressed=True)
        except Exception:  # noqa: BLE001 - a broken object is not ours to judge here
            continue
        if any(key in source for key in keys):
            hits += 1
    return hits


def check_pdf(path: Path, own_fonts: Iterable[str] = ()) -> list[Check]:
    """Run every check on `path`. `own_fonts` are fonts this tool adds."""
    path = Path(path)
    checks: list[Check] = []
    own = tuple(own_fonts)

    def add(rule: str, section: str, ok: bool | None, detail: str, soft: bool = False) -> None:
        status = "info" if ok is None else "pass" if ok else ("warn" if soft else "fail")
        checks.append(Check(rule, section, status, detail))

    with pymupdf.open(path) as document:
        version = str(document.metadata.get("format") or "")
        number = re.search(r"(\d\.\d)", version)
        add("PDF version 1.4-1.7", "Version", bool(number) and "1.4" <= number.group(1) <= "1.7", version or "unknown")
        add("No security or password", "Security", not document.is_encrypted and not document.needs_pass,
            "not encrypted" if not document.is_encrypted else "encrypted")
        javascript = _objects_with(document, ("/JavaScript", "/JS "))
        add("No JavaScript", "Version", javascript == 0, f"{javascript} object(s) with JavaScript")
        dynamic = _objects_with(document, ("/EmbeddedFile", "/RichMedia", "/3D", "/Movie", "/Sound"))
        add("No attachments, multimedia or 3D", "Version", dynamic == 0, f"{dynamic} such object(s)")

        kinds: dict[str, int] = {}
        for page in document:
            for annot in page.annots() or ():
                kinds[annot.type[1]] = kinds.get(annot.type[1], 0) + 1
            for _widget in page.widgets() or ():
                kinds["Widget"] = kinds.get("Widget", 0) + 1
        add("No PDF annotations (annotation boxes are page content)", "Version", not kinds,
            "none" if not kinds else ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
            + (" - form fields come from the blank CRF" if "Widget" in kinds else ""))

        fonts = _fonts(document)
        missing = sorted(f for f, embedded in fonts.items() if not embedded and not _STANDARD_FONTS.match(f))
        add("Non-standard fonts embedded", "Fonts", not missing,
            "all embedded" if not missing else "not embedded: " + ", ".join(missing[:8]))
        subsets = sorted(f for f, embedded in fonts.items() if embedded and _SUBSET.match(f))
        ours_subset = [f for f in subsets if any(o.lower() in f.lower() for o in own)]
        add("Embedded fonts are full, not subsets", "Fonts", not subsets,
            "all full" if not subsets else
            (f"{len(subsets)} subset font(s) from the blank CRF ({', '.join(subsets[:3])}"
             f"{', ...' if len(subsets) > 3 else ''}); re-export the blank CRF with full fonts if your "
             "submission requires it" if not ours_subset else "this tool's font is subset: " + ", ".join(ours_subset)),
            soft=not ours_subset)

        oversize = [i + 1 for i, page in enumerate(document)
                    if min(page.rect.width, page.rect.height) > _LETTER_OR_A4[0] + 1
                    or max(page.rect.width, page.rect.height) > _LETTER_OR_A4[1] + 1]
        add("Pages fit 8.5x11in / A4", "Page Size and Margins", not oversize,
            "all pages" if not oversize else f"larger pages: {oversize[:10]}", soft=True)

        no_text = [i + 1 for i, page in enumerate(document) if not page.get_text().strip()]
        add("Text searchable (not scanned images)", "Source of Electronic Documents", not no_text,
            "every page has text" if not no_text else f"pages without text: {no_text[:10]}", soft=True)

        toc = document.get_toc(simple=False)
        needs = document.page_count >= 5
        add("Bookmarks in documents of 5+ pages", "Document Navigation", bool(toc) or not needs,
            f"{len(toc)} bookmark(s)")
        zooms = {round(float((entry[3] or {}).get("zoom", 0) or 0), 3) for entry in toc if len(entry) > 3}
        add("Bookmarks use Inherit Zoom", "Document Navigation", not toc or zooms <= {0.0},
            "inherit zoom" if zooms <= {0.0} else f"zoom levels set: {sorted(zooms)}")
        mode = document.xref_get_key(document.pdf_catalog(), "PageMode")[1]
        add("Opens to Bookmarks Panel and Page", "Initial View Settings",
            mode == "/UseOutlines" if toc else mode in ("null", "/UseNone"), f"PageMode {mode}")

    try:
        import pikepdf

        with pikepdf.open(path) as pdf:
            linear = bool(pdf.is_linearized)
        add("Optimized for Fast Web View (linearized)", "Optimize for Fast Web View", linear,
            "linearized" if linear else "not linearized")
    except ImportError:  # pragma: no cover - depends on install
        add("Optimized for Fast Web View (linearized)", "Optimize for Fast Web View", None,
            "not checked (pikepdf not installed)")

    add("File name lower case, no special characters", "Naming PDF Files", bool(_FILE_NAME.match(path.name)),
        path.name)
    return checks


def write_report(checks: list[Check], json_path: Path, text_path: Path) -> dict[str, int]:
    counts = {status: sum(c.status == status for c in checks) for status in ("pass", "fail", "warn", "info")}
    Path(json_path).write_text(json.dumps({"spec": SPEC, "counts": counts, "checks": [asdict(c) for c in checks]},
                                          indent=2), encoding="utf-8")
    width = max(len(c.rule) for c in checks)
    lines = [f"aCRF check against the {SPEC}", "",
             f"{counts['pass']} passed, {counts['fail']} failed, {counts['warn']} to review", ""]
    lines += [f"[{c.status.upper():4}] {c.rule.ljust(width)}  {c.detail}" for c in checks]
    Path(text_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return counts
