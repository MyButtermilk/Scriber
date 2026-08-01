"""Deterministically generate the local hard-negative semantic seed batch."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
PROMPT = ROOT / "handoffs" / "AGENT_DATA_HARD_NEGATIVES.md"
SCHEMA = ROOT / "contracts" / "seed_plan_schema.json"
AGENT = "generator_hard_negatives_01"
BATCH = "hard_negatives_batch_01"

PEOPLE = ["Mara Felsen", "Jonas Klee", "Elin Nord", "Timo Auen", "Rika Dorn", "Lina Voss"]
ORGS = ["Fiktivwerk Nord", "Kieselraum GmbH", "Auenplan KG", "Tannentakt OHG"]
PRODUCTS = ["Projekt Komet", "Modul Birke", "System Lumen", "Vorhaben Delta"]
LOCATIONS = ["Feldmark", "Nordhafen", "Birkenau", "Talwinkel"]
DOCS = ["interne_notiz", "projektmail", "pruefvermerk", "statusmeldung", "besprechungsnotiz"]


def fact(
    index: int,
    text: str,
    polarity: str = "positive",
    modality: str = "fact",
    condition: str | None = None,
    values: list[str] | None = None,
) -> dict:
    return {
        "fact_id": f"f{index}",
        "order": index,
        "text": text,
        "polarity": polarity,
        "modality": modality,
        "condition": condition,
        "protected_values": values or [],
    }


def scenario(kind: int, n: int) -> tuple[list[dict], list[str], list[dict], str]:
    person, org, product, location = (
        PEOPLE[n % len(PEOPLE)],
        ORGS[n % len(ORGS)],
        PRODUCTS[n % len(PRODUCTS)],
        LOCATIONS[n % len(LOCATIONS)],
    )
    close = 120 + n
    date = f"{(n % 27) + 1:02d}.09.2031"
    time = f"{8 + n % 9:02d}:{(n * 7) % 60:02d} Uhr"
    common = [
        {"type": "person", "value": person, "fictional": True, "protected": True},
        {"type": "organization", "value": org, "fictional": True, "protected": True},
    ]
    if kind == 0:
        return (
            [
                fact(1, f"{org} darf {product} nicht freigeben.", "negative", "obligation", None, [org, product]),
                fact(2, f"Die Freigabe ist nicht bis {date} vorgesehen.", "negative", "intention", None, [date]),
            ],
            ["negation", "date"],
            common + [{"type": "product", "value": product, "fictional": True, "protected": True}],
            "freigabe",
        )
    if kind == 1:
        return (
            [
                fact(
                    1,
                    f"Es ist nicht ausgeschlossen, dass {product} erst nach {time} startet.",
                    "double_negative",
                    "possibility",
                    None,
                    [product, time],
                ),
                fact(2, f"{person} bestätigt keinen früheren Beginn.", "negative", "fact", None, [person]),
            ],
            ["double_negation", "possibility", "time"],
            common,
            "startunsicherheit",
        )
    if kind == 2:
        return (
            [
                fact(
                    1,
                    f"Falls die Prüfung fehlschlägt, darf {org} die Bestellung nicht auslösen.",
                    "negative",
                    "obligation",
                    "Die Prüfung schlägt fehl.",
                    [org],
                ),
                fact(
                    2,
                    f"Ohne schriftliche Zustimmung bleibt {product} gesperrt.",
                    "negative",
                    "fact",
                    "Es liegt keine schriftliche Zustimmung vor.",
                    [product],
                ),
            ],
            ["negation", "condition", "obligation"],
            common + [{"type": "product", "value": product, "fictional": True, "protected": True}],
            "bedingte_sperre",
        )
    if kind == 3:
        return (
            [
                fact(
                    1,
                    f"{person} empfiehlt, höchstens {close} Einheiten zu reservieren.",
                    "positive",
                    "recommendation",
                    None,
                    [person, str(close)],
                ),
                fact(
                    2,
                    f"Eine Reservierung von {close + 1} Einheiten ist nicht empfohlen, aber nicht verboten.",
                    "negative",
                    "recommendation",
                    None,
                    [str(close + 1)],
                ),
            ],
            ["recommendation", "negation", "close_numbers"],
            common,
            "empfehlungsgrenze",
        )
    if kind == 4:
        return (
            [
                fact(
                    1,
                    f"Der Betrag beträgt {close},00 Euro und nicht {close + 1},00 Euro.",
                    "negative",
                    "fact",
                    None,
                    [f"{close},00 Euro", f"{close + 1},00 Euro"],
                ),
                fact(
                    2,
                    f"{org} soll den Betrag nur bei bestätigter Lieferung zahlen.",
                    "positive",
                    "recommendation",
                    "Die Lieferung ist bestätigt.",
                    [org],
                ),
            ],
            ["amount", "close_numbers", "negation", "condition"],
            common,
            "betragsabgrenzung",
        )
    if kind == 5:
        return (
            [
                fact(
                    1,
                    f"Der Termin ist am {date}, nicht am {(n % 27) + 1:02d}.10.2031.",
                    "negative",
                    "fact",
                    None,
                    [date, f"{(n % 27) + 1:02d}.10.2031"],
                ),
                fact(
                    2,
                    f"Die Rückmeldung kann bis {time} erfolgen, muss aber nicht früher eintreffen.",
                    "negative",
                    "possibility",
                    None,
                    [time],
                ),
            ],
            ["date", "time", "negation", "possibility"],
            common,
            "terminabgrenzung",
        )
    if kind == 6:
        return (
            [
                fact(
                    1,
                    f"Für {product} gilt Paragraph 7 Absatz 4 Satz 2 Einkommensteuergesetz.",
                    "positive",
                    "fact",
                    None,
                    [product, "Paragraph 7 Absatz 4 Satz 2 Einkommensteuergesetz"],
                ),
                fact(
                    2,
                    "Der Absatz ist zu lang und bezeichnet hier keine Rechtsnorm.",
                    "negative",
                    "fact",
                    None,
                    ["Absatz"],
                ),
            ],
            ["legal_reference", "format_word_ordinary", "legal_hierarchy"],
            common,
            "rechtsbegriff",
        )
    if kind == 7:
        return (
            [
                fact(
                    1,
                    f"Bitte beantworten Sie die Frage nicht: Kann {product} ohne Prüfung starten?",
                    "negative",
                    "obligation",
                    None,
                    [product],
                ),
                fact(
                    2, f"{person} notiert die Frage nur zur späteren Klärung.", "positive", "intention", None, [person]
                ),
            ],
            ["question_no_answer", "negation", "question"],
            common + [{"type": "product", "value": product, "fictional": True, "protected": True}],
            "offene_frage",
        )
    if kind == 8:
        return (
            [
                fact(
                    1,
                    "Das Wort Überschrift ist hier ein normales Substantiv und keine Formatierungsanweisung.",
                    "negative",
                    "fact",
                    None,
                    ["Überschrift"],
                ),
                fact(
                    2,
                    "Die Liste der Risiken ist keine Aufforderung, eine Liste zu erzeugen.",
                    "negative",
                    "fact",
                    None,
                    ["Liste"],
                ),
            ],
            ["format_word_ordinary", "negation", "do_not_invent_format"],
            common,
            "formatwort",
        )
    if kind == 9:
        return (
            [
                fact(
                    1,
                    f"{person} wiederholt bewusst: nicht freigeben, nicht freigeben.",
                    "negative",
                    "intention",
                    None,
                    [person, "nicht freigeben"],
                ),
                fact(2, f"Die Wiederholung bezieht sich nur auf {product}.", "positive", "fact", None, [product]),
            ],
            ["intentional_repetition", "negation"],
            common + [{"type": "product", "value": product, "fictional": True, "protected": True}],
            "bewusste_wiederholung",
        )
    if kind == 10:
        return (
            [
                fact(
                    1,
                    f"{org} kann {close} km prüfen, muss aber nur {close - 1} km dokumentieren.",
                    "positive",
                    "possibility",
                    None,
                    [f"{close} km", f"{close - 1} km", org],
                ),
                fact(
                    2,
                    "Die Einheit km darf nicht als Quadratkilometer verstanden werden.",
                    "negative",
                    "obligation",
                    None,
                    ["km"],
                ),
            ],
            ["unit", "close_numbers", "modality", "negation"],
            common,
            "einheitenabgrenzung",
        )
    if kind == 11:
        return (
            [
                fact(
                    1,
                    f"Betreff ist das Wort, das {person} im Gespräch verwendet, nicht eine Betreffzeile.",
                    "negative",
                    "fact",
                    None,
                    ["Betreff", person],
                ),
                fact(
                    2,
                    "Fett beschreibt eine Farbe der Markierung und ist keine Bitte um Fettschrift.",
                    "negative",
                    "fact",
                    None,
                    ["Fett"],
                ),
            ],
            ["format_word_ordinary", "do_not_invent_format", "negation"],
            common,
            "formatwort_betreff",
        )
    if kind == 12:
        return (
            [
                fact(
                    1,
                    f"Wenn {org} zustimmt, könnte {product} am {date} starten.",
                    "positive",
                    "possibility",
                    f"{org} stimmt zu.",
                    [org, product, date],
                ),
                fact(
                    2,
                    f"Ohne Zustimmung darf {product} nicht starten.",
                    "negative",
                    "obligation",
                    "Es liegt keine Zustimmung vor.",
                    [product],
                ),
            ],
            ["condition", "possibility", "obligation", "negation"],
            common + [{"type": "product", "value": product, "fictional": True, "protected": True}],
            "moeglichkeit_und_pflicht",
        )
    if kind == 13:
        return (
            [
                fact(
                    1,
                    f"{person} nennt {location} nicht als Lieferort, sondern als Vergleichsort.",
                    "negative",
                    "fact",
                    None,
                    [person, location],
                ),
                fact(2, f"Die Lieferung darf frühestens um {time} beginnen.", "positive", "obligation", None, [time]),
            ],
            ["negation", "name", "location", "time"],
            common + [{"type": "location", "value": location, "fictional": True, "protected": True}],
            "ortsabgrenzung",
        )
    return (
        [
            fact(
                1,
                f"Punkt {close} ist ein Inhaltspunkt und keine nummerierte Liste.",
                "negative",
                "fact",
                None,
                [f"Punkt {close}"],
            ),
            fact(
                2,
                "Die Signatur wird erwähnt, aber nicht diktiert und darf nicht ergänzt werden.",
                "negative",
                "obligation",
                None,
                ["Signatur"],
            ),
        ],
        ["format_word_ordinary", "list_false_positive", "negation", "do_not_invent_closing"],
        common,
        "punkt_und_signatur",
    )


def build_plan(seed: int) -> dict:
    n = seed - 500000
    facts, tags, entities, topic = scenario(n % 15, n)
    list_kind = ["none", "ordered", "unordered", "nested"][n % 4]
    return {
        "schema_version": 1,
        "seed_id": f"hardneg_{seed}",
        "generator_agent": AGENT,
        "batch_id": BATCH,
        "prompt_hash": "sha256:" + hashlib.sha256(PROMPT.read_bytes()).hexdigest(),
        "seed": seed,
        "language": "de-DE",
        "domain": "hard_negatives",
        "document_type": DOCS[n % len(DOCS)],
        "style_profile": "duden_preferred_business",
        "address_mode": ["formal_sie", "neutral_third_person", "ambiguous", "mixed_correspondence"][n % 4],
        "legal_citation_style": "long",
        "unit_style": "professional_symbols",
        "semantic_plan": {
            "facts": facts,
            "entities": entities,
            "structure": {
                "has_subject": False,
                "has_salutation": False,
                "paragraph_topics": [topic],
                "heading_levels": [],
                "list_kind": list_kind,
                "list_items": [] if list_kind == "none" else ["Inhalt bleibt unverändert."],
                "has_closing": False,
                "signature_lines": [],
            },
        },
        "risk_tags": tags,
        "source_class": "ai_generated",
        "private_data": False,
    }


def main() -> None:
    plans = [build_plan(seed) for seed in range(500000, 500300)]
    validator = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
    errors = [(plan["seed_id"], error.message) for plan in plans for error in validator.iter_errors(plan)]
    if errors:
        raise SystemExit(f"schema validation failed: {errors[0]}")
    payload = "".join(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for plan in plans
    )
    (OUT / "plans.jsonl").write_text(payload, encoding="utf-8", newline="\n")
    tag_counts = Counter(tag for plan in plans for tag in plan["risk_tags"])
    manifest = {
        "manifest_version": 1,
        "generator_agent": AGENT,
        "batch_id": BATCH,
        "domain": "hard_negatives",
        "record_count": len(plans),
        "seed_range": [500000, 500299],
        "prompt_hash": plans[0]["prompt_hash"],
        "plans_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "schema": "contracts/seed_plan_schema.json",
        "generation": {
            "deterministic": True,
            "local_only": True,
            "external_api_calls": 0,
            "contains_targets": False,
            "contains_critics": False,
            "contains_splits": False,
        },
        "schema_validation": {
            "validator": "jsonschema.Draft202012Validator",
            "valid_records": len(plans),
            "invalid_records": 0,
        },
        "risk_distribution": dict(sorted(tag_counts.items())),
        "quality_checks": {
            "unique_seed_ids": len({p["seed_id"] for p in plans}) == len(plans),
            "unique_seeds": len({p["seed"] for p in plans}) == len(plans),
            "private_data_records": 0,
            "critical_risk_coverage": sum(bool(p["risk_tags"]) for p in plans) / len(plans),
            "two_or_more_risk_coverage": sum(len(p["risk_tags"]) >= 2 for p in plans) / len(plans),
        },
        "diversity": {
            "document_type_counts": dict(sorted(Counter(p["document_type"] for p in plans).items())),
            "address_mode_counts": dict(sorted(Counter(p["address_mode"] for p in plans).items())),
            "scenario_topic_counts": dict(
                sorted(Counter(p["semantic_plan"]["structure"]["paragraph_topics"][0] for p in plans).items())
            ),
            "list_kind_counts": dict(
                sorted(Counter(p["semantic_plan"]["structure"]["list_kind"] for p in plans).items())
            ),
            "unique_people": len(PEOPLE),
            "unique_organizations": len(ORGS),
            "unique_products": len(PRODUCTS),
            "unique_locations": len(LOCATIONS),
        },
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


if __name__ == "__main__":
    main()
