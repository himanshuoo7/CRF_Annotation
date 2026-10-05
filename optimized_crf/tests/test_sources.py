"""Adapter registry and per-EDC study-design readers."""

from __future__ import annotations

import json

import pymupdf
import pytest

from crf_annotation import annotate_crf
from crf_annotation.constants import OUTPUT_PDF_NAME
from crf_annotation.design import DesignError
from crf_annotation.sources import identify, load_study_design, registered
from crf_annotation.sources.profile import Profile, ProfileSource


def annotations(pdf_path):
    with pymupdf.open(pdf_path) as document:
        return {i: [a.info.get("title", "") for a in p.annots()]
                for i, p in enumerate(document, start=1)}


def test_builtin_adapters_are_registered():
    names = {a.name for a in registered()}
    assert {"Medidata Rave ALS", "CDISC ODM-XML", "Rave source_metadata JSON"} <= names


def test_odm_is_recognised_over_other_adapters(odm_study):
    ranked = identify(odm_study["odm"])
    assert ranked, "no adapter claimed the ODM file"
    assert ranked[0][1].name == "CDISC ODM-XML"


def test_odm_yields_the_same_design_as_rave(study, odm_study):
    rave = load_study_design([
        study["meta"] / "source_metadata_test.json",
        study["meta"] / "als_test.xlsx",
    ])
    odm = load_study_design([odm_study["odm"]])

    assert {f.oid for f in odm.forms} == {f.oid for f in rave.forms}
    assert {(f.form_oid, f.oid) for f in odm.fields} == {(f.form_oid, f.oid) for f in rave.fields}
    assert odm.study_name == "STUDY123"


def test_odm_prefers_the_english_question_text(odm_study):
    design = load_study_design([odm_study["odm"]])
    sex = next(f for f in design.fields if f.oid == "SEX")
    assert sex.label == "Sex"          # not the German TranslatedText


def test_odm_reads_the_visit_matrix(odm_study):
    design = load_study_design([odm_study["odm"]])
    matrix = design.visit_frame
    assert set(matrix["VISIT"]) == {"Screening", "Visit 1"}
    assert set(matrix.loc[matrix["VISIT"] == "Visit 1", "DS_OID"]) == {"AE_FORM", "NT_FORM"}


def test_full_run_from_odm_metadata_only(odm_study, study):
    """The end-to-end proof: no Rave file anywhere, identical annotations."""
    annotate_crf(**study["job"])
    from_rave = annotations(study["out"] / OUTPUT_PDF_NAME)

    annotate_crf(**odm_study["job"])
    from_odm = annotations(odm_study["out"] / OUTPUT_PDF_NAME)

    assert from_odm == from_rave


def test_unreadable_metadata_fails_with_every_reason(tmp_path):
    bad = tmp_path / "mystery.txt"
    bad.write_text("not a study design")
    with pytest.raises(DesignError) as excinfo:
        load_study_design([bad])
    assert "mystery.txt" in str(excinfo.value)


# --- profile-driven adapter (the no-code path for a new vendor) -----------
VENDOR_EXPORT = {
    "studyDesign": {
        "casebookForms": [
            {"formName": "DM_FORM", "displayLabel": "Demographics", "seq": 1},
            {"formName": "AE_FORM", "displayLabel": "Adverse Events", "seq": 2},
        ],
        "items": [
            {"itemName": "BRTHDT", "parentForm": "DM_FORM", "prompt": "Date of Birth"},
            {"itemName": "SEX", "parentForm": "DM_FORM", "prompt": "Sex"},
            {"itemName": "AETERM", "parentForm": "AE_FORM", "prompt": "Adverse Event Term"},
        ],
    }
}

VENDOR_PROFILE = {
    "name": "Example Vault CDMS export",
    "format": "json",
    "match": {"filename_contains": ["casebook"], "requires_keys": ["studyDesign"]},
    "forms": {"path": "studyDesign.casebookForms", "oid": "formName",
              "label": "displayLabel", "order": "seq"},
    "fields": {"path": "studyDesign.items", "oid": "itemName",
               "form_oid": "parentForm", "label": "prompt"},
}


def test_profile_reads_an_arbitrary_vendor_json(tmp_path):
    export = tmp_path / "casebook_export.json"
    export.write_text(json.dumps(VENDOR_EXPORT))

    source = ProfileSource(Profile.from_dict(VENDOR_PROFILE))
    assert source.sniff(export) > 0.9

    design = source.load(export).validate()
    assert [f.label for f in design.forms] == ["Demographics", "Adverse Events"]
    assert {f.oid for f in design.fields} == {"BRTHDT", "SEX", "AETERM"}


def test_profile_does_not_claim_a_foreign_file(tmp_path):
    other = tmp_path / "source_metadata_x.json"
    other.write_text(json.dumps({"forms": [], "fields": []}))
    source = ProfileSource(Profile.from_dict(VENDOR_PROFILE))
    assert source.sniff(other) == 0.0


def test_profile_requires_forms_and_fields_sections():
    with pytest.raises(DesignError):
        Profile.from_dict({"name": "broken", "format": "json", "forms": {}})


def test_odm_keeps_each_fields_section(odm_study):
    """One form can hold sections for several domains; the section travels with the field."""
    design = load_study_design([odm_study["odm"]])
    sections = {field.oid: field.section for field in design.fields}
    assert sections["SEX"] == "DM" and sections["AETERM"] == "AE"


def test_odm_items_sharing_a_name_stay_separate_fields(tmp_path):
    """A value, its normal-range flag and its clinical significance, all Named
    'Creatinine': three fields, not one."""
    odm = tmp_path / "shared_names.xml"
    odm.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3" ODMVersion="1.3.2">
  <Study OID="S.1"><MetaDataVersion OID="MDV.1" Name="v1">
    <FormDef OID="F.1" Name="Screening"><ItemGroupRef ItemGroupOID="IG.1" Mandatory="Yes"/></FormDef>
    <ItemGroupDef OID="IG.1" Name="Laboratory results">
      <ItemRef ItemOID="I.76" Mandatory="No"/><ItemRef ItemOID="I.77" Mandatory="No"/>
      <ItemRef ItemOID="I.9" Mandatory="No"/>
    </ItemGroupDef>
    <ItemDef OID="I.76" Name="Creatinine" DataType="integer"><Question><TranslatedText xml:lang="en">Creatinine value</TranslatedText></Question></ItemDef>
    <ItemDef OID="I.77" Name="Creatinine" DataType="boolean"><Question><TranslatedText xml:lang="en">Creatinine within normal range?</TranslatedText></Question></ItemDef>
    <ItemDef OID="I.9" Name="Urea" DataType="integer"><Question><TranslatedText xml:lang="en">Urea value</TranslatedText></Question></ItemDef>
  </MetaDataVersion></Study>
</ODM>""")
    design = load_study_design([odm])
    assert sorted(f.oid for f in design.fields) == ["I.76", "I.77", "Urea"]
