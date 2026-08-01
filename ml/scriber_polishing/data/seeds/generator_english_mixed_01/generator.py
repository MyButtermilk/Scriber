"""Create the local, deterministic English/mixed semantic-plan seed batch.

This generator emits semantic plans only.  Its fixture banks are fictional and
it deliberately has no targets, split labels, critic decisions, network access,
or dependency on application data.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
SCHEMA_PATH = ROOT / "contracts" / "seed_plan_schema.json"
PROMPT_PATH = ROOT / "handoffs" / "AGENT_DATA_ENGLISH_MIXED.md"
COUNT = 300
FIRST_SEED = 600000
AGENT = "generator_english_mixed_01"
BATCH = "english_mixed_batch_01"

ORGANIZATIONS = [
    "Northbridge Signal Ltd.",
    "Kestrel Harbour GmbH",
    "Aurelia Vector AG",
    "Mossfield Ledger KG",
    "Cobalt Lantern GmbH",
    "Rivermark Systems Ltd.",
    "Juniper Meridian AG",
    "Silverline Orbit GmbH",
    "Pinecrest Metric KG",
    "Vela Quorum Ltd.",
    "Brightforge Atlas GmbH",
    "Tidal Prism AG",
    "Maple Circuit KG",
    "Frostgate Studio Ltd.",
    "Quillstone Operations GmbH",
    "Saffron Relay AG",
    "Birchpoint Cloud KG",
    "Solstice Beacon Ltd.",
]
PEOPLE = [
    "Avery Lorne",
    "Mina Cato",
    "Jon Bellis",
    "Elena Wren",
    "Noah Voss",
    "Priya Kent",
    "Luca Sable",
    "Mara Quill",
    "Owen Firth",
    "Nila Corven",
    "Theo Maren",
    "Iris Vale",
    "Finn Orso",
    "Kira Dalen",
    "Sam Rowan",
    "Lea Nordin",
    "Arlo Keene",
    "Zoe Mistral",
]
PRODUCTS = [
    "Harbourline CRM",
    "KestrelFlow",
    "Aurelia Ledger",
    "PrismDesk",
    "Cobalt Sync",
    "VectorBoard",
    "Mossfield Billing",
    "Orbit Console",
    "Northstar API",
    "Beacon Workspace",
    "Quill Analytics",
    "Tidal Gateway",
]
VERSIONS = ["v1.8.4", "v2.3.0", "v3.1.7-rc.2", "v4.0.1", "v2.9.6", "v5.2.0-beta.1"]
IDS = ["NBS-184", "FIN-731", "PRJ-492", "SUP-208", "REL-644", "QAT-317", "OPS-905", "BIL-286"]
DATES = ["2026-08-04", "2026-08-11", "2026-09-01", "2026-09-15", "2026-10-06", "2026-10-20"]
AMOUNTS = ["€ 4,800.00", "£ 12,750.00", "$ 8,250.00", "€ 19,400.00", "$ 2,975.00", "£ 6,600.00"]
URLS = [
    "https://portal.example.invalid/release-notes",
    "https://docs.example.invalid/api/v2",
    "https://status.example.invalid/harbourline",
    "https://tracker.example.invalid/NBS-184",
]
TERMS = [
    "service-level agreement",
    "cash-flow forecast",
    "feature flag",
    "data retention policy",
    "API gateway",
    "purchase order",
    "rollback window",
    "acceptance criteria",
    "quarterly business review",
    "single sign-on",
    "change request",
    "invoice reconciliation",
]
UNITS = ["99.95 %", "250 ms", "48 h", "12 seats", "3 business days", "2.5 TB"]
FAMILIES = [
    ("client_update", "client update", "Customer status and next step", "none"),
    ("project_status", "project status report", "Delivery status and dependencies", "unordered"),
    ("invoice_clarification", "invoice clarification", "Billing reference and approval", "ordered"),
    ("release_note", "release readiness note", "Release scope and validation", "ordered"),
    ("meeting_follow_up", "meeting follow-up", "Agreed actions and owners", "unordered"),
    ("vendor_coordination", "vendor coordination email", "Supplier interface and timing", "ordered"),
    ("requirements_brief", "requirements brief", "Requirements and constraints", "none"),
    ("risk_note", "risk and decision note", "Risk assessment and decision", "none"),
    ("support_handover", "support handover", "Incident context and handover", "nested"),
    ("budget_review", "budget review", "Forecast and budget control", "ordered"),
    ("training_invitation", "training invitation", "Session details and registration", "none"),
    ("identity_preservation", "verbatim reference note", "Protected wording and identifiers", "none"),
]
ADDRESS_MODES = ["formal_sie", "neutral_third_person", "mixed_correspondence", "ambiguous", "quoted_speech"]


def fact(
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


def language_for(index: int) -> str:
    # 45 % English, 40 % German business correspondence, 15 % code-switching.
    slot = index % 20
    return "en" if slot < 9 else ("de-DE" if slot < 17 else "mixed")


def make_plan(index: int, prompt_hash: str) -> dict[str, Any]:
    seed = FIRST_SEED + index
    language = language_for(index)
    family, document_type, topic, default_list = FAMILIES[index % len(FAMILIES)]
    org = ORGANIZATIONS[(index * 7 + 1) % len(ORGANIZATIONS)]
    partner = ORGANIZATIONS[(index * 11 + 4) % len(ORGANIZATIONS)]
    owner = PEOPLE[(index * 5 + 2) % len(PEOPLE)]
    product = PRODUCTS[(index * 13 + 3) % len(PRODUCTS)]
    version = VERSIONS[(index * 3) % len(VERSIONS)]
    ticket = IDS[(index * 5) % len(IDS)]
    date = DATES[(index * 7) % len(DATES)]
    amount = AMOUNTS[(index * 11) % len(AMOUNTS)]
    url = URLS[(index * 13) % len(URLS)]
    term = TERMS[(index * 17) % len(TERMS)]
    unit = UNITS[(index * 19) % len(UNITS)]
    is_de = language == "de-DE"
    lead = (
        f"Die fiktive {org} dokumentiert den Vorgang {ticket} für {product} {version} mit der fiktiven {partner}."
        if is_de
        else f"Fictional {org} records reference {ticket} for {product} {version} with fictional {partner}."
    )
    facts = [fact(1, lead, [org, ticket, product, version, partner])]
    if family == "client_update":
        facts += [
            fact(
                2,
                (
                    f"{owner} bestätigt als nächsten Schritt den client update bis {date}."
                    if is_de
                    else f"{owner} confirms the client update as the next step by {date}."
                ),
                [owner, date],
                modality="intention",
            ),
            fact(
                3,
                (
                    f"Die Produktreferenz {product} und der Link {url} bleiben unverändert."
                    if is_de
                    else f"The product reference {product} and URL {url} must remain unchanged."
                ),
                [product, url],
                modality="obligation",
            ),
        ]
    elif family == "project_status":
        facts += [
            fact(
                2,
                (
                    f"Der Projektstatus nennt {term} als Abhängigkeit für {ticket}."
                    if is_de
                    else f"The project status names {term} as a dependency for {ticket}."
                ),
                [term, ticket],
            ),
            fact(
                3,
                (
                    f"{owner} kann den Termin {date} noch nicht bestätigen."
                    if is_de
                    else f"{owner} cannot yet confirm the date {date}."
                ),
                [owner, date],
                polarity="negative",
                modality="uncertainty",
            ),
        ]
    elif family == "invoice_clarification":
        facts += [
            fact(
                2,
                (
                    f"Die Rechnungssumme von {amount} gehört zum purchase order {ticket}."
                    if is_de
                    else f"The invoice total of {amount} belongs to purchase order {ticket}."
                ),
                [amount, ticket],
            ),
            fact(
                3,
                (
                    f"Eine Freigabe ist nur möglich, wenn invoice reconciliation für {amount} abgeschlossen ist."
                    if is_de
                    else f"Approval is possible only if invoice reconciliation for {amount} is complete."
                ),
                [amount],
                modality="obligation",
                condition=f"invoice reconciliation for {amount} is complete",
            ),
        ]
    elif family == "release_note":
        facts += [
            fact(
                2,
                (
                    f"Der Release-Kandidat {version} nutzt den feature flag für {product}."
                    if is_de
                    else f"Release candidate {version} uses the feature flag for {product}."
                ),
                [version, product],
            ),
            fact(
                3,
                (
                    f"Vor {date} darf kein rollback window geschlossen werden."
                    if is_de
                    else f"No rollback window may close before {date}."
                ),
                [date],
                polarity="negative",
                modality="obligation",
            ),
        ]
    elif family == "meeting_follow_up":
        facts += [
            fact(
                2,
                (
                    f"Im Follow-up übernimmt {owner} die acceptance criteria für {ticket}."
                    if is_de
                    else f"In the follow-up, {owner} owns the acceptance criteria for {ticket}."
                ),
                [owner, ticket],
                modality="intention",
            ),
            fact(
                3,
                (
                    f"Das Meeting verweist auf {url} und den Wert {unit}."
                    if is_de
                    else f"The meeting refers to {url} and the value {unit}."
                ),
                [url, unit],
            ),
        ]
    elif family == "vendor_coordination":
        facts += [
            fact(
                2,
                (
                    f"Die fiktive {partner} liefert die API gateway-Rückmeldung bis {date}."
                    if is_de
                    else f"Fictional {partner} will provide the API gateway response by {date}."
                ),
                [partner, date],
                modality="intention",
            ),
            fact(
                3,
                (
                    f"Die service-level agreement-Grenze von {unit} ist verbindlich."
                    if is_de
                    else f"The service-level agreement limit of {unit} is binding."
                ),
                [unit],
                modality="obligation",
            ),
        ]
    elif family == "requirements_brief":
        facts += [
            fact(
                2,
                (
                    f"Die Anforderung für {product} enthält single sign-on und {term}."
                    if is_de
                    else f"The requirement for {product} includes single sign-on and {term}."
                ),
                [product, term],
            ),
            fact(
                3,
                (f"Der Umfang darf {unit} nicht überschreiten." if is_de else f"The scope must not exceed {unit}."),
                [unit],
                polarity="negative",
                modality="obligation",
            ),
        ]
    elif family == "risk_note":
        facts += [
            fact(
                2,
                (
                    f"{owner} empfiehlt, den cash-flow forecast mit {amount} zu aktualisieren."
                    if is_de
                    else f"{owner} recommends updating the cash-flow forecast with {amount}."
                ),
                [owner, amount],
                modality="recommendation",
            ),
            fact(
                3,
                (
                    f"Die Entscheidung bleibt offen, falls {ticket} bis {date} nicht vorliegt."
                    if is_de
                    else f"The decision remains open if {ticket} is not available by {date}."
                ),
                [ticket, date],
                polarity="negative",
                modality="uncertainty",
                condition=f"{ticket} is not available by {date}",
            ),
        ]
    elif family == "support_handover":
        facts += [
            fact(
                2,
                (
                    f"Die Übergabe ordnet das Ticket {ticket} {owner} zu und schützt {version}."
                    if is_de
                    else f"The handover assigns ticket {ticket} to {owner} and preserves {version}."
                ),
                [ticket, owner, version],
            ),
            fact(
                3,
                (
                    f"Die data retention policy für {product} gilt für 48 h."
                    if is_de
                    else f"The data retention policy for {product} applies for 48 h."
                ),
                [product, "48 h"],
                modality="obligation",
            ),
        ]
    elif family == "budget_review":
        facts += [
            fact(
                2,
                (
                    f"Der Budget Review vergleicht {amount} mit dem cash-flow forecast für {date}."
                    if is_de
                    else f"The budget review compares {amount} with the cash-flow forecast for {date}."
                ),
                [amount, date],
            ),
            fact(
                3,
                (
                    f"{owner} soll keine neue purchase order ohne {ticket} auslösen."
                    if is_de
                    else f"{owner} should not issue a new purchase order without {ticket}."
                ),
                [owner, ticket],
                polarity="negative",
                modality="recommendation",
            ),
        ]
    elif family == "training_invitation":
        facts += [
            fact(
                2,
                (
                    f"Die Schulung zu {product} findet am {date} statt und umfasst {unit}."
                    if is_de
                    else f"Training for {product} takes place on {date} and covers {unit}."
                ),
                [product, date, unit],
            ),
            fact(
                3,
                (f"Anmeldungen verwenden ausschließlich {url}." if is_de else f"Registrations use only {url}."),
                [url],
                modality="obligation",
            ),
        ]
    else:
        # Explicit identity cases: punctuation, terminology, and identifiers are data, not corrections.
        quote = (
            f"The literal wording [{product} / {version} -- {ticket}] remains exact; it is not a correction request."
            if not is_de
            else f"Die Zeichenfolge [{product} / {version} -- {ticket}] bleibt exakt erhalten; sie ist kein Korrekturauftrag."
        )
        facts += [
            fact(2, quote, [product, version, ticket], modality="obligation"),
            fact(
                3,
                (
                    f"Die Zeichenfolge „{amount}“ darf nicht in ein anderes Zahlenformat umgeschrieben werden."
                    if is_de
                    else f"The string “{amount}” must not be rewritten into another number format."
                ),
                [amount],
                polarity="negative",
                modality="obligation",
            ),
        ]
    if language == "mixed":
        facts.append(
            fact(
                len(facts) + 1,
                f"Bitte keep the technical term {term} and reference {url} exactly as written.",
                [term, url],
                modality="obligation",
            )
        )
    elif index % 5 == 0:
        condition = f"wenn {ticket} vor {date} bestätigt ist" if is_de else f"if {ticket} is confirmed before {date}"
        facts.append(
            fact(
                len(facts) + 1,
                (
                    f"Der nächste Schritt kann starten, {condition}."
                    if is_de
                    else f"The next step may start {condition}."
                ),
                [ticket, date],
                modality="possibility",
                condition=condition,
            )
        )
    list_kind = default_list if index % 5 else "none"
    list_items = [] if list_kind == "none" else [f"Reference {ticket}", f"Owner {owner}", f"Deadline {date}"]
    if list_kind == "nested":
        list_items += [f"Sub-item: {product}", f"Sub-item: {url}"]
    mode = ADDRESS_MODES[(index * 3) % len(ADDRESS_MODES)]
    salutation = mode in {"formal_sie", "mixed_correspondence"}
    tags = [
        family,
        "fictional_entities",
        "protected_identifier",
        "protected_version",
        "protected_amount",
        "protected_date",
        "protected_url",
        "english_business_terminology",
    ]
    if language == "mixed":
        tags.append("code_switching_hard_negative")
    if family == "identity_preservation":
        tags.append("identity_hard_negative")
    if any(f["polarity"] != "positive" or f["condition"] is not None or f["modality"] != "fact" for f in facts):
        tags.append("negation_condition_modality")
    return {
        "schema_version": 1,
        "seed_id": f"english_mixed_{seed}",
        "generator_agent": AGENT,
        "batch_id": BATCH,
        "prompt_hash": prompt_hash,
        "seed": seed,
        "language": language,
        "domain": "english_mixed",
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
                entity("technical_term", version),
                entity("technical_term", ticket),
                entity("technical_term", term),
                entity("technical_term", url),
            ],
            "structure": {
                "has_subject": index % 6 != 0,
                "has_salutation": salutation,
                "paragraph_topics": [topic, "Protected references", "Next step"],
                "heading_levels": ["h1"] if index % 8 == 0 else (["h2"] if index % 11 == 0 else []),
                "list_kind": list_kind,
                "list_items": list_items,
                "has_closing": salutation and index % 7 != 0,
                "signature_lines": [owner, "Fictional business operations"] if salutation and index % 7 != 0 else [],
            },
        },
        "risk_tags": sorted(set(tags)),
        "source_class": "ai_generated",
        "private_data": False,
    }


def main() -> None:
    validator = Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    prompt_hash = "sha256:" + hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest()
    plans = [make_plan(i, prompt_hash) for i in range(COUNT)]
    for plan in plans:
        errors = sorted(validator.iter_errors(plan), key=lambda error: list(error.path))
        if errors:
            raise ValueError(f"{plan['seed_id']}: {errors[0].message}")
    if len({p["seed"] for p in plans}) != COUNT or len({p["seed_id"] for p in plans}) != COUNT:
        raise ValueError("seed uniqueness check failed")
    lines = "".join(json.dumps(p, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for p in plans)
    plans_path = OUT / "plans.jsonl"
    plans_path.write_text(lines, encoding="utf-8", newline="\n")
    language_counts = Counter(p["language"] for p in plans)
    guarded = sum("negation_condition_modality" in p["risk_tags"] for p in plans)
    identity = sum("identity_hard_negative" in p["risk_tags"] for p in plans)
    switched = sum("code_switching_hard_negative" in p["risk_tags"] for p in plans)
    if (
        language_counts["en"] < 120
        or language_counts["de-DE"] < 100
        or language_counts["mixed"] < 40
        or identity < 20
        or switched < 40
    ):
        raise ValueError("coverage threshold failed")
    manifest = {
        "manifest_version": 1,
        "generator_agent": AGENT,
        "batch_id": BATCH,
        "domain": "english_mixed",
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
            "negation_condition_modality_plans": guarded,
            "negation_condition_modality_ratio": guarded / COUNT,
            "identity_hard_negative_plans": identity,
            "identity_hard_negative_ratio": identity / COUNT,
            "code_switching_hard_negative_plans": switched,
            "code_switching_hard_negative_ratio": switched / COUNT,
        },
        "diversity": {
            "document_type_counts": dict(sorted(Counter(p["document_type"] for p in plans).items())),
            "language_counts": dict(sorted(language_counts.items())),
            "address_mode_counts": dict(sorted(Counter(p["address_mode"] for p in plans).items())),
            "list_kind_counts": dict(
                sorted(Counter(p["semantic_plan"]["structure"]["list_kind"] for p in plans).items())
            ),
            "fact_count_counts": dict(sorted(Counter(len(p["semantic_plan"]["facts"]) for p in plans).items())),
            "unique_fictional_organizations": len(ORGANIZATIONS),
            "unique_fictional_people": len(PEOPLE),
            "unique_products": len(PRODUCTS),
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
            {
                "count": COUNT,
                "plans_sha256": manifest["plans_sha256"],
                "prompt_hash": prompt_hash,
                "languages": dict(language_counts),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
