# Coverage Recovery v1.01 repair

- Repaired plans and targets: `40`
- Preflight rejected cases: `14`
- Changed seeds: `14`
- Address repairs: `7`
- Duplicate subject/heading repairs: `10`
- Overlapping repairs: `3`
- Unchanged source plans: `26`
- Canonical expansions validated: `280`
- Plans SHA-256: `sha256:e4168a7b3fc3fafbccc55320651fcd90009741c38c2e213ae04ba4aa0ccbf86c`
- Targets SHA-256: `sha256:926dab6a48605fadb06ae52aa3af337cf8d83d3c956068754f64e76318b1fd4c`
- Expanded variants SHA-256: `sha256:1a745ff3afe8785297eca15529bedb750457ed83d24ec028c0cf68993c717a78`
- Repair writer SHA-256: `sha256:a56681da3a90e65040f7eb34e02c97793427c9f5032330bdb1930d65d244abd5`

All fourteen semantic changes are pinned to the supplied blinded preflight mapping. Seven formal records now use `personal_du_capitalized` and retain the distinct `Seit`/`seid` spelling while capitalizing only the direct-address `Ihr`. Ten records retain their subject and remove only its duplicate heading block; three records receive both repairs. Every SST, plain-text, Markdown, and HTML field was rendered from the repaired canonical AST. The deterministic rebuild validates all 40 plans and targets, then all 280 seven-variant canonical expansions.

## Changed seed scope

- Address: covrecovery_1020201, covrecovery_1020205, covrecovery_1020213, covrecovery_1020217, covrecovery_1020225, covrecovery_1020229, covrecovery_1020237
- Heading: covrecovery_1020201, covrecovery_1020205, covrecovery_1020210, covrecovery_1020211, covrecovery_1020216, covrecovery_1020220, covrecovery_1020225, covrecovery_1020226, covrecovery_1020231, covrecovery_1020235
- Both: covrecovery_1020201, covrecovery_1020205, covrecovery_1020225
