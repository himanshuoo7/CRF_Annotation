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
    assert "AE_FORM.AESER" in proposal.unmapped        # "Serious?" is a question: left for the model
    assert ("SYS", "VSORRES", "SYSBP") in placed
    assert ("VSDAT", "VSDTC", "") in placed
    assert all(m.source == "rule" for m in proposal.mappings)


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
    # the invalid answer for JUNK is reported as unmapped, not silently dropped
    assert proposal.counts == {"proposed": 1, "by_rule": 0, "by_ai": 1, "unmapped_fields": 1}


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
    assert "AESEVX in SUPPAE" in texts
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


def test_rules_from_a_real_study_place_each_field_correctly():
    """Labels from GSK LPL107988 that the first rules got wrong."""
    design = _design(
        ("CM_FORM", "Concomitant Medication", [
            ("SUBJ", "Subject Identifier"), ("ANYCM", "Were any concomitant medications taken by the subject?"),
            ("DRUG", "Drug Name"), ("WHY", "Reason for medication"), ("ONG", "Ongoing medication?")]),
        ("DM_FORM", "Demography", [("SITE", "Center Number")]),
        ("IP_FORM", "Investigational Product", [("SUBJ2", "Subject Identifier")]),  # no domain rule
        ("AE_FORM", "Non-serious adverse events", [
            ("ANYAE", "Did the subject experience any non-serious adverse events during the study?"),
            ("ACN", "Action Taken with Investigational Product(s) as a Result of the Non-Serious AE")]),
        ("SAE_FORM", "Serious Adverse Events", [
            ("ANYSAE", "Did the subject experience a serious adverse event during the study?")]),
        ("EG_FORM", "ECG", [("BRADY", "ECG Rhythm Sinus bradycardia (heart rate < 30 beats/min)")]),
    )
    proposal = propose_mappings(design, client=None)
    placed = {m.field_oid: f"{m.domain}.{m.variable}" for m in proposal.mappings}

    assert placed["SUBJ"] == "DM.SUBJID" and placed["SITE"] == "DM.SITEID"
    assert placed["SUBJ2"] == "DM.SUBJID"
    assert placed["DRUG"] == "CM.CMTRT"
    assert placed["WHY"] == "CM.CMINDC"                         # terse label: the rule places it
    assert "BRADY" not in placed                                # not a vital-sign PULSE
    # questions and long labels are the model's (rules matched them by accident),
    # including yes/no gates: only the model can tell "any medications?" (not
    # submitted) from "any diuretics?" (CMOCCUR)
    assert sorted(proposal.unmapped) == ["AE_FORM.ACN", "AE_FORM.ANYAE", "CM_FORM.ANYCM", "CM_FORM.ONG",
                                         "EG_FORM.BRADY", "SAE_FORM.ANYSAE"]


def test_the_same_question_on_two_forms_gets_the_same_code():
    design = _design(("EG1", "12-Lead ECG Abnormalities", [("AF", "ECG Rhythm Atrial fibrillation")]),
                     ("EG2", "Telemetry ECG Abnormalities", [("AF2", "ECG Rhythm Atrial fibrillation")]))

    def answer(form, field, testcd, confidence):
        return {"form_oid": form, "domain": "EG", "domain_label": "ECG", "class": "FINDINGS", "fields": [
            {"field_oid": field, "kind": "finding", "variable": "EGORRES", "testcd": testcd, "qnam": "",
             "confidence": confidence, "reason": "rhythm"}]}

    client = FakeClient({"forms": [answer("EG1", "AF", "ATFIB", 0.9), answer("EG2", "AF2", "ATRFIB", 0.8)]})
    proposal = propose_mappings(design, client)
    assert {m.testcd for m in proposal.mappings} == {"ATFIB"}


def test_an_identifier_on_any_form_is_written_to_the_dm_spec():
    design = _design(("CM_FORM", "Concomitant Medication", [("SUBJ", "Subject Identifier"), ("DRUG", "Drug Name")]))
    documents = to_spec_documents(propose_mappings(design, client=None))
    assert documents["CM_FORM__DM"]["variables"][0]["variable"] == "SUBJID"
    assert documents["CM_FORM__DM"]["domain"]["functions"] == [{"source_dataset": "CM_FORM"}]
    assert documents["CM_FORM__DM"]["domain"]["label"] == "Demographics"
    assert documents["CM_FORM__CM"]["variables"][0]["variable"] == "CMTRT"


def test_a_field_named_with_a_comma_keeps_its_annotation(tmp_path):
    """Some EDCs use the label as the field name: 'Myocardial infarction, inferior'."""
    design = _design(("EG_FORM", "ECG Abnormalities", [("Myocardial infarction, inferior", "Myocardial infarction, inferior")]))
    client = FakeClient({"forms": [{"form_oid": "EG_FORM", "domain": "EG", "domain_label": "ECG", "class": "FINDINGS",
                                    "fields": [{"field_oid": "Myocardial infarction, inferior", "domain": "", "kind": "finding",
                                                "variable": "EGORRES", "testcd": "MIINF", "qnam": "", "confidence": 0.9,
                                                "reason": "ECG finding"}]}]})
    from crf_annotation.specs import SpecCatalog

    proposal = propose_mappings(design, client)
    catalog = SpecCatalog.load(write_proposal(proposal, tmp_path))
    assert list(catalog.mappings["RSRC_VAR"]) == ["Myocardial infarction, inferior"]


def test_the_model_can_put_a_field_in_another_domain_than_its_form():
    design = _design(("EX_FORM", "Investigational Product", [("RANDDT", "Date of Randomisation")]))
    client = FakeClient({"forms": [{"form_oid": "EX_FORM", "domain": "EX", "domain_label": "Exposure", "class": "INTERVENTIONS",
                                    "fields": [{"field_oid": "RANDDT", "domain": "DS", "kind": "variable", "variable": "DSSTDTC",
                                                "testcd": "", "qnam": "", "confidence": 0.9, "reason": "randomisation"}]}]})
    proposal = propose_mappings(design, client)
    assert [(m.domain, m.variable) for m in proposal.mappings] == [("DS", "DSSTDTC")]
    assert "EX_FORM__DS" in to_spec_documents(proposal)



def test_a_field_the_model_skipped_reuses_the_same_question_on_another_form():
    design = _design(("EG1", "12-Lead ECG Abnormalities", [("LAD1", "Left axis deviation")]),
                     ("EG2", "Telemetry ECG Abnormalities", [("LAD2", "Left axis deviation")]))
    client = FakeClient({"forms": [
        {"form_oid": "EG1", "domain": "EG", "domain_label": "ECG", "class": "FINDINGS",
         "fields": [{"field_oid": "LAD1", "domain": "", "kind": "finding", "variable": "EGORRES", "testcd": "LAD",
                     "qnam": "", "confidence": 0.9, "reason": "axis"}]},
        {"form_oid": "EG2", "domain": "EG", "domain_label": "ECG", "class": "FINDINGS", "fields": []},
    ]})
    proposal = propose_mappings(design, client)
    assert {(m.field_oid, m.testcd) for m in proposal.mappings} == {("LAD1", "LAD"), ("LAD2", "LAD")}
    assert proposal.unmapped == []


def test_one_form_with_many_sections_maps_each_field_to_its_own_domain():
    """A screening form holding history, labs and eligibility: the variable's
    name decides the domain, never the form's."""
    design = _design(("SCR", "Visit 1", [("HB", "Haemoglobin"), ("INC1", "Age 18 or over?"), ("SURG", "Prior surgery")]))
    client = FakeClient({"forms": [{"form_oid": "SCR", "domain": "MH", "domain_label": "Medical History", "class": "EVENTS",
                                    "fields": [
        {"field_oid": "HB", "domain": "", "kind": "finding", "variable": "LBORRES", "testcd": "HGB", "qnam": "",
         "confidence": 0.9, "reason": "lab"},
        {"field_oid": "INC1", "domain": "", "kind": "finding", "variable": "IEORRES", "testcd": "INCL01", "qnam": "",
         "confidence": 0.9, "reason": "criterion"},
        {"field_oid": "SURG", "domain": "", "kind": "variable", "variable": "MHTERM", "testcd": "", "qnam": "",
         "confidence": 0.9, "reason": "history"}]}]})
    proposal = propose_mappings(design, client)
    assert {(m.field_oid, m.domain) for m in proposal.mappings} == {("HB", "LB"), ("INC1", "IE"), ("SURG", "MH")}
    documents = to_spec_documents(proposal)
    assert {"SCR__LB", "SCR__IE", "SCR__MH"} <= set(documents)


def test_odm_sections_reach_the_model():
    from crf_annotation.ai.propose_specs import propose_mappings as propose
    from crf_annotation.design import Field, Form, StudyDesign

    design = StudyDesign(source_system="test", forms=(Form(oid="SCR", label="Visit 1"),),
                         fields=(Field(oid="HB", form_oid="SCR", label="Haemoglobin", section="Laboratory results"),))
    client = FakeClient({"forms": []})
    propose(design, client)
    assert '"section": "Laboratory results"' in client.asked[0]


def test_a_one_letter_test_code_is_valid():
    """Potassium is K in CDISC Controlled Terminology; it was silently dropped."""
    design = _design(("LB_FORM", "Laboratory results", [("K", "Serum potassium measurement value")]))
    client = FakeClient({"forms": [{"form_oid": "LB_FORM", "domain": "LB", "domain_label": "Labs", "class": "FINDINGS",
                                    "fields": [{"field_oid": "K", "domain": "", "kind": "finding", "variable": "LBORRES",
                                                "testcd": "K", "qnam": "", "confidence": 0.95, "reason": "potassium"}]}]})
    proposal = propose_mappings(design, client)
    assert [(m.variable, m.testcd) for m in proposal.mappings] == [("LBORRES", "K")]


def test_an_invalid_answer_is_reported_unmapped_not_silently_skipped():
    design = _design(("LB_FORM", "Laboratory results", [("X", "Odd value")]))
    client = FakeClient({"forms": [{"form_oid": "LB_FORM", "domain": "LB", "domain_label": "Labs", "class": "FINDINGS",
                                    "fields": [{"field_oid": "X", "domain": "", "kind": "finding", "variable": "LB ORRES",
                                                "testcd": "X", "qnam": "", "confidence": 0.5, "reason": "?"}]}]})
    assert propose_mappings(design, client).unmapped == ["LB_FORM.X"]


def test_an_invented_variable_is_asked_again_and_never_drawn():
    """CMONGO looks plausible and does not exist: the model gets one more try."""
    design = _design(("CM_FORM", "Concomitant Medication", [("ONG", "Is the medication ongoing?")]))

    class Correcting(FakeClient):
        def json_call(self, *, system, user, schema, cache_salt=None):
            self.asked.append(user)
            variable = "CMENRTPT" if "rejected" in user else "CMONGO"
            return {"forms": [{"form_oid": "CM_FORM", "domain": "CM", "domain_label": "CM", "class": "INTERVENTIONS",
                               "fields": [{"field_oid": "ONG", "domain": "", "kind": "variable", "variable": variable,
                                           "testcd": "", "qnam": "", "confidence": 0.8, "reason": "ongoing"}]}]}

    client = Correcting({})
    proposal = propose_mappings(design, client)
    assert [m.variable for m in proposal.mappings] == ["CMENRTPT"]
    assert len(client.asked) == 2 and "CM.CMONGO is not an SDTM variable" in client.asked[1]


def test_sdtm_names():
    from crf_annotation.sdtm_names import is_standard_variable

    for domain, variable in [("CM", "CMENRTPT"), ("CM", "CMOCCUR"), ("AE", "AESMIE"), ("DM", "SEX"), ("DM", "DTHDTC"),
                             ("CO", "COVAL"), ("LB", "LBCLSIG"), ("DD", "DDORRES"), ("SUPPAE", "QVAL")]:
        assert is_standard_variable(domain, variable), (domain, variable)
    for domain, variable in [("CM", "CMONGO"), ("CM", "CMDOS"), ("CM", "CMDSTXT"), ("AE", "LBORRES"), ("VS", "SEX")]:
        assert not is_standard_variable(domain, variable), (domain, variable)


def test_rules_name_whole_labels_and_leave_lookalikes_to_the_model():
    design = _design(("AE_FORM", "Adverse Events", [
        ("TERM", "Adverse Event Term"), ("SEV", "Maximum Intensity"), ("REL", "Relationship to study drug"),
        ("SMIE", "Other Medically Important Event"), ("RELNST", "Relationship to Non-Study Treatment"),
        ("RACE", "<b>Outcome</b>")]))
    proposal = propose_mappings(design, client=None)
    placed = {m.field_oid: m.variable for m in proposal.mappings}
    assert placed == {"TERM": "AETERM", "SEV": "AESEV", "REL": "AEREL", "RACE": "AEOUT"}
    assert sorted(proposal.unmapped) == ["AE_FORM.RELNST", "AE_FORM.SMIE"]


def test_a_field_named_with_quotes_keeps_its_annotation(tmp_path):
    name = 'Patient must be classified "operable"'
    design = _design(("IE_FORM", "Inclusion criteria", [(name, name)]))
    client = FakeClient({"forms": [{"form_oid": "IE_FORM", "domain": "IE", "domain_label": "IE", "class": "FINDINGS",
                                    "fields": [{"field_oid": name, "domain": "", "kind": "finding", "variable": "IEORRES",
                                                "testcd": "INCL09", "qnam": "", "confidence": 0.9, "reason": "criterion"}]}]})
    from crf_annotation.specs import SpecCatalog

    catalog = SpecCatalog.load(write_proposal(propose_mappings(design, client), tmp_path))
    assert list(catalog.mappings["RSRC_VAR"]) == [name]
