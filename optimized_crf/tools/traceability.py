"""User requirements for aCRF Gen and the automated tests that verify each.

This is the traceability matrix the OQ report is built from
(`python tools/oq_report.py`). Every requirement must name at least one test,
and every test named must exist and pass for the release to be qualified.

Risk follows GAMP 5: High = wrong output could reach a submission unnoticed;
Medium = wrong output is visible to a reviewer; Low = convenience.
"""

from __future__ import annotations

REQUIREMENTS = [
    # --- inputs --------------------------------------------------------------
    ("URS-01", "Read the study design from CDISC ODM-XML, keeping each field's form, section, label and a unique name",
     "High", ["test_full_run_from_odm_metadata_only", "test_odm_keeps_each_fields_section",
              "test_odm_items_sharing_a_name_stay_separate_fields", "test_odm_form_title_is_its_name_not_its_instructions",
              "test_odm_prefers_the_english_question_text", "test_odm_reads_the_visit_matrix"]),
    ("URS-02", "Read the study design from Medidata Rave ALS and vendor JSON exports",
     "High", ["test_odm_yields_the_same_design_as_rave", "test_profile_reads_an_arbitrary_vendor_json",
              "test_profile_requires_forms_and_fields_sections"]),
    ("URS-03", "Refuse unusable inputs with a clear reason instead of producing a partial aCRF",
     "Medium", ["test_missing_blank_crf_is_an_input_error", "test_unreadable_metadata_fails_with_every_reason",
                "test_no_usable_specs_raises", "test_nothing_to_do_is_explained", "test_a_run_without_inputs_is_refused"]),
    # --- matching CRF text to EDC fields ---------------------------------------
    ("URS-04", "Bind each CRF page to its form; never guess a page's form without evidence",
     "High", ["test_banner_binder_resolves_rave_style_pages", "test_header_binder_works_without_any_banner_prefix",
              "test_a_continuation_page_inherits_its_form_only_on_evidence", "test_unbindable_pages_are_left_empty_not_guessed"]),
    ("URS-05", "Match printed questions to EDC fields exactly, including wrapped and numbered questions",
     "High", ["test_exact_matches_are_labelled_and_left_alone", "test_a_label_wrapped_over_two_lines_matches_exactly",
              "test_a_numbered_question_wrapping_to_an_indented_line_matches_exactly",
              "test_question_numbering_on_the_crf_does_not_stop_a_match", "test_a_number_that_is_part_of_the_question_is_kept",
              "test_a_separator_line_is_not_joined_into_the_label_below_it"]),
    ("URS-06", "Fuzzy and AI matching only choose among the form's own fields, below-threshold matches are rejected",
     "High", ["test_fuzzy_resolves_a_near_miss_label", "test_fuzzy_leaves_unrelated_text_alone",
              "test_ai_tier_rejects_an_invented_field", "test_min_confidence_gate_rejects_weak_matches"]),
    # --- mapping proposals -------------------------------------------------------
    ("URS-07", "Propose SDTM mappings only with variable names SDTMIG defines; re-ask once, never draw an invented name",
     "High", ["test_sdtm_names", "test_an_invented_variable_is_asked_again_and_never_drawn",
              "test_an_invalid_answer_is_reported_unmapped_not_silently_skipped", "test_a_one_letter_test_code_is_valid"]),
    ("URS-08", "Map each field to its own domain, including several domains on one form and fields submitted elsewhere",
     "High", ["test_one_form_with_many_sections_maps_each_field_to_its_own_domain",
              "test_the_model_can_put_a_field_in_another_domain_than_its_form",
              "test_an_identifier_on_any_form_is_written_to_the_dm_spec"]),
    ("URS-09", "Keep each form's annotations to that form (no cross-form bleed from shared items)",
     "High", ["test_strict_scope_keeps_each_form_to_its_own_annotation", "test_auto_scope_stays_strict_when_most_keys_line_up",
              "test_auto_scope_uses_strict_when_the_keys_line_up"]),
    ("URS-10", "Give the same question the same annotation on every form",
     "Medium", ["test_the_same_question_on_two_forms_gets_the_same_code",
                "test_a_field_the_model_skipped_reuses_the_same_question_on_another_form"]),
    ("URS-11", "Mark collected-but-unmapped fields NOT SUBMITTED at the field; never annotate derived variables",
     "High", ["test_unmapped_fields_are_annotated_not_submitted", "test_not_submitted_marks_sit_at_their_field",
              "test_derived_variables_are_not_annotated", "test_collected_variables_are_not_mistaken_for_derived"]),
    ("URS-12", "No mapping is lost between proposal, spec and PDF (names with commas, quotes, single letters)",
     "High", ["test_a_field_named_with_a_comma_keeps_its_annotation", "test_a_field_named_with_quotes_keeps_its_annotation",
              "test_proposals_become_ordinary_spec_files_the_catalog_reads"]),
    ("URS-13", "A failing or unavailable AI model never fails the run; transient failures are retried",
     "Medium", ["test_missing_credentials_degrade_to_deterministic_tiers", "test_ai_tier_survives_a_failed_call",
                "test_a_timed_out_gemini_call_is_retried", "test_a_cut_off_gemini_answer_is_retried",
                "test_a_broken_resolver_does_not_fail_the_run"]),
    # --- layout -----------------------------------------------------------------------
    ("URS-14", "Place each annotation beside its own question: inside the page, not overlapping, never in another column",
     "High", ["test_annotations_stay_inside_the_page", "test_annotations_do_not_overlap_each_other",
              "test_a_box_wraps_rather_than_leaving_its_column", "test_no_room_in_the_column_means_unplaced_not_misplaced",
              "test_a_box_does_not_drift_past_the_next_question", "test_a_box_does_not_slide_into_the_next_column",
              "test_a_question_wrapped_over_lines_gets_one_box"]),
    # --- output -------------------------------------------------------------------------
    ("URS-15", "Produce acrf.pdf meeting the FDA PDF Specifications v4.1, with a rule-by-rule check report",
     "High", ["test_the_acrf_meets_the_fda_pdf_rules_this_tool_controls", "test_output_filename_is_exactly_acrf_pdf",
              "test_form_fields_in_a_fillable_blank_crf_are_flattened", "test_the_fda_pdf_check_is_served_with_the_acrf"]),
    ("URS-16", "Bookmark the aCRF by domain and by visit, with inherit zoom",
     "Medium", ["test_outline_covers_domains_and_visits", "test_bookmarks_use_inherit_zoom"]),
    ("URS-17", "A reviewed workbook re-renders exactly; reviewer edits are honoured on rerun",
     "High", ["test_rerun_reproduces_the_same_annotations", "test_edited_annotation_text_is_honoured_on_rerun",
              "test_workbook_round_trip_is_lossless"]),
    ("URS-18", "Publish outputs atomically; refuse a concurrent run on the same output",
     "Medium", ["test_publishing_is_atomic", "test_a_second_concurrent_run_is_refused"]),
    # --- service, access and audit -------------------------------------------------------
    ("URS-19", "Every service request is authorised for exactly one user's job",
     "High", ["test_every_route_needs_a_valid_ticket_for_that_job"]),
    ("URS-20", "Record every user action in an append-only, tamper-evident audit trail with before/after values",
     "High", ["test_every_user_action_lands_in_the_audit_trail", "test_the_audit_chain_detects_tampering",
              "test_chat_can_remove_add_and_move_annotations_and_each_is_audited"]),
    ("URS-21", "Chat edits (change, remove, add, move) are checked before they are applied; refused edits are reported",
     "High", ["test_chat_can_remove_add_and_move_annotations_and_each_is_audited",
              "test_an_add_that_would_duplicate_is_refused_and_said_so", "test_chat_lists_reviews_without_a_model_and_edits_with_one"]),
]
