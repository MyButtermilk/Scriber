# Deterministic Orthopron repair-overlay report

- Repaired canonical targets: 300
- Explicit seed rejects: 0
- Canonical expansion gate: 300 targets × 7 variants = 2100 validated variants
- Approved adversarial package SHA-256: `sha256:f94987cd85b0ed98720489e8276d725f795cd02effd1342eabd7a733251f978a`
- Mapped old-v3 adversarial rejects: 845 of 845
- Orthopron reviewed seeds: 196
- Critical repair coverage: omitted_condition: 68, pronoun_referent_error: 141
- Explicit referent repairs: 141 of 141
- Explicit condition repairs: 68 of 68
- Subject/heading repairs: 28 of 28
- Noncritical repair coverage: redundant_subject_heading: 28 across 12 noncritical-only seeds
- Baseline reviewed targets with changed plain-text surface: 196 of 196
- Baseline reviewed targets left unchanged: 0
- Baseline unreviewed targets preserved byte-for-byte at plain-text surface: 104
- Target JSONL SHA-256: `sha256:98aa005cedb566e102661130b1e74736d2d4cf900b70d4c4577a9f25f20cb947`
- Baseline target JSONL SHA-256: `sha256:aca996b916746a11632e46f026e55c62ae19b714e7dff26a9283f20ca58ef421`
- Repair implementation SHA-256: `sha256:0527e4f1df27e64019dbe1d628b0105ff716c75940b81edf6d1ccafe693e00fc`

The overlay preserves the source semantic plans and every protected span. It makes third-person referents explicit, emits each declared omitted condition as a complete requirement, and selects a distinct declared heading topic where the approved review found a duplicate subject/heading. Validation checks AST/SST/AST and all renderer surfaces, protected spans, fact order, exact condition preservation, complete canonical expansion, and the reviewed baseline surface-change proof. The report records no adversarial case IDs or private-index data.

## Reviewed Orthopron seed IDs

- orthopron_800000, orthopron_800001, orthopron_800003, orthopron_800004, orthopron_800005, orthopron_800007, orthopron_800008, orthopron_800009
- orthopron_800010, orthopron_800011, orthopron_800012, orthopron_800013, orthopron_800014, orthopron_800015, orthopron_800016, orthopron_800017
- orthopron_800019, orthopron_800020, orthopron_800021, orthopron_800022, orthopron_800023, orthopron_800024, orthopron_800025, orthopron_800027
- orthopron_800028, orthopron_800029, orthopron_800031, orthopron_800032, orthopron_800033, orthopron_800034, orthopron_800035, orthopron_800036
- orthopron_800037, orthopron_800039, orthopron_800040, orthopron_800041, orthopron_800043, orthopron_800044, orthopron_800045, orthopron_800046
- orthopron_800047, orthopron_800048, orthopron_800049, orthopron_800051, orthopron_800052, orthopron_800053, orthopron_800055, orthopron_800056
- orthopron_800057, orthopron_800058, orthopron_800059, orthopron_800060, orthopron_800061, orthopron_800063, orthopron_800064, orthopron_800065
- orthopron_800067, orthopron_800068, orthopron_800069, orthopron_800070, orthopron_800071, orthopron_800072, orthopron_800073, orthopron_800075
- orthopron_800076, orthopron_800077, orthopron_800079, orthopron_800080, orthopron_800081, orthopron_800082, orthopron_800083, orthopron_800084
- orthopron_800085, orthopron_800087, orthopron_800088, orthopron_800089, orthopron_800091, orthopron_800092, orthopron_800093, orthopron_800094
- orthopron_800095, orthopron_800096, orthopron_800097, orthopron_800098, orthopron_800099, orthopron_800100, orthopron_800101, orthopron_800103
- orthopron_800104, orthopron_800105, orthopron_800106, orthopron_800107, orthopron_800108, orthopron_800109, orthopron_800111, orthopron_800112
- orthopron_800113, orthopron_800115, orthopron_800116, orthopron_800117, orthopron_800118, orthopron_800119, orthopron_800120, orthopron_800121
- orthopron_800123, orthopron_800124, orthopron_800126, orthopron_800128, orthopron_800132, orthopron_800133, orthopron_800134, orthopron_800136
- orthopron_800138, orthopron_800140, orthopron_800144, orthopron_800146, orthopron_800148, orthopron_800150, orthopron_800151, orthopron_800153
- orthopron_800154, orthopron_800155, orthopron_800157, orthopron_800159, orthopron_800160, orthopron_800161, orthopron_800163, orthopron_800165
- orthopron_800169, orthopron_800170, orthopron_800171, orthopron_800175, orthopron_800177, orthopron_800180, orthopron_800181, orthopron_800182
- orthopron_800183, orthopron_800185, orthopron_800187, orthopron_800189, orthopron_800190, orthopron_800193, orthopron_800195, orthopron_800196
- orthopron_800199, orthopron_800200, orthopron_800201, orthopron_800203, orthopron_800205, orthopron_800207, orthopron_800210, orthopron_800211
- orthopron_800213, orthopron_800215, orthopron_800217, orthopron_800219, orthopron_800220, orthopron_800223, orthopron_800224, orthopron_800225
- orthopron_800229, orthopron_800230, orthopron_800231, orthopron_800235, orthopron_800237, orthopron_800238, orthopron_800240, orthopron_800241
- orthopron_800243, orthopron_800245, orthopron_800247, orthopron_800249, orthopron_800250, orthopron_800253, orthopron_800255, orthopron_800259
- orthopron_800260, orthopron_800261, orthopron_800265, orthopron_800266, orthopron_800267, orthopron_800270, orthopron_800271, orthopron_800273
- orthopron_800275, orthopron_800277, orthopron_800279, orthopron_800280, orthopron_800283, orthopron_800285, orthopron_800287, orthopron_800289
- orthopron_800290, orthopron_800291, orthopron_800295, orthopron_800297
