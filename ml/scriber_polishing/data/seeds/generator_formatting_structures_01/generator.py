"""Deterministic semantic-plan generator for rich formatting structures."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
HANDOFF = ROOT / "handoffs" / "AGENT_DATA_FORMATTING_STRUCTURES.md"
SCHEMA = ROOT / "contracts" / "seed_plan_schema.json"
OUT = HERE / "plans.jsonl"
MANIFEST = HERE / "manifest.json"
PROMPT_HASH = "sha256:" + hashlib.sha256(HANDOFF.read_bytes()).hexdigest()
AGENT = "generator_formatting_structures_01"
BATCH = "formatting_structures_batch_01"


def entity(kind: str, value: str) -> dict:
    return {"type": kind, "value": value, "fictional": True, "protected": True}


def fact(
    fid: int,
    text: str,
    polarity: str = "positive",
    modality: str = "fact",
    condition: str | None = None,
    values: list[str] | None = None,
) -> dict:
    return {
        "fact_id": f"f{fid}",
        "order": fid,
        "text": text,
        "polarity": polarity,
        "modality": modality,
        "condition": condition,
        "protected_values": values or [],
    }


def category(i: int) -> tuple[str, str, str, str]:
    if i < 140:
        return ("general_business", "formales Geschaeftsschreiben", "formal_sie", "Nordlicht Kontor GmbH")
    if i < 200:
        return ("technical_project", "Projekt-Nachbereitung", "mixed_correspondence", "Kernspur Projektwerk GmbH")
    if i < 250:
        return ("tax_legal", "Steuer- und Rechtskorrespondenz", "formal_sie", "Falkenbogen Rechtsberatung GmbH")
    return (
        "real_estate_energy",
        "Immobilien- und Energiekorrespondenz",
        "formal_sie",
        "Sonnenfeld Liegenschaften GmbH",
    )


def make_plan(i: int) -> dict:
    seed = 700000 + i
    domain, document_type, address_mode, org = category(i)
    person = f"Mara Linden {i + 1}"
    project = f"Vorhaben Lumen-{i + 101}"
    date = f"{(i % 27) + 1:02d}.09.2026"
    amount = f"{1_250 + i * 17:,}".replace(",", ".") + ",00 EUR"
    percent = f"{5 + (i % 16)} %"
    unit = f"{12 + (i % 37)} kWh"
    direct = i % 2 == 0
    combined = i < 200 and direct
    has_heading = i < 150
    has_attachments = i < 180
    has_post_script = i < 120
    has_quote = i < 200
    nested = i < 120
    facts = []
    if direct:
        facts.append(
            fact(
                1,
                f"Bitte bestätigen Sie den Termin am {date} für {project}.",
                "positive",
                "recommendation",
                None,
                [date, project],
            )
        )
    else:
        facts.append(
            fact(
                1,
                f"Der Termin am {date} für {project} bleibt vorbehaltlich der Freigabe bestehen.",
                "positive",
                "uncertainty",
                "wenn die Freigabe vorliegt",
                [date, project],
            )
        )
    if combined:
        facts.append(
            fact(
                2,
                f"{person} teilte mit, dass sie die Unterlagen nicht vor dem Termin versendet.",
                "negative",
                "fact",
                None,
                [person],
            )
        )
    else:
        facts.append(
            fact(
                2,
                f"Für die Prüfung sind {amount}, {percent} und {unit} verbindlich dokumentiert.",
                "positive",
                "obligation",
                None,
                [amount, percent, unit],
            )
        )
    if domain == "tax_legal":
        facts.append(
            fact(
                len(facts) + 1,
                "Die Frist nach § 355 Absatz 1 Satz 1 der Abgabenordnung wird nicht verlängert.",
                "negative",
                "fact",
                None,
                ["§ 355 Absatz 1 Satz 1 der Abgabenordnung"],
            )
        )
    if has_heading:
        facts.append(
            fact(
                len(facts) + 1,
                "Bitte die Hauptüberschrift „Verbindliche nächste Schritte“ und die Zwischenüberschrift „Prüfpunkt“ verwenden.",
                "positive",
                "obligation",
                None,
                ["Verbindliche nächste Schritte", "Prüfpunkt"],
            )
        )
    else:
        facts.append(
            fact(
                len(facts) + 1, "Es wurde keine Überschrift und keine Zwischenüberschrift diktiert.", "negative", "fact"
            )
        )
    if has_quote:
        quote = f"Die Freigabe erfolgt nur nach vollständiger Prüfung {project}."
        facts.append(fact(len(facts) + 1, f"Wörtlich festzuhalten ist: {quote}", "positive", "fact", None, [quote]))
    else:
        quote = None
    if has_attachments:
        attachment = f"Anlage: Prüfprotokoll {project} vom {date}"
        facts.append(
            fact(
                len(facts) + 1,
                f"Beizufügen ist die Anlage: {attachment}.",
                "positive",
                "obligation",
                None,
                [attachment],
            )
        )
    else:
        attachment = None
    if has_post_script:
        postscript = f"Die Rückmeldung ist bis {date} erforderlich."
        facts.append(
            fact(len(facts) + 1, f"Der Nachsatz lautet: {postscript}", "positive", "obligation", None, [postscript])
        )
    else:
        postscript = None
    list_kind = "nested" if nested else ("ordered" if i % 3 == 0 else "unordered" if i % 3 == 1 else "none")
    items = (
        ["Prüfung der Unterlagen", "  Frist und Betrag abgleichen", "  Verantwortliche Person benennen"]
        if nested
        else (["Termin bestätigen", "Freigabe dokumentieren"] if list_kind != "none" else [])
    )
    structure = {
        "has_subject": True,
        "has_salutation": True,
        "paragraph_topics": ["Anlass und Termin", "Verbindliche Werte", "Nächste Schritte"],
        "heading_levels": ["h1", "h2"] if has_heading else [],
        "list_kind": list_kind,
        "list_items": items,
        "has_closing": True,
        "signature_lines": [person, org],
        "has_quote": has_quote,
        "quote_text": quote,
        "has_attachments": has_attachments,
        "attachment_lines": [attachment] if attachment else [],
        "has_post_script": has_post_script,
        "post_script_text": postscript,
    }
    tags = ["formatting_structures", "protected_values"]
    if nested:
        tags.append("nested_list")
    if has_heading:
        tags.append("explicit_heading")
    if direct:
        tags.append("direct_address")
    if combined:
        tags.append("third_person_reference")
    return {
        "schema_version": 1,
        "seed_id": f"formatplan_{seed}",
        "generator_agent": AGENT,
        "batch_id": BATCH,
        "prompt_hash": PROMPT_HASH,
        "seed": seed,
        "language": "de-DE",
        "domain": domain,
        "document_type": document_type,
        "style_profile": "duden_preferred_business",
        "address_mode": address_mode,
        "legal_citation_style": "long",
        "unit_style": "professional_symbols",
        "semantic_plan": {
            "facts": facts,
            "entities": [entity("person", person), entity("organization", org), entity("product", project)],
            "structure": structure,
        },
        "risk_tags": tags,
        "source_class": "ai_generated",
        "private_data": False,
    }


def coverage(plans: list[dict]) -> dict:
    structures = [p["semantic_plan"]["structure"] for p in plans]
    return {
        "has_attachments": sum(s["has_attachments"] for s in structures),
        "has_post_script": sum(s["has_post_script"] for s in structures),
        "has_quote": sum(s["has_quote"] for s in structures),
        "explicit_headings": sum(bool(s["heading_levels"]) for s in structures),
        "nested_lists": sum(s["list_kind"] == "nested" for s in structures),
        "direct_address": sum("direct_address" in p["risk_tags"] for p in plans),
        "direct_and_third_person": sum("third_person_reference" in p["risk_tags"] for p in plans),
        "formal_business_letters": sum(p["document_type"] == "formales Geschaeftsschreiben" for p in plans),
        "meeting_project_followups": sum(p["document_type"] == "Projekt-Nachbereitung" for p in plans),
        "tax_legal_correspondence": sum(p["domain"] == "tax_legal" for p in plans),
        "property_energy_correspondence": sum(p["domain"] == "real_estate_energy" for p in plans),
    }


def main() -> None:
    plans = [make_plan(i) for i in range(300)]
    validator = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
    errors = [error for plan in plans for error in validator.iter_errors(plan)]
    if errors:
        raise SystemExit("Schema validation failed: " + errors[0].message)
    payload = "".join(json.dumps(p, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for p in plans)
    OUT.write_text(payload, encoding="utf-8", newline="\n")
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    manifest = {
        "generator_agent": AGENT,
        "batch_id": BATCH,
        "record_count": len(plans),
        "seed_range": [700000, 700299],
        "prompt_hash": PROMPT_HASH,
        "plans_sha256": digest,
        "creation_timestamp": "2026-07-29T00:00:00Z",
        "coverage_counts": coverage(plans),
        "schema_validation_counts": {"draft202012_valid": len(plans), "draft202012_invalid": 0},
        "generation_boundary": {
            "semantic_plans_only": True,
            "targets_generated": False,
            "splits_generated": False,
            "corruptions_generated": False,
            "critic_verdicts_generated": False,
        },
    }
    MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    main()
