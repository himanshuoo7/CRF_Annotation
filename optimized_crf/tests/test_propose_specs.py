"""Drafting mapping specs from the study design when none were supplied."""

from __future__ import annotations

import json

import pytest

from crf_annotation import MatchingOptions, annotate_crf
from crf_annotation.ai.propose_specs import (
    PROPOSAL_FILE,
    propose_mappings,
    to_spec_documents,
    write_proposal,
)
from crf_annotation.design import Field, Form, StudyDesign
from crf_annotation.job import InputError
from crf_annotation.specs import SpecCatalog


def _design(*forms: tuple[str, str, list[tuple[str, str]]]) -> StudyDesign:
    return StudyDesign(
        source_system="test",
        forms=tuple(Form(oid=oid, label=label) for oid, label, _ in forms),
        fields=tuple(Field(oid=f, form_oid=oid, label=label) for oid, _, fields in forms for f, label in fields),
    )


class FakeClient:
    """Stands in for ClaudeClient: answers once, records what it was asked."""

    def __init__(self, answer: dict) -> None:
        self.answer = answer
        self.asked: list[str] = []

    def available(self) -> bool:
        return True

    def json_call(self, *, system, user, schema, cache_salt=None):
        self.asked.append(user)
        return self.answer


def test_cdisc_rules_place_common_forms_and_fields_without_a_model():
    design = _design(
        ("DM_FORM", "Demographics", [("BRTHDT", "Date of Birth"), ("SEX", "Sex"), ("RACE", "Race")]),
        ("AE_FORM", "Adverse Events", [("AETERM", "Adverse Event Term"), ("AESTDT", "Start Date"),
                                       ("AESER", "Serious?")]),
        ("VS_FORM", "Vital Signs", [("SYS", "Systolic Blood Pressure"), ("VSDAT", "Date of Assessment")]),
    )
    proposal = propose_mappings(design, client=None)
    placed = {(m.field_oid, m.variable, m.testcd) for m in proposal.mappings}
    assert ("BRTHDT", "BRTHDTC", "") in placed
    assert ("AESTDT", "AESTDTC", "") in placed
    assert ("AESER", "AESER", "") in placed
    assert ("SYS", "VSORRES", "SYSBP") in placed
    assert ("VSDAT", "VSDTC", "") in placed
    assert all(m.source == "rule" for m in proposal.mappings)
    assert proposal.unmapped == []


def test_a_large_study_is_asked_in_small_batches_so_answers_are_not_truncated():
    from crf_annotation.ai.propose_specs import BATCH_FIELDS

    big = [(f"Q{i}", f"Odd question {i}") for i in range(60)]
    small = [(f"R{i}", f"Other question {i}") for i in range(5)]
    design = _design(("XX_FORM", "Questionnaire", big), ("YY_FORM", "Extras", small))
    client = FakeClient({"forms": []})

    propose_mappings(design, client)

    sizes = [user.count('"field_oid"') for user in client.asked]
    assert len(client.asked) == 3 and sum(sizes) == 65
    assert max(sizes) <= BATCH_FIELDS


def test_the_model_places_what_the_rules_cannot_and_bad_answers_are_dropped():
    design = _design(("NT_FORM", "Notes", [("COMMENT", "Comments"), ("DONE", "Was the form completed?"),
                                           ("JUNK", "Something odd")]))
    client = FakeClient({"forms": [{
        "form_oid": "NT_FORM", "domain": "CO", "domain_label": "Comments", "class": "SPECIAL PURPOSE",
        "fields": [
            {"field_oid": "COMMENT", "kind": "variable", "variable": "COVAL", "testcd": "", "qnam": "",
             "confidence": 0.9, "reason": "free-text comment"},
            {"field_oid": "DONE", "kind": "skip", "variable": "", "testcd": "", "qnam": "",
             "confidence": 0.95, "reason": "operational"},
            {"field_oid": "JUNK", "kind": "variable", "variable": "not a name", "testcd": "", "qnam": "",
             "confidence": 0.2, "reason": "?"},
            {"field_oid": "NOT_IN_DESIGN", "kind": "variable", "variable": "COVAL", "testcd": "", "qnam": "",
             "confidence": 1, "reason": "invented"},
        ],
    }]})
    proposal = propose_mappings(design, client)
    assert [(m.field_oid, m.domain, m.variable, m.source) for m in proposal.mappings] == [
        ("COMMENT", "CO", "COVAL", "ai")
    ]
    assert "Comments" in client.asked[0]
    assert proposal.counts == {"proposed": 1, "by_rule": 0, "by_ai": 1, "unmapped_fields": 0}


def test_proposals_become_ordinary_spec_files_the_catalog_reads(tmp_path):
    design = _design(
        ("AE_FORM", "Adverse Events", [("AETERM", "Adverse Event Term")]),
        ("VS_FORM", "Vital Signs", [("SYS", "Systolic Blood Pressure")]),
    )
    proposal = propose_mappings(design, client=None)
    proposal.mappings.append(type(proposal.mappings[0])(
        "AE_FORM", "AESEVX", "Severity (local scale)", "AE", "QVAL", qnam="AESEVX", source="ai"))
    files = write_proposal(proposal, tmp_path)
    catalog = SpecCatalog.load(files)
    texts = set(catalog.mappings["ATEXT"])
    assert "AE.AETERM" in texts
    assert "VS.VSORRES where VSTESTCD = SYSBP" in texts
    assert "SUPPAE.QVAL where QNAM = AESEVX" in texts
    summary = json.loads((tmp_path / PROPOSAL_FILE).read_text())
    assert summary["counts"]["proposed"] == 3
    assert {"AE_FORM__AE", "VS_FORM__VS", "AE_FORM__SUPPAE"} <= set(to_spec_documents(proposal))


def test_a_run_with_only_a_crf_and_a_design_annotates_from_proposed_specs(study):
    job = dict(study["job"])
    job.pop("specs")
    with pytest.raises(InputError):
        annotate_crf(**job)
    job["matching"] = MatchingOptions(ai=False, propose_specs=True)
    result = annotate_crf(**job)
    assert result.annotations > 0
    assert result.proposed_specs["proposed"] >= 5
    assert result.to_dict()["proposed_specs"] == result.proposed_specs
    assert (study["out"] / "proposed_specs" / PROPOSAL_FILE).exists()
