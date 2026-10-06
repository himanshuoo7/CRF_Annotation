# Operational Qualification report - aCRF Gen 1.0.0

- **Release:** commit `4470353` (uncommitted changes present)
- **Executed:** 2026-10-06 04:05 UTC
- **Environment:** Python 3.13.2 on Darwin arm64
- **Result:** NOT QUALIFIED - 21/21 requirements met; 137/137 automated tests passed

## Component versions

| Component | Version |
|---|---|
| pymupdf | 1.28.2 |
| pymupdf-fonts | 1.0.5 |
| pikepdf | 10.16.0 |
| pypdf | 6.19.0 |
| pandas | 3.0.6 |
| openpyxl | 3.1.5 |
| google-genai | 2.28.0 |
| anthropic | 1.11.0 |

## Requirements and evidence

| ID | Requirement | Risk | Result | Evidence (automated tests) |
|---|---|---|---|---|
| URS-01 | Read the study design from CDISC ODM-XML, keeping each field's form, section, label and a unique name | High | Met | ✅ `test_full_run_from_odm_metadata_only`<br>✅ `test_odm_keeps_each_fields_section`<br>✅ `test_odm_items_sharing_a_name_stay_separate_fields`<br>✅ `test_odm_form_title_is_its_name_not_its_instructions`<br>✅ `test_odm_prefers_the_english_question_text`<br>✅ `test_odm_reads_the_visit_matrix` |
| URS-02 | Read the study design from Medidata Rave ALS and vendor JSON exports | High | Met | ✅ `test_odm_yields_the_same_design_as_rave`<br>✅ `test_profile_reads_an_arbitrary_vendor_json`<br>✅ `test_profile_requires_forms_and_fields_sections` |
| URS-03 | Refuse unusable inputs with a clear reason instead of producing a partial aCRF | Medium | Met | ✅ `test_missing_blank_crf_is_an_input_error`<br>✅ `test_unreadable_metadata_fails_with_every_reason`<br>✅ `test_no_usable_specs_raises`<br>✅ `test_nothing_to_do_is_explained`<br>✅ `test_a_run_without_inputs_is_refused` |
| URS-04 | Bind each CRF page to its form; never guess a page's form without evidence | High | Met | ✅ `test_banner_binder_resolves_rave_style_pages`<br>✅ `test_header_binder_works_without_any_banner_prefix`<br>✅ `test_a_continuation_page_inherits_its_form_only_on_evidence`<br>✅ `test_unbindable_pages_are_left_empty_not_guessed` |
| URS-05 | Match printed questions to EDC fields exactly, including wrapped and numbered questions | High | Met | ✅ `test_exact_matches_are_labelled_and_left_alone`<br>✅ `test_a_label_wrapped_over_two_lines_matches_exactly`<br>✅ `test_a_numbered_question_wrapping_to_an_indented_line_matches_exactly`<br>✅ `test_question_numbering_on_the_crf_does_not_stop_a_match`<br>✅ `test_a_number_that_is_part_of_the_question_is_kept`<br>✅ `test_a_separator_line_is_not_joined_into_the_label_below_it` |
| URS-06 | Fuzzy and AI matching only choose among the form's own fields, below-threshold matches are rejected | High | Met | ✅ `test_fuzzy_resolves_a_near_miss_label`<br>✅ `test_fuzzy_leaves_unrelated_text_alone`<br>✅ `test_ai_tier_rejects_an_invented_field`<br>✅ `test_min_confidence_gate_rejects_weak_matches` |
| URS-07 | Propose SDTM mappings only with variable names SDTMIG defines; re-ask once, never draw an invented name | High | Met | ✅ `test_sdtm_names`<br>✅ `test_an_invented_variable_is_asked_again_and_never_drawn`<br>✅ `test_an_invalid_answer_is_reported_unmapped_not_silently_skipped`<br>✅ `test_a_one_letter_test_code_is_valid` |
| URS-08 | Map each field to its own domain, including several domains on one form and fields submitted elsewhere | High | Met | ✅ `test_one_form_with_many_sections_maps_each_field_to_its_own_domain`<br>✅ `test_the_model_can_put_a_field_in_another_domain_than_its_form`<br>✅ `test_an_identifier_on_any_form_is_written_to_the_dm_spec` |
| URS-09 | Keep each form's annotations to that form (no cross-form bleed from shared items) | High | Met | ✅ `test_strict_scope_keeps_each_form_to_its_own_annotation`<br>✅ `test_auto_scope_stays_strict_when_most_keys_line_up`<br>✅ `test_auto_scope_uses_strict_when_the_keys_line_up` |
| URS-10 | Give the same question the same annotation on every form | Medium | Met | ✅ `test_the_same_question_on_two_forms_gets_the_same_code`<br>✅ `test_a_field_the_model_skipped_reuses_the_same_question_on_another_form` |
| URS-11 | Mark collected-but-unmapped fields NOT SUBMITTED at the field; never annotate derived variables | High | Met | ✅ `test_unmapped_fields_are_annotated_not_submitted`<br>✅ `test_not_submitted_marks_sit_at_their_field`<br>✅ `test_derived_variables_are_not_annotated`<br>✅ `test_collected_variables_are_not_mistaken_for_derived` |
| URS-12 | No mapping is lost between proposal, spec and PDF (names with commas, quotes, single letters) | High | Met | ✅ `test_a_field_named_with_a_comma_keeps_its_annotation`<br>✅ `test_a_field_named_with_quotes_keeps_its_annotation`<br>✅ `test_proposals_become_ordinary_spec_files_the_catalog_reads` |
| URS-13 | A failing or unavailable AI model never fails the run; transient failures are retried | Medium | Met | ✅ `test_missing_credentials_degrade_to_deterministic_tiers`<br>✅ `test_ai_tier_survives_a_failed_call`<br>✅ `test_a_timed_out_gemini_call_is_retried`<br>✅ `test_a_cut_off_gemini_answer_is_retried`<br>✅ `test_a_broken_resolver_does_not_fail_the_run` |
| URS-14 | Place each annotation beside its own question: inside the page, not overlapping, never in another column | High | Met | ✅ `test_annotations_stay_inside_the_page`<br>✅ `test_annotations_do_not_overlap_each_other`<br>✅ `test_a_box_wraps_rather_than_leaving_its_column`<br>✅ `test_no_room_in_the_column_means_unplaced_not_misplaced`<br>✅ `test_a_box_does_not_drift_past_the_next_question`<br>✅ `test_a_box_does_not_slide_into_the_next_column`<br>✅ `test_a_question_wrapped_over_lines_gets_one_box` |
| URS-15 | Produce acrf.pdf meeting the FDA PDF Specifications v4.1, with a rule-by-rule check report | High | Met | ✅ `test_the_acrf_meets_the_fda_pdf_rules_this_tool_controls`<br>✅ `test_output_filename_is_exactly_acrf_pdf`<br>✅ `test_form_fields_in_a_fillable_blank_crf_are_flattened`<br>✅ `test_the_fda_pdf_check_is_served_with_the_acrf` |
| URS-16 | Bookmark the aCRF by domain and by visit, with inherit zoom | Medium | Met | ✅ `test_outline_covers_domains_and_visits`<br>✅ `test_bookmarks_use_inherit_zoom` |
| URS-17 | A reviewed workbook re-renders exactly; reviewer edits are honoured on rerun | High | Met | ✅ `test_rerun_reproduces_the_same_annotations`<br>✅ `test_edited_annotation_text_is_honoured_on_rerun`<br>✅ `test_workbook_round_trip_is_lossless` |
| URS-18 | Publish outputs atomically; refuse a concurrent run on the same output | Medium | Met | ✅ `test_publishing_is_atomic`<br>✅ `test_a_second_concurrent_run_is_refused` |
| URS-19 | Every service request is authorised for exactly one user's job | High | Met | ✅ `test_every_route_needs_a_valid_ticket_for_that_job` |
| URS-20 | Record every user action in an append-only, tamper-evident audit trail with before/after values | High | Met | ✅ `test_every_user_action_lands_in_the_audit_trail`<br>✅ `test_the_audit_chain_detects_tampering`<br>✅ `test_chat_can_remove_add_and_move_annotations_and_each_is_audited` |
| URS-21 | Chat edits (change, remove, add, move) are checked before they are applied; refused edits are reported | High | Met | ✅ `test_chat_can_remove_add_and_move_annotations_and_each_is_audited`<br>✅ `test_an_add_that_would_duplicate_is_refused_and_said_so`<br>✅ `test_chat_lists_reviews_without_a_model_and_edits_with_one` |

## Scope

This report covers operational qualification of the software release against its user requirements. Installation qualification of a deployment and performance qualification on the customer's own CRFs are described in docs/governance/validation-plan.md. aCRF Gen output is a draft for qualified review.
