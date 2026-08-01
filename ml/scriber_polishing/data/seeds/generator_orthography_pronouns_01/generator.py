"""Deterministic semantic seed plans for German orthography and pronouns."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from jsonschema import Draft202012Validator

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
HANDOFF = HERE / "handoff.md"
SCHEMA = ROOT / "contracts" / "seed_plan_schema.json"
OUT = HERE / "plans.jsonl"
MANIFEST = HERE / "manifest.json"
AGENT = "generator_orthography_pronouns_01"
BATCH = "orthography_pronouns_batch_01"
PROMPT_HASH = "sha256:" + hashlib.sha256(HANDOFF.read_bytes()).hexdigest()
GENERATOR_REVISION = 2
PREVIOUS_PLANS_SHA256 = "a779d258fffcf6da6f126acd15601077098aa6284e07253e04038f3ff92f2342"

PEOPLE = ("Mara Linden", "Jonas Falk", "Elin Werner", "Timo Berg", "Sina Körner", "Lea Dorn")
ORGS = ("Nordspur Büroservice GmbH", "Wellenkern Handel GmbH", "Falkenblatt Projekt GmbH")
DOCS = (
    "Geschäfts-E-Mail", "Projektstatus", "Terminbestätigung", "Kundeninformation",
    "Abstimmungsnotiz", "Lieferantenanfrage", "Übergabeprotokoll", "Teamrundschreiben",
    "Angebotsbegleitung", "Beschwerdeantwort", "Freigabehinweis", "Besprechungsnachbereitung",
)

# Each entry contains the surface forms that must remain visible to the later
# document stages. The hard-negative entries contrast a close, different form.
GENERAL_CASES = (
    ("sodass", "Die Prüfung ist vollständig, sodass die Freigabe erfolgen kann.", "house_form_normalization"),
    ("zurzeit", "Die weitere Rückmeldung liegt zurzeit nicht vor.", "house_form_normalization"),
    ("infolge", "Der Versand verschiebt sich infolge der Abstimmung nicht.", "house_form_normalization"),
    ("des Weiteren", "Wir halten des Weiteren fest, dass der vereinbarte Umfang unverändert bleibt.", "business_capitalization"),
    ("aufgrund", "Die Frist wird aufgrund der Rückfrage nur vorbehaltlich bestätigt.", "accepted_variant"),
    ("kennenlernen", "Das Team soll den neuen Ablauf kennenlernen; dabei werden keine Daten geändert.", "accepted_variant"),
    ("E-Mail", "Die E-Mail geht heute an die Projektgruppe.", "compound_hyphen"),
    ("Online-Shop", "Der Online-Shop bleibt bis zur Abnahme erreichbar.", "compound_hyphen"),
    ("B2B-Portal", "Im B2B-Portal ist die Freigabe bereits sichtbar.", "compound_hyphen"),
    ("Freigabeprozess", "Der Freigabeprozess endet erst nach der Dokumentation.", "compound"),
    ("im Allgemeinen", "Die Reihenfolge der Schritte ist im Allgemeinen verbindlich.", "business_capitalization"),
    ("so, dass", "Wir formulieren die Hinweise so, dass sie ohne Rückfrage verständlich bleiben.", "accepted_variant"),
)

HARD_CASES = (
    ("das_dass", ("das", "dass"), "Bitte prüfen Sie das Angebot, das Frau Linden vorbereitet hat, bevor Sie bestätigen, dass es vollständig ist."),
    ("seit_seid", ("Seit", "seid", "Ihr"), "Seit Montag seid Ihr für die Prüfung der Anlage verantwortlich."),
    ("wider_wieder", ("wider", "wieder"), "Der Einwand richtet sich wider die Annahme, dass die Lieferung wieder verschoben wird."),
    ("ss_sz", ("Maße", "Masse"), "Die Maße des Musters sind verbindlich; seine Masse wird erst nach der Messung genannt."),
    ("third_person_sie", ("sie", "ihre"), "Frau Linden erklärte, sie habe ihre Unterlagen bereits versendet."),
    ("formal_sie", ("Sie", "Ihnen", "Ihre"), "Bitte senden Sie Ihre Rückmeldung, damit wir Ihnen den Termin bestätigen können."),
    ("du_capitalized", ("Du", "Deine", "Dir"), "Liebe Mara, Du kannst Deine Freigabe bis Freitag an Jonas senden; er bestätigt Dir den Eingang."),
    ("du_lowercase", ("du", "deine", "dir"), "Im Protokoll steht, dass du deine Notizen Jonas geben willst und er dir danach antwortet."),
    ("ihr_euch_euer", ("Ihr", "Euch", "Euer"), "Ihr gebt Euer Ergebnis an Jonas, damit er Euch den finalen Stand senden kann."),
    ("comma_infinitive", ("Wir bitten Sie, die Rückmeldung zu bestätigen.",), "Wir bitten Sie, die Rückmeldung zu bestätigen."),
    ("comma_dass", ("Wir bestätigen, dass", "dass"), "Wir bestätigen, dass der Termin unverändert bestehen bleibt."),
    ("hyphen_identity", ("E-Mail-Anhang", "E-Mail"), "Der E-Mail-Anhang mit der Freigabe bleibt unverändert bezeichnet."),
)


def entity(kind: str, value: str) -> dict:
    return {"type": kind, "value": value, "fictional": True, "protected": True}


def fact(number: int, text: str, *, polarity: str = "positive", modality: str = "fact", condition: str | None = None, protected: tuple[str, ...] | list[str] = ()) -> dict:
    return {
        "fact_id": f"f{number}", "order": number, "text": text, "polarity": polarity,
        "modality": modality, "condition": condition, "protected_values": list(protected),
    }


def address_for(i: int) -> str:
    if i < 38:
        return "personal_du_capitalized"
    if i < 75:
        return "personal_du_lowercase"
    if i < 125:
        return "formal_sie"
    if i < 150:
        return "mixed_correspondence"
    if i < 205:
        return "formal_sie"
    if i < 255:
        return "mixed_correspondence"
    if i < 275:
        return "ambiguous"
    return "neutral_third_person"


def structure(i: int, direct: bool) -> dict:
    kinds = ("none", "ordered", "unordered", "nested")
    kind = kinds[i % len(kinds)]
    list_items = [] if kind == "none" else ["Wortlaut prüfen", "Rückmeldung sichern"]
    return {
        "has_subject": i % 3 != 0,
        "has_salutation": direct,
        "paragraph_topics": ["Anlass und Wortlaut", "Abstimmung und Rückmeldung"],
        "heading_levels": ["h1"] if i % 7 == 0 else [],
        "list_kind": kind,
        "list_items": list_items,
        "has_closing": direct and i % 4 != 0,
        "signature_lines": [PEOPLE[i % len(PEOPLE)], ORGS[i % len(ORGS)]] if direct and i % 4 != 0 else [],
    }


def general_plan(i: int) -> dict:
    seed = 800000 + i
    surface, sentence, category = GENERAL_CASES[i % len(GENERAL_CASES)]
    mode = address_for(i)
    person = PEOPLE[i % len(PEOPLE)]
    colleague = PEOPLE[(i + 1) % len(PEOPLE)]
    org = ORGS[i % len(ORGS)]
    direct = True
    # Keep the 75 singular familiar-address examples separate from a full
    # 50-plan plural Ihr/Euch/Euer coverage block.
    plural = 75 <= i < 125
    if mode == "personal_du_capitalized":
        address = f"Liebe {person}, bitte gib Deine Rückmeldung an {colleague}; er dokumentiert sie im Vorgang."
        address_values = ("Deine", "sie")
    elif mode == "personal_du_lowercase":
        address = f"{person}, bitte gib deine Rückmeldung an {colleague}; er dokumentiert sie im Vorgang."
        address_values = ("deine", "sie")
    elif plural:
        address = f"Bitte gebt Ihr Eure Rückmeldung an {colleague}, damit er Euch den Stand senden kann."
        address_values = ("Ihr", "Eure", "Euch", "er")
    elif mode == "formal_sie":
        address = f"Bitte bestätigen Sie {colleague}, dass Ihre Rückmeldung vorliegt; sie prüft den Vorgang."
        address_values = ("Sie", "Ihre", "sie")
    else:
        address = f"Bitte stimmen Sie sich mit {colleague} ab; sie hält die vereinbarte Reihenfolge fest."
        address_values = ("Sie", "sie")
    facts = [
        fact(1, address, modality="recommendation", protected=address_values),
        fact(2, sentence, polarity="negative" if i % 5 == 0 else "positive", modality="uncertainty" if i % 4 == 0 else "fact", condition="wenn die Prüfung abgeschlossen ist" if i % 4 == 0 else None, protected=(surface,)),
        fact(3, f"{person} bestätigt für Vorgang OP-{seed}, dass der Wortlaut für {org} weder gekürzt noch inhaltlich erweitert wird.", polarity="double_negative" if i % 11 == 0 else "negative", modality="obligation", protected=(person, org, f"OP-{seed}", "dass")),
    ]
    tags = ["orthography_pronouns", category, "direct_address", "third_person_reference", "protected_surface"]
    if i % 3 == 0:
        tags.append("identity_preferred_form")
    if mode.startswith("personal_du"):
        tags.append("personal_du")
    if plural:
        tags.append("ihr_euch_euer")
    return plan(seed, "general_business", DOCS[i % len(DOCS)], mode, facts, [entity("person", person), entity("person", colleague), entity("organization", org)], structure(i, direct), tags)


def hard_plan(offset: int) -> dict:
    i = 150 + offset
    seed = 800000 + i
    name, surfaces, sentence = HARD_CASES[offset % len(HARD_CASES)]
    mode = address_for(i)
    person = PEOPLE[i % len(PEOPLE)]
    org = ORGS[i % len(ORGS)]
    direct = offset < 100
    identity = offset < 120
    case_fact = fact(1, sentence, polarity="negative" if offset % 3 == 0 else "positive", modality="possibility" if offset % 5 == 0 else "fact", condition="wenn der zitierte Wortlaut bestätigt wird" if offset % 5 == 0 else None, protected=surfaces)
    preserve = fact(2, f"Der genaue Wortlaut mit {', '.join('„' + value + '“' for value in surfaces)} ist als Bedeutungskontrast geschützt und darf nicht automatisch angeglichen werden.", modality="obligation", protected=surfaces)
    if direct:
        contact = f"Bitte stimmen Sie sich mit {person} zu Vorgang OP-{seed} ab; sie bestätigt nur die Schreibweise, nicht eine andere Bedeutung."
        contact_values = ("Sie", "sie", person, f"OP-{seed}")
    else:
        contact = f"{person} hält für Vorgang OP-{seed} fest, dass sie die geschützte Form nicht ersetzt."
        contact_values = (person, f"OP-{seed}", "sie", "dass")
    facts = [case_fact, preserve, fact(3, contact, polarity="negative", modality="obligation", protected=contact_values)]
    tags = ["orthography_pronouns", "hard_negative", "no_normalization", name, "protected_surface"]
    if identity:
        tags.extend(["meaning_dependent_hard_negative", "identity_no_normalization"])
    if direct:
        tags.extend(["direct_address", "third_person_reference"])
    if name == "ihr_euch_euer":
        tags.append("ihr_euch_euer")
    if name in {"du_capitalized", "du_lowercase"}:
        tags.append("personal_du")
    return plan(seed, "hard_negatives", "Bedeutungskontrast", mode, facts, [entity("person", person), entity("organization", org)], structure(i, direct), tags)


def plan(seed: int, domain: str, document_type: str, address_mode: str, facts: list[dict], entities: list[dict], doc_structure: dict, tags: list[str]) -> dict:
    return {
        "schema_version": 1, "seed_id": f"orthopron_{seed}", "generator_agent": AGENT,
        "batch_id": BATCH, "prompt_hash": PROMPT_HASH, "seed": seed, "language": "de-DE",
        "domain": domain, "document_type": document_type, "style_profile": "duden_preferred_business",
        "address_mode": address_mode, "legal_citation_style": "long", "unit_style": "professional_symbols",
        "semantic_plan": {"facts": facts, "entities": entities, "structure": doc_structure},
        "risk_tags": sorted(set(tags)), "source_class": "ai_generated", "private_data": False,
    }


def coverage(plans: list[dict]) -> dict:
    tags = [set(item["risk_tags"]) for item in plans]
    address_modes = Counter(item["address_mode"] for item in plans)
    surfaces = ("das_dass", "seit_seid", "wider_wieder", "ss_sz", "comma_infinitive", "comma_dass", "compound_hyphen", "house_form_normalization", "accepted_variant", "business_capitalization", "compound", "identity_preferred_form", "formal_sie", "third_person_sie", "du_capitalized", "du_lowercase")
    return {
        "domain_counts": dict(sorted(Counter(item["domain"] for item in plans).items())),
        "address_mode_counts": dict(sorted(address_modes.items())),
        "direct_address_and_third_person": sum({"direct_address", "third_person_reference"} <= value for value in tags),
        "personal_du": sum("personal_du" in value for value in tags),
        "ihr_euch_euer": sum("ihr_euch_euer" in value for value in tags),
        "meaning_dependent_or_identity_hard_negatives": sum("meaning_dependent_hard_negative" in value or "identity_no_normalization" in value for value in tags),
        "no_normalization_hard_negatives": sum("no_normalization" in value for value in tags),
        "orthography_or_pronoun_case_counts": {key: sum(key in value for value in tags) for key in surfaces},
        "unique_fact_fingerprints": len({hashlib.sha256(json.dumps(item["semantic_plan"]["facts"], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest() for item in plans}),
    }


def repair_summary() -> dict:
    spelling_reasons = {
        "lowercase_zurzeit_embedded_verbatim": [1],
        "lowercase_infolge_embedded_verbatim": [2],
        "lowercase_des_weiteren_embedded_verbatim": [3],
        "lowercase_aufgrund_embedded_verbatim": [4],
        "infinitive_kennenlernen_embedded_verbatim": [5],
        "lowercase_im_allgemeinen_embedded_verbatim": [10],
        "literal_so_comma_dass_embedded_verbatim": [11],
    }
    pronoun_reasons = {
        "capitalized_du_deine_dir_all_embedded_verbatim": [6],
        "lowercase_du_deine_dir_all_embedded_verbatim": [7],
        "plural_ihr_euch_euer_all_embedded_verbatim": [8],
    }
    reasons: dict[str, list[str]] = {}
    for reason, residues in spelling_reasons.items():
        reasons[reason] = [
            f"orthopron_{800000 + i}"
            for i in range(150)
            if i % len(GENERAL_CASES) in residues
        ]
    for reason, residues in pronoun_reasons.items():
        reasons[reason] = [
            f"orthopron_{800150 + offset}"
            for offset in range(150)
            if offset % len(HARD_CASES) in residues
        ]
    repaired_ids = sorted({seed_id for ids in reasons.values() for seed_id in ids})
    return {
        "generator_revision": GENERATOR_REVISION,
        "previous_plans_sha256": PREVIOUS_PLANS_SHA256,
        "repaired_record_count": len(repaired_ids),
        "previous_absent_protected_value_count": 137,
        "current_absent_protected_value_count": 0,
        "repaired_seed_ids": repaired_ids,
        "reasons": reasons,
    }


def validate(plans: list[dict]) -> None:
    validator = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
    errors = [error for item in plans for error in validator.iter_errors(item)]
    if errors:
        raise SystemExit("Schema validation failed: " + errors[0].message)
    result = coverage(plans)
    assert result["domain_counts"] == {"general_business": 150, "hard_negatives": 150}
    assert result["direct_address_and_third_person"] >= 100
    assert result["personal_du"] >= 75
    assert result["ihr_euch_euer"] >= 50
    assert result["meaning_dependent_or_identity_hard_negatives"] >= 100
    assert result["unique_fact_fingerprints"] == 300
    assert len({item["seed"] for item in plans}) == 300
    assert len({item["seed_id"] for item in plans}) == 300
    assert all(item["private_data"] is False for item in plans)
    absent_protected = [
        (item["seed_id"], item_fact["fact_id"], value)
        for item in plans
        for item_fact in item["semantic_plan"]["facts"]
        for value in item_fact["protected_values"]
        if value not in item_fact["text"]
    ]
    assert absent_protected == []


def main() -> None:
    plans = [general_plan(i) for i in range(150)] + [hard_plan(i) for i in range(150)]
    validate(plans)  # First deterministic validation: in-memory records.
    payload = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for item in plans)
    OUT.write_text(payload, encoding="utf-8", newline="\n")
    serialized = [json.loads(line) for line in OUT.read_text(encoding="utf-8").splitlines()]
    validate(serialized)  # Second deterministic validation: serialized JSONL.
    manifest = {
        "manifest_version": 2, "generator_revision": GENERATOR_REVISION,
        "generator_agent": AGENT, "batch_id": BATCH,
        "record_count": len(plans), "seed_range": [800000, 800299], "prompt_hash": PROMPT_HASH,
        "plans_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "creation_timestamp": "2026-07-29T00:00:00Z", "coverage_counts": coverage(plans),
        "repair": repair_summary(),
        "schema_validation": {"first_pass_valid": 300, "first_pass_invalid": 0, "second_pass_valid": 300, "second_pass_invalid": 0, "validator": "jsonschema.Draft202012Validator"},
        "generation_boundary": {"semantic_plans_only": True, "targets_generated": False, "splits_generated": False, "corruptions_generated": False, "critic_verdicts_generated": False, "external_or_model_api_calls": 0, "private_data_records": 0, "all_entities_fictional_and_protected": True},
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
