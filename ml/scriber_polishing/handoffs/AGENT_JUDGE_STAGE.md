# Blinded staged pairwise judge

Judge only the anonymous cases in the provided stage packet. Read only this
prompt, that packet and its stage-packet manifest. Do not list directories,
search the repository or open private mappings, source prediction packages,
training artifacts, evaluation summaries, aggregate scores or prior-stage
evidence. Do not infer or request candidate, model, checkpoint or training
identity.

Evaluate every packet row independently. Compare `candidate_a` and
`candidate_b` against `source_text`, `semantic_plan`, `protected_spans`,
`reference_ast`, `reference_text`, the row rubric and the deterministic flags.
Treat a conservative, meaning-preserving result as better than a more elegant
result that adds, omits, reorders or changes content. Protect facts, negations,
conditions, modalities, numbers, amounts, dates, times, units, names,
technical terms, legal references, speaker labels and timestamps before
judging spelling, grammar, punctuation or formatting. Judge swapped A/B cases
independently and do not use an earlier row's position choice.

Select the better candidate as `A` or `B`. If neither is acceptable, select
the less harmful one and set `acceptable` to `false`. Score only the selected
candidate. If both are otherwise tied, prefer the candidate that stays closer
to the source and reference and performs fewer unsupported transformations.
If `candidate_a` and `candidate_b` are exactly identical, select `A` as the
canonical transport choice and assess that shared output normally. This choice
does not represent a winner; the private aggregator records the pair as
equivalent independently of its A/B position.

The selected candidate's deterministic critical flags are binding. If its
`deterministic_critical_flags` list is non-empty, copy every listed flag,
set `critical_error` to `true` and `acceptable` to `false`. Add an applicable
schema flag when you independently find another critical error. When
`critical_error` is `false`, `flags` must be empty. A selected candidate may be
unacceptable without a critical error when accumulated non-critical quality
defects are serious.

Emit exactly one compact canonical JSON object per input row, in the same
order, followed by one LF. Emit no Markdown, prose, headings or code fences.
Every object must contain exactly these fields:

- `schema_version`: integer `2`
- `case_id`: copy from the packet row
- `judge_id`: use the fixed value supplied by the task
- `judge_session_id`: use the fixed value supplied by the task and preserve it
  across every resumed batch of this stage
- `judge_stage`, `model_family`, `reasoning_effort`, `prompt_hash`,
  `reviewed_bundle_sha256`, `stage_assignment_sha256`: copy exactly from the
  stage-packet manifest
- `candidate`: `A` or `B`
- `acceptable`: Boolean
- `critical_error`: Boolean
- `scores`: all 15 rubric dimensions, each as an integer from 1 through 5
- `flags`: unique valid critical flags
- `brief_reason`: concise German reason, 1 to 500 characters, with no identity
  speculation

The required score keys are `semantic_fidelity`, `no_additions`,
`no_omissions`, `spelling`, `grammar`, `punctuation`,
`paragraph_structure`, `heading_structure`, `numbered_lists`,
`disfluency_cleanup`, `unit_normalization`, `legal_citations`,
`address_pronouns`, `formatting` and `style_preservation`.

Valid critical flags are `changed_fact`, `changed_negation`,
`changed_condition`, `changed_modality`, `changed_number`, `changed_amount`,
`changed_percentage`, `changed_date_or_time`, `changed_unit_dimension`,
`changed_name`, `changed_company_name`, `changed_technical_term`,
`changed_case_identifier`, `changed_contact_detail`, `changed_language`,
`changed_speaker_label`, `changed_timestamp`, `changed_legal_reference`,
`changed_legal_hierarchy`, `invented_law_abbreviation`, `omitted_content`,
`added_content`, `answered_question`, `summarized_content`,
`reordered_content`, `damaged_protected_span`, `invalid_sst`,
`invented_heading`, `missed_heading`, `wrong_heading_level`,
`wrong_heading_style`, `false_subject`, `missed_subject`,
`paragraph_oversegmentation`, `paragraph_undersegmentation`,
`missing_blank_line`, `excessive_blank_lines`, `salutation_layout_error`,
`closing_layout_error`, `signature_layout_error`, `wrong_list_type`,
`malformed_numbered_list`, `wrong_list_nesting`,
`formatting_command_leaked`, `formatting_command_misinterpreted`,
`false_unit_normalization`, `wrong_address_pronoun`,
`false_variant_correction`, `output_not_clean_text` and
`unsafe_renderer_output`.
