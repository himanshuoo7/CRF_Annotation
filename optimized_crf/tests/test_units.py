"""Unit coverage for the parts with the sharpest edges."""

from __future__ import annotations

import pandas as pd
import pytest

from crf_annotation.annotate import parse_pos
from crf_annotation.config_workbook import read_config_workbook, write_config_workbook
from crf_annotation.constants import ANNOTATION_FIELD, COORDINATE_COLUMNS, EMPTY_POS
from crf_annotation.layout import _Occupancy, place_annotations
from crf_annotation.merge import unmapped_variables
from crf_annotation.job import CrfJob, InputError
from crf_annotation.pdf_text import PageGeometry
from crf_annotation.specs import SpecCatalog, domain_of, strip_supp


# --- coordinate parsing ----------------------------------------------------
def test_parse_pos_accepts_a_normal_rectangle():
    assert parse_pos("[10, 20, 30, 40]") == (10.0, 20.0, 30.0, 40.0)


def test_parse_pos_rejects_the_empty_marker():
    assert parse_pos(EMPTY_POS) is None
    assert parse_pos("") is None
    assert parse_pos(None) is None


@pytest.mark.parametrize("value", ["[1,2,3]", "[a,b,c,d]", "__import__('os').system('id')"])
def test_parse_pos_never_executes_its_input(value):
    """The old code called eval() on this spreadsheet cell."""
    assert parse_pos(value) is None


def test_parse_pos_rejects_inverted_rectangles():
    assert parse_pos("[30, 40, 10, 20]") is None


# --- spec helpers ----------------------------------------------------------
def test_strip_supp_and_domain_of():
    assert strip_supp("SUPPAE") == "AE"
    assert strip_supp("AE") == "AE"
    assert domain_of("SUPPAE.QVAL where QNAM = X") == "AE"
    assert domain_of("AEFREQ in SUPPAE") == "AE"
    assert domain_of("RACEOTH in SUPPDM") == "DM"
    assert domain_of("no dot here") == ""


def test_spec_catalog_prefers_invar_parameters(tmp_path):
    import json

    spec = {
        "domain": {"dataset": "VS", "label": "Vital Signs",
                   "functions": [{"source_dataset": "VS_FORM"}]},
        "variables": [{
            "dataset": "VS", "variable": "VSORRES",
            "functions": [{
                "function": "g_filter",
                "parameters": [
                    {"name": "inval_filter", "value": "SYSBP"},   # a literal, not a variable
                    {"name": "invar", "value": ["SYSBP_RESULT"]},
                ],
            }],
        }],
    }
    path = tmp_path / "vs.json"
    path.write_text(json.dumps(spec))

    catalog = SpecCatalog.load([path])
    assert list(catalog.mappings["RSRC_VAR"]) == ["SYSBP_RESULT"]
    assert catalog.header_text("VS") == "VS (Vital Signs)"


def test_spec_catalog_skips_malformed_files(tmp_path):
    (tmp_path / "broken.json").write_text("{not json")
    (tmp_path / "nodomain.json").write_text('{"variables": []}')
    catalog = SpecCatalog.load(list(tmp_path.glob("*.json")))
    assert catalog.mappings.empty


# --- form context ----------------------------------------------------------
def test_two_banners_on_one_page_do_not_duplicate_items():
    """Binding must not multiply rows: a page may carry more than one banner."""
    from crf_annotation.binding import BannerBinder, bind_forms
    from crf_annotation.design import Form, build

    design = build("test", forms=[Form(oid="A", label="A"), Form(oid="B", label="B")],
                   fields=[])
    items = pd.DataFrame({
        "PAGE_NO": [1, 1, 1, 1],
        "ITM_TEXT": ["Form: A", "Height", "Form: B", "Weight"],
    })
    bound = bind_forms(items, design, BannerBinder())
    assert len(bound) == len(items)
    assert list(bound["FORM_OID"]) == ["A", "A", "B", "B"]


# --- layout ----------------------------------------------------------------
def test_occupancy_detects_overlap():
    index = _Occupancy([(0, 0, 10, 10)])
    assert index.collides((5, 5, 15, 15))
    assert not index.collides((11, 0, 20, 10))


def _row(page, x0, x1, y0, text):
    return {"PAGE_NO": page, "ITM_TEXT": "label", "ATEXT": text,
            "X0": x0, "Y0": y0, "X1": x1, "Y1": y0 + 12}


def test_annotations_do_not_overlap_each_other():
    merged = pd.DataFrame([_row(1, 60, 120, 700, "AE.AETERM"), _row(1, 60, 120, 700, "AE.AESEV")])
    geometry = PageGeometry({1: (612.0, 792.0)})
    placed, stats = place_annotations(merged, geometry)

    assert stats.placed == 2
    boxes = [parse_pos(p) for p in placed["POS1"]]
    (ax0, ay0, ax1, ay1), (bx0, by0, bx1, by1) = boxes
    assert ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0


def test_annotations_stay_inside_the_page():
    merged = pd.DataFrame([_row(1, 400, 560, 700, "AE.A_VERY_LONG_ANNOTATION_INDEED")])
    geometry = PageGeometry({1: (612.0, 792.0)})
    placed, _ = place_annotations(merged, geometry)
    box = parse_pos(placed.loc[0, "POS1"])
    assert box is None or box[2] <= 612.0


def test_rows_without_annotation_text_get_no_box():
    merged = pd.DataFrame([_row(1, 60, 120, 700, "")])
    placed, stats = place_annotations(merged, PageGeometry({1: (612.0, 792.0)}))
    assert stats.placed == 0
    assert placed.loc[0, "POS1"] == EMPTY_POS


# --- config workbook -------------------------------------------------------
def _coordinates() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"PAGE_NO": 1, "DS_OID": "DM_FORM", "ITM_TEXT": "Sex",
             "DS_NAME": "Demographics", "POS1": "[10, 20, 30, 40]",
             "A_TEXT": "DM.SEX", "ANNOTATION_TYPE": ANNOTATION_FIELD,
             "MATCH_METHOD": "exact", "MATCH_CONFIDENCE": 1.0, "MATCH_NOTE": ""}
        ],
        columns=list(COORDINATE_COLUMNS),
    )


def test_workbook_round_trip_is_lossless(tmp_path):
    path = tmp_path / "config.xlsx"
    original = _coordinates()
    write_config_workbook(path, original)
    restored = read_config_workbook(path)
    pd.testing.assert_frame_equal(
        original.reset_index(drop=True), restored.reset_index(drop=True), check_dtype=False
    )


def test_legacy_atext_column_is_understood(tmp_path):
    path = tmp_path / "legacy.xlsx"
    legacy = _coordinates().rename(columns={"A_TEXT": "ATEXT"})
    with pd.ExcelWriter(path, engine="xlsxwriter") as writer:
        legacy.to_excel(writer, sheet_name="coordinates", index=False)
    restored = read_config_workbook(path)
    assert list(restored["A_TEXT"]) == ["DM.SEX"]


# --- job -------------------------------------------------------------------
def test_a_missing_crf_is_named(tmp_path):
    with pytest.raises(InputError, match="Blank CRF not found"):
        CrfJob.build(crf=tmp_path / "nope.pdf", output_dir=tmp_path / "out")


def test_specs_and_design_accept_a_directory(tmp_path):
    crf = tmp_path / "blank.pdf"
    crf.write_bytes(b"%PDF-1.4")
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "a.json").write_text("{}")
    (specs / "b.json").write_text("{}")
    design = tmp_path / "design"
    design.mkdir()
    (design / "d.xml").write_text("<ODM/>")

    job = CrfJob.build(crf=crf, output_dir=tmp_path / "out", specs=specs, design=design)
    assert [p.name for p in job.specs] == ["a.json", "b.json"]
    assert [p.name for p in job.design] == ["d.xml"]
    assert str(job.mode) == "first_run"


def test_a_config_makes_it_a_rerun(tmp_path):
    crf = tmp_path / "blank.pdf"
    crf.write_bytes(b"%PDF-1.4")
    config = tmp_path / "aCRF_Config.xlsx"
    config.write_bytes(b"stub")

    job = CrfJob.build(crf=crf, output_dir=tmp_path / "out", config=config)
    assert str(job.mode) == "rerun"


def test_study_name_defaults_to_the_crf_filename(tmp_path):
    crf = tmp_path / "STUDY999_blank.pdf"
    crf.write_bytes(b"%PDF-1.4")
    config = tmp_path / "c.xlsx"
    config.write_bytes(b"stub")
    job = CrfJob.build(crf=crf, output_dir=tmp_path / "out", config=config)
    assert job.study_name == "STUDY999_blank"


def test_unmapped_variables_ignore_annotated_ones():
    merged = pd.DataFrame(
        {
            "PAGE_NO": [1, 2, 3],
            "DS_OID": ["DM_FORM", "DM_FORM", "NT_FORM"],
            "VAR_OID": ["SEX", "SEX", "COMMENT"],
            "ATEXT": ["DM.SEX", "", ""],
        }
    )
    unmapped = unmapped_variables(merged)
    assert list(unmapped["RSRC_VAR"]) == ["COMMENT"]


# --- derived / assigned SDTM variables are not annotated -------------------
@pytest.mark.parametrize("variable", [
    "USUBJID", "STUDYID", "DOMAIN", "EPOCH", "ARMCD",
    "AESEQ", "VSSEQ", "LBSEQ",          # --SEQ
    "LBSTRESC", "LBSTRESN", "VSSTRESU",  # --STRES*
    "AESTDY", "AEENDY", "LBDY",          # --STDY / --ENDY / --DY
    "usubjid", " AESEQ ",                # case and whitespace tolerant
])
def test_derived_variables_are_recognised(variable):
    from crf_annotation.constants import is_derived_variable

    assert is_derived_variable(variable) is True


@pytest.mark.parametrize("variable", [
    "AETERM", "BRTHDTC", "SEX", "RACE", "AESER", "VSORRES", "QVAL",
    "SUBJID", "SITEID",                 # the subject and site numbers are collected
    "AESTDTC", "LBORRES", "SEQUENCE",   # not --SEQ: wrong length
    "DY", "", None,
])
def test_collected_variables_are_not_mistaken_for_derived(variable):
    from crf_annotation.constants import is_derived_variable

    assert is_derived_variable(variable) is False


def _spec_with(variables, tmp_path):
    import json

    spec = {
        "domain": {"dataset": "AE", "label": "Adverse Events",
                   "functions": [{"source_dataset": "AE_FORM"}]},
        "variables": [
            {"dataset": "AE", "variable": v,
             "functions": [{"function": "g_copy",
                            "parameters": [{"name": "invar", "value": [f"RAW_{v}"]}]}]}
            for v in variables
        ],
        "value_level": [],
    }
    path = tmp_path / "ae.json"
    path.write_text(json.dumps(spec))
    return [path]


def test_catalogue_drops_derived_targets(tmp_path):
    from crf_annotation.specs import SpecCatalog

    catalog = SpecCatalog.load(_spec_with(["AETERM", "AESEQ", "USUBJID", "AESTDTC"], tmp_path))

    assert set(catalog.mappings["TGT_VAR"]) == {"AETERM", "AESTDTC"}
    assert catalog.derived_skipped == ("AE.AESEQ", "AE.USUBJID")


def test_derived_targets_can_be_kept_deliberately(tmp_path):
    from crf_annotation.specs import SpecCatalog

    catalog = SpecCatalog.load(_spec_with(["AETERM", "AESEQ"], tmp_path), include_derived=True)

    assert set(catalog.mappings["TGT_VAR"]) == {"AETERM", "AESEQ"}
    assert catalog.derived_skipped == ()


def test_a_box_for_a_right_column_question_stays_in_that_column():
    """No room beside it: the box goes under the question, not across the page."""
    import pandas as pd

    from crf_annotation.layout import _Occupancy, _place_one

    row = pd.Series({"X0": 400.0, "X1": 590.0, "Y0": 500.0, "Y1": 510.0, "ATEXT": "AE.AEACN"})
    rect = _place_one(row, _Occupancy([(400.0, 500.0, 590.0, 510.0)]), page_width=612.0, page_height=792.0)
    assert rect is not None
    assert rect[0] >= 400.0 and rect[1] < 500.0


def test_a_box_does_not_slide_into_the_next_column():
    import pandas as pd

    from crf_annotation.layout import _Occupancy, _place_one

    # middle-column field; the space beside it is taken, the right column is free
    row = pd.Series({"X0": 210.0, "X1": 300.0, "Y0": 500.0, "Y1": 510.0, "ATEXT": "AE.AESEV"})
    taken = _Occupancy([(210.0, 500.0, 300.0, 510.0), (300.0, 495.0, 400.0, 515.0)])
    rect = _place_one(row, taken, page_width=612.0, page_height=792.0, edges=[40.0, 210.0, 400.0])
    assert rect is not None
    assert rect[2] <= 400.0 and rect[1] < 500.0   # under it, not across in the next column


def test_columns_are_found_from_where_lines_start_and_indents_are_not_columns():
    import pandas as pd

    from crf_annotation.layout import column_edges, column_of

    starts = [40, 40, 40, 52, 52, 52, 210, 210, 210, 222, 222, 222, 400, 400, 400, 330]
    items = pd.DataFrame({"X0": starts})
    edges = column_edges(items)
    assert edges == [40.0, 208.0, 400.0]            # 52 and 222 are answer-option indents
    left, right = column_of(222.0, edges, 592.0)    # an option in the middle column
    assert left == 208.0 and right < 400.0


def test_a_box_finds_the_gap_after_a_short_answer_in_a_narrow_column():
    import pandas as pd

    from crf_annotation.layout import _Occupancy, _place_one

    # question fills its line; the row below holds a short "No" with room after it
    row = pd.Series({"X0": 210.0, "X1": 370.0, "Y0": 500.0, "Y1": 510.0, "ATEXT": "PGXSMPL in SUPPDS"})
    taken = _Occupancy([(210.0, 500.0, 370.0, 510.0), (210.0, 486.0, 240.0, 498.0)])
    rect = _place_one(row, taken, page_width=612.0, page_height=792.0, edges=[40.0, 208.0, 376.0])
    assert rect is not None
    assert rect[0] > 240.0 and rect[2] <= 376.0 and rect[1] < 500.0


def test_a_long_findings_annotation_wraps_to_fit_its_column():
    import pandas as pd

    from crf_annotation.layout import TWO_LINE_HEIGHT, _Occupancy, _place_one, two_lines

    assert two_lines("IE.IEORRES where IETESTCD = INCL02") == ["IE.IEORRES", "where IETESTCD = INCL02"]
    row = pd.Series({"X0": 210.0, "X1": 300.0, "Y0": 500.0, "Y1": 510.0,
                     "ATEXT": "IE.IEORRES where IETESTCD = INCL02"})
    rect = _place_one(row, _Occupancy([(210.0, 500.0, 300.0, 510.0)]), page_width=612.0,
                      page_height=792.0, edges=[40.0, 208.0, 376.0])
    assert rect is not None
    assert rect[0] >= 208.0 and rect[2] <= 376.0            # inside its own column
    assert rect[3] - rect[1] >= TWO_LINE_HEIGHT - 0.5        # on two lines


def test_a_box_wraps_rather_than_leaving_its_column():
    """The AFIB case: one line fits the column but not after the 'Yes' beneath
    the question; it must wrap there, never move to another column."""
    import pandas as pd

    from crf_annotation.layout import _Occupancy, _place_one

    edges = [44.0, 216.0, 388.0]
    question = (388.0, 383.0, 498.0, 394.0)
    yes, no = (401.0, 369.0, 414.0, 380.0), (401.0, 356.0, 412.0, 367.0)
    next_question = (389.0, 329.0, 532.0, 340.0)
    taken = _Occupancy([question, yes, no, next_question])
    row = pd.Series({"X0": 388.0, "X1": 498.0, "Y0": 383.0, "Y1": 394.0,
                     "ATEXT": "EG.EGORRES where EGTESTCD = AFIB"})
    rect = _place_one(row, taken, page_width=595.0, page_height=842.0, edges=edges)
    assert rect is not None
    assert rect[0] >= 388.0                       # in its own column
    assert rect[1] >= 340.0 and rect[3] <= 394.0  # beside its own Yes/No, above the next question


def test_no_room_in_the_column_means_unplaced_not_misplaced():
    import pandas as pd

    from crf_annotation.layout import _Occupancy, _place_one

    # the whole right column is full; the left column is empty
    full = _Occupancy([(388.0, y, 575.0, y + 10.0) for y in range(0, 842, 10)])
    row = pd.Series({"X0": 388.0, "X1": 498.0, "Y0": 383.0, "Y1": 394.0, "ATEXT": "AE.AESEV"})
    assert _place_one(row, full, page_width=595.0, page_height=842.0, edges=[44.0, 216.0, 388.0]) is None


def test_a_box_does_not_drift_past_the_next_question():
    """'GOT value' has entry boxes and a unit under it; its box must stay above
    the next question ('Is GOT within normal range?'), narrowing to three lines
    if that is what fits."""
    import pandas as pd

    from crf_annotation.layout import _Occupancy, _place_one

    label = (45.0, 696.5, 140.0, 707.4)
    unit = (147.0, 680.0, 160.0, 690.9)
    next_question = (45.0, 645.0, 190.0, 655.9)
    yes = (57.5, 630.9, 70.0, 641.8)
    taken = _Occupancy([label, unit, next_question, yes])
    row = pd.Series({"X0": 45.0, "X1": 140.0, "Y0": 696.5, "Y1": 707.4,
                     "ATEXT": "LB.LBORRES where LBTESTCD = AST"})
    rect = _place_one(row, taken, page_width=595.0, page_height=842.0, edges=[40.0, 210.0, 376.0],
                      floor=655.9, soft=taken)
    assert rect is not None
    assert rect[1] >= 655.9                                   # above the next question
    x0, y0, x1, y1 = rect
    assert not (x0 < unit[2] and unit[0] < x1 and y0 < unit[3] and unit[1] < y1)   # never over the unit


def test_a_domain_banner_avoids_the_page_title():
    from crf_annotation.config_workbook import header_rows_for_page

    page_w, page_h = 595.0, 842.0
    title = (40.0, 790.0, 560.0, 815.0)            # a title across the top rows
    rows = header_rows_for_page(1, ["CM (Concomitant Medications)"], page_w, page_h, obstacles=[title])
    x0, y0, x1, y1 = (float(v) for v in rows[0]["POS1"].strip("[]").split(","))
    assert not (x0 < title[2] and title[0] < x1 and y0 < title[3] and title[1] < y1)

    clear = header_rows_for_page(1, ["CM (Concomitant Medications)"], page_w, page_h)
    assert clear[0]["POS1"].startswith("[60.0, 797.0")   # nothing in the way: the usual top-left spot


def test_a_box_for_a_heading_across_columns_goes_beside_it():
    import pandas as pd

    from crf_annotation.layout import place_box

    page = pd.DataFrame([
        {"PAGE_NO": 6, "ITM_TEXT": "Background Heart Failure Maintenance Medications Part 1", "X0": 48.0, "X1": 446.0, "Y0": 796.4, "Y1": 812.5},
        {"PAGE_NO": 6, "ITM_TEXT": "Ref", "X0": 48.0, "X1": 60.0, "Y0": 746.0, "Y1": 760.0},
        {"PAGE_NO": 6, "ITM_TEXT": "CRF Question", "X0": 150.0, "X1": 210.0, "Y0": 746.0, "Y1": 760.0},
        {"PAGE_NO": 6, "ITM_TEXT": "Data Collected", "X0": 330.0, "X1": 400.0, "Y0": 746.0, "Y1": 760.0},
        {"PAGE_NO": 6, "ITM_TEXT": "1.1 Were any ACE inhibitors taken?", "X0": 48.0, "X1": 300.0, "Y0": 720.0, "Y1": 731.0},
        {"PAGE_NO": 6, "ITM_TEXT": "No (N)", "X0": 330.0, "X1": 360.0, "Y0": 720.0, "Y1": 731.0},
        {"PAGE_NO": 6, "ITM_TEXT": "1.2 If No, reason", "X0": 48.0, "X1": 200.0, "Y0": 700.0, "Y1": 711.0},
        {"PAGE_NO": 6, "ITM_TEXT": "Cost (COST)", "X0": 330.0, "X1": 380.0, "Y0": 700.0, "Y1": 711.0},
    ])
    title = page.iloc[0]
    rect = place_box(title, "CM.CMCAT", page, [], 595.0, 842.0)
    assert rect is not None and rect[0] > 446.0 and rect[1] >= 790.0 and rect[3] <= 842.0
