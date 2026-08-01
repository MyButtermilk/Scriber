"""Deterministic, local-only generator for the general-business semantic plans.

This module deliberately produces semantic plans, never transcripts or target
renderings.  All names, organizations, products and locations below are
invented for this dataset.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "contracts" / "seed_plan_schema.json"
PROMPT_PATH = Path(
    r"C:\Users\Alexander.Immler\.codex\attachments\13c9b34b-811e-460d-b77c-a6349df08521\pasted-text-1.txt"
)
AGENT = "generator_general_business_01"
BATCH = "general_business_batch_01"

# The banks encode business situations rather than merely swapping a noun in a
# fixed sentence.  Their combinations determine the document purpose, facts,
# pragmatic force, structure, and risk coverage independently.
ORGANIZATIONS = [
    "Asterwerk Solutions GmbH",
    "Nordfeder Handel KG",
    "Kiesel & Partner OHG",
    "Lumenpfad Services GmbH",
    "Birkenfeld Logistik AG",
    "Morgenrot Bürobedarf KG",
    "Quarzbrücke Digital GmbH",
    "Falkenau Industriebedarf GmbH",
    "Wellenform Technik KG",
    "Eichenstern Beratung GmbH",
    "Silberhain Projekte AG",
    "Kometenhof Verpackung KG",
    "Sonnenkai Vertrieb GmbH",
    "Talblick Manufaktur OHG",
    "Mosaikraum Planung GmbH",
    "Hafenlicht Handelshaus KG",
    "Juniper Datenservice GmbH",
    "Rosenwerk Montage AG",
    "Kranichweg Laborbedarf GmbH",
    "Violettkern Medien KG",
    "Blauquell Systemhaus GmbH",
    "Fichtenring Objektservice GmbH",
    "Neonfokus Kommunikation KG",
    "Waldkante Prozessberatung GmbH",
]
PEOPLE = [
    "Mara Feld",
    "Jonas Rehm",
    "Elin Sommer",
    "Tobias Kern",
    "Nora Wendt",
    "Levin Brandt",
    "Kaya Winter",
    "Milan Ahrens",
    "Sina Vogt",
    "Robin Hecht",
    "Tessa Fink",
    "Arvid Lenz",
    "Jule Hart",
    "Nico Bering",
    "Lina Dorn",
    "Pia Kernig",
    "Oskar Seif",
    "Mina Faber",
]
LOCATIONS = ["Fiktivstadt", "Nordtal", "Kieselau", "Hafenbogen", "Lindenquell", "Auenhof"]
PRODUCTS = [
    "Projektpaket Atlas",
    "Servicepaket Linden",
    "Modul Komet",
    "Arbeitsplatzset Quarz",
    "Wartungsplan Falken",
    "Lieferlos Birke",
    "Analysepaket Mosaik",
    "Portalzugang Juniper",
]
DOCS = [
    "formal_letter",
    "business_email",
    "offer",
    "invoice_notice",
    "payment_reminder",
    "deadline_notice",
    "appointment_confirmation",
    "supplier_request",
    "customer_update",
    "contract_coordination",
    "complaint_response",
    "handover_note",
    "budget_update",
    "project_status",
    "banking_instruction",
]
PURPOSES = {
    "formal_letter": ("Schriftliche Abstimmung", "die weitere Zusammenarbeit"),
    "business_email": ("Kurze Projektabstimmung", "den nächsten Arbeitsschritt"),
    "offer": ("Angebot und Leistungsumfang", "die Angebotsfreigabe"),
    "invoice_notice": ("Rechnungsinformation", "die Zuordnung der Rechnung"),
    "payment_reminder": ("Zahlungserinnerung", "den Zahlungseingang"),
    "deadline_notice": ("Fristabstimmung", "die fristgerechte Rückmeldung"),
    "appointment_confirmation": ("Terminbestätigung", "die Vorbereitung des Termins"),
    "supplier_request": ("Lieferantenanfrage", "die Lieferfähigkeit"),
    "customer_update": ("Kundeninformation", "den transparenten Projektstand"),
    "contract_coordination": ("Vertragsabstimmung", "die gemeinsame Vertragsfassung"),
    "complaint_response": ("Reklamationsbearbeitung", "die nachvollziehbare Klärung"),
    "handover_note": ("Übergabedokumentation", "die geordnete Übergabe"),
    "budget_update": ("Budgetmeldung", "die Budgetfreigabe"),
    "project_status": ("Projektstatus", "die priorisierte Umsetzung"),
    "banking_instruction": ("Bankverbindung und Zahlungsweg", "die korrekte Überweisung"),
}
ADDRESS_MODES = [
    "formal_sie",
    "personal_du_capitalized",
    "personal_du_lowercase",
    "neutral_third_person",
    "mixed_correspondence",
    "ambiguous",
]
LIST_KINDS = ["none", "ordered", "unordered", "nested"]
DATES = ["14.08.2026", "21.08.2026", "02.09.2026", "15.09.2026", "30.09.2026", "08.10.2026"]
TIMES = ["09:30 Uhr", "11:00 Uhr", "14:15 Uhr", "16:45 Uhr"]
AMOUNTS = ["1.280,00 €", "3.750,00 €", "8.460,00 €", "12.900,00 €", "24.500,00 €"]
PERCENTAGES = ["5 %", "10 %", "15 %", "20 %"]


def protected_fact(
    order: int,
    text: str,
    values: list[str],
    *,
    polarity: str = "positive",
    modality: str = "fact",
    condition: str | None = None,
) -> dict:
    return {
        "fact_id": f"f{order}",
        "order": order,
        "text": text,
        "polarity": polarity,
        "modality": modality,
        "condition": condition,
        "protected_values": values,
    }


def make_plan(index: int, prompt_hash: str) -> dict:
    seed = 100000 + index
    org = ORGANIZATIONS[(index * 7) % len(ORGANIZATIONS)]
    partner = ORGANIZATIONS[(index * 11 + 3) % len(ORGANIZATIONS)]
    person = PEOPLE[(index * 5) % len(PEOPLE)]
    location = LOCATIONS[(index * 3) % len(LOCATIONS)]
    product = PRODUCTS[(index * 13) % len(PRODUCTS)]
    doc = DOCS[index % len(DOCS)]
    subject, objective = PURPOSES[doc]
    date, time, amount, percentage = (
        DATES[index % len(DATES)],
        TIMES[index % len(TIMES)],
        AMOUNTS[index % len(AMOUNTS)],
        PERCENTAGES[index % len(PERCENTAGES)],
    )
    mode, list_kind = ADDRESS_MODES[(index // 3) % len(ADDRESS_MODES)], LIST_KINDS[(index // 5) % len(LIST_KINDS)]
    facts = [
        protected_fact(
            1,
            f"Die fiktive {org} stimmt sich mit der fiktiven {partner} zu {subject} für {product} ab.",
            [org, partner, product],
        ),
        protected_fact(
            2,
            f"{person} koordiniert die Rückmeldung am Standort {location} in der vereinbarten Reihenfolge.",
            [person, location],
            modality="intention",
        ),
    ]
    numeric_case = index % 2 == 0
    guarded_case = index % 3 == 0
    if numeric_case:
        value_kind = index % 4
        if value_kind == 0:
            text, values = f"Die Rückmeldung ist bis zum {date} erforderlich.", [date]
        elif value_kind == 1:
            text, values = f"Der bestätigte Betrag beträgt {amount} und ist dem Vorgang zuzuordnen.", [amount]
        elif value_kind == 2:
            text, values = f"Der Termin beginnt am {date} um {time}.", [date, time]
        else:
            text, values = f"Der vereinbarte Nachlass beträgt {percentage} für {product}.", [percentage, product]
        facts.append(protected_fact(3, text, values))
    if guarded_case:
        condition = f"wenn die fiktive {partner} die Unterlagen vollständig bestätigt"
        polarity = "negative" if index % 6 == 0 else "positive"
        text = (
            f"Die Freigabe darf nicht erfolgen, {condition}."
            if polarity == "negative"
            else f"Die nächste Lieferung kann erfolgen, {condition}."
        )
        facts.append(
            protected_fact(
                len(facts) + 1,
                text,
                [partner],
                polarity=polarity,
                modality="obligation" if polarity == "negative" else "possibility",
                condition=condition,
            )
        )
    if index % 5 == 0:
        facts.append(
            protected_fact(
                len(facts) + 1,
                f"Eine Änderung des Umfangs wird nur nach schriftlicher Bestätigung von {org} berücksichtigt.",
                [org],
                modality="recommendation",
                condition="nach schriftlicher Bestätigung",
            )
        )
    if index % 7 == 0:
        facts.append(
            protected_fact(
                len(facts) + 1,
                f"Es besteht noch keine verbindliche Zusage für den Folgetermin mit {partner}.",
                [partner],
                polarity="negative",
                modality="uncertainty",
            )
        )
    list_items = (
        []
        if list_kind == "none"
        else [f"Rückmeldung von {partner}", f"Prüfung für {product}", f"Dokumentation durch {person}"]
    )
    paragraphs = [subject, objective]
    if numeric_case:
        paragraphs.append("Geschützte Werte")
    if guarded_case:
        paragraphs.append("Bedingung und Freigabe")
    has_subject = doc not in {"handover_note", "project_status"} or index % 4 != 0
    has_salutation = mode in {"formal_sie", "personal_du_capitalized", "personal_du_lowercase", "mixed_correspondence"}
    has_closing = has_salutation and index % 11 != 0
    signature = [person, "Fiktive Projektkoordination"] if has_closing else []
    risks = ["fictional_entities", "business_context"]
    if numeric_case:
        risks.append("protected_numeric_value")
    if guarded_case:
        risks.append("negation_condition_modality")
    if mode == "formal_sie":
        risks.append("formal_address")
    if list_kind != "none":
        risks.append("justified_list")
    return {
        "schema_version": 1,
        "seed_id": f"general_business_{seed}",
        "generator_agent": AGENT,
        "batch_id": BATCH,
        "prompt_hash": prompt_hash,
        "seed": seed,
        "language": "de-DE",
        "domain": "general_business",
        "document_type": doc,
        "style_profile": "duden_preferred_business",
        "address_mode": mode,
        "legal_citation_style": "long",
        "unit_style": "professional_symbols",
        "semantic_plan": {
            "facts": facts,
            "entities": [
                {"type": "organization", "value": org, "fictional": True, "protected": True},
                {"type": "organization", "value": partner, "fictional": True, "protected": True},
                {"type": "person", "value": person, "fictional": True, "protected": True},
                {"type": "location", "value": location, "fictional": True, "protected": True},
                {"type": "product", "value": product, "fictional": True, "protected": True},
            ],
            "structure": {
                "has_subject": has_subject,
                "has_salutation": has_salutation,
                "paragraph_topics": paragraphs,
                "heading_levels": ["h1"] if doc in {"budget_update", "project_status", "handover_note"} else [],
                "list_kind": list_kind,
                "list_items": list_items,
                "has_closing": has_closing,
                "signature_lines": signature,
            },
        },
        "risk_tags": risks,
        "source_class": "ai_generated",
        "private_data": False,
    }


def main() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    prompt_hash = "sha256:" + hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest()
    plans = [make_plan(i, prompt_hash) for i in range(300)]
    errors = [(plan["seed_id"], list(validator.iter_errors(plan))) for plan in plans]
    failures = [(sid, err) for sid, err in errors if err]
    if failures:
        raise ValueError(f"schema validation failed: {failures[0]}")
    if len({p["seed_id"] for p in plans}) != 300 or len({p["seed"] for p in plans}) != 300:
        raise ValueError("seed identifiers are not unique")
    if any(p["prompt_hash"] != prompt_hash for p in plans):
        raise ValueError("prompt hash mismatch")
    if sum("protected_numeric_value" in p["risk_tags"] for p in plans) < 60:
        raise ValueError("numeric coverage below 20 percent")
    if sum("negation_condition_modality" in p["risk_tags"] for p in plans) < 60:
        raise ValueError("condition/modality coverage below 20 percent")
    jsonl = "".join(json.dumps(p, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for p in plans)
    (OUT / "plans.jsonl").write_text(jsonl, encoding="utf-8", newline="\n")
    counts = {key: dict(sorted(Counter(p[key] for p in plans).items())) for key in ("document_type", "address_mode")}
    manifest = {
        "manifest_version": 1,
        "generator_agent": AGENT,
        "batch_id": BATCH,
        "domain": "general_business",
        "record_count": len(plans),
        "seed_range": [100000, 100299],
        "prompt_hash": prompt_hash,
        "plans_sha256": hashlib.sha256(jsonl.encode("utf-8")).hexdigest(),
        "schema": "contracts/seed_plan_schema.json",
        "schema_validation": {
            "validator": "jsonschema.Draft202012Validator",
            "valid_records": len(plans),
            "invalid_records": 0,
        },
        "quality_checks": {
            "unique_seed_ids": True,
            "unique_seeds": True,
            "private_data_records": 0,
            "external_api_calls": 0,
            "protected_numeric_coverage": sum("protected_numeric_value" in p["risk_tags"] for p in plans) / len(plans),
            "negation_condition_modality_tag_coverage": sum(
                "negation_condition_modality" in p["risk_tags"] for p in plans
            )
            / len(plans),
            "negation_or_condition_or_nonfactual_coverage": sum(
                any(
                    f["polarity"] != "positive" or f["condition"] is not None or f["modality"] != "fact"
                    for f in p["semantic_plan"]["facts"]
                )
                for p in plans
            )
            / len(plans),
        },
        "diversity": {
            "document_type_counts": counts["document_type"],
            "address_mode_counts": counts["address_mode"],
            "list_kind_counts": dict(
                sorted(Counter(p["semantic_plan"]["structure"]["list_kind"] for p in plans).items())
            ),
            "fact_count_counts": dict(sorted(Counter(len(p["semantic_plan"]["facts"]) for p in plans).items())),
            "unique_organizations": len(ORGANIZATIONS),
            "unique_people": len(PEOPLE),
            "unique_locations": len(LOCATIONS),
            "unique_products": len(PRODUCTS),
        },
        "generation": {
            "deterministic": True,
            "local_only": True,
            "contains_targets": False,
            "contains_critic_verdicts": False,
            "contains_splits": False,
        },
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


if __name__ == "__main__":
    main()
