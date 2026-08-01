"""Deterministic, wholly fictional tax/legal semantic-plan generator.

This module deliberately composes case narratives from independently authored
scenario, evidence, procedure and drafting banks.  It never calls a model,
network service, or reads private input.  The small citation catalogue is the
repository's verified statutory vocabulary, not legal advice.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts"
HERE = Path(__file__).resolve().parent
PROMPT = ROOT / "handoffs" / "AGENT_DATA_TAX_LEGAL.md"
sys.path.insert(0, str(ROOT / "src"))
from scriber_polishing.schemas import validate_seed_plan  # noqa: E402

AGENT = "generator_tax_legal_01"
BATCH = "tax_legal_batch_01"
PROMPT_HASH = "sha256:" + hashlib.sha256(PROMPT.read_bytes()).hexdigest()

# All people, organisations, locations and file references below are invented.
ORGS = [
    "Asterion Handels GmbH",
    "Nordlicht Maschinenbau GmbH",
    "Kometenwerk KG",
    "Silberhain Logistik GmbH",
    "Vektorraum Services GmbH",
    "Buchenring Technik GmbH",
    "Falkenbrück Projekt GmbH",
    "Morgenstern Bürobedarf KG",
    "Elbwiese Verpackung GmbH",
    "Wolkenpfad Energie GmbH",
    "Kieselgrund Vertrieb GmbH",
    "Sternwarte Anlagen GmbH",
]
OFFICES = [
    "Finanzamt Sonnenfeld",
    "Finanzamt Nordtor",
    "Finanzamt Flussbogen",
    "Finanzamt Höhenrain",
    "Finanzamt Lindenau",
    "Finanzamt Marktquell",
]
ADVISERS = [
    "Kanzlei Quader & Partner",
    "Steuerbüro Blaupunkt",
    "Kanzlei Elsterwinkel",
    "Beratungshaus Faktor",
    "Kanzlei Morgenrot",
    "Steuerkontor Seeblick",
]
PEOPLE = [
    "Mara Fink",
    "Jonas Klee",
    "Nora Voss",
    "Levin Berg",
    "Tessa Dorn",
    "Ravi Stein",
    "Alma Kern",
    "Milan Aue",
    "Kira Falk",
    "Noel Brand",
    "Vera Quell",
    "Tomke Ried",
]
CASE_KINDS = [
    (
        "objection_letter",
        "Einspruch gegen den Bescheid",
        "Einspruch",
        "Die Einwendungen werden fristwahrend vorgebracht.",
    ),
    (
        "audit_response",
        "Stellungnahme zur Außenprüfung",
        "Außenprüfung",
        "Die angeforderten Nachweise werden geordnet erläutert.",
    ),
    (
        "tax_office_request",
        "Anforderung von Unterlagen",
        "Unterlagenanforderung",
        "Die Unterlagen sollen vollständig und lesbar vorliegen.",
    ),
    (
        "assessment_note",
        "Prüfung des Steuerbescheids",
        "Bescheidprüfung",
        "Die Festsetzung wird ausschließlich rechnerisch nachvollzogen.",
    ),
    (
        "vat_correspondence",
        "Rückfrage zur Umsatzsteuer",
        "Umsatzsteuer",
        "Die Voranmeldung wird mit den Buchungsdaten abgeglichen.",
    ),
    (
        "trade_tax_memo",
        "Vermerk zur Gewerbesteuer",
        "Gewerbesteuer",
        "Die Hinzurechnung wird als Prüfpunkt festgehalten.",
    ),
    (
        "corporate_tax_note",
        "Vermerk zur Körperschaftsteuer",
        "Körperschaftsteuer",
        "Die Verlustnutzung bleibt ein offener Prüfungspunkt.",
    ),
    (
        "deadline_notice",
        "Fristbezogene Rückmeldung",
        "Frist",
        "Die Frist wird nur unter der genannten Bedingung erbeten.",
    ),
    ("payment_query", "Abstimmung einer Zahlung", "Zahlung", "Die Zahlung wird nicht als Anerkenntnis behandelt."),
    (
        "record_request",
        "Bitte um Belegzuordnung",
        "Belegzuordnung",
        "Die Zuordnung erfolgt anhand der fiktiven Belegliste.",
    ),
]
CITATIONS = [
    "§ 7 Absatz 4 Satz 2 EStG",
    "§ 8c Absatz 1 Satz 1 KStG",
    "§ 8 Nummer 1 Buchstabe a GewStG",
    "Artikel 3 Absatz 1 GG",
    "§§ 8c und 8d KStG",
    "§ 8d Absatz 1 KStG",
    "§ 7 Absatz 4 EStG",
    "§ 8 Nummer 1 GewStG",
]
TOPICS = [
    "Belegkette",
    "Frist und Eingang",
    "Bemessungsgrundlage",
    "Kontenabstimmung",
    "Vorsteuerabzug",
    "Verlustvortrag",
    "Mietaufwand",
    "Abschreibungsbasis",
    "Bescheidvergleich",
    "Rückfrage",
    "Zahlungsstand",
    "Aktennotiz",
]
EVIDENCE = [
    "Buchungsjournal",
    "Rechnungskopie",
    "Vertragsauszug",
    "Kontenblatt",
    "Zahlungsübersicht",
    "Anlagenverzeichnis",
    "Abstimmungsnotiz",
    "E-Mail-Ausdruck",
    "Leistungsnachweis",
    "Umsatzliste",
    "Saldenbestätigung",
    "Belegindex",
]
AMOUNTS = [
    "1.240,00 €",
    "3.875,50 €",
    "8.460,00 €",
    "12.900,00 €",
    "24.115,75 €",
    "39.800,00 €",
    "57.240,30 €",
    "86.500,00 €",
    "104.200,00 €",
    "218.750,00 €",
]
PERCENTS = ["7 %", "10 %", "19 %", "25 %", "40 %", "60 %", "75 %", "100 %"]
DATES = [
    "03.02.2026",
    "17.02.2026",
    "28.02.2026",
    "12.03.2026",
    "31.03.2026",
    "15.04.2026",
    "30.04.2026",
    "14.05.2026",
    "29.05.2026",
    "16.06.2026",
    "30.06.2026",
    "15.07.2026",
]
CONDITIONS = [
    "wenn die Belegkette vollständig bestätigt wird",
    "sofern die Zuordnung zum Wirtschaftsjahr unverändert bleibt",
    "falls die angeforderten Unterlagen fristgerecht eingehen",
    "wenn keine abweichende Feststellung dokumentiert ist",
    "soweit die Buchungslogik nachvollziehbar erläutert wird",
    "falls die Rückfrage nicht bereits beantwortet wurde",
]
HARD_NEGATIVES = [
    "Es wird keine steuerliche Würdigung beantragt.",
    "Die Unterlage enthält keine Zustimmung zu einer abweichenden Festsetzung.",
    "Es wird nicht bestätigt, dass ein Betrag doppelt berücksichtigt wurde.",
    "Ohne vollständigen Nachweis soll keine Schlussfolgerung gezogen werden.",
    "Die Fristverlängerung gilt nicht als Verzicht auf Rechtsbehelfe.",
    "Eine Zahlung erfolgt nicht als Anerkenntnis einer rechtlichen Bewertung.",
]
SIGNATURES = [
    "Fiktive Steuerabteilung",
    "Fiktive Rechtsbehelfsstelle",
    "Fiktive Buchhaltung",
    "Fiktives Prüfungsteam",
    "Fiktive Geschäftsleitung",
]


def entity(kind: str, value: str, fictional: bool) -> dict:
    # The batch boundary requires every protected plan entity to be synthetic.
    # ``fictional`` stays in the call signature so the authored banks remain
    # visually auditable, but fail closed to True for every emitted entity.
    del fictional
    return {"type": kind, "value": value, "fictional": True, "protected": True}


def fact(n: int, text: str, polarity: str, modality: str, condition: str | None, values: list[str]) -> dict:
    return {
        "fact_id": f"f{n}",
        "order": n,
        "text": text,
        "polarity": polarity,
        "modality": modality,
        "condition": condition,
        "protected_values": values,
    }


def make_plan(seed: int) -> dict:
    i = seed - 200000
    org, office, adviser, person = (
        ORGS[i % len(ORGS)],
        OFFICES[(i * 3) % len(OFFICES)],
        ADVISERS[(i * 5) % len(ADVISERS)],
        PEOPLE[(i * 7) % len(PEOPLE)],
    )
    doc, subject, focus, lead = CASE_KINDS[i % len(CASE_KINDS)]
    amount, percent, date = (
        AMOUNTS[(i * 7) % len(AMOUNTS)],
        PERCENTS[(i * 3) % len(PERCENTS)],
        DATES[(i * 11) % len(DATES)],
    )
    evidence, topic, condition = (
        EVIDENCE[(i * 13) % len(EVIDENCE)],
        TOPICS[(i * 17) % len(TOPICS)],
        CONDITIONS[(i * 19) % len(CONDITIONS)],
    )
    use_citation = i % 2 == 0 or i % 5 == 0
    citation = CITATIONS[(i * 7) % len(CITATIONS)] if use_citation else None
    negative = HARD_NEGATIVES[(i * 5) % len(HARD_NEGATIVES)]
    facts = [
        fact(
            1,
            f"{lead} Die fiktive {org} bezieht sich auf die Mitteilung des {office} vom {date}.",
            "positive",
            "fact",
            None,
            [org, office, date],
        ),
        fact(
            2,
            f"Der Betrag von {amount} ist im {evidence} ausgewiesen und wird mit einem Anteil von {percent} abgegrenzt.",
            "positive",
            "fact",
            condition,
            [amount, evidence, percent],
        ),
        fact(
            3,
            f"{person} von {adviser} soll die Erläuterung zu {topic} bis zum {DATES[(i + 4) % len(DATES)]} übermitteln.",
            "positive",
            "obligation",
            condition,
            [person, adviser, topic, DATES[(i + 4) % len(DATES)]],
        ),
    ]
    entities = [
        entity("organization", org, True),
        entity("organization", office, True),
        entity("organization", adviser, True),
        entity("person", person, True),
        entity("technical_term", evidence, True),
    ]
    tags = ["synthetic", "tax_legal", "amount", "percentage", "date", "condition"]
    if citation:
        facts.append(
            fact(
                4,
                f"Das Zitat {citation} wird ausschließlich als wörtliche Referenz im Vorgang geführt.",
                "positive",
                "uncertainty",
                "ohne inhaltliche Bewertung",
                [citation],
            )
        )
        entities.append(entity("legal_term", citation, False))
        tags.extend(["legal_citation", "legal_reference"])
    else:
        facts.append(fact(4, negative, "negative", "recommendation", "wenn Unterlagen fehlen", []))
        tags.append("legal_hard_negative")
    if i % 3 == 0:
        facts.append(
            fact(
                len(facts) + 1,
                f"Es ist nicht ausgeschlossen, dass die Differenz nach Eingang des {evidence} erneut abgeglichen werden muss.",
                "double_negative",
                "possibility",
                condition,
                [evidence],
            )
        )
        tags.append("negation")
    if i % 4 == 0:
        facts.append(
            fact(
                len(facts) + 1,
                "Die Rückmeldung kann nach Prüfung der Unterlagen erfolgen; eine Vorfestlegung wird nicht getroffen.",
                "negative",
                "possibility",
                "nach vollständiger Prüfung",
                [],
            )
        )
        tags.append("modality")
    list_kind = ["none", "ordered", "unordered", "nested"][i % 4]
    list_items = (
        []
        if list_kind == "none"
        else [f"{evidence} zum Betrag von {amount}", f"Rückmeldung bis {date}", f"Prüfpunkt {topic}"]
    )
    formal = i % 5 != 0
    structure = {
        "has_subject": True,
        "has_salutation": formal,
        "paragraph_topics": [focus, topic, "Frist und Nachweis"],
        "heading_levels": (["h1", "h2"] if i % 6 == 0 else (["h1"] if i % 2 == 0 else [])),
        "list_kind": list_kind,
        "list_items": list_items,
        "has_closing": formal,
        "signature_lines": ([person, SIGNATURES[i % len(SIGNATURES)]] if formal else []),
    }
    return {
        "schema_version": 1,
        "seed_id": f"tax_legal_{seed}",
        "generator_agent": AGENT,
        "batch_id": BATCH,
        "prompt_hash": PROMPT_HASH,
        "seed": seed,
        "language": "de-DE",
        "domain": "tax_legal",
        "document_type": doc,
        "style_profile": "duden_preferred_business",
        "address_mode": "formal_sie" if formal else "neutral_third_person",
        "legal_citation_style": "long",
        "unit_style": "professional_symbols",
        "semantic_plan": {"facts": facts, "entities": entities, "structure": structure},
        "risk_tags": sorted(set(tags)),
        "source_class": "ai_generated",
        "private_data": False,
    }


def main() -> None:
    plans = [make_plan(seed) for seed in range(200000, 200300)]
    for plan in plans:
        validate_seed_plan(plan)
    if len({p["seed_id"] for p in plans}) != 300 or len({p["seed"] for p in plans}) != 300:
        raise ValueError("seed identities are not unique")
    lines = "".join(json.dumps(p, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for p in plans)
    (HERE / "plans.jsonl").write_text(lines, encoding="utf-8", newline="\n")
    legal = [p for p in plans if "legal_citation" in p["risk_tags"] or "legal_hard_negative" in p["risk_tags"]]
    citations = Counter(e["value"] for p in plans for e in p["semantic_plan"]["entities"] if e["type"] == "legal_term")
    manifest = {
        "manifest_version": 1,
        "generator_agent": AGENT,
        "batch_id": BATCH,
        "generator_version": "1.0.1",
        "source_class": "ai_generated",
        "private_data": False,
        "record_count": len(plans),
        "plan_count": len(plans),
        "seed_range": [200000, 200299],
        "prompt_hash": PROMPT_HASH,
        "plans_sha256": hashlib.sha256(lines.encode("utf-8")).hexdigest(),
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "schema_validation": {
            "validator": "scriber_polishing.schemas.validate_seed_plan",
            "validated_count": len(plans),
            "status": "passed",
        },
        "diversity": {
            "document_type_counts": dict(sorted(Counter(p["document_type"] for p in plans).items())),
            "address_mode_counts": dict(sorted(Counter(p["address_mode"] for p in plans).items())),
            "list_kind_counts": dict(
                sorted(Counter(p["semantic_plan"]["structure"]["list_kind"] for p in plans).items())
            ),
            "unique_fact_texts": len({f["text"] for p in plans for f in p["semantic_plan"]["facts"]}),
            "legal_or_hard_negative_count": len(legal),
            "legal_or_hard_negative_ratio": round(len(legal) / len(plans), 6),
        },
        "norm_distribution": dict(sorted(citations.items())),
        "official_law_abbreviations": ["EStG", "KStG", "GewStG", "GG"],
    }
    (HERE / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    print(
        json.dumps(
            {
                "plans": len(plans),
                "validated": len(plans),
                "legal_or_hard_negative": len(legal),
                "sha256": manifest["plans_sha256"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
