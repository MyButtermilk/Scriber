"""Generate a deterministic, synthetic German coverage-recovery seed batch.

This generator emits semantic plans only.  It deliberately creates no targets,
splits, corruptions, or critic material, and it never accesses network services.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from jsonschema import Draft202012Validator


HERE = Path(__file__).resolve().parent
ML_ROOT = HERE.parents[2]
SCHEMA = ML_ROOT / "contracts" / "seed_plan_schema.json"
OUT = HERE / "plans.jsonl"
MANIFEST = HERE / "manifest.json"
VALIDATION = HERE / "validation.json"
REPORT = HERE / "generation_report.md"

AGENT = "generator_coverage_recovery_02"
BATCH = "coverage_recovery_batch_02"
MASTER_SEED = 1_020_200
RECORD_COUNT = 40
CREATED = "2026-07-30T00:00:00Z"
PROMPT_VARIANT = (
    "Erzeuge ausschließlich schema-valide, vollsynthetische semantische Plaene "
    "fuer deutsche Geschaefts- und Immobilienkommunikation. Bewahre die Fakten, "
    "verwende professionelle Einheitensymbole und markiere kontextgerechte "
    "Orthografierisiken ohne Targets oder Kritik."
)
PROMPT_HASH = "sha256:" + hashlib.sha256(PROMPT_VARIANT.encode("utf-8")).hexdigest()

# This vocabulary is authored for this isolated batch; all values are fictional.
PEOPLE = ("Mara Klee", "Jonas Venn", "Elif Dorn", "Timo Rehn", "Nina Aue")
ORGANIZATIONS = (
    "Fiktivraum Projekt GmbH",
    "Lindenbogen Verwaltung KG",
    "Auenfeld Immobilien eG",
    "Kernhof Vermietung GmbH",
    "Morgenring Baukontor KG",
)
LOCATIONS = ("Quartier Auenstern", "Hof Lindenmark", "Campus Morgenrain", "Parkfeld Vela")
DOCUMENT_TYPES = (
    "Mietanpassungsnotiz",
    "Objektstatus-E-Mail",
    "Vermietungsfreigabe",
    "Besichtigungsprotokoll",
    "Nachtragsvermerk",
    "Eigentümerinformation",
    "Flächenprüfvermerk",
    "Projektabstimmung",
)
ORTHOGRAPHY = (
    ("das_dass", "Das Exposé, das den Mietansatz ausweist, wird erst versandt, nachdem bestätigt ist, dass die Freigabe vorliegt."),
    ("seit_seid", "Seit Montag seid ihr für die Rückmeldung zur Flächenprüfung eingeteilt; die Reihenfolge der Angaben bleibt unverändert."),
    ("wider_wieder", "Der Einwand richtet sich wider die frühere Zuordnung; die Unterlage wird wieder vorgelegt, sobald die Prüfung abgeschlossen ist."),
    ("ss_sz", "Die Maße des Musterraums sind geprüft, während die Masse des Bauteils getrennt im technischen Anhang dokumentiert wird."),
)


def entity(kind: str, value: str) -> dict[str, object]:
    return {"type": kind, "value": value, "fictional": True, "protected": True}


def fact(
    number: int,
    text: str,
    protected: tuple[str, ...],
    *,
    modality: str = "fact",
    polarity: str = "positive",
    condition: str | None = None,
) -> dict[str, object]:
    return {
        "fact_id": f"f{number}",
        "order": number,
        "text": text,
        "polarity": polarity,
        "modality": modality,
        "condition": condition,
        "protected_values": list(protected),
    }


def structure(index: int, person: str, organization: str) -> dict[str, object]:
    list_kinds = ("none", "unordered", "ordered", "nested")
    list_kind = list_kinds[index % len(list_kinds)]
    result: dict[str, object] = {
        "has_subject": index % 3 != 0,
        "has_salutation": index % 2 == 0,
        "paragraph_topics": ["Objekt und Mietansatz", "Prüfhinweis", "Freigabe"],
        "heading_levels": ["h1"] if index % 5 == 0 else (["h2"] if index % 5 == 1 else []),
        "list_kind": list_kind,
        "list_items": [] if list_kind == "none" else ["Mietansatz prüfen", "Freigabe dokumentieren"],
        "has_closing": index % 2 == 0,
        "signature_lines": [person, organization] if index % 2 == 0 else [],
    }
    if index % 7 == 0:
        result.update({"has_quote": True, "quote_text": "Die Freigabe gilt nur für den geprüften Wortlaut."})
    if index % 9 == 0:
        result.update({"has_attachments": True, "attachment_lines": ["Anlage: fiktive Flächenübersicht"]})
    if index % 11 == 0:
        result.update({"has_post_script": True, "post_script_text": "PS: Bitte den dokumentierten Stand verwenden."})
    return result


def make_plan(index: int) -> dict[str, object]:
    seed = MASTER_SEED + index
    person = PEOPLE[index % len(PEOPLE)]
    organization = ORGANIZATIONS[(index * 3) % len(ORGANIZATIONS)]
    location = LOCATIONS[(index * 2) % len(LOCATIONS)]
    risk_tag, orthography_fact = ORTHOGRAPHY[index % len(ORTHOGRAPHY)]
    reference = f"CR-{seed}"
    area = f"{72 + index * 3} m²"
    document_type = DOCUMENT_TYPES[index % len(DOCUMENT_TYPES)]
    facts = [
        fact(1, f"Für {location} dokumentiert {organization} eine geprüfte Fläche von {area} im Vorgang {reference}.", (location, organization, area, reference)),
        fact(2, f"Der verbindliche Mietansatz beträgt 48 €/m² und wird von {person} nur nach der internen Freigabe kommuniziert.", ("48 €/m²", person), modality="obligation"),
        fact(3, orthography_fact, tuple({"das_dass": ("Das", "das", "dass"), "seit_seid": ("Seit", "seid"), "wider_wieder": ("wider", "wieder"), "ss_sz": ("Maße", "Masse")}[risk_tag]), modality="obligation" if index % 2 else "fact"),
        fact(4, f"{person} hält fest, dass keine zusätzlichen Flächen, Preise oder Zusagen in {reference} ergänzt werden.", (person, "dass", reference), modality="obligation", polarity="negative"),
    ]
    return {
        "schema_version": 1,
        "seed_id": f"covrecovery_{seed}",
        "generator_agent": AGENT,
        "batch_id": BATCH,
        "prompt_hash": PROMPT_HASH,
        "seed": seed,
        "language": "de-DE",
        "domain": "real_estate_energy" if index % 2 == 0 else "general_business",
        "document_type": document_type,
        "style_profile": "duden_preferred_business",
        "address_mode": "formal_sie" if index % 3 else "mixed_correspondence",
        "legal_citation_style": "long",
        "unit_style": "professional_symbols",
        "semantic_plan": {
            "facts": facts,
            "entities": [entity("person", person), entity("organization", organization), entity("location", location)],
            "structure": structure(index, person, organization),
        },
        "risk_tags": sorted({"coverage_recovery", "fictional_real_estate", "professional_unit_surface", risk_tag}),
        "source_class": "ai_generated",
        "private_data": False,
    }


def render(plans: list[dict[str, object]]) -> str:
    return "".join(json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for plan in plans)


def coverage(plans: list[dict[str, object]]) -> dict[str, object]:
    return {
        "domain_counts": dict(sorted(Counter(str(plan["domain"]) for plan in plans).items())),
        "document_type_count": len({str(plan["document_type"]) for plan in plans}),
        "risk_family_counts": {tag: sum(tag in plan["risk_tags"] for plan in plans) for tag, _ in ORTHOGRAPHY},
        "unit_surface_count": sum("48 €/m²" in str(fact_item["text"]) for plan in plans for fact_item in plan["semantic_plan"]["facts"]),
        "unique_fact_fingerprints": len({hashlib.sha256(json.dumps(plan["semantic_plan"]["facts"], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest() for plan in plans}),
    }


def validate(plans: list[dict[str, object]]) -> dict[str, object]:
    validator = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
    errors = [error for plan in plans for error in validator.iter_errors(plan)]
    if errors:
        raise AssertionError(errors[0].message)
    assert len(plans) == RECORD_COUNT
    assert [plan["seed"] for plan in plans] == list(range(MASTER_SEED, MASTER_SEED + RECORD_COUNT))
    assert len({plan["seed_id"] for plan in plans}) == RECORD_COUNT
    assert all(plan["private_data"] is False and plan["source_class"] == "ai_generated" for plan in plans)
    assert all("48 €/m²" in " ".join(str(fact_item["text"]) for fact_item in plan["semantic_plan"]["facts"]) for plan in plans)
    assert all(any(tag in plan["risk_tags"] for tag, _ in ORTHOGRAPHY) for plan in plans)
    assert all(value in str(fact_item["text"]) for plan in plans for fact_item in plan["semantic_plan"]["facts"] for value in fact_item["protected_values"])
    summary = coverage(plans)
    assert summary["risk_family_counts"] == {"das_dass": 10, "seit_seid": 10, "wider_wieder": 10, "ss_sz": 10}
    assert summary["unit_surface_count"] == RECORD_COUNT
    assert summary["document_type_count"] == len(DOCUMENT_TYPES)
    assert summary["unique_fact_fingerprints"] == RECORD_COUNT
    return summary


def main() -> None:
    plans = [make_plan(index) for index in range(RECORD_COUNT)]
    summary = validate(plans)
    payload = render(plans)
    OUT.write_text(payload, encoding="utf-8", newline="\n")
    serialized = [json.loads(line) for line in OUT.read_text(encoding="utf-8").splitlines()]
    serialized_summary = validate(serialized)
    assert render(serialized).encode("utf-8") == payload.encode("utf-8")
    plans_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    generator_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    validation = {
        "record_count": RECORD_COUNT,
        "schema_validation": {"in_memory_valid": RECORD_COUNT, "serialized_valid": RECORD_COUNT, "validator": "jsonschema.Draft202012Validator"},
        "duplicate_check": {"duplicate_seed_ids": 0, "duplicate_seeds": 0, "unique_fact_fingerprints": summary["unique_fact_fingerprints"]},
        "semantic_checks": {"professional_unit_surface": "48 €/m²", "all_records_have_unit_surface": True, "all_records_have_orthography_risk_family": True, "coverage": serialized_summary},
        "determinism": {"master_seed": MASTER_SEED, "byte_identical_serialization": True, "external_or_model_api_calls": 0},
        "plans_sha256": plans_hash,
        "generator_sha256": generator_hash,
        "prompt_hash": PROMPT_HASH,
        "generator_agent": AGENT,
        "batch_id": BATCH,
    }
    VALIDATION.write_text(json.dumps(validation, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
    manifest = {
        "manifest_version": 1, "generator_agent": AGENT, "batch_id": BATCH, "generator_revision": 1,
        "record_count": RECORD_COUNT, "seed_range": [MASTER_SEED, MASTER_SEED + RECORD_COUNT - 1], "master_seed": MASTER_SEED,
        "prompt_hash": PROMPT_HASH, "prompt_variant": "coverage_recovery_de_real_estate_orthography_v1",
        "plans_sha256": plans_hash, "generator_sha256": generator_hash, "creation_timestamp": CREATED,
        "coverage_counts": summary, "schema_validation": validation["schema_validation"],
        "generation_boundary": {"semantic_plans_only": True, "targets_generated": False, "splits_generated": False, "corruptions_generated": False, "critic_verdicts_generated": False, "external_or_model_api_calls": 0, "private_data_records": 0, "all_entities_fictional_and_protected": True},
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
    REPORT.write_text(
        "# Coverage Recovery 02 – Generation Report\n\n"
        f"- Agent-ID: `{AGENT}`\n- Batch-ID: `{BATCH}`\n- Master-Seed: `{MASTER_SEED}`\n- Plan-Count: `{RECORD_COUNT}`\n"
        f"- Prompt-Hash: `{PROMPT_HASH}`\n- Plans-SHA-256: `{plans_hash}`\n- Generator-SHA-256: `{generator_hash}`\n"
        "- Lokale Schema- und Duplikatprüfung: bestanden\n- Externe oder Modell-API-Aufrufe: 0\n"
        "- Erzeugungsgrenze: ausschließlich synthetische semantische Pläne; keine Targets, Splits oder Critic-Artefakte.\n",
        encoding="utf-8", newline="\n",
    )


if __name__ == "__main__":
    main()
