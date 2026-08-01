"""Deterministic coverage-completion semantic plans (AI-only, local)."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from jsonschema import Draft202012Validator

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SCHEMA = ROOT / "contracts" / "seed_plan_schema.json"
HANDOFF = HERE / "handoff.md"
OUT = HERE / "plans.jsonl"
MANIFEST = HERE / "manifest.json"
REPORT = HERE / "report.json"
AGENT = "generator_coverage_completion_01"
BATCH = "coverage_completion_batch_01"
PROMPT_HASH = "sha256:" + hashlib.sha256(HANDOFF.read_bytes()).hexdigest()
CREATED = "2026-07-30T00:00:00Z"

PEOPLE = ("Mara Linden", "Jonas Falk", "Elin Werner", "Timo Berg", "Sina Körner", "Lea Dorn")
ORGS = ("Nordlicht Quartier GmbH", "Auenwerk Energie KG", "Kernlinie Projekt GmbH", "Feldrain Verwaltung GmbH")
PLACES = ("Quartier Lindenhof", "Gewerbepark Auenfeld", "Hafenhof Nord", "Campus Wiesenring")
DOCS = ("Prüfnotiz", "Projektstatus", "Kundeninformation", "Freigabeprotokoll", "Abstimmungsbrief", "Übergabevermerk")
COMPOUND_UNITS = ("m³/h", "km/h", "m/s", "Mbit/s")
SPELLING = (
    ("das", "dass", "Bitte prüfen Sie das Protokoll, das Elin Werner erstellt hat, bevor Sie bestätigen, dass der Inhalt vollständig ist."),
    ("Seit", "seid", "Seit Dienstag seid Ihr für die Freigabe verantwortlich; Jonas Falk dokumentiert den Eingang."),
    ("wider", "wieder", "Der Einwand richtet sich wider die Annahme, dass die Frist wieder verlängert wird."),
    ("Maße", "Masse", "Die Maße des Bauteils bleiben geschützt; seine Masse wird separat nachgewiesen."),
    ("Euer", "Euch", "Ihr sendet Euer Ergebnis an Lea Dorn, damit sie Euch den geprüften Stand zurückmeldet."),
)


def entity(kind: str, value: str) -> dict:
    return {"type": kind, "value": value, "fictional": True, "protected": True}


def fact(number: int, text: str, protected: tuple[str, ...] | list[str], *, modality: str = "fact", polarity: str = "positive", condition: str | None = None) -> dict:
    return {"fact_id": f"f{number}", "order": number, "text": text, "polarity": polarity, "modality": modality, "condition": condition, "protected_values": list(protected)}


def structure(i: int, topics: list[str], *, direct: bool = False, no_heading: bool = False) -> dict:
    kind = ("none", "ordered", "unordered", "nested")[i % 4]
    result = {
        "has_subject": i % 3 != 0,
        "has_salutation": direct,
        "paragraph_topics": topics,
        "heading_levels": [] if no_heading or i % 5 else ["h1"],
        "list_kind": kind,
        "list_items": [] if kind == "none" else ["Sachstand sichern", "Rückmeldung dokumentieren"],
        "has_closing": direct and i % 3 != 0,
        "signature_lines": [PEOPLE[i % len(PEOPLE)], ORGS[i % len(ORGS)]] if direct and i % 3 != 0 else [],
    }
    if i % 7 == 0:
        result.update({"has_quote": True, "quote_text": "Die Freigabe gilt nur für den dokumentierten Wortlaut."})
    if i % 11 == 0:
        result.update({"has_attachments": True, "attachment_lines": ["Anlage: fiktive Prüfübersicht"]})
    if i % 13 == 0:
        result.update({"has_post_script": True, "post_script_text": "PS: Bitte keine zusätzlichen Inhalte ergänzen."})
    return result


def plan(seed: int, domain: str, document_type: str, address_mode: str, facts: list[dict], entities: list[dict], doc_structure: dict, tags: list[str]) -> dict:
    return {
        "schema_version": 1,
        "seed_id": f"covcomp_{seed}",
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
        "semantic_plan": {"facts": facts, "entities": entities, "structure": doc_structure},
        "risk_tags": sorted(set(tags)),
        "source_class": "ai_generated",
        "private_data": False,
    }


def energy_plan(i: int) -> dict:
    seed = 900000 + i
    person, org, place = PEOPLE[i % 6], ORGS[i % 4], PLACES[i % 4]
    volume = f"{120 + i} m³" if i % 2 else f"{840 + i * 3} m²"
    compound = COMPOUND_UNITS[i % len(COMPOUND_UNITS)]
    compound_value = (f"{260 + i} {compound}" if compound == "m³/h" else f"{30 + i % 40} {compound}")
    facts = [
        fact(1, f"Für {place} weist {org} eine Nutzfläche beziehungsweise ein Volumen von {volume} aus.", (place, org, volume)),
        fact(2, f"Der kalkulierte Mietansatz beträgt {18 + i % 17} €/m² und wird erst nach der Freigabe durch {person} veröffentlicht.", (f"{18 + i % 17} €/m²", person), modality="obligation"),
        fact(3, f"Der spezifische Energiekennwert liegt bei {58 + i % 39} kWh/(m²·a); die Messung verwendet zusätzlich den Referenzwert {compound_value}.", (f"{58 + i % 39} kWh/(m²·a)", compound_value)),
        fact(4, f"Vorgang EN-{seed} bleibt auf den dokumentierten Stand begrenzt und enthält keine Schätzung außerhalb des geprüften Gebäudeteils.", (f"EN-{seed}",), polarity="negative", modality="obligation"),
    ]
    return plan(seed, "real_estate_energy", DOCS[i % len(DOCS)], "formal_sie", facts, [entity("person", person), entity("organization", org), entity("location", place)], structure(i, ["Objektkennzahl", "Mietansatz", "Energiekennwert", "Freigabe"]), ["real_estate_energy", "positive_unit_symbols", "unit_m2_or_m3", "unit_eur_per_m2", "unit_kwh_per_m2_a", "unit_compound", "protected_units"])


def ambiguous_unit_plan(i: int) -> dict:
    seed = 900100 + i
    person, org = PEOPLE[i % 6], ORGS[i % 4]
    cases = (
        ("m zwei", "Die Produktbezeichnung m zwei ist ein interner Name und keine Flächenangabe."),
        ("18 K", "Die Kennung 18 K bezeichnet eine Prüfstufe und keine Temperaturangabe."),
        ("pro Jahr", "Die Formulierung pro Jahr steht im historischen Titel und bleibt ohne Einheit unverändert."),
        ("M-2", "Das Modell M-2 ist eine Serienbezeichnung und darf nicht als Maß gelesen werden."),
        ("Q18K", "Der interne Identifikator Q18K bleibt als Zeichenfolge unverändert."),
    )
    token, sentence = cases[i % len(cases)]
    facts = [
        fact(1, sentence, (token,), modality="obligation"),
        fact(2, f"{person} bestätigt für Referenz UN-{seed}, dass {token} in {org} nicht normalisiert oder in ein Symbol umgeschrieben wird.", (person, f"UN-{seed}", token, org), modality="obligation", polarity="negative"),
        fact(3, "Der Wortlaut bleibt wegen seiner kontextgebundenen Bedeutung unverändert; eine technische Einheit ist nicht belegt.", ("unverändert",), modality="obligation"),
    ]
    return plan(seed, "hard_negatives", "Ambiguitätsvermerk", "ambiguous", facts, [entity("person", person), entity("organization", org)], structure(i, ["Ambige Zeichenfolge", "Kontextschutz", "Freigabehinweis"]), ["hard_negative", "ambiguous_unit_hard_negative", "no_normalization", "meaning_dependent_hard_negative", "protected_surface"])


def legal_plan(i: int) -> dict:
    seed = 900150 + i
    person, org = PEOPLE[i % 6], ORGS[i % 4]
    citation = f"§ {12 + i % 5} Absatz {1 + i % 3} Satz {1 + i % 2} des fiktiven Quartiersvertrags"
    facts = [
        fact(1, "Die Prüfung berücksichtigt Artikel 3 Absatz 1 GG als geschützte Rangstufe der Begründung.", ("Artikel 3 Absatz 1 GG",), modality="obligation"),
        fact(2, f"Für die interne Vertragslogik wird {citation} wörtlich und mit Paragraph, Absatz und Satz beibehalten.", (citation,), modality="obligation"),
        fact(3, "Die steuerliche Einordnung nennt § 8 Nummer 1 Buchstabe a GewStG ohne Verkürzung der Hierarchie.", ("§ 8 Nummer 1 Buchstabe a GewStG",), modality="obligation"),
        fact(4, "Die Verlustregelung verweist getrennt auf §§ 8c und 8d KStG; beide Paragraphen bleiben als gekoppelte Zitierung erhalten.", ("§§ 8c und 8d KStG",), modality="obligation"),
        fact(5, f"{person} gibt die fiktive Prüfung von {org} erst frei, wenn keine Rechtsquelle ergänzt oder umgedeutet wurde.", (person, org), modality="obligation", polarity="negative"),
    ]
    return plan(seed, "tax_legal", "Rechts- und Steuervermerk", "formal_sie", facts, [entity("person", person), entity("organization", org), entity("legal_term", "Artikel 3 Absatz 1 GG"), entity("legal_term", "§ 8 Nummer 1 Buchstabe a GewStG"), entity("legal_term", "§§ 8c und 8d KStG")], structure(i, ["Verfassungsbezug", "Vertragsnorm", "Gewerbesteuer", "Körperschaftsteuer", "Freigabe"]), ["legal_citation", "tax_legal", "legal_hierarchy", "protected_citation", "legal_precision_risk"])


def pronoun_plan(i: int) -> dict:
    seed = 900200 + i
    person, colleague, org = PEOPLE[i % 6], PEOPLE[(i + 1) % 6], ORGS[i % 4]
    a, b, contrast = SPELLING[i % len(SPELLING)]
    hard = i < 25
    facts = [
        fact(1, f"Bitte gebt Ihr Euer Prüfergebnis an {person}, damit er Euch den abgestimmten Stand von {org} zurücksendet.", ("Ihr", "Euer", person, "er", "Euch", org), modality="recommendation"),
        fact(2, contrast, (a, b), modality="obligation" if hard else "fact", polarity="negative" if hard else "positive"),
        fact(3, f"{colleague} hält fest, dass sie den Wortlaut nicht durch eine gleich klingende, aber anders bedeutende Form ersetzt.", (colleague, "sie", "dass"), modality="obligation", polarity="negative"),
    ]
    tags = ["orthography_pronouns", "ihr_euch_euer", "direct_address", "third_person_reference", "protected_surface"]
    if hard:
        tags += ["hard_negative", "meaning_dependent_hard_negative", "no_normalization"]
    else:
        tags += ["spelling_contrast", "contextual_normalization"]
    return plan(seed, "hard_negatives" if hard else "general_business", "Adress- und Wortlautprüfung", "mixed_correspondence", facts, [entity("person", person), entity("person", colleague), entity("organization", org)], structure(i, ["Adressierung", "Bedeutungskontrast", "Dokumentation"], direct=True), tags)


def formatting_plan(i: int) -> dict:
    seed = 900250 + i
    person, org, place = PEOPLE[i % 6], ORGS[i % 4], PLACES[i % 4]
    word = ("Überschrift", "Absatz", "Punkt", "Betreff")[i % 4]
    facts = [
        fact(1, f"Der Begriff {word} wird im Fließtext als gewöhnliches Sachwort verwendet und ist keine Anweisung zur Formatänderung.", (word,), modality="obligation", polarity="negative"),
        fact(2, f"Für {place} dokumentiert {org} den Fortschritt in vier getrennten Themen ohne zusätzliche Überschrift.", (place, org, "ohne zusätzliche Überschrift"), modality="obligation"),
        fact(3, f"{person} bestätigt den Betreff als Inhalt des Vorgangs FT-{seed}; daraus darf keine neue Betreffzeile erzeugt werden.", (person, "Betreff", f"FT-{seed}"), modality="obligation", polarity="negative"),
        fact(4, "Die Absätze bleiben in der vorgegebenen Reihenfolge zu Anlass, Entscheidung, Termin und Rückmeldung; kein Punkt wird in eine Liste umgedeutet.", ("Absätze", "Anlass", "Entscheidung", "Termin", "Rückmeldung", "Punkt"), modality="obligation"),
    ]
    return plan(seed, "hard_negatives", "Vier-Themen-Geschäftsvermerk", "neutral_third_person", facts, [entity("person", person), entity("organization", org), entity("location", place)], structure(i, ["Anlass", "Entscheidung", "Termin", "Rückmeldung"], no_heading=True), ["formatting", "hard_negative", "no_heading", "do_not_invent_heading", "ordinary_format_word", "no_normalization", "four_theme_document"])


def coverage(plans: list[dict]) -> dict:
    tags = [set(item["risk_tags"]) for item in plans]
    facts = [fact_item for item in plans for fact_item in item["semantic_plan"]["facts"]]
    def contains(value: str) -> int:
        return sum(any(value in fact_item["text"] for fact_item in item["semantic_plan"]["facts"]) for item in plans)
    return {
        "domain_counts": dict(sorted(Counter(item["domain"] for item in plans).items())),
        "energy_positive": sum("positive_unit_symbols" in item for item in tags),
        "ambiguous_unit_hard_negative": sum("ambiguous_unit_hard_negative" in item for item in tags),
        "dense_legal": sum("legal_citation" in item and "tax_legal" in item for item in tags),
        "pronoun_contrast": sum("orthography_pronouns" in item for item in tags),
        "pronoun_meaning_hard_negative": sum("meaning_dependent_hard_negative" in item and "orthography_pronouns" in item for item in tags),
        "four_theme_no_heading": sum("four_theme_document" in item and "no_heading" in item for item in tags),
        "m2_or_m3": contains("m²") + sum("m³" in fact_item["text"] and "m²" not in fact_item["text"] for fact_item in facts),
        "eur_per_m2": contains("€/m²"), "kwh_per_m2_a": contains("kWh/(m²·a)"),
        "compound_unit": sum(any(unit in fact_item["text"] for fact_item in facts) for unit in COMPOUND_UNITS),
        "legal_article": contains("Artikel 3 Absatz 1 GG"),
        "legal_gewerbesteuer": contains("§ 8 Nummer 1 Buchstabe a GewStG"),
        "legal_koerperschaftsteuer": contains("§§ 8c und 8d KStG"),
        "unique_fact_fingerprints": len({hashlib.sha256(json.dumps(item["semantic_plan"]["facts"], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest() for item in plans}),
    }


def validate(plans: list[dict]) -> None:
    validator = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
    errors = [error for item in plans for error in validator.iter_errors(item)]
    if errors:
        raise AssertionError(errors[0].message)
    assert len(plans) == 300
    assert [item["seed"] for item in plans] == list(range(900000, 900300))
    assert len({item["seed_id"] for item in plans}) == 300
    assert all(item["private_data"] is False and item["source_class"] == "ai_generated" for item in plans)
    assert all(value in item_fact["text"] for item in plans for item_fact in item["semantic_plan"]["facts"] for value in item_fact["protected_values"])
    result = coverage(plans)
    assert result["domain_counts"] == {"general_business": 25, "hard_negatives": 125, "real_estate_energy": 100, "tax_legal": 50}
    assert result["energy_positive"] == 100 and result["ambiguous_unit_hard_negative"] == 50
    assert result["dense_legal"] == 50 and result["pronoun_contrast"] == 50
    assert result["pronoun_meaning_hard_negative"] >= 25 and result["four_theme_no_heading"] == 50
    assert result["m2_or_m3"] >= 100 and result["eur_per_m2"] == 100 and result["kwh_per_m2_a"] == 100 and result["compound_unit"] == 4
    assert result["legal_article"] == result["legal_gewerbesteuer"] == result["legal_koerperschaftsteuer"] == 50
    assert result["unique_fact_fingerprints"] == 300
    for item in plans[100:150]:
        text = " ".join(fact_item["text"] for fact_item in item["semantic_plan"]["facts"])
        assert not any(symbol in text for symbol in ("€/m²", "kWh/(m²·a)", "m²", "m³/h", "km/h", "m/s", "Mbit/s"))


def render(plans: list[dict]) -> str:
    return "".join(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for item in plans)


def main() -> None:
    plans = [energy_plan(i) for i in range(100)]
    plans += [ambiguous_unit_plan(i) for i in range(50)]
    plans += [legal_plan(i) for i in range(50)]
    plans += [pronoun_plan(i) for i in range(50)]
    plans += [formatting_plan(i) for i in range(50)]
    validate(plans)
    payload = render(plans)
    OUT.write_text(payload, encoding="utf-8", newline="\n")
    serialized = [json.loads(line) for line in OUT.read_text(encoding="utf-8").splitlines()]
    validate(serialized)
    assert render(serialized).encode("utf-8") == payload.encode("utf-8")
    checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    summary = coverage(plans)
    report = {"record_count": 300, "plans_sha256": checksum, "coverage_counts": summary, "validation": {"in_memory": "passed", "serialized": "passed", "byte_identical_regeneration": "passed", "private_data_records": 0, "external_or_model_api_calls": 0}}
    REPORT.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
    manifest = {"manifest_version": 1, "generator_agent": AGENT, "batch_id": BATCH, "generator_revision": 1, "record_count": 300, "seed_range": [900000, 900299], "prompt_hash": PROMPT_HASH, "plans_sha256": checksum, "creation_timestamp": CREATED, "coverage_counts": summary, "schema_validation": {"first_pass_valid": 300, "second_pass_valid": 300, "validator": "jsonschema.Draft202012Validator"}, "generation_boundary": {"semantic_plans_only": True, "targets_generated": False, "splits_generated": False, "corruptions_generated": False, "critic_verdicts_generated": False, "external_or_model_api_calls": 0, "private_data_records": 0, "all_entities_fictional_and_protected": True}}
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
