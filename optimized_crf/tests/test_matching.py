"""Matching tiers, form binding, and the guardrails around the AI tier."""

from __future__ import annotations

import pandas as pd
import pytest

from crf_annotation.binding import BannerBinder, HeaderNameBinder, bind_forms
from crf_annotation.design import Field, Form, build
from crf_annotation.matching import (
    METHOD_EXACT,
    Candidate,
    FuzzyResolver,
    MatchRequest,
    MatchResult,
    build_requests,
    refine,
)


def _design():
    return build(
        "test",
        forms=[Form(oid="DM_FORM", label="Demographics", order=0),
               Form(oid="AE_FORM", label="Adverse Events", order=1)],
        fields=[Field(oid="BRTHDT", form_oid="DM_FORM", label="Date of Birth"),
                Field(oid="AETERM", form_oid="AE_FORM", label="Adverse Event Term")],
    )


def _items(rows):
    return pd.DataFrame(rows, columns=["PAGE_NO", "ITM_TEXT", "X0", "Y0", "X1", "Y1"])


# --- form binding ---------------------------------------------------------
def test_banner_binder_resolves_rave_style_pages():
    items = _items([
        (1, "Form: Demographics", 60, 700, 200, 712),
        (1, "Date of Birth", 60, 650, 140, 662),
    ])
    bound = bind_forms(items, _design(), BannerBinder())
    assert list(bound["FORM_OID"]) == ["DM_FORM", "DM_FORM"]
    assert list(bound["FORM_LABEL"]) == ["Demographics", "Demographics"]


def test_header_binder_works_without_any_banner_prefix():
    """A CRF that just prints the form name in its header band."""
    items = _items([
        (1, "Adverse Events", 60, 740, 200, 752),
        (1, "Adverse Event Term", 60, 600, 200, 612),
    ])
    bound = bind_forms(items, _design(), HeaderNameBinder())
    assert set(bound["FORM_OID"]) == {"AE_FORM"}


def test_auto_binder_picks_whichever_strategy_binds_more():
    """No 'Form:' prefix anywhere — the header strategy has to win."""
    items = _items([
        (1, "Demographics", 60, 740, 200, 752),
        (1, "Date of Birth", 60, 600, 200, 612),
    ])
    bound = bind_forms(items, _design())
    assert set(bound["FORM_OID"]) == {"DM_FORM"}


def test_unbindable_pages_are_left_empty_not_guessed():
    items = _items([(1, "Some unrelated page", 60, 740, 200, 752)])
    bound = bind_forms(items, _design())
    assert set(bound["FORM_OID"]) == {""}


# --- fuzzy tier -----------------------------------------------------------
def _merged():
    return pd.DataFrame([
        {"PAGE_NO": 1, "ITM_TEXT": "Adverse Event Terms", "FORM_OID": "AE_FORM",
         "FORM_LABEL": "Adverse Events", "VAR_OID": None, "VAR_TXT": None},
        {"PAGE_NO": 1, "ITM_TEXT": "Completely unrelated legal notice",
         "FORM_OID": "AE_FORM", "FORM_LABEL": "Adverse Events",
         "VAR_OID": None, "VAR_TXT": None},
    ])


def _form_fields():
    return pd.DataFrame([
        {"DS_OID": "AE_FORM", "VAR_OID": "AETERM", "VAR_TXT": "Adverse Event Term"},
    ])


def test_fuzzy_resolves_a_near_miss_label():
    result = refine(_merged(), _form_fields(), resolvers=[FuzzyResolver()])
    assert result.loc[0, "VAR_OID"] == "AETERM"
    assert result.loc[0, "MATCH_METHOD"] == "fuzzy"
    assert result.loc[0, "MATCH_CONFIDENCE"] >= 0.88


def test_fuzzy_leaves_unrelated_text_alone():
    result = refine(_merged(), _form_fields(), resolvers=[FuzzyResolver()])
    assert pd.isna(result.loc[1, "VAR_OID"])
    assert result.loc[1, "MATCH_METHOD"] == ""


def test_min_confidence_gate_rejects_weak_matches():
    result = refine(_merged(), _form_fields(),
                    resolvers=[FuzzyResolver(threshold=0.1)], min_confidence=0.99)
    assert pd.isna(result.loc[0, "VAR_OID"])


def test_exact_matches_are_labelled_and_left_alone():
    merged = _merged()
    merged.loc[0, "VAR_OID"] = "AETERM"
    result = refine(merged, _form_fields(), resolvers=[FuzzyResolver()])
    assert result.loc[0, "MATCH_METHOD"] == METHOD_EXACT
    assert result.loc[0, "MATCH_CONFIDENCE"] == 1.0


def test_already_matched_fields_are_not_offered_again():
    """One field, one annotation — a claimed variable leaves the candidate list."""
    merged = _merged()
    merged.loc[1, "VAR_OID"] = "AETERM"
    requests = build_requests(merged, _form_fields())
    assert all("AETERM" not in [c.var_oid for c in r.candidates] for r in requests)


# --- AI tier guardrails (no network) --------------------------------------
class FakeClient:
    """Stands in for ClaudeClient; returns whatever payload it was given."""

    def __init__(self, payload):
        self.payload = payload
        self.seen = []

    def json_call(self, *, system, user, schema, cache_salt=None):
        self.seen.append(user)
        return self.payload

    def usage_summary(self):
        return "fake"


def _requests():
    return [MatchRequest(row_index=0, page_no=1, crf_text="DOB", form_oid="AE_FORM",
                         form_label="Adverse Events",
                         candidates=(Candidate("AETERM", "Adverse Event Term"),))]


def test_ai_tier_accepts_a_valid_choice():
    from crf_annotation.ai.label_match import ClaudeLabelResolver

    client = FakeClient({"matches": [
        {"id": 0, "variable": "AETERM", "confidence": 0.82, "reason": "abbreviation"}
    ]})
    results = ClaudeLabelResolver(client=client).resolve(_requests())
    assert results == [MatchResult(0, "AETERM", 0.82, "claude", "abbreviation")]


def test_ai_tier_rejects_an_invented_field():
    """The model must choose from the candidate list; anything else is dropped."""
    from crf_annotation.ai.label_match import ClaudeLabelResolver

    client = FakeClient({"matches": [
        {"id": 0, "variable": "NOT_A_REAL_FIELD", "confidence": 0.99, "reason": "made up"}
    ]})
    assert ClaudeLabelResolver(client=client).resolve(_requests()) == []


def test_ai_tier_ignores_an_empty_variable():
    from crf_annotation.ai.label_match import ClaudeLabelResolver

    client = FakeClient({"matches": [
        {"id": 0, "variable": "", "confidence": 0.4, "reason": "section heading"}
    ]})
    assert ClaudeLabelResolver(client=client).resolve(_requests()) == []


def test_ai_tier_survives_a_failed_call():
    from crf_annotation.ai.label_match import ClaudeLabelResolver

    assert ClaudeLabelResolver(client=FakeClient(None)).resolve(_requests()) == []


def test_a_broken_resolver_does_not_fail_the_run():
    class Exploding:
        name = "boom"

        def resolve(self, requests):
            raise RuntimeError("upstream is down")

    result = refine(_merged(), _form_fields(), resolvers=[Exploding()])
    assert len(result) == 2          # the run continues, just unrefined


def test_ai_can_be_switched_off(monkeypatch):
    from crf_annotation.ai import AiSettings, build_resolvers

    monkeypatch.setenv("ACRF_AI", "0")
    resolvers = build_resolvers(AiSettings.from_env())
    assert [r.name for r in resolvers] == ["fuzzy"]


def test_ai_is_on_by_default(monkeypatch):
    from crf_annotation.ai import AiSettings

    monkeypatch.delenv("ACRF_AI", raising=False)
    assert AiSettings.from_env().enabled is True


def test_missing_credentials_degrade_to_deterministic_tiers(monkeypatch):
    """A run must never fail because AI assist could not start."""
    from crf_annotation.ai import AiSettings, build_resolvers
    from crf_annotation.ai import client as client_module

    monkeypatch.setattr(client_module.ClaudeClient, "available", lambda self: False)
    resolvers = build_resolvers(AiSettings(enabled=True))
    assert [r.name for r in resolvers] == ["fuzzy"]


def test_an_outdated_sdk_is_reported_once_not_per_request(monkeypatch):
    """An old SDK must be a startup warning, not a silent per-batch failure."""
    import sys
    import types
    from crf_annotation.ai.client import AiUnavailable, ClaudeClient

    fake = types.ModuleType("anthropic")
    fake.__version__ = "0.25.0"
    fake.Anthropic = lambda *a, **k: object()
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    client = ClaudeClient()
    assert client.available() is False
    with pytest.raises(AiUnavailable, match="too old"):
        client._ensure_client()


def test_vertex_settings_pick_the_vertex_client(monkeypatch):
    """On Cloud Run, Claude is reached through Vertex AI with the service's
    own Google credentials, not an Anthropic key."""
    import sys
    import types
    from crf_annotation.ai.client import AiSettings, ClaudeClient

    made = {}
    fake = types.ModuleType("anthropic")
    fake.__version__ = "1.11.0"
    fake.Anthropic = lambda *a, **k: pytest.fail("first-party client used on Vertex")
    fake.AnthropicVertex = lambda **kwargs: made.update(kwargs) or object()
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setitem(sys.modules, "google", types.ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.auth", types.ModuleType("google.auth"))
    monkeypatch.setenv("ACRF_VERTEX_PROJECT", "acrf-project")
    monkeypatch.delenv("ACRF_VERTEX_REGION", raising=False)
    monkeypatch.delenv("CLOUD_ML_REGION", raising=False)

    settings = AiSettings.from_env()
    assert ClaudeClient(settings).available() is True
    assert made == {"project_id": "acrf-project", "region": "global"}
    assert settings.fingerprint() == AiSettings(model=settings.model, effort=settings.effort).fingerprint()


def test_a_gemini_model_gets_a_gemini_request_and_the_same_contract(tmp_path):
    """While testing, a cheap Gemini model stands in for Claude: same JSON
    contract, same cache, nothing sent to the Anthropic SDK."""
    from types import SimpleNamespace

    from crf_annotation.ai.client import AiSettings, ClaudeClient

    captured = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                text='{"answer": "DM.SEX"}', prompt_feedback=None,
                usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=4, thoughts_token_count=6),
            )

    client = ClaudeClient(AiSettings(model="gemini-3.8-flash", effort="xhigh", cache_dir=tmp_path))
    client._client = SimpleNamespace(models=FakeModels())
    system = [{"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}}]
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}

    assert client.json_call(system=system, user="Sex", schema=schema) == {"answer": "DM.SEX"}
    config = captured["config"]
    assert captured["model"] == "gemini-3.8-flash" and captured["contents"] == "Sex"
    assert config.system_instruction == "rules"
    assert config.response_mime_type == "application/json" and config.response_json_schema == schema
    assert config.thinking_config.thinking_level.value.lower() == "high"  # xhigh has no Gemini level
    assert (client.input_tokens, client.output_tokens) == (10, 10)

    captured.clear()
    assert client.json_call(system=system, user="Sex", schema=schema) == {"answer": "DM.SEX"}
    assert captured == {} and client.cache_hits == 1


@pytest.mark.parametrize("model,thinking,effort", [
    ("claude-opus-5", True, True),
    ("claude-sonnet-5", True, True),
    ("claude-haiku-4-5", False, False),      # 400s if either is sent
    ("claude-sonnet-4-5", False, False),
])
def test_request_is_shaped_to_the_model(model, thinking, effort):
    from crf_annotation.ai.client import supports_adaptive_thinking, supports_effort

    assert supports_adaptive_thinking(model) is thinking
    assert supports_effort(model) is effort


def test_haiku_request_omits_thinking_and_effort(monkeypatch):
    """A cheap model must get a valid request, not one it rejects."""
    from crf_annotation.ai.client import AiSettings, ClaudeClient

    captured = {}

    class FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop here — we only care about the request shape")

    class FakeSdk:
        messages = FakeMessages()

    client = ClaudeClient(AiSettings(model="claude-haiku-4-5"))
    monkeypatch.setattr(client, "_ensure_client", lambda: FakeSdk())

    client.json_call(system="s", user="u", schema={"type": "object"})

    assert "thinking" not in captured
    assert "effort" not in captured["output_config"]
    assert captured["output_config"]["format"]["type"] == "json_schema"


# --- dataset-qualified annotation matching --------------------------------
def _catalog(rows):
    from crf_annotation.specs import MAPPING_COLUMNS, SpecCatalog

    return SpecCatalog(mappings=pd.DataFrame(rows, columns=MAPPING_COLUMNS))


def _two_forms_sharing_a_variable():
    """`AETERM` is defined on two different forms — the classic collision."""
    return pd.DataFrame([
        {"PAGE_NO": 1, "FORM_OID": "AE_FORM", "DS_OID": "AE_FORM", "VAR_OID": "AETERM"},
        {"PAGE_NO": 2, "FORM_OID": "MH_FORM", "DS_OID": "MH_FORM", "VAR_OID": "AETERM"},
    ])


def test_strict_scope_keeps_each_form_to_its_own_annotation():
    from crf_annotation.merge import apply_annotations

    catalog = _catalog([
        {"RSRC_DS": "AE_FORM", "RSRC_VAR": "AETERM", "TARGET_DS": "AE", "ATEXT": "AE.AETERM"},
        {"RSRC_DS": "MH_FORM", "RSRC_VAR": "AETERM", "TARGET_DS": "MH", "ATEXT": "MH.MHTERM"},
    ])
    out = apply_annotations(_two_forms_sharing_a_variable(), catalog, dataset_scope="strict")

    assert len(out) == 2
    assert set(out.loc[out["FORM_OID"] == "AE_FORM", "ATEXT"]) == {"AE.AETERM"}
    assert set(out.loc[out["FORM_OID"] == "MH_FORM", "ATEXT"]) == {"MH.MHTERM"}


def test_legacy_scope_reproduces_the_cross_form_bleed():
    from crf_annotation.merge import apply_annotations

    catalog = _catalog([
        {"RSRC_DS": "AE_FORM", "RSRC_VAR": "AETERM", "TARGET_DS": "AE", "ATEXT": "AE.AETERM"},
        {"RSRC_DS": "MH_FORM", "RSRC_VAR": "AETERM", "TARGET_DS": "MH", "ATEXT": "MH.MHTERM"},
    ])
    out = apply_annotations(_two_forms_sharing_a_variable(), catalog, dataset_scope="off")
    assert len(out) == 4          # every row picks up both annotations


def test_auto_scope_uses_strict_when_the_keys_line_up():
    from crf_annotation.merge import apply_annotations

    catalog = _catalog([
        {"RSRC_DS": "AE_FORM", "RSRC_VAR": "AETERM", "TARGET_DS": "AE", "ATEXT": "AE.AETERM"},
        {"RSRC_DS": "MH_FORM", "RSRC_VAR": "AETERM", "TARGET_DS": "MH", "ATEXT": "MH.MHTERM"},
    ])
    out = apply_annotations(_two_forms_sharing_a_variable(), catalog)
    assert len(out) == 2


def test_auto_scope_falls_back_when_specs_use_a_different_vocabulary(caplog):
    """Spec says RAW.SOMETHINGELSE; a strict join would annotate nothing."""
    from crf_annotation.merge import apply_annotations

    catalog = _catalog([
        {"RSRC_DS": "RAW.ADVERSE", "RSRC_VAR": "AETERM", "TARGET_DS": "AE", "ATEXT": "AE.AETERM"},
    ])
    frame = pd.DataFrame([
        {"PAGE_NO": 1, "FORM_OID": "AE_FORM", "DS_OID": "AE_FORM", "VAR_OID": "AETERM"},
    ])
    with caplog.at_level("WARNING"):
        out = apply_annotations(frame, catalog)

    assert set(out["ATEXT"]) == {"AE.AETERM"}       # annotations are not lost
    assert "do not line up" in caplog.text          # but the risk is stated


def test_layer_prefixes_are_stripped_when_comparing_datasets():
    from crf_annotation.merge import apply_annotations, dataset_key

    assert dataset_key("RAW.AE_FORM") == "AE_FORM"
    assert dataset_key("ae_form") == "AE_FORM"

    catalog = _catalog([
        {"RSRC_DS": "RAW.AE_FORM", "RSRC_VAR": "AETERM", "TARGET_DS": "AE", "ATEXT": "AE.AETERM"},
    ])
    frame = pd.DataFrame([
        {"PAGE_NO": 1, "FORM_OID": "AE_FORM", "DS_OID": "AE_FORM", "VAR_OID": "AETERM"},
    ])
    out = apply_annotations(frame, catalog, dataset_scope="strict")
    assert set(out["ATEXT"]) == {"AE.AETERM"}


def test_auto_scope_stays_strict_when_most_keys_line_up():
    """A shared "Visit Date" item on every form, unmapped on one of them: the
    stray miss must not switch the whole run to variable-name matching, or
    every form's date annotation lands on every page."""
    from crf_annotation.merge import apply_annotations

    forms = ["DM_FORM", "AE_FORM", "VS_FORM", "CM_FORM", "LB_FORM"]
    catalog = _catalog([
        {"RSRC_DS": form, "RSRC_VAR": "VISDAT", "TARGET_DS": form[:2], "ATEXT": f"{form[:2]}.{form[:2]}DTC"}
        for form in forms[:4]
    ])
    frame = pd.DataFrame([
        {"PAGE_NO": page, "FORM_OID": form, "DS_OID": form, "VAR_OID": "VISDAT"}
        for page, form in enumerate(forms, start=1)
    ])
    out = apply_annotations(frame, catalog)

    assert len(out) == 5
    assert set(out.loc[out["FORM_OID"] == "DM_FORM", "ATEXT"]) == {"DM.DMDTC"}
    assert set(out.loc[out["FORM_OID"] == "LB_FORM", "ATEXT"]) == {""}


# --- continuation pages -----------------------------------------------------
def test_a_continuation_page_inherits_its_form_only_on_evidence():
    design = build(
        "test",
        forms=[Form(oid="AE_FORM", label="Adverse Events", order=0)],
        fields=[Field(oid=oid, form_oid="AE_FORM", label=label) for oid, label in
                [("AETERM", "Adverse Event Term"), ("AESTDT", "Start Date"),
                 ("AEENDT", "End Date"), ("AESER", "Serious?")]],
    )
    items = _items([
        (1, "Adverse Events", 60, 740, 200, 752),         # names the form
        (1, "Adverse Event Term", 60, 600, 200, 612),
        (2, "Page 2 of 3", 60, 740, 200, 752),            # continuation, no name
        (2, "End Date", 60, 600, 200, 612),
        (2, "Serious?", 60, 560, 200, 572),
        (3, "Study title page", 60, 740, 200, 752),       # cover sheet: no labels
        (3, "Protocol LPL107988", 60, 600, 200, 612),
        (4, "Start Date", 60, 740, 200, 752),             # after a gap: stays unbound
        (4, "End Date", 60, 600, 200, 612),
    ])
    bound = bind_forms(items, design)
    by_page = bound.groupby("PAGE_NO")["FORM_OID"].first().to_dict()
    assert by_page == {1: "AE_FORM", 2: "AE_FORM", 3: "", 4: ""}


def test_a_question_wrapped_over_lines_gets_one_box():
    from crf_annotation.merge import once_per_page

    frame = pd.DataFrame([
        {"PAGE_NO": 1, "DS_OID": "AE", "VAR_OID": "AEREL", "ATEXT": "AE.AEREL", "Y0": 500, "X0": 60},
        {"PAGE_NO": 1, "DS_OID": "AE", "VAR_OID": "AEREL", "ATEXT": "AE.AEREL", "Y0": 488, "X0": 60},
        {"PAGE_NO": 1, "DS_OID": "AE", "VAR_OID": "AEREL", "ATEXT": "AE.AEREL", "Y0": 476, "X0": 60},
        {"PAGE_NO": 2, "DS_OID": "AE", "VAR_OID": "AEREL", "ATEXT": "AE.AEREL", "Y0": 476, "X0": 60},
        {"PAGE_NO": 1, "DS_OID": "AE", "VAR_OID": "AEOUT", "ATEXT": "AE.AEOUT", "Y0": 400, "X0": 60},
    ])
    out = once_per_page(frame)
    assert list(out["ATEXT"]) == ["AE.AEREL", "", "", "AE.AEREL", "AE.AEOUT"]
