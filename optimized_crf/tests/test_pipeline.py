"""End-to-end behaviour of a first run and of a rerun."""

from __future__ import annotations

import pandas as pd
import pymupdf
import pytest
from pypdf import PdfReader

from crf_annotation import (
    InputError,
    MatchingOptions,
    PipelineError,
    annotate_crf,
)
from crf_annotation.constants import OUTPUT_CONFIG_NAME, OUTPUT_PDF_NAME


def annotations(pdf_path) -> dict[int, list[str]]:
    found: dict[int, list[str]] = {}
    with pymupdf.open(pdf_path) as document:
        for index, page in enumerate(document, start=1):
            found[index] = [a.info.get("title", "") for a in page.annots()]
    return found


def outline_titles(pdf_path) -> list[str]:
    reader = PdfReader(str(pdf_path))
    titles: list[str] = []

    def walk(items):
        for item in items:
            if isinstance(item, list):
                walk(item)
            else:
                titles.append(item.title)

    walk(reader.outline)
    return titles


def test_first_run_produces_pdf_and_workbook(study):
    result = annotate_crf(**study["job"])

    assert result.study == "STUDY123"
    assert result.mode == "first_run"

    pdf = study["out"] / OUTPUT_PDF_NAME
    workbook = study["out"] / OUTPUT_CONFIG_NAME
    assert pdf.is_file() and workbook.is_file()

    drawn = annotations(pdf)
    assert "DM.BRTHDTC" in drawn[1] and "DM (Demographics)" in drawn[1]
    assert "AE.AETERM" in drawn[2]
    assert "SUPPAE.QVAL where QNAM = AESEV" in drawn[2]
    # Page 3 has a form whose only field carries no mapping, so that field is
    # annotated NOT SUBMITTED individually.
    assert drawn[3] == ["NOT SUBMITTED"]


def test_workspace_is_removed(study):
    annotate_crf(**study["job"])
    leftovers = [p for p in study["out"].iterdir() if p.is_dir()]
    assert leftovers == []


def test_unmapped_variables_are_reported(study):
    annotate_crf(**study["job"])
    unmapped = pd.read_excel(study["out"] / OUTPUT_CONFIG_NAME, sheet_name="unmap")
    assert set(unmapped["RSRC_VAR"]) == {"COMMENT"}


def test_outline_covers_domains_and_visits(study):
    annotate_crf(**study["job"])
    titles = outline_titles(study["out"] / OUTPUT_PDF_NAME)
    assert "STUDY123" in titles
    assert "Domains" in titles and "Visits" in titles
    assert "DM (Demographics)" in titles
    assert "Screening" in titles and "Visit 1" in titles


def test_rerun_reproduces_the_same_annotations(study):
    annotate_crf(**study["job"])
    first = annotations(study["out"] / OUTPUT_PDF_NAME)

    result = annotate_crf(
        crf=study["job"]["crf"],
        output_dir=study["job"]["output_dir"],
        config=study["out"] / OUTPUT_CONFIG_NAME,
    )

    assert result.mode == "rerun"
    assert annotations(study["out"] / OUTPUT_PDF_NAME) == first


def test_edited_annotation_text_is_honoured_on_rerun(study):
    annotate_crf(**study["job"])
    workbook = study["out"] / OUTPUT_CONFIG_NAME

    coordinates = pd.read_excel(workbook, sheet_name="coordinates")
    coordinates.loc[coordinates["ANNOTATION"] == "DM.SEX", "ANNOTATION"] = "DM.SEXREPORTED"
    others = {
        name: pd.read_excel(workbook, sheet_name=name) for name in ("unmap", "FileNames")
    }
    with pd.ExcelWriter(workbook, engine="xlsxwriter") as writer:
        coordinates.to_excel(writer, sheet_name="coordinates", index=False)
        for name, frame in others.items():
            frame.to_excel(writer, sheet_name=name, index=False)

    annotate_crf(crf=study["job"]["crf"], output_dir=study["job"]["output_dir"],
                 config=workbook)

    drawn = annotations(study["out"] / OUTPUT_PDF_NAME)
    assert "DM.SEXREPORTED" in drawn[1]
    assert "DM.SEX" not in drawn[1]


def test_missing_blank_crf_is_an_input_error(study):
    job = dict(study["job"], crf=str(study["root"] / "nope.pdf"))
    with pytest.raises(InputError, match="not found"):
        annotate_crf(**job)


def test_no_usable_specs_raises(study):
    for spec in study["specs"].glob("*.json"):
        spec.write_text("{}")
    with pytest.raises(PipelineError, match="mappings"):
        annotate_crf(**study["job"])


def test_a_first_run_without_a_design_says_so(study):
    job = dict(study["job"])
    job.pop("design")
    with pytest.raises(InputError, match="study design"):
        annotate_crf(**job)


def test_nothing_to_do_is_explained(study):
    with pytest.raises(InputError, match="Nothing to do"):
        annotate_crf(crf=study["job"]["crf"], output_dir=study["job"]["output_dir"])


def test_result_carries_an_operational_summary(study):
    result = annotate_crf(**study["job"])

    assert result.mode == "first_run"
    assert result.pages == 3
    assert result.annotations > 0
    assert result.forms == 3 and result.fields == 7
    assert result.match_methods["exact"] > 0
    assert result.needs_review == 0          # nothing fuzzy in the clean fixture

    payload = result.to_dict()               # the agent-facing shape
    assert payload["acrf"].endswith(OUTPUT_PDF_NAME)
    assert payload["study"] == "STUDY123"


def test_pdf_metadata_is_caller_supplied(study):
    """Author and subject come from the caller, never a baked-in default."""
    from pypdf import PdfReader

    annotate_crf(**dict(study["job"], author="Acme CRO"))
    meta = PdfReader(str(study["out"] / OUTPUT_PDF_NAME)).metadata
    assert meta["/Author"] == "Acme CRO"
    assert meta["/Subject"] == "STUDY123"
    assert meta["/Title"] == "SDTM Annotated CRF: STUDY123"


def test_a_second_concurrent_run_is_refused(study):
    """Two runs into one target would interleave their published files."""
    from crf_annotation.workspace import ConcurrentRunError, Workspace

    with Workspace(study["out"]), pytest.raises(ConcurrentRunError), Workspace(study["out"]):
        pass


def test_publishing_is_atomic(study, monkeypatch):
    """A crash mid-publish must not leave a truncated deliverable."""
    import shutil as shutil_module
    from crf_annotation.constants import OUTPUT_PDF_NAME
    from crf_annotation.workspace import Workspace

    annotate_crf(**study["job"])
    good = (study["out"] / OUTPUT_PDF_NAME).read_bytes()

    with Workspace(study["out"]) as workspace:
        workspace.annotated_pdf.write_bytes(b"%PDF-broken")

        def explode(src, dst, *a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(shutil_module, "copy2", explode)
        with pytest.raises(OSError):
            workspace.publish()

    # The previously published file is untouched, not half-overwritten.
    assert (study["out"] / OUTPUT_PDF_NAME).read_bytes() == good
    assert not list(study["out"].glob(".*staging"))


def test_bookmarks_use_inherit_zoom(study):
    """The FDA conformance guide asks for Inherit Zoom, not fit-whole-page."""
    from pypdf import PdfReader

    annotate_crf(**study["job"])
    reader = PdfReader(str(study["out"] / OUTPUT_PDF_NAME))

    checked = 0

    def walk(items):
        nonlocal checked
        for item in items:
            if isinstance(item, list):
                walk(item)
                continue
            # `/XYZ` with left, top and zoom all null is Inherit Zoom: the
            # viewer keeps the reader's current magnification. `/Fit` would
            # re-zoom to the whole page on every jump.
            assert item.raw_get("/Type") == "/XYZ", \
                f"{item.title!r} uses {item.raw_get('/Type')}, not /XYZ"
            for key in ("/Left", "/Top", "/Zoom"):
                assert str(item.get(key)) == "NullObject", \
                    f"{item.title!r} has {key}={item.get(key)!r}; Inherit Zoom needs null"
            checked += 1

    walk(reader.outline)
    assert checked > 0


def test_derived_variables_are_not_annotated(study):
    """USUBJID and --SEQ are derived; CDISC says they belong in define.xml."""
    import json

    spec = json.loads((study["specs"] / "ae.json").read_text())
    spec["variables"].append({
        "dataset": "AE", "variable": "AESEQ",
        "functions": [{"function": "g_copy",
                       "parameters": [{"name": "invar", "value": ["AETERM"]}]}],
    })
    (study["specs"] / "ae.json").write_text(json.dumps(spec))

    annotate_crf(**study["job"])
    drawn = [t for page in annotations(study["out"] / OUTPUT_PDF_NAME).values() for t in page]

    assert any("AE.AETERM" in t for t in drawn)        # collected: annotated
    assert not any("AESEQ" in t for t in drawn)        # derived: not


# --- FDA Study Data Technical Conformance Guide 4.1.4.6 -------------------
def test_output_filename_is_exactly_acrf_pdf(study):
    """"The aCRF should be provided as a PDF with the file name 'acrf.pdf.'"""
    result = annotate_crf(**study["job"])

    assert result.acrf.name == "acrf.pdf"
    assert result.acrf.name == result.acrf.name.lower()
    assert result.acrf.is_file()


def test_not_submitted_text_is_unbracketed(study):
    """"the CRF should be annotated with the text 'NOT SUBMITTED.'"""
    annotate_crf(**study["job"])
    drawn = [t for page in annotations(study["out"] / OUTPUT_PDF_NAME).values() for t in page]

    assert "NOT SUBMITTED" in drawn
    assert not any(t.startswith("[") for t in drawn)


def _partially_mapped_study(study):
    """A form with three mapped fields and two collected-but-unmapped ones."""
    import json

    import pymupdf

    crf = study["root"] / "partial.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((60, 100), "Form: Demographics", fontsize=12)
    y = 150
    for label in ["Date of Birth", "Sex", "Race", "Interviewer initials", "Was form completed?"]:
        page.insert_text((60, y), label, fontsize=10)
        y += 40
    doc.save(str(crf))
    doc.close()

    meta = study["meta"] / "source_metadata_test.json"
    design = json.loads(meta.read_text())
    design["fields"] += [
        {"form": "DM_FORM", "field": "INTINIT", "field_label": "Interviewer initials"},
        {"form": "DM_FORM", "field": "FORMDONE", "field_label": "Was form completed?"},
    ]
    meta.write_text(json.dumps(design))
    return dict(study["job"], crf=str(crf))


def test_unmapped_fields_are_annotated_not_submitted(study):
    """Per 4.1.4.6 this is per field, not a page-level banner."""
    job = _partially_mapped_study(study)
    result = annotate_crf(**job)

    drawn = annotations(study["out"] / OUTPUT_PDF_NAME)[1]
    assert "DM.BRTHDTC" in drawn                       # mapped: annotated
    assert drawn.count("NOT SUBMITTED") == 2           # the two unmapped fields
    assert result.not_submitted == 2


def test_not_submitted_marks_sit_at_their_field(study):
    """A per-field mark must be beside its field, not in the header band."""
    job = _partially_mapped_study(study)
    result = annotate_crf(**job)

    import pandas as pd

    coords = pd.read_excel(result.config, sheet_name="coordinates")
    marks = coords[coords["ANNOTATION"] == "NOT SUBMITTED"]
    assert len(marks) == 2
    for pos in marks["LEFT_BOTTOM_RIGHT_TOP"]:
        y0 = float(str(pos).strip("[]").split(",")[1])
        assert y0 < 700, f"{pos} is in the header band, not beside a field"


def test_unmap_sheet_still_reports_the_gap(study):
    """Annotating NOT SUBMITTED must not hide which variables lack a mapping."""
    import pandas as pd

    job = _partially_mapped_study(study)
    result = annotate_crf(**job)

    unmapped = pd.read_excel(result.config, sheet_name="unmap")
    assert set(unmapped["RSRC_VAR"]) == {"INTINIT", "FORMDONE"}
    assert result.unmapped_variables == 2


def test_not_submitted_is_not_counted_as_needing_review(study):
    """It is a deliberate statement, not a guessed match."""
    job = _partially_mapped_study(study)
    result = annotate_crf(**job)

    assert result.not_submitted == 2
    assert result.needs_review == 0
    assert result.match_methods["not_submitted"] == 2


def test_marking_can_be_switched_off(study):
    job = _partially_mapped_study(study)
    job["matching"] = MatchingOptions(ai=False, mark_not_submitted=False)
    result = annotate_crf(**job)

    drawn = annotations(study["out"] / OUTPUT_PDF_NAME)[1]
    assert "NOT SUBMITTED" not in drawn
    assert result.not_submitted == 0
    assert result.unmapped_variables == 2      # still recorded
