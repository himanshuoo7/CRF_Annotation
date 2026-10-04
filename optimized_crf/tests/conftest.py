"""Synthetic study fixture: a 3-page CRF, EDC metadata, specs and an ALS book."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pymupdf
import pytest

from crf_annotation import MatchingOptions

PAGES = [
    ("Form: Demographics", ["Date of Birth", "Sex", "Race"]),
    ("Form: Adverse Events", ["Adverse Event Term", "Start Date", "Serious?"]),
    ("Form: Notes", ["Comments"]),
]

SOURCE_METADATA = {
    "forms": [
        {"form": "DM_FORM", "form_label": "Demographics"},
        {"form": "AE_FORM", "form_label": "Adverse Events"},
        {"form": "NT_FORM", "form_label": "Notes"},
    ],
    "fields": [
        {"form": "DM_FORM", "field": "BRTHDT", "field_label": "Date of Birth"},
        {"form": "DM_FORM", "field": "SEX", "field_label": "Sex"},
        {"form": "DM_FORM", "field": "RACE", "field_label": "<b>Race</b>"},
        {"form": "AE_FORM", "field": "AETERM", "field_label": "Adverse Event Term"},
        {"form": "AE_FORM", "field": "AESTDT", "field_label": "Start Date"},
        {"form": "AE_FORM", "field": "AESER", "field_label": "Serious?"},
        {"form": "NT_FORM", "field": "COMMENT", "field_label": "Comments"},
    ],
}


def _copy(name, invar):
    return {"function": name, "parameters": [{"name": "invar", "value": invar}]}


SPECS = {
    "dm.json": {
        "domain": {"dataset": "DM", "label": "Demographics", "class": "SPECIAL PURPOSE",
                   "functions": [{"source_dataset": "DM_FORM"}]},
        "variables": [
            {"dataset": "DM", "variable": "BRTHDTC", "functions": [_copy("g_copy", ["BRTHDT"])]},
            {"dataset": "DM", "variable": "SEX", "functions": [_copy("g_copy", "SEX")]},
            {"dataset": "DM", "variable": "RACE", "functions": [_copy("g_copy", ["RACE"])]},
        ],
        "value_level": [],
    },
    "ae.json": {
        "domain": {"dataset": "AE", "label": "Adverse Events", "class": "EVENTS",
                   "functions": [{"source_dataset": "AE_FORM"}]},
        "variables": [
            {"dataset": "AE", "variable": "AETERM", "functions": [_copy("g_copy", ["AETERM"])]},
            {"dataset": "AE", "variable": "AESTDTC", "functions": [_copy("g_concat", ["AESTDT"])]},
        ],
        "value_level": [
            {"dataset": "AE", "variable": "AESER", "where_clause": "AESER EQ Y",
             "functions": [_copy("g_copy", ["AESER"])]},
        ],
    },
    "suppae.json": {
        "domain": {"dataset": "SUPPAE", "label": "Supplemental Qualifiers for AE",
                   "class": "RELATIONSHIP", "functions": [{"source_dataset": "AE_FORM"}]},
        "variables": [],
        "value_level": [
            {"variable": "QVAL", "where_clause": "QNAM EQ AESEV",
             "functions": [_copy("g_copy", ["AESER"])]},
        ],
    },
}


@pytest.fixture
def study(tmp_path: Path) -> dict:
    specs, meta, crf, out, logs = (tmp_path / n for n in ("specs", "meta", "crf", "out", "logs"))
    for directory in (specs, meta, crf, out, logs):
        directory.mkdir()

    document = pymupdf.open()
    for banner, fields in PAGES:
        page = document.new_page(width=612, height=792)
        page.insert_text((60, 100), banner, fontsize=12)
        y = 150
        for label in fields:
            page.insert_text((60, y), label, fontsize=10)
            y += 40
    document.save(crf / "blank_crf.pdf")
    document.close()

    (meta / "source_metadata_test.json").write_text(json.dumps(SOURCE_METADATA))
    for name, spec in SPECS.items():
        (specs / name).write_text(json.dumps(spec))

    forms = pd.DataFrame({"OID": ["DM_FORM", "AE_FORM", "NT_FORM"], "Ordinal": [1, 2, 3],
                          "DraftFormName": ["Demographics", "Adverse Events", "Notes"]})
    folders = pd.DataFrame({"OID": ["SCRN", "V1"], "Ordinal": [1, 2],
                            "FolderName": ["Screening", "Visit 1"],
                            "ParentFolderOID": ["MTX", "MTX"]})
    matrix = pd.DataFrame({"OID": ["DM_FORM", "AE_FORM", "NT_FORM"],
                           "SCRN": ["X", "", ""], "V1": ["", "X", "X"]})
    with pd.ExcelWriter(meta / "als_test.xlsx", engine="xlsxwriter") as writer:
        forms.to_excel(writer, sheet_name="Forms", index=False)
        folders.to_excel(writer, sheet_name="Folders", index=False)
        matrix.to_excel(writer, sheet_name="Matrix#MTX", index=False)

    return {
        "root": tmp_path,
        "out": out,
        "logs": logs,
        "specs": specs,
        "meta": meta,
        "crf": crf / "blank_crf.pdf",
        # Keyword arguments for annotate_crf(), so a test can override one.
        "job": {
            "crf": str(crf / "blank_crf.pdf"),
            "output_dir": str(out),
            "specs": str(specs),
            "design": [str(meta / "source_metadata_test.json"), str(meta / "als_test.xlsx")],
            "study": "STUDY123",
            "log_dir": str(logs),
            "matching": MatchingOptions(ai=False),
        },
    }


ODM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3" ODMVersion="1.3.2">
  <Study OID="S.STUDY123">
    <GlobalVariables><StudyName>STUDY123</StudyName></GlobalVariables>
    <MetaDataVersion OID="MDV.1" Name="v1">
      <Protocol>
        <StudyEventRef StudyEventOID="SE.SCRN" OrderNumber="1"/>
        <StudyEventRef StudyEventOID="SE.V1" OrderNumber="2"/>
      </Protocol>
      <StudyEventDef OID="SE.SCRN" Name="Screening">
        <FormRef FormOID="DM_FORM" OrderNumber="1"/>
      </StudyEventDef>
      <StudyEventDef OID="SE.V1" Name="Visit 1">
        <FormRef FormOID="AE_FORM" OrderNumber="1"/>
        <FormRef FormOID="NT_FORM" OrderNumber="2"/>
      </StudyEventDef>
      <FormDef OID="DM_FORM" Name="DM_FORM">
        <Description><TranslatedText xml:lang="en">Demographics</TranslatedText></Description>
        <ItemGroupRef ItemGroupOID="IG.DM" OrderNumber="1"/>
      </FormDef>
      <FormDef OID="AE_FORM" Name="AE_FORM" Repeating="Yes">
        <Description><TranslatedText xml:lang="en">Adverse Events</TranslatedText></Description>
        <ItemGroupRef ItemGroupOID="IG.AE" OrderNumber="1"/>
      </FormDef>
      <FormDef OID="NT_FORM" Name="NT_FORM">
        <Description><TranslatedText xml:lang="en">Notes</TranslatedText></Description>
        <ItemGroupRef ItemGroupOID="IG.NT" OrderNumber="1"/>
      </FormDef>
      <ItemGroupDef OID="IG.DM" Name="DM">
        <ItemRef ItemOID="I.BRTHDT" OrderNumber="1"/>
        <ItemRef ItemOID="I.SEX" OrderNumber="2"/>
        <ItemRef ItemOID="I.RACE" OrderNumber="3"/>
      </ItemGroupDef>
      <ItemGroupDef OID="IG.AE" Name="AE" Repeating="Yes">
        <ItemRef ItemOID="I.AETERM" OrderNumber="1"/>
        <ItemRef ItemOID="I.AESTDT" OrderNumber="2"/>
        <ItemRef ItemOID="I.AESER" OrderNumber="3"/>
      </ItemGroupDef>
      <ItemGroupDef OID="IG.NT" Name="NT">
        <ItemRef ItemOID="I.COMMENT" OrderNumber="1"/>
      </ItemGroupDef>
      <ItemDef OID="I.BRTHDT" Name="BRTHDT" DataType="date">
        <Question><TranslatedText xml:lang="en">Date of Birth</TranslatedText></Question>
      </ItemDef>
      <ItemDef OID="I.SEX" Name="SEX" DataType="text">
        <Question><TranslatedText xml:lang="de">Geschlecht</TranslatedText>
                  <TranslatedText xml:lang="en">Sex</TranslatedText></Question>
      </ItemDef>
      <ItemDef OID="I.RACE" Name="RACE" DataType="text">
        <Question><TranslatedText xml:lang="en">Race</TranslatedText></Question>
      </ItemDef>
      <ItemDef OID="I.AETERM" Name="AETERM" DataType="text">
        <Question><TranslatedText xml:lang="en">Adverse Event Term</TranslatedText></Question>
      </ItemDef>
      <ItemDef OID="I.AESTDT" Name="AESTDT" DataType="date">
        <Question><TranslatedText xml:lang="en">Start Date</TranslatedText></Question>
      </ItemDef>
      <ItemDef OID="I.AESER" Name="AESER" DataType="text">
        <Question><TranslatedText xml:lang="en">Serious?</TranslatedText></Question>
      </ItemDef>
      <ItemDef OID="I.COMMENT" Name="COMMENT" DataType="text">
        <Question><TranslatedText xml:lang="en">Comments</TranslatedText></Question>
      </ItemDef>
    </MetaDataVersion>
  </Study>
</ODM>
"""


@pytest.fixture
def odm_study(study, tmp_path):
    """The same study, but its design comes from a CDISC ODM-XML export."""
    odm_path = study["meta"] / "study_design.xml"
    odm_path.write_text(ODM_XML)
    out = tmp_path / "out_odm"
    out.mkdir()
    job = dict(study["job"], design=str(odm_path), output_dir=str(out))
    return {**study, "job": job, "odm": odm_path, "out": out}
