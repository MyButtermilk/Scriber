"""Build the technical-project semantic-plan seed batch locally and deterministically.

The banks below are authored synthetic fixtures.  This generator creates plans
only: it has no target text, split assignment, critic result, network access,
or dependency on Scriber production records.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "contracts" / "seed_plan_schema.json"
PROMPT_PATH = Path(
    r"C:\Users\Alexander.Immler\.codex\attachments\13c9b34b-811e-460d-b77c-a6349df08521\pasted-text-1.txt"
)
COUNT = 300
FIRST_SEED = 400000
AGENT = "generator_technical_project_01"
BATCH = "technical_project_batch_01"

# Fictional people and organizations are deliberately unlike project data.
ORGANIZATIONS = [
    "Fjordbyte Systems GmbH",
    "Mosaikring Software KG",
    "Asterlink Platform AG",
    "Kieselwerk Cloud eG",
    "Nordfaden Digital GmbH",
    "Lumenquell Automation KG",
    "Wellenkern Labs GmbH",
    "Birkenorbit Services AG",
    "Quarzpfad Integration KG",
    "Sternbogen Engineering GmbH",
    "Vektortau Runtime eG",
    "Hafenmatrix IT KG",
    "Tallicht Interface GmbH",
    "Juniperkamm Solutions AG",
    "Kometenfaktor DevOps KG",
    "Eichenmodul Analytics GmbH",
    "Polarwerk Delivery eG",
    "Zinnober Signal AG",
]
PEOPLE = [
    "Mara Venn",
    "Jonas Ried",
    "Elif Karo",
    "Timo Lenz",
    "Nina Voigt",
    "Levin Sorn",
    "Kira Mert",
    "Ruben Falk",
    "Alma Dorn",
    "Milan Kest",
    "Sora Veld",
    "Darian Noll",
    "Pia Rohn",
    "Arvid Selm",
    "Jule Kern",
    "Oskar Vey",
    "Mina Raut",
    "Robin Eltz",
]
PRODUCTS = [
    "Orion Relay",
    "Luma Console",
    "Kitebridge API",
    "Mistral Ledger",
    "Vela Runner",
    "Cobalt Mesh",
    "Arbor Gateway",
    "Nova Trace",
    "Silex Portal",
    "Cirrus Agent",
    "Tern Cache",
    "Auron Scheduler",
    "Helix Vault",
    "Pico Monitor",
    "Raven Queue",
]
RELEASES = ["v2.4.0", "v3.1.7", "v4.0.2-rc.3", "v1.9.5", "v5.2.1", "v2.8.0-beta.2"]
TICKETS = ["ARC-184", "OPS-731", "REL-492", "QAT-208", "INT-644", "SEC-317", "DEL-905", "SUP-286"]
PATHS = [
    "services/relay/src/router.ts",
    "infra/mesh/terraform/main.tf",
    "apps/console/config/release.yaml",
    "packages/ledger/src/reconcile.py",
    "deploy/charts/orion/values.yaml",
    "docs/runbooks/cache-failover.md",
    "workers/signal/src/consumer.rs",
    "tests/contracts/gateway.spec.ts",
]
URLS = [
    "https://status.example.invalid/orion",
    "https://docs.example.invalid/relay/v2",
    "https://tracker.example.invalid/ARC-184",
    "https://vendor.example.invalid/api/changelog",
]
TECH_TERMS = [
    "API gateway",
    "canary deployment",
    "rollback window",
    "feature flag",
    "rate limit",
    "schema migration",
    "idempotency key",
    "load test",
    "observability dashboard",
    "CI pipeline",
    "service-level objective",
    "dependency lockfile",
    "webhook signature",
    "zero-downtime deployment",
]
UNITS = [
    ("250 ms", "Latenzbudget"),
    ("99,95 %", "Verfügbarkeitsziel"),
    ("4 GiB", "Speichergrenze"),
    ("12 vCPU", "Rechenkapazität"),
    ("40 req/s", "Durchsatzlimit"),
    ("15 min", "Rollback-Fenster"),
    ("2,5 TB", "Archivvolumen"),
    ("768 MiB", "Container-Limit"),
    ("3 s", "Timeout"),
    ("48 h", "Aufbewahrungsdauer"),
    ("25 °C", "Betriebstemperatur"),
    ("10 Mbit/s", "Bandbreite"),
]

# Family definitions vary facts, communicative purpose, and document structure;
# they are not a single sentence with nouns substituted.
FAMILIES = [
    ("release_readiness", "Release-Freigabe", "Release-Entscheidung", "ordered"),
    ("incident_followup", "Incident-Nachbereitung", "Ursachen und Nachverfolgung", "unordered"),
    ("requirement_note", "Anforderungsnotiz", "Fachliche Anforderungen", "none"),
    ("project_status", "Projektstatusbericht", "Fortschritt und Risiken", "none"),
    ("architecture_decision", "Architekturentscheidung", "Entscheidungsgrundlage", "ordered"),
    ("delivery_plan", "Lieferplan", "Abhängigkeiten und Termine", "nested"),
    ("test_report", "Testbericht", "Validierungsergebnis", "unordered"),
    ("vendor_coordination", "Lieferantenabstimmung", "Externe Schnittstelle", "ordered"),
    ("security_review", "Sicherheitsprüfung", "Schutzmaßnahme", "none"),
    ("migration_brief", "Migrationshinweis", "Datenübernahme", "ordered"),
    ("operations_runbook", "Betriebshinweis", "Wiederherstellung", "nested"),
    ("change_request", "Änderungsantrag", "Umfang und Freigabe", "none"),
]
ADDRESS_MODES = ["formal_sie", "neutral_third_person", "mixed_correspondence", "ambiguous", "quoted_speech"]
CONDITION_PATTERNS = [
    "wenn die fiktive Abnahme für {ticket} dokumentiert ist",
    "sofern die Prüfsumme der Paketdatei bestätigt wurde",
    "falls der canary deployment keine Fehlerklasse auslöst",
    "unter der Voraussetzung, dass die schema migration rückwärtskompatibel bleibt",
]
AMBIGUOUS_NOTES = [
    "Die Notiz „Version 4“ bezeichnet nur einen Gesprächsstand und darf nicht zu {release} ergänzt werden.",
    "Der Ausdruck „mehr Speicher“ enthält ohne Einheit keinen normierbaren technischen Wert.",
    "Die Angabe „später deployen“ legt weder Datum noch Uhrzeit fest und bleibt unverändert.",
    "Die Zeichenfolge „ARC Punkt 184“ ist ohne Diktierkontext kein sicherer Ersatz für {ticket}.",
]


def protected_fact(
    order: int,
    text: str,
    values: list[str],
    *,
    polarity: str = "positive",
    modality: str = "fact",
    condition: str | None = None,
) -> dict[str, Any]:
    return {
        "fact_id": f"f{order}",
        "order": order,
        "text": text,
        "polarity": polarity,
        "modality": modality,
        "condition": condition,
        "protected_values": values,
    }


def entity(kind: str, value: str) -> dict[str, Any]:
    return {"type": kind, "value": value, "fictional": True, "protected": True}


def make_plan(index: int, prompt_hash: str) -> dict[str, Any]:
    seed = FIRST_SEED + index
    family, document_type, topic, default_list = FAMILIES[index % len(FAMILIES)]
    org = ORGANIZATIONS[(index * 7 + 1) % len(ORGANIZATIONS)]
    partner = ORGANIZATIONS[(index * 11 + 4) % len(ORGANIZATIONS)]
    owner = PEOPLE[(index * 5 + 2) % len(PEOPLE)]
    product = PRODUCTS[(index * 13 + 3) % len(PRODUCTS)]
    release = RELEASES[(index * 3) % len(RELEASES)]
    ticket = TICKETS[(index * 5) % len(TICKETS)]
    path = PATHS[(index * 7) % len(PATHS)]
    url = URLS[(index * 11) % len(URLS)]
    term = TECH_TERMS[(index * 17) % len(TECH_TERMS)]
    unit, unit_name = UNITS[(index * 19) % len(UNITS)]
    facts = [
        protected_fact(
            1,
            f"Die fiktive {org} dokumentiert für {product} {release} den Vorgang {ticket} gemeinsam mit der fiktiven {partner}.",
            [org, product, release, ticket, partner],
        ),
    ]
    if family == "release_readiness":
        facts += [
            protected_fact(
                2, f"Der Release-Kandidat verwendet {path}; die referenzierte Anleitung liegt unter {url}.", [path, url]
            ),
            protected_fact(
                3,
                f"Vor der Freigabe wird das {term} gegen das Latenzbudget von {unit} geprüft.",
                [term, unit],
                modality="obligation",
            ),
        ]
    elif family == "incident_followup":
        facts += [
            protected_fact(
                2,
                f"Die fiktive Ansprechperson {owner} ordnet das beobachtete Verhalten dem Modul {path} zu.",
                [owner, path],
            ),
            protected_fact(
                3,
                f"Die Nachverfolgung misst {unit} als {unit_name}; der Wert ist keine Ursachebehauptung.",
                [unit, unit_name],
                modality="uncertainty",
            ),
        ]
    elif family == "requirement_note":
        facts += [
            protected_fact(
                2,
                f"Die Anforderung für {product} verlangt eine {term}-Prüfung im Pfad {path}.",
                [product, term, path],
                modality="obligation",
            ),
            protected_fact(3, f"Die Schnittstellenbeschreibung bleibt über {url} versioniert erreichbar.", [url]),
        ]
    elif family == "project_status":
        facts += [
            protected_fact(
                2,
                f"{owner} berichtet zum Vorgang {ticket} über den Fortschritt des {term}.",
                [owner, ticket, term],
                modality="intention",
            ),
            protected_fact(3, f"Für die nächste Etappe sind {unit} als {unit_name} eingeplant.", [unit, unit_name]),
        ]
    elif family == "architecture_decision":
        facts += [
            protected_fact(
                2,
                f"Die fiktive {org} empfiehlt {product} als Grenze vor {path}, nicht als Ersatz für den {term}.",
                [org, product, path, term],
                polarity="negative",
                modality="recommendation",
            ),
            protected_fact(3, f"Die Entscheidung referenziert {release} und {url} unverändert.", [release, url]),
        ]
    elif family == "delivery_plan":
        facts += [
            protected_fact(
                2,
                f"Die Lieferung von {product} beginnt mit der Prüfung von {path} und endet mit {ticket}.",
                [product, path, ticket],
                modality="intention",
            ),
            protected_fact(
                3, f"Als technische Grenze gelten {unit} für {unit_name} im {term}.", [unit, unit_name, term]
            ),
        ]
    elif family == "test_report":
        facts += [
            protected_fact(
                2, f"Der load test für {product} lief mit dem Artefakt {release} aus {path}.", [product, release, path]
            ),
            protected_fact(3, f"Der Bericht verlinkt die reproduzierbare Prüfbeschreibung unter {url}.", [url]),
        ]
    elif family == "vendor_coordination":
        facts += [
            protected_fact(
                2,
                f"Die fiktive {partner} bestätigt den Integrationspunkt {term} für {ticket}.",
                [partner, term, ticket],
            ),
            protected_fact(
                3,
                f"Die technische Rückmeldung muss den Grenzwert {unit} als {unit_name} nennen.",
                [unit, unit_name],
                modality="obligation",
            ),
        ]
    elif family == "security_review":
        facts += [
            protected_fact(
                2, f"Die Prüfung schützt die webhook signature in {path} und verweist auf {url}.", [path, url]
            ),
            protected_fact(
                3,
                f"{owner} kann die Wirksamkeit nicht bestätigen, bevor {ticket} geschlossen ist.",
                [owner, ticket],
                polarity="negative",
                modality="uncertainty",
            ),
        ]
    elif family == "migration_brief":
        facts += [
            protected_fact(
                2,
                f"Die schema migration für {product} verwendet {path} als dokumentierten Ausführungspunkt.",
                [product, path],
            ),
            protected_fact(3, f"Das Migrationsfenster beträgt {unit}; die Referenz ist {release}.", [unit, release]),
        ]
    elif family == "operations_runbook":
        facts += [
            protected_fact(
                2,
                f"Das Runbook für {product} nennt {path} vor dem {term} und verlinkt {url}.",
                [product, path, term, url],
            ),
            protected_fact(
                3,
                f"Die Wiederherstellung darf das Limit von {unit} für {unit_name} nicht überschreiten.",
                [unit, unit_name],
                polarity="negative",
                modality="obligation",
            ),
        ]
    else:
        facts += [
            protected_fact(
                2,
                f"Der Änderungsantrag {ticket} betrifft {product} in {release} und die Datei {path}.",
                [ticket, product, release, path],
            ),
            protected_fact(
                3,
                f"Die fiktive {org} schlägt {term} mit einem Zielwert von {unit} vor.",
                [org, term, unit],
                modality="recommendation",
            ),
        ]
    guarded = index % 3 != 1
    if guarded:
        condition = CONDITION_PATTERNS[(index // 3) % len(CONDITION_PATTERNS)].format(ticket=ticket)
        polarity = "negative" if index % 6 == 0 else "positive"
        sentence = (
            f"Die Produktion darf nicht aktualisiert werden, {condition}."
            if polarity == "negative"
            else f"Die nächste Stufe kann gestartet werden, {condition}."
        )
        facts.append(
            protected_fact(
                len(facts) + 1,
                sentence,
                [],
                polarity=polarity,
                modality="obligation" if polarity == "negative" else "possibility",
                condition=condition,
            )
        )
    if index % 4 == 0:
        facts.append(
            protected_fact(
                len(facts) + 1,
                AMBIGUOUS_NOTES[(index // 4) % len(AMBIGUOUS_NOTES)].format(release=release, ticket=ticket),
                [],
                modality="uncertainty",
            )
        )
    list_kind = default_list if index % 5 else "none"  # prose cases explicitly remain prose.
    list_items = (
        []
        if list_kind == "none"
        else [
            f"Prüfung von {path}",
            f"Abgleich mit {ticket}",
            f"Dokumentation für {product}",
        ]
    )
    if list_kind == "nested":
        list_items.extend([f"Unterpunkt: Grenzwert {unit}", f"Unterpunkt: Referenz {url}"])
    mode = ADDRESS_MODES[(index * 3) % len(ADDRESS_MODES)]
    has_salutation = mode in {"formal_sie", "mixed_correspondence"}
    has_closing = has_salutation and index % 7 != 0
    tags = [
        family,
        "fictional_technical_entities",
        "protected_version",
        "protected_code_path",
        "protected_identifier",
        "protected_url",
        "protected_technical_unit",
    ]
    if guarded:
        tags.append("negation_condition_modality")
    if index % 4 == 0:
        tags.append("technical_ambiguity_hard_negative")
    if list_kind != "none":
        tags.append("justified_technical_list")
    return {
        "schema_version": 1,
        "seed_id": f"technical_project_{seed}",
        "generator_agent": AGENT,
        "batch_id": BATCH,
        "prompt_hash": prompt_hash,
        "seed": seed,
        "language": "mixed" if index % 2 == 0 else "de-DE",
        "domain": "technical_project",
        "document_type": document_type,
        "style_profile": "duden_preferred_business",
        "address_mode": mode,
        "legal_citation_style": "long",
        "unit_style": "professional_symbols",
        "semantic_plan": {
            "facts": facts,
            "entities": [
                entity("organization", org),
                entity("organization", partner),
                entity("person", owner),
                entity("product", product),
                entity("technical_term", release),
                entity("technical_term", ticket),
                entity("technical_term", path),
                entity("technical_term", url),
                entity("technical_term", term),
            ],
            "structure": {
                "has_subject": index % 6 != 0,
                "has_salutation": has_salutation,
                "paragraph_topics": [topic, "Technische Referenzen", "Nächster Schritt"],
                "heading_levels": ["h1"] if index % 8 == 0 else (["h2"] if index % 11 == 0 else []),
                "list_kind": list_kind,
                "list_items": list_items,
                "has_closing": has_closing,
                "signature_lines": [owner, "Fiktive technische Projektleitung"] if has_closing else [],
            },
        },
        "risk_tags": sorted(tags),
        "source_class": "ai_generated",
        "private_data": False,
    }


def main() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    prompt_hash = "sha256:" + hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest()
    plans = [make_plan(index, prompt_hash) for index in range(COUNT)]
    for plan in plans:
        errors = sorted(validator.iter_errors(plan), key=lambda e: list(e.path))
        if errors:
            raise ValueError(f"{plan['seed_id']}: {errors[0].message}")
    if len({p["seed"] for p in plans}) != COUNT or len({p["seed_id"] for p in plans}) != COUNT:
        raise ValueError("seed uniqueness check failed")
    if any(p["prompt_hash"] != prompt_hash for p in plans):
        raise ValueError("prompt hash mismatch")
    technical_english = sum(p["language"] == "mixed" for p in plans)
    guarded = sum(
        any(
            f["polarity"] != "positive" or f["condition"] is not None or f["modality"] != "fact"
            for f in p["semantic_plan"]["facts"]
        )
        for p in plans
    )
    if technical_english < COUNT * 0.25 or guarded < COUNT * 0.20:
        raise ValueError("coverage threshold failed")
    jsonl = "".join(json.dumps(p, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for p in plans)
    plans_path = OUT / "plans.jsonl"
    plans_path.write_text(jsonl, encoding="utf-8", newline="\n")
    manifest = {
        "manifest_version": 1,
        "generator_agent": AGENT,
        "batch_id": BATCH,
        "domain": "technical_project",
        "record_count": COUNT,
        "seed_range": [FIRST_SEED, FIRST_SEED + COUNT - 1],
        "prompt_hash": prompt_hash,
        "plans_sha256": hashlib.sha256(plans_path.read_bytes()).hexdigest(),
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "schema_sha256": hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest(),
        "schema": "contracts/seed_plan_schema.json",
        "schema_validation": {
            "validator": "jsonschema.Draft202012Validator",
            "valid_records": COUNT,
            "invalid_records": 0,
        },
        "quality_checks": {
            "unique_seed_ids": True,
            "unique_seeds": True,
            "private_data_records": 0,
            "external_generative_api_calls": 0,
            "targets_present": False,
            "critic_verdicts_present": False,
            "splits_present": False,
            "english_technical_german_plans": technical_english,
            "english_technical_german_ratio": technical_english / COUNT,
            "negation_condition_modality_plans": guarded,
            "negation_condition_modality_ratio": guarded / COUNT,
        },
        "diversity": {
            "document_type_counts": dict(sorted(Counter(p["document_type"] for p in plans).items())),
            "scenario_family_counts": dict(sorted(Counter(p["risk_tags"][0] for p in plans).items())),
            "address_mode_counts": dict(sorted(Counter(p["address_mode"] for p in plans).items())),
            "list_kind_counts": dict(
                sorted(Counter(p["semantic_plan"]["structure"]["list_kind"] for p in plans).items())
            ),
            "fact_count_counts": dict(sorted(Counter(len(p["semantic_plan"]["facts"]) for p in plans).items())),
            "unique_fictional_organizations": len(ORGANIZATIONS),
            "unique_fictional_people": len(PEOPLE),
            "unique_products": len(PRODUCTS),
            "technical_ambiguity_hard_negative_plans": sum(
                "technical_ambiguity_hard_negative" in p["risk_tags"] for p in plans
            ),
        },
        "protected_technical_values": {
            "versions": len(RELEASES),
            "product_names": len(PRODUCTS),
            "ticket_ids": len(TICKETS),
            "code_paths": len(PATHS),
            "urls": len(URLS),
            "technical_units": len(UNITS),
            "technical_terms": len(TECH_TERMS),
        },
        "generation": {
            "deterministic": True,
            "local_only": True,
            "external_generative_api_used": False,
            "contains_targets": False,
            "contains_critic_verdicts": False,
            "contains_splits": False,
        },
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(
        json.dumps(
            {"count": COUNT, "plans_sha256": manifest["plans_sha256"], "prompt_hash": prompt_hash}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
