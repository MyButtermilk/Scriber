"""Create the deterministic, fully synthetic real-estate/energy seed batch.

This is a semantic-plan generator only: it deliberately emits neither targets,
splits, nor critic verdicts.  All names, properties, references, and figures
are invented fixture content.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[5]
OUT = Path(__file__).resolve().parent
PROMPT = Path(r"C:\Users\Alexander.Immler\.codex\attachments\13c9b34b-811e-460d-b77c-a6349df08521\pasted-text-1.txt")
SCHEMA_PATH = ROOT / "ml" / "scriber_polishing" / "contracts" / "seed_plan_schema.json"
COUNT = 300
FIRST_SEED = 300000
AGENT = "generator_real_estate_energy_01"
BATCH = "real_estate_energy_batch_01"

# These banks were authored for this batch; they are not sampled from customer
# records, listings, cadastral data, or external generative services.
ORGS = [
    "Asterhaus Verwaltung GmbH",
    "Nordlicht Quartier KG",
    "Kiesel & Partner Projektbau GmbH",
    "Fjordfenster Energie eG",
    "Lindenbogen Immobilien GmbH",
    "Kupferhain Baukontor KG",
    "Sonnenwerft Anlagen GmbH",
    "Moorweg Wohnraum eG",
    "Rheinfeld Finanzierung GmbH",
    "Wacholder Objektservice KG",
    "Polarstern Bauplanung GmbH",
    "Birkenspeer Invest GmbH",
]
PEOPLE = [
    "Mara Voss",
    "Jonas Ehlers",
    "Elif Brandt",
    "Timo Kerner",
    "Nina Falk",
    "Levin Reuter",
    "Sora Winter",
    "Milan Groth",
    "Kira Damm",
    "Ruben Ahl",
    "Alma Seifert",
    "Darian Vogt",
]
PROPERTIES = [
    "Quartier Auenkai 7",
    "Hof Lumen 14",
    "Haus Zinnober 3",
    "Parkfeld Nord 22",
    "Gartenhof Mistral 8",
    "Speicher Komet 5",
    "Wohnhof Fasan 11",
    "Baufeld Vela 6",
    "Riegelhaus Aster 19",
    "Hafenhof Silex 4",
    "Atrium Juniper 16",
    "Wiesenbogen 9",
]
LOCATIONS = ["Fiktivstadt-West", "Lindenmark", "Kranichau", "Velaheim", "Morgenfeld", "Sternbrück"]
REFS = ["Vorgang REE-71", "Projektakte NQ-208", "Los BAU-44", "Prüfvermerk EN-63", "Nachtrag NF-19", "Abruf FIN-52"]
UNITS = [
    ("84,5 m²", "Wohnfläche"),
    ("312 m³", "umbauter Raum"),
    ("12,80 €/m²", "Nettokaltmiete"),
    ("18 kW", "Anschlussleistung"),
    ("6.480 kWh", "Jahresverbrauch"),
    ("14,2 MWh", "Wärmemenge"),
    ("76 kWh/(m²·a)", "Endenergiekennwert"),
    ("1,35 €/m²", "Betriebskostenvorauszahlung"),
    ("42 m", "Leitungslänge"),
    ("0,28 m³/h", "Volumenstrom"),
    ("3,5 kWp", "PV-Nennleistung"),
    ("21 °C", "Raumtemperatur"),
    ("2.450 €", "Sicherheitseinbehalt"),
    ("4,15 %", "Sollzinssatz"),
    ("640 kg", "Traglast"),
    ("1.200 l", "Puffervolumen"),
    ("8,5 l/(m²·a)", "Wasseransatz"),
    ("0,19 €/kWh", "Arbeitspreis"),
    ("30 dB(A)", "Schallpegel"),
    ("2,4 m", "lichte Höhe"),
]
SCENARIOS = [
    (
        "lease_notice",
        "Mietvertragsnachtrag",
        "Mietanpassung",
        "Die vereinbarte {unit} bleibt für die Einheit maßgeblich.",
    ),
    (
        "operating_costs",
        "Betriebskostenabrechnung",
        "Betriebskosten",
        "Der Ansatz von {unit} wurde der Kostenposition zugeordnet.",
    ),
    ("defect_report", "Mängelanzeige", "Mangelmeldung", "Für die Prüfung wurde ein Wert von {unit} protokolliert."),
    ("handover_record", "Übergabeprotokoll", "Übergabe", "Im Übergabebereich wurde {unit} festgestellt."),
    ("financing_note", "Finanzierungsvermerk", "Finanzierung", "Die Berechnung verwendet unverändert {unit}."),
    ("construction_release", "Baufreigabe", "Baufortschritt", "Die Freigabe bezieht sich auf {unit}."),
    ("energy_review", "Energiebericht", "Energieverbrauch", "Der geprüfte Kennwert beträgt {unit}."),
    ("investment_memo", "Investitionsfreigabe", "Investition", "Die Entscheidung berücksichtigt {unit}."),
    ("maintenance_order", "Wartungsauftrag", "Anlagentechnik", "Der technische Sollwert ist {unit}."),
    ("tenant_information", "Mieterinformation", "Nutzungshinweis", "Für die Nutzung gilt der Wert {unit}."),
    ("construction_change", "Nachtragsmeldung", "Nachtrag", "Der Nachtrag betrifft {unit}."),
    ("energy_procurement", "Energiebeschaffung", "Energievertrag", "Die Preisannahme lautet {unit}."),
]
HARD_NEGATIVES = [
    "Die Angabe „acht Komma fünf“ ist ohne Bezugsgröße absichtlich nicht zu normalisieren.",
    "Der Vermerk „pro Jahr“ bleibt ohne Zahlenwert eine unvollständige Einheit.",
    "Die Notiz „18 K“ kann Kelvin oder eine interne Kennung bedeuten und bleibt unverändert.",
    "Der Eintrag „m zwei“ ist ohne eindeutigen Diktierkontext kein sicherer Ersatz für m².",
    "Die Aussage „etwa ein Drittel“ darf nicht in einen Dezimalwert umgerechnet werden.",
    "Die Schreibweise „0,19 je Einheit“ enthält kein bestimmtes Einheitenzeichen.",
]


def entity(kind: str, value: str) -> dict[str, Any]:
    return {"type": kind, "value": value, "fictional": True, "protected": True}


def fact(
    number: int,
    text: str,
    *,
    polarity: str = "positive",
    modality: str = "fact",
    condition: str | None = None,
    protected: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "fact_id": f"f{number}",
        "order": number,
        "text": text,
        "polarity": polarity,
        "modality": modality,
        "condition": condition,
        "protected_values": protected or [],
    }


def plan(index: int, prompt_hash: str) -> dict[str, Any]:
    seed = FIRST_SEED + index
    org, person, prop, location, reference = (
        ORGS[index % len(ORGS)],
        PEOPLE[(index * 5) % len(PEOPLE)],
        PROPERTIES[(index * 7) % len(PROPERTIES)],
        LOCATIONS[(index * 3) % len(LOCATIONS)],
        REFS[(index * 11) % len(REFS)],
    )
    unit, unit_label = UNITS[(index * 13) % len(UNITS)]
    family, doc_type, topic, unit_sentence = SCENARIOS[index % len(SCENARIOS)]
    condition = None
    polarity, modality = "positive", "fact"
    if index % 5 == 0:
        condition = "wenn die fiktive Freigabe in der Projektakte dokumentiert ist"
        modality = "obligation"
    elif index % 7 == 0:
        polarity, modality = "negative", "uncertainty"
    elif index % 9 == 0:
        modality = "recommendation"
    facts = [
        fact(
            1,
            f"Die fiktive Organisation {org} bearbeitet für die fiktive Liegenschaft {prop} in {location} den fiktiven {reference}.",
            protected=[org, prop, location, reference],
        ),
        fact(2, unit_sentence.format(unit=unit), protected=[unit]),
    ]
    if polarity == "negative":
        facts.append(
            fact(
                3,
                f"Die fiktive Ansprechperson {person} kann nicht bestätigen, dass der Wert ohne weitere Prüfung übernommen werden darf.",
                polarity="negative",
                modality=modality,
                condition="solange der fiktive Prüfvermerk nicht vorliegt",
                protected=[person],
            )
        )
    elif condition:
        facts.append(
            fact(
                3,
                f"Die fiktive Ansprechperson {person} muss die Maßnahme nur umsetzen, wenn die fiktive Freigabe in der Projektakte dokumentiert ist.",
                modality=modality,
                condition=condition,
                protected=[person],
            )
        )
    else:
        facts.append(
            fact(
                3,
                f"Die fiktive Ansprechperson {person} empfiehlt eine Rückmeldung zur Position {unit_label} bis zum 18.09.2031.",
                modality=modality,
                protected=[person, "18.09.2031"],
            )
        )
    hard = index % 4 == 0
    if hard:
        warning = HARD_NEGATIVES[(index // 4) % len(HARD_NEGATIVES)]
        facts.append(fact(4, warning, modality="uncertainty", protected=[]))
    list_kind = ["none", "unordered", "ordered", "nested"][index % 4]
    items = [] if list_kind == "none" else [f"Fiktive Prüfung von {unit_label}", f"Zuordnung zu {reference}"]
    structure = {
        "has_subject": index % 6 != 0,
        "has_salutation": index % 3 != 0,
        "paragraph_topics": [topic, unit_label, "Rückmeldung"],
        "heading_levels": ["h1"] if index % 8 == 0 else (["h2"] if index % 11 == 0 else []),
        "list_kind": list_kind,
        "list_items": items,
        "has_closing": index % 3 != 1,
        "signature_lines": [person, "Fiktives Objektteam"] if index % 3 != 1 else [],
    }
    tags = [family, "fictional_entity", "protected_quantity", "protected_unit"]
    if condition or polarity == "negative" or modality != "fact":
        tags.append("condition_modality_negation")
    if hard:
        tags.append("ambiguous_unit_hard_negative")
    return {
        "schema_version": 1,
        "seed_id": f"realestate_energy_{seed}",
        "generator_agent": AGENT,
        "batch_id": BATCH,
        "prompt_hash": prompt_hash,
        "seed": seed,
        "language": "de-DE",
        "domain": "real_estate_energy",
        "document_type": doc_type,
        "style_profile": "duden_preferred_business",
        "address_mode": ["formal_sie", "neutral_third_person", "ambiguous", "mixed_correspondence"][index % 4],
        "legal_citation_style": "long",
        "unit_style": "professional_symbols",
        "semantic_plan": {
            "facts": facts,
            "entities": [
                entity("organization", org),
                entity("person", person),
                entity("location", prop),
                entity("location", location),
                entity("product", reference),
                entity("technical_term", unit_label),
            ],
            "structure": structure,
        },
        "risk_tags": sorted(set(tags)),
        "source_class": "ai_generated",
        "private_data": False,
    }


def main() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    prompt_hash = "sha256:" + hashlib.sha256(PROMPT.read_bytes()).hexdigest()
    records = [plan(index, prompt_hash) for index in range(COUNT)]
    for record in records:
        errors = sorted(validator.iter_errors(record), key=lambda error: list(error.path))
        if errors:
            raise ValueError(f"{record['seed_id']}: {errors[0].message}")
    if len({record["seed"] for record in records}) != COUNT or len({record["seed_id"] for record in records}) != COUNT:
        raise ValueError("seed or seed_id uniqueness failed")
    lines = [json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for record in records]
    plans_path = OUT / "plans.jsonl"
    plans_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    unit_count = sum(
        any(
            any(
                symbol in value
                for symbol in ("m²", "m³", "€", "kW", "kWh", "MWh", "kWp", "°C", "dB", "%", "kg", " l", " m")
            )
            for value in item["protected_values"]
        )
        for record in records
        for item in record["semantic_plan"]["facts"]
    )
    conditioned = sum(
        any(
            item["condition"] is not None or item["polarity"] != "positive" or item["modality"] != "fact"
            for item in record["semantic_plan"]["facts"]
        )
        for record in records
    )
    manifest = {
        "schema_version": 1,
        "generator_agent": AGENT,
        "batch_id": BATCH,
        "source_class": "ai_generated",
        "private_data": False,
        "count": COUNT,
        "record_count": COUNT,
        "seed_range": [FIRST_SEED, FIRST_SEED + COUNT - 1],
        "prompt_hash": prompt_hash,
        "plans_sha256": hashlib.sha256(plans_path.read_bytes()).hexdigest(),
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "schema_sha256": hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest(),
        "schema_validation": {
            "validator": "jsonschema.Draft202012Validator",
            "valid_records": COUNT,
            "invalid_records": 0,
        },
        "diversity": {
            "document_types": len({r["document_type"] for r in records}),
            "scenario_families": len(SCENARIOS),
            "unique_fictional_organizations": len(set(ORGS)),
            "unique_fictional_people": len(set(PEOPLE)),
            "unique_fictional_properties": len(set(PROPERTIES)),
            "hard_negative_plans": sum("ambiguous_unit_hard_negative" in r["risk_tags"] for r in records),
            "condition_negation_modality_plans": conditioned,
        },
        "unit_coverage": {
            "plans_with_quantity_unit": unit_count,
            "ratio": unit_count / COUNT,
            "distinct_unit_expressions": len(UNITS),
            "required_minimum_ratio": 0.45,
            "condition_modality_negation_ratio": conditioned / COUNT,
            "required_condition_modality_negation_ratio": 0.20,
        },
        "deterministic": True,
        "external_generative_api_used": False,
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(
        json.dumps(
            {
                "count": COUNT,
                "plans_sha256": manifest["plans_sha256"],
                "unit_ratio": manifest["unit_coverage"]["ratio"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
