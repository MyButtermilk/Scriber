# Coverage Recovery 02 – Target Writer Report

- Writer-ID: `target_writer_coverage_recovery_02`
- Writer-ID-SHA-256: `sha256:0fa5be4fdb8cbc9c1be4228c92f8c22281441e6c8ec5b08099cc14f6993ffb2a`
- Target-Prompt-Hash: `sha256:e35e642f6478bfe5434b1a2b38fe72e38324d313e96d12ba9d2cca2d482a3ac0`
- Quelle: `generator_coverage_recovery_02` / `coverage_recovery_batch_02`
- Targets: `40`
- `targets.jsonl`-SHA-256: `sha256:1683bb22502f2f1186f2c43850dcd0ff4c047db2c870ff1a607249ec2b9d35c0`
- `manifest.json`-SHA-256: `sha256:15e362f6bd606f1eabebf9b6ff70fb5e34698a03f339cdccd3e701cdc25a78ea`
- `target_writer.py`-SHA-256: `sha256:a3fd50ed5505cb5085b06604a56d53f4807937b87b2ebac886dbff4651109cb4`
- Doppelte deterministische Erzeugung: bestanden; die JSONL-Bytes sind identisch.
- `validate_canonical_record`: 40/40 bestanden.
- Öffentlicher Batch-Validator: bestanden, isoliert gegen ausschließlich dieses Paket.
- Canonical Expander: `7/7` Varianten je Target, `280` Varianten insgesamt, doppelt deterministisch validiert.
- Faktenreihenfolge, Negation, Modalität, Bedingungen, geschützte Werte und `48 €/m²`: erhalten.
- Plain, Markdown und HTML werden ausschließlich aus dem AST gerendert; HTML verwendet `&nbsp;` für die zentrale Einheiten-Schreibweise.
- Externe oder Modell-API-Aufrufe: 0. Menschliche Prüfung: nein.
- Hinweis: `contracts/canonical_example_schema.json` war nicht vorhanden; maßgeblich waren `contracts/sst_v1_schema.json` und `validators.validate_canonical_record`.

## Exakte Eingabe-Hashes

  - `data/seeds/generator_coverage_recovery_02/plans.jsonl`: `sha256:bce116acc85a075bcc135755b5fff24999c13328e015fc4287ea168357631c20`
  - `data/seeds/generator_coverage_recovery_02/manifest.json`: `sha256:364926fd4999e957423fa53da976aa5eb8c678d932c2260f69f29ecd67950c5d`
  - `contracts/seed_plan_schema.json`: `sha256:ae59eafa61ab6b501b5b09bbccae03b11161cb5850f012050c9e93713507cef3`
  - `contracts/sst_v1_schema.json`: `sha256:2eb50bed34137833ebde9ed78e2708329c2a5a909e0066d0b248af140aa49ab9`
  - `contracts/behavioral_contract.yaml`: `sha256:c416dcd0617529e754c734baeb73ca63718ac2a8fa12f0cc8014e9fa7fe498fe`
  - `contracts/de_business_style.yaml`: `sha256:02f4ee40467c3643ec2447d17c5a44dec54f9fc55495b1c15d52a84162917dfd`
