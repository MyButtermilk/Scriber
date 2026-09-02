"""Small, dependency-free product safety layer for local SST generation."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from itertools import pairwise
from pathlib import Path


class SafetyError(ValueError):
    """Model output cannot be proven safe enough to return."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ProtectedTranscript:
    text: str
    markers: tuple[str, ...]
    values: tuple[str, ...]


_AMOUNT = re.compile(
    r"(?<!\w)(?:(?:EUR|USD|CHF|€|\$|£)\s*[-+]?\d[\d.]*(?:,\d+)?|"
    r"[-+]?\d[\d.]*(?:,\d+)?\s*(?:EUR|USD|CHF|€|\$|£))(?!\w)",
    re.IGNORECASE,
)
_LEGAL_REFERENCE = re.compile(
    r"(?<!\w)(?:§§?\s*\d+[a-zA-Z]*(?:\s+(?:Abs\.\s*\d+|Satz\s*\d+|Nr\.\s*\d+))*"
    r"|(?:Art\.|Artikel)\s*\d+[a-zA-Z]*|ECLI:[A-Z]{2,}:[A-Z0-9]+(?::[A-Z0-9.]+)+)",
)
_NORM = re.compile(r"(?<!\w)(?:(?:DIN(?:\s+EN)?|ISO|IEC|VDE)\s+[A-Z0-9]+(?:[-:/.][A-Z0-9]+)*)(?!\w)", re.IGNORECASE)
_CRITICAL_LITERAL = re.compile(
    r"(?:https?://[^\s,]+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+|"
    r"\b(?:DE\d{20}|[A-Z]{2}\d{2}(?:\s?[A-Z0-9]){10,30})\b|"
    r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b)"
)
_NUMBER = re.compile(r"[-+]?\d+(?:[.,:/-]\d+)*")
_POLARITY = re.compile(r"\b(?:nicht|nichts|kein(?:e|en|em|er|es)?|nie|niemals|weder|ohne)\b", re.IGNORECASE)
_MODALITY = re.compile(
    r"\b(?:muss|musst|müsst|müssen|müsste|kann|kannst|könnt|können|könnte|"
    r"darf|darfst|dürft|dürfen|soll|sollst|sollt|sollen|wird|wirst|werdet|werden|würde|würden)\b",
    re.IGNORECASE,
)
_MARKER = re.compile(r"⟦KEEP_(?:[A-Z]|A[A-F])⟧")
_TAG = re.compile(r"\[(/?)([A-Z0-9_]+)\]")
_TEXT_TAGS = frozenset({"SUBJECT", "H1", "H2", "SALUTATION", "DATE_LINE", "P", "QUOTE", "CLOSING", "ATTACHMENTS", "PS"})
_INLINE_TAGS = frozenset({"B", "U", "BU"})
_PLAIN_CONTROL_MARKUP = re.compile(
    r"(?:"
    r"<\|[^>\r\n]{1,128}\|>|<start_of_turn>|<end_of_turn>|"
    r"<s>|</s>|<unk>|\[PAD\]|\[BOS\]|\[EOS\]|\[INST\]|\[/INST\]|<<SYS>>|<</SYS>>|"
    r"\$\{transcript\}|"
    r"<\s*/?\s*(?:assistant|system|user|response|output|html|body|pre|div|p|span|section|article)"
    r"\b[^>\r\n]{0,256}>|"
    r"\[/?(?:DOC|SUBJECT|H1|H2|SALUTATION|DATE_LINE|P|QUOTE|CLOSING|ATTACHMENTS|PS|"
    r"OL|UL|LI[123]|SIGNATURE|LINE|B|U|BU)\]|\[BR/\]"
    r")",
    re.IGNORECASE,
)
_PLAIN_WRAPPER_LABEL = re.compile(
    r"^\s*(?:>\s*)?(?:(?:[-*+]|\d+[.)])\s+)?(?:#{1,6}\s*)?(?:\*\*|__)?"
    r"(?:bereinigte\s+fassung|bereinigter\s+text|ausgabe|antwort|ergebnis|resultat|result|output|"
    r"cleaned\s+(?:transcript|text)|polished\s+(?:transcript|text)|assistant|system|user|transkript)"
    r"(?:\*\*|__)?\s*:",
    re.IGNORECASE | re.MULTILINE,
)
_PLAIN_BRACKET_LABEL = re.compile(
    r"^\s*\[(?:bereinigte\s+fassung|bereinigter\s+text|ausgabe|antwort|ergebnis|resultat|result|output|"
    r"cleaned\s+(?:transcript|text)|polished\s+(?:transcript|text))\]\s+",
    re.IGNORECASE,
)
_PLAIN_BOILERPLATE = re.compile(
    r"^\s*(?:(?:hier|nachfolgend)\s+(?:ist|folgt)\s+"
    r"(?:dein(?:e|er|en|es)?|ihr(?:e|er|en|es)?|der|die|das)\s+"
    r"(?:bereinigte(?:r|n|s)?\s+(?:fassung|text)|ergebnis)|"
    r"here\s+is\s+(?:your|the)\s+(?:cleaned|polished)\s+(?:transcript|text)|"
    r"(?:the|your)\s+(?:cleaned|polished)\s+(?:transcript|text)\s+(?:is|follows)\s*:?)\b",
    re.IGNORECASE,
)
_PLAIN_OUTER_XML = re.compile(
    r"^\s*<([A-Z][A-Z0-9_.:-]{0,63})(?:\s+[^>\r\n]{1,256})?>[\s\S]*</\1>\s*$",
    re.IGNORECASE,
)
_PLAIN_OUTER_QUOTES = (
    ('"', '"'),
    ("'", "'"),
    ("`", "`"),
    ("**", "**"),
    ("__", "__"),
    ("*", "*"),
    ("_", "_"),
    ("~~", "~~"),
    ("„", "“"),
    ("“", "”"),
    ("‚", "‘"),
    ("«", "»"),
    ("‹", "›"),
)
_WORD = re.compile(r"\w+", re.UNICODE)
_NUMERIC_OR_WORD = re.compile(r"[-+]?\d+(?:[.,:/-]\d+)*|[^\W\d_]+", re.UNICODE)
_GERMAN_CARDINALS = {
    "null": 0,
    "ein": 1,
    "eins": 1,
    "eine": 1,
    "einen": 1,
    "einem": 1,
    "einer": 1,
    "zwei": 2,
    "drei": 3,
    "vier": 4,
    "fünf": 5,
    "sechs": 6,
    "sieben": 7,
    "acht": 8,
    "neun": 9,
    "zehn": 10,
    "elf": 11,
    "zwölf": 12,
    "dreizehn": 13,
    "vierzehn": 14,
    "fünfzehn": 15,
    "sechzehn": 16,
    "siebzehn": 17,
    "achtzehn": 18,
    "neunzehn": 19,
    "zwanzig": 20,
    "dreissig": 30,
    "vierzig": 40,
    "fünfzig": 50,
    "sechzig": 60,
    "siebzig": 70,
    "achtzig": 80,
    "neunzig": 90,
    "hundert": 100,
    "tausend": 1000,
    "million": 1_000_000,
    "millionen": 1_000_000,
}
_GERMAN_ORDINALS = {
    form: value
    for value, forms in {
        1: ("erste", "erster", "ersten", "erstem", "erstes"),
        2: ("zweite", "zweiter", "zweiten", "zweitem", "zweites"),
        3: ("dritte", "dritter", "dritten", "drittem", "drittes"),
        4: ("vierte", "vierter", "vierten", "viertem", "viertes"),
        5: ("fünfte", "fünfter", "fünften", "fünftem", "fünftes"),
        6: ("sechste", "sechster", "sechsten", "sechstem", "sechstes"),
        7: ("siebte", "siebter", "siebten", "siebtem", "siebtes"),
        8: ("achte", "achter", "achten", "achtem", "achtes"),
        9: ("neunte", "neunter", "neunten", "neuntem", "neuntes"),
        10: ("zehnte", "zehnter", "zehnten", "zehntem", "zehntes"),
        11: ("elfte", "elfter", "elften", "elftem", "elftes"),
        12: ("zwölfte", "zwölfter", "zwölften", "zwölftem", "zwölftes"),
        13: ("dreizehnte", "dreizehnter", "dreizehnten"),
        14: ("vierzehnte", "vierzehnter", "vierzehnten"),
        15: ("fünfzehnte", "fünfzehnter", "fünfzehnten"),
        16: ("sechzehnte", "sechzehnter", "sechzehnten"),
        17: ("siebzehnte", "siebzehnter", "siebzehnten"),
        18: ("achtzehnte", "achtzehnter", "achtzehnten"),
        19: ("neunzehnte", "neunzehnter", "neunzehnten"),
        20: ("zwanzigste", "zwanzigster", "zwanzigsten"),
    }.items()
    for form in forms
}
_GERMAN_MONTHS = {
    "januar": 1,
    "februar": 2,
    "märz": 3,
    "maerz": 3,
    "april": 4,
    "mai": 5,
    "juni": 6,
    "juli": 7,
    "august": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "dezember": 12,
}
_YEAR_CONTEXT_WORDS = frozenset({"jahr", "jahre", "jahren", "jahreszahl", "kalenderjahr"})
_YEAR_CONTEXT_CONNECTORS = frozenset({"ist", "war", "wird", "bleibt", "lautet"})
# One ordered alternation is intentional.  The regular-expression engine picks
# the first alternative at one position, so a compound such as kWh/m²a or €/m²
# can never also contribute the overlapping kWh, m², €, or m anchors.  This is
# materially different from applying a list of independent findall calls.
_UNIT = re.compile(
    r"(?P<mbit_s>(?<!\w)(?:mbit\s*/\s*s|megabit\s+pro\s+sekunde)(?!\w))|"
    r"(?P<l_min>(?<!\w)(?:l\s*/\s*min|liter\s+pro\s+minute)(?!\w))|"
    r"(?P<l_m2>(?<!\w)(?:l\s*/\s*m(?:²|\^2)|liter\s+pro\s+quadratmeter)(?!\w))|"
    r"(?P<kwh_m2a>(?<!\w)(?:kwh\s*/\s*(?:m(?:²|\^2)\s*a|\(\s*m(?:²|\^2)\s*[·*]\s*a\s*\))|"
    r"kilowattstunden?\s+pro\s+quadratmeter\s+und\s+jahr)(?!\w))|"
    r"(?P<eur_m2>(?<!\w)(?:€\s*/\s*m(?:²|\^2)|(?:euro|eur)\s+pro\s+quadratmeter)(?!\w))|"
    r"(?P<km_h>(?<!\w)(?:km\s*/\s*h|kilometer\s+pro\s+stunde)(?!\w))|"
    r"(?P<kwp>(?<!\w)(?:kwp|kilowatt\s+peak)(?!\w))|"
    r"(?P<mwh>(?<!\w)(?:mwh|megawattstunden?)(?!\w))|"
    r"(?P<nm>(?<!\w)(?:nm|newtonmeter)(?!\w))|"
    r"(?P<mb>(?<!\w)(?:mb|megabyte)(?!\w))|"
    r"(?P<gb>(?<!\w)(?:gb|gigabyte)(?!\w))|"
    r"(?P<kw>(?<!\w)(?:kw|kilowatt)(?!\w))|"
    r"(?P<km>(?<!\w)(?:km|kilometer)(?!\w))|"
    r"(?P<mm>(?<!\w)(?:mm|millimeter)(?!\w))|"
    r"(?P<tonne>(?<!\w)(?:t|tonnen?)(?!\w))|"
    r"(?P<liter>(?<!\w)(?:l|liter)(?!\w))|"
    r"(?P<m2>(?<!\w)(?:m(?:²|\^2)|quadratmeter)(?!\w))|"
    r"(?P<m3>(?<!\w)(?:m(?:³|\^3)|kubikmeter)(?!\w))|"
    r"(?P<kwh>(?<!\w)(?:kwh|kilowattstunden?)(?!\w))|"
    r"(?P<celsius>(?<!\w)(?:°\s*c|grad\s+celsius)(?!\w))|"
    r"(?P<percent>(?<!\w)(?:%|prozent)(?!\w))|"
    r"(?P<eur>(?<!\w)(?:€|eur|euro)(?!\w))|"
    r"(?P<kg>(?<!\w)(?:kg|kilogramm)(?!\w))|"
    r"(?P<cm>(?<!\w)(?:cm|zentimeter)(?!\w))|"
    r"(?P<m>(?<!\w)(?:m|meter)(?!\w))|"
    r"(?P<time>(?<!\w)uhr(?!\w))",
    re.IGNORECASE,
)
_ECLI_REFERENCE = re.compile(r"(?<!\w)ECLI:[A-Z]{2,}:[A-Z0-9]+(?::[A-Z0-9.]+)+")
_LEGAL_REFERENCE_PREFIX = re.compile(r"(?<!\w)(?P<kind>§§?|Art\.|Artikel)(?!\w)", re.IGNORECASE)
_LEGAL_REFERENCE_PART = re.compile(
    r"[\s,;]*(?P<label>Abs\.|Absatz|S\.|Satz|Nr\.|Nummer|lit\.|Buchstabe)\s*",
    re.IGNORECASE,
)
_UNIT_CANONICAL = {
    "mbit_s": "mbit/s",
    "l_min": "l/min",
    "l_m2": "l/m2",
    "kwh_m2a": "kwh/m2a",
    "eur_m2": "eur/m2",
    "km_h": "km/h",
    "kwp": "kwp",
    "mwh": "mwh",
    "nm": "nm",
    "mb": "mb",
    "gb": "gb",
    "kw": "kw",
    "km": "km",
    "mm": "mm",
    "tonne": "t",
    "liter": "l",
    "m2": "m2",
    "m3": "m3",
    "kwh": "kwh",
    "celsius": "celsius",
    "percent": "percent",
    "eur": "eur",
    "kg": "kg",
    "cm": "cm",
    "m": "m",
    "time": "time",
}
_UNIT_WORDS = frozenset(
    {
        "prozent",
        "eur",
        "euro",
        "quadratmeter",
        "kubikmeter",
        "kilometer",
        "stunde",
        "kilowattstunde",
        "kilowattstunden",
        "kilowatt",
        "megawattstunde",
        "megawattstunden",
        "megabit",
        "sekunde",
        "newtonmeter",
        "megabyte",
        "gigabyte",
        "liter",
        "minute",
        "peak",
        "millimeter",
        "tonne",
        "tonnen",
        "jahr",
        "grad",
        "celsius",
        "kilogramm",
        "zentimeter",
        "meter",
        "uhr",
    }
)
_ACOUSTIC_FILLER_WORDS = frozenset({"äh", "ähm", "hm", "uh"})
_PAUSED_FILLER_SEQUENCES = (
    ("also",),
    ("gewissermassen",),
    ("halt",),
    ("im", "grunde"),
    ("irgendwie",),
    ("genau",),
    ("na", "ja"),
    ("praktisch",),
    ("quasi",),
    ("sozusagen",),
    ("um",),
    ("you", "know"),
    ("i", "mean"),
)
_FILLER_WORDS = frozenset(_ACOUSTIC_FILLER_WORDS | {word for sequence in _PAUSED_FILLER_SEQUENCES for word in sequence})
_GRAMMAR_INSERTION_WORDS = frozenset(
    {
        "der",
        "die",
        "das",
        "den",
        "dem",
        "des",
        "ein",
        "eine",
        "einen",
        "einem",
        "einer",
        "eines",
        "zu",
        "zur",
        "zum",
    }
)
_ROLE_CONTEXT_EXEMPT = frozenset(
    {
        "und",
        "oder",
        "aber",
        "der",
        "die",
        "das",
        "den",
        "dem",
        "des",
        "ein",
        "eine",
        "einen",
        "einem",
        "einer",
        "eines",
        "zu",
        "zur",
        "zum",
        "im",
        "in",
        "am",
        "an",
        "auf",
        "von",
        "für",
        "mit",
        "wir",
        "ich",
        "sie",
        "er",
        "es",
        *_FILLER_WORDS,
    }
)
_NAME_ROLES = frozenset({"herr", "frau", "dr", "doktor", "prof", "professor", "firma", "unternehmen"})
_SPOKEN_NAME_ROLES = frozenset({"herr", "frau", "dr", "doktor", "prof", "professor"})
_CLAUSE_BREAK = re.compile(r"[.!?;:\n]")
_SEMANTIC_FORMS = {
    form: lemma
    for lemma, forms in {
        "ablehnen": ("ablehnen", "abgelehnt", "lehne", "lehnt", "lehnte", "lehnten"),
        "akzeptieren": ("akzeptieren", "akzeptiert", "akzeptiere", "akzeptierte", "akzeptierten"),
        "genehmigen": ("genehmigen", "genehmigt", "genehmige", "genehmigte", "genehmigten"),
        "bewilligen": ("bewilligen", "bewilligt", "bewilligte", "bewilligten"),
        "kündigen": ("kündigen", "gekündigt", "kündigt", "kündigte", "kündigten"),
        "verlängern": ("verlängern", "verlängert", "verlängerte", "verlängerten"),
        "widerrufen": ("widerrufen", "widerruft", "widerrief", "widerriefen"),
        "bestätigen": ("bestätigen", "bestätigt", "bestätigte", "bestätigten"),
        "erlauben": ("erlauben", "erlaubt", "erlaubte", "erlaubten"),
        "verbieten": ("verbieten", "verboten", "verbietet", "verbot", "verboten"),
        "erhöhen": ("erhöhen", "erhöht", "erhöhte", "erhöhten"),
        "senken": ("senken", "gesenkt", "senkt", "senkte", "senkten"),
        "zahlen": ("zahlen", "bezahlen", "gezahlt", "bezahlt", "zahlt", "zahlte", "zahlten"),
        "schulden": ("schulden", "schuldet", "schuldete", "schuldeten"),
        "kaufen": ("kaufen", "gekauft", "kauft", "kaufte", "kauften"),
        "verkaufen": ("verkaufen", "verkauft", "verkaufte", "verkauften"),
        "mieten": ("mieten", "gemietet", "mietet", "mietete", "mieteten"),
        "vermieten": ("vermieten", "vermietet", "vermietete", "vermieteten"),
        "senden": ("senden", "gesendet", "sendet", "sandte", "sandten"),
        "erhalten": ("erhalten", "erhält", "erhielt", "erhielten"),
        "beginnen": ("beginnen", "begonnen", "beginnt", "begann", "begannen"),
        "enden": ("enden", "beendet", "endet", "endete", "endeten"),
        "kein": ("kein", "keine", "keinen", "keinem", "keiner", "keines"),
    }.items()
    for form in forms
}
_REVIEWED_LEXICAL_EQUIVALENTS = frozenset(
    {
        frozenset({"mietvertag", "mietvertrag"}),
    }
)
_FORMAT_COMMAND_NOUN_GUARDS = frozenset(
    {
        "der",
        "die",
        "das",
        "den",
        "dem",
        "des",
        "ein",
        "eine",
        "einen",
        "einem",
        "einer",
        "eines",
        "kein",
        "keine",
        "keinen",
        "keinem",
        "keiner",
        "keines",
        "mit",
        "ohne",
        "als",
        "wort",
        "begriff",
        "zeichen",
    }
)
_FORMAT_COMMAND_COPULAS = frozenset({"ist", "sind", "war", "waren", "bleibt", "heisst", "bedeutet"})
_FORMAT_COMMAND_LINE_GUARDS = _FORMAT_COMMAND_NOUN_GUARDS | frozenset(
    {"nach", "laut", "gemaess", "in", "im", "zu", "zum"}
)
_SPOKEN_LINE_BREAK_COMMAND = re.compile(
    r"\s+(?P<command>neue\s+zeile|zeilenumbruch|neuer\s+absatz|absatz)\s+",
    re.IGNORECASE,
)
_SPOKEN_INLINE_FORMAT_COMMAND = re.compile(r"\s+(?P<command>komma)(?=\s+[^\W\d_]+)", re.IGNORECASE)
_SPOKEN_TERMINAL_FORMAT_COMMAND = re.compile(
    r"\s+(?P<command>punkt|fragezeichen|ausrufezeichen)\s*$",
    re.IGNORECASE,
)
_SPOKEN_LEGAL_REFERENCE_PREFIX = re.compile(
    r"(?<!\w)(?P<kind>paragraph|paragraf|artikel)\b",
    re.IGNORECASE,
)
_SPOKEN_LIST_ITEM = re.compile(
    r"(?<!\w)(?P<item>erstens|zweitens|drittens|viertens|fünftens|sechstens|siebtens|achtens|neuntens|zehntens)(?!\w)",
    re.IGNORECASE,
)
_SPOKEN_LIST_ITEM_VALUES = {
    "erstens": 1,
    "zweitens": 2,
    "drittens": 3,
    "viertens": 4,
    "fünftens": 5,
    "sechstens": 6,
    "siebtens": 7,
    "achtens": 8,
    "neuntens": 9,
    "zehntens": 10,
}
_DIGIT_LIST_ITEM = re.compile(r"(?m)^\s*(?P<item>[0-9]{1,3})[.)](?=\s)")
_SPELLED_INITIALISM = re.compile(r"(?<!\w)(?P<letters>[A-ZÄÖÜ](?:\s+[A-ZÄÖÜ]){1,})(?!\w)")
_STANDARD_UST_ID = re.compile(r"(?<!\w)(?:ustid|ust\s*-\s*idnr\.?)(?!\w)", re.IGNORECASE)
_SPOKEN_COURT_ROMAN = re.compile(r"(?P<prefix>[–—-]\s*)(?P<number>[A-Za-zÄÖÜäöüß]+)\s+(?P<division>ZR|R)(?!\w)")


def _german_number_value(word: str) -> int | None:
    normalized = word.casefold()
    direct = _GERMAN_CARDINALS.get(normalized)
    if direct is not None:
        return direct
    ordinal = _GERMAN_ORDINALS.get(normalized)
    if ordinal is not None:
        return ordinal
    for scale, multiplier in (("millionen", 1_000_000), ("million", 1_000_000), ("tausend", 1000), ("hundert", 100)):
        if scale not in normalized:
            continue
        prefix, suffix = normalized.split(scale, 1)
        prefix_value = 1 if not prefix else _german_number_value(prefix)
        suffix_value = 0 if not suffix else _german_number_value(suffix)
        if prefix_value is not None and suffix_value is not None:
            return prefix_value * multiplier + suffix_value
    if "und" in normalized:
        unit, tens = normalized.split("und", 1)
        unit_value = _german_number_value(unit)
        tens_value = _german_number_value(tens)
        if unit_value is not None and 0 < unit_value < 10 and tens_value is not None and tens_value % 10 == 0:
            return unit_value + tens_value
    return None


def _german_ordinal_value(word: str) -> int | None:
    normalized = word.casefold()
    direct = _GERMAN_ORDINALS.get(normalized)
    if direct is not None:
        return direct
    for suffix in ("sten", "ster", "stem", "stes", "ste"):
        if not normalized.endswith(suffix):
            continue
        value = _german_number_value(normalized[: -len(suffix)])
        if value is not None and 21 <= value <= 31:
            return value
    return None


def _digit_values(value: str) -> tuple[str, ...]:
    normalized = value.lstrip("+")
    sign = "-" if normalized.startswith("-") else ""
    normalized = normalized.lstrip("-")
    for grouping_separator, decimal_separator in ((".", ","), (",", ".")):
        grouped_decimal = re.fullmatch(
            rf"(?P<whole>\d{{1,3}}(?:{re.escape(grouping_separator)}\d{{3}})+)"
            rf"{re.escape(decimal_separator)}(?P<fraction>\d+)",
            normalized,
        )
        if grouped_decimal is not None:
            whole = grouped_decimal.group("whole").replace(grouping_separator, "")
            fraction = grouped_decimal.group("fraction").rstrip("0") or "0"
            return (f"{sign}{int(whole)}.{fraction}",)
    if "." in normalized and "," in normalized:
        raise SafetyError("changed_number")
    if "," in normalized and all(separator not in normalized for separator in ".:/-"):
        grouped_integer = re.fullmatch(r"\d{1,3}(?:,\d{3})+", normalized)
        if grouped_integer is not None and normalized.count(",") > 1:
            return (f"{sign}{int(normalized.replace(',', ''))}",)
        decimal_comma = re.fullmatch(r"(?P<whole>\d+),(?P<fraction>\d+)", normalized)
        if decimal_comma is None:
            raise SafetyError("changed_number")
        whole = decimal_comma.group("whole")
        fraction = decimal_comma.group("fraction")
        return (f"{sign}{int(whole)}.{fraction.rstrip('0') or '0'}",)
    if "." in normalized:
        parts = normalized.split(".")
        if len(parts) >= 2 and all(len(part) == 3 for part in parts[1:]):
            return (f"{sign}{int(''.join(parts))}",)
        if len(parts) > 2 and re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", normalized) is None:
            raise SafetyError("changed_number")
        return tuple(f"{sign if index == 0 else ''}{int(part)}" for index, part in enumerate(parts))
    if any(separator in normalized for separator in ":/-"):
        parts = re.split(r"[:/-]", normalized)
        return tuple(f"{sign if index == 0 else ''}{int(part)}" for index, part in enumerate(parts))
    return (f"{sign}{int(normalized)}",)


@dataclass(frozen=True, slots=True)
class _NumberMention:
    start: int
    end: int
    values: tuple[str, ...]
    kind: str = "number"
    repairable_source: bool = True


@dataclass(frozen=True, slots=True)
class _FormatCommandAnchor:
    kind: str
    left: str
    right: str
    mark: str
    left_occurrence: int
    right_occurrence: int


@dataclass(frozen=True, slots=True)
class _UnitMention:
    start: int
    end: int
    unit: str


@dataclass(frozen=True, slots=True)
class _BoundSemanticAnchor:
    start: int
    end: int
    values: tuple[str, ...]
    unit: str | None
    left_context: tuple[str, ...]
    right_context: tuple[str, ...]
    kind: str
    mentions: tuple[_NumberMention, ...]


@dataclass(frozen=True, slots=True)
class _LexicalMention:
    text: str
    normalized: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _LegalReferenceMention:
    start: int
    end: int
    anchor: tuple[str, ...]


def _list_item_anchors(text: str) -> tuple[int, ...]:
    mentions = [
        (match.start(), _SPOKEN_LIST_ITEM_VALUES[match.group("item").casefold()])
        for match in _SPOKEN_LIST_ITEM.finditer(text)
    ]
    mentions.extend((match.start("item"), int(match.group("item"))) for match in _DIGIT_LIST_ITEM.finditer(text))
    return tuple(value for _start, value in sorted(mentions))


def _digit_list_item_ranges(text: str) -> tuple[tuple[int, int], ...]:
    return tuple(match.span("item") for match in _DIGIT_LIST_ITEM.finditer(text))


def _next_legal_number(text: str, start: int) -> tuple[tuple[str, ...], int] | None:
    tail = text[start:]
    mentions = _semantic_number_mentions(tail)
    if not mentions:
        return None
    mention = mentions[0]
    if tail[: mention.start].strip() or mention.kind != "number" or len(mention.values) != 1:
        return None
    end = start + mention.end
    suffix = text[end : end + 1]
    value = mention.values[0]
    if suffix and suffix.isalpha() and not suffix.isspace():
        value = f"{value}{suffix.casefold()}"
        end += 1
    else:
        separated_suffix = re.match(
            r"\s+(?P<suffix>[a-z])(?=(?:[\s,;]+(?:Abs\.|Absatz|S\.|Satz|Nr\.|Nummer)(?!\w)|\s+[A-ZÄÖÜ]))",
            text[end:],
            re.IGNORECASE,
        )
        if separated_suffix is not None:
            value = f"{value}{separated_suffix.group('suffix').casefold()}"
            end += separated_suffix.end()
    return (value,), end


def _legal_reference_mentions(text: str) -> tuple[_LegalReferenceMention, ...]:
    result: list[_LegalReferenceMention] = [
        _LegalReferenceMention(match.start(), match.end(), ("ecli", match.group(0).casefold()))
        for match in _ECLI_REFERENCE.finditer(text)
    ]
    labels = {
        "abs.": "abs",
        "absatz": "abs",
        "s.": "sentence",
        "satz": "sentence",
        "nr.": "number",
        "nummer": "number",
        "lit.": "letter",
        "buchstabe": "letter",
    }
    for prefix in _LEGAL_REFERENCE_PREFIX.finditer(text):
        first = _next_legal_number(text, prefix.end())
        if first is None:
            continue
        values, end = first
        kind = "section" if prefix.group("kind").startswith("§") else "article"
        anchor = [kind, values[0]]
        while True:
            part = _LEGAL_REFERENCE_PART.match(text, end)
            if part is None:
                break
            label = labels[part.group("label").casefold()]
            if label == "letter":
                following_letter = re.match(r"(?P<letter>[a-z])(?!\w)", text[part.end() :], re.IGNORECASE)
                if following_letter is None:
                    break
                end = part.end() + following_letter.end()
                anchor.extend((label, following_letter.group("letter").casefold()))
                continue
            following = _next_legal_number(text, part.end())
            if following is None:
                break
            values, end = following
            anchor.extend((label, values[0]))
        result.append(_LegalReferenceMention(prefix.start(), end, tuple(anchor)))
    return tuple(sorted(result, key=lambda item: item.start))


def _legal_reference_anchors(text: str) -> tuple[tuple[str, ...], ...]:
    return tuple(mention.anchor for mention in _legal_reference_mentions(text))


def _legal_reference_ranges(text: str) -> tuple[tuple[int, int], ...]:
    return tuple((mention.start, mention.end) for mention in _legal_reference_mentions(text))


def _compose_german_number_run(values: list[int]) -> int:
    total = 0
    current = 0
    for value in values:
        if value >= 1_000_000:
            total += max(1, current) * value
            current = 0
        elif value == 1_000:
            total += max(1, current) * 1_000
            current = 0
        elif value == 100:
            current = max(1, current) * 100
        else:
            current += value
    return total + current


def _integer_components(values: tuple[str, ...]) -> tuple[int, ...] | None:
    if not all(re.fullmatch(r"-?[0-9]+", value) for value in values):
        return None
    return tuple(int(value) for value in values)


def _valid_date_values(values: tuple[str, ...]) -> bool:
    components = _integer_components(values)
    if components is None or len(components) != 3:
        return False
    day, month, year = components
    if year < 1000 or year > 9999:
        return False
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


def _valid_time_values(values: tuple[str, ...]) -> bool:
    components = _integer_components(values)
    if components is None or not 1 <= len(components) <= 3:
        return False
    hour, *remainder = components
    return 0 <= hour <= 23 and all(0 <= component <= 59 for component in remainder)


def _digit_kind(token: str, values: tuple[str, ...]) -> str:
    date_match = re.fullmatch(r"[0-9]{1,2}([./-])[0-9]{1,2}\1[0-9]{4}", token)
    if date_match is not None:
        return "date" if _valid_date_values(values) else "invalid"
    time_match = re.fullmatch(r"[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?", token)
    if time_match is not None:
        return "time" if _valid_time_values(values) else "invalid"
    return "number"


def _has_explicit_year_context(
    text: str,
    matches: list[re.Match[str]],
    index: int,
    end_index: int,
) -> bool:
    """Return whether a split 19/20 + suffix sequence is clearly a year."""

    current_start = matches[index].start()
    current_end = matches[end_index].end()
    following_unit = _UNIT.search(text, current_end)
    if following_unit is not None:
        gap = text[current_end : following_unit.start()]
        if _CLAUSE_BREAK.search(gap) is None and _WORD.search(gap) is None:
            return False
    for previous_index in range(max(0, index - 3), index):
        previous = matches[previous_index]
        if _CLAUSE_BREAK.search(text[previous.end() : current_start]) is not None:
            continue
        word = previous.group(0).casefold()
        if word in _YEAR_CONTEXT_WORDS:
            intervening = (match.group(0).casefold() for match in matches[previous_index + 1 : index])
            if all(token in _YEAR_CONTEXT_CONNECTORS for token in intervening):
                return True
        if previous_index == index - 1 and word in _GERMAN_MONTHS:
            return True
    return False


def _semantic_number_mentions(text: str) -> tuple[_NumberMention, ...]:
    matches = list(_NUMERIC_OR_WORD.finditer(text))
    tokens = [match.group(0) for match in matches]
    list_item_ranges = _digit_list_item_ranges(text)
    mentions: list[_NumberMention] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _NUMBER.fullmatch(token):
            if any(matches[index].start() < end and start < matches[index].end() for start, end in list_item_ranges):
                index += 1
                continue
            values = _digit_values(token)
            kind = _digit_kind(token, values)
            if kind == "time":
                values = tuple(values[index] for index in range(len(values)))
                while len(values) > 1 and values[-1] == "0":
                    values = values[:-1]
            mentions.append(
                _NumberMention(
                    matches[index].start(),
                    matches[index].end(),
                    values,
                    kind=kind,
                )
            )
            index += 1
            continue
        ordinal = _german_ordinal_value(token)
        if ordinal is not None and index + 2 < len(tokens):
            month_word = tokens[index + 1].casefold()
            month = _german_ordinal_value(month_word) or _GERMAN_MONTHS.get(month_word)
            year_first = _german_number_value(tokens[index + 2])
            if month is not None and year_first is not None:
                consumed = 3
                year = year_first
                if index + 3 < len(tokens):
                    year_second = _german_number_value(tokens[index + 3])
                    if year_first in {19, 20} and year_second is not None and year_second < 100:
                        year = year_first * 100 + year_second
                        consumed = 4
                mentions.append(
                    _NumberMention(
                        matches[index].start(),
                        matches[index + consumed - 1].end(),
                        (str(ordinal), str(month), str(year)),
                        kind=("date" if _valid_date_values((str(ordinal), str(month), str(year))) else "invalid"),
                    )
                )
                index += consumed
                continue
        value = _german_number_value(token)
        if value is None:
            index += 1
            continue
        if token.casefold() == "acht":
            clause_start = max(
                (item.end() for item in _CLAUSE_BREAK.finditer(text, 0, matches[index].start())),
                default=0,
            )
            clause_end_match = _CLAUSE_BREAK.search(text, matches[index].end())
            clause_end = clause_end_match.start() if clause_end_match is not None else len(text)
            clause_word_matches = list(_WORD.finditer(text, clause_start, clause_end))
            clause_words = [_normalize_word(item.group(0)) for item in clause_word_matches]
            acht_index = next(
                (
                    word_index
                    for word_index, item in enumerate(clause_word_matches)
                    if item.start() == matches[index].start()
                ),
                -1,
            )
            give_forms = frozenset({"geben", "gebe", "gib", "gibt", "gebt", "gab", "gaben", "gegeben"})
            before = clause_words[max(0, acht_index - 3) : acht_index]
            after = clause_words[acht_index + 1 : acht_index + 4]
            idiom_continuations = frozenset({"auf", "darauf", "dass", "damit", "wenn", "ob", "und", "oder"})
            if acht_index >= 0 and (
                (any(word in give_forms for word in before) and (not after or after[0] in idiom_continuations))
                or (after and after[0] in give_forms)
                or (any(word in {"ausser", "außer"} for word in before) and "lassen" in after)
                or (before and before[-1] == "in" and "nehmen" in after)
            ):
                index += 1
                continue
        if token.casefold() in {"ein", "eine", "einen", "einem", "einer"}:
            following = tokens[index + 1].casefold() if index + 1 < len(tokens) else ""
            if following not in _UNIT_WORDS and following not in {"hundert", "tausend", "million", "millionen"}:
                index += 1
                continue
        if index + 2 < len(tokens) and tokens[index + 1].casefold() == "komma":
            fraction = _german_number_value(tokens[index + 2])
            separators = (
                text[matches[index].end() : matches[index + 1].start()],
                text[matches[index + 1].end() : matches[index + 2].start()],
            )
            if (
                fraction is not None
                and fraction < 100
                and all(re.fullmatch(r"[\s-]*", separator) is not None for separator in separators)
            ):
                fraction_digits = [str(fraction)]
                cursor = index + 3
                while cursor < len(tokens):
                    separator = text[matches[cursor - 1].end() : matches[cursor].start()]
                    following_fraction = _german_number_value(tokens[cursor])
                    if (
                        re.fullmatch(r"[\s-]*", separator) is None
                        or following_fraction is None
                        or not 0 <= following_fraction <= 9
                    ):
                        break
                    fraction_digits.append(str(following_fraction))
                    cursor += 1
                normalized_fraction = "".join(fraction_digits).rstrip("0") or "0"
                mentions.append(
                    _NumberMention(
                        matches[index].start(),
                        matches[cursor - 1].end(),
                        (f"{value}.{normalized_fraction}",),
                    )
                )
                index = cursor
                continue
        run = [value]
        cursor = index + 1
        while cursor < len(tokens):
            separator = text[matches[cursor - 1].end() : matches[cursor].start()]
            if re.fullmatch(r"[\s-]*", separator) is None:
                break
            if tokens[cursor].casefold() in {"ein", "eine", "einen", "einem", "einer"}:
                break
            following = _german_number_value(tokens[cursor])
            if following is None or tokens[cursor].casefold() in _GERMAN_ORDINALS:
                break
            run.append(following)
            cursor += 1
        split_year = len(run) == 2 and run[0] in {19, 20} and run[1] < 100
        explicit_year = split_year and _has_explicit_year_context(text, matches, index, cursor - 1)
        number = run[0] * 100 + run[1] if explicit_year else _compose_german_number_run(run)
        mentions.append(
            _NumberMention(
                matches[index].start(),
                matches[cursor - 1].end(),
                (str(number),),
                repairable_source=(len(run) == 1 or explicit_year or any(component >= 100 for component in run)),
            )
        )
        index = cursor
    merged: list[_NumberMention] = []
    for mention in mentions:
        if merged:
            previous = merged[-1]
            separator = text[previous.end : mention.start]
            if (
                previous.kind == "number"
                and mention.kind == "number"
                and re.fullmatch(r"\s+schrägstrich\s+", separator, re.IGNORECASE) is not None
            ):
                merged[-1] = _NumberMention(
                    previous.start,
                    mention.end,
                    previous.values + mention.values,
                    kind="number",
                    repairable_source=previous.repairable_source and mention.repairable_source,
                )
                continue
        merged.append(mention)
    return tuple(merged)


def _render_unambiguous_spoken_legal_references(text: str) -> str:
    replacements: list[tuple[int, int, str]] = []
    for match in _SPOKEN_LEGAL_REFERENCE_PREFIX.finditer(text):
        clause_end_match = _CLAUSE_BREAK.search(text, match.end())
        clause_end = clause_end_match.start() if clause_end_match is not None else len(text)
        tail = text[match.end() : clause_end]
        mentions = _semantic_number_mentions(tail)
        if not mentions:
            continue
        number = mentions[0]
        if tail[: number.start].strip(" \t\r\n,;:—–-") or len(number.values) != 1:
            continue
        prefix = "Art." if _normalize_word(match.group("kind")) == "artikel" else "§"
        replacements.append(
            (
                match.start(),
                match.end() + number.end,
                f"{prefix} {number.values[0]}",
            )
        )
    rendered = text
    for start, end, replacement in reversed(replacements):
        rendered = f"{rendered[:start]}{replacement}{rendered[end:]}"
    return rendered


def _semantic_number_anchors(text: str) -> tuple[str, ...]:
    return tuple(value for mention in _semantic_number_mentions(text) for value in mention.values)


def _literal_zero_signature(raw: str, kind: str) -> tuple[str, ...]:
    signless = raw.lstrip("+-")
    parts = re.findall(r"\d+", signless)
    if kind in {"date", "time"} or any(separator in signless for separator in ":/-"):
        return tuple(part if len(part) > 1 and part.startswith("0") else "" for part in parts)
    if re.fullmatch(r"\d+", signless) is not None:
        return (signless if len(signless) > 1 and signless.startswith("0") else "",)
    grouped = re.fullmatch(r"(?P<whole>\d{1,3}(?:[.,]\d{3})+)(?:[.,]\d+)?", signless)
    if grouped is not None:
        first = re.match(r"\d+", grouped.group("whole"))
        value = first.group(0) if first is not None else ""
        return (value if len(value) > 1 and value.startswith("0") else "",)
    decimal = re.fullmatch(r"(?P<whole>\d+)[.,]\d+", signless)
    if decimal is not None:
        whole = decimal.group("whole")
        return (whole if len(whole) > 1 and whole.startswith("0") else "",)
    return tuple(part if len(part) > 1 and part.startswith("0") else "" for part in parts)


def _literal_number_format_bindings(
    text: str,
) -> tuple[dict[tuple[int, int], tuple[str, ...]], frozenset[int]]:
    zeroes: dict[tuple[int, int], tuple[str, ...]] = {}
    explicit_plus_offsets: set[int] = set()
    value_offset = 0
    for mention in _semantic_number_mentions(text):
        width = len(mention.values)
        raw = text[mention.start : mention.end]
        if _NUMBER.fullmatch(raw) is not None:
            zeroes[(value_offset, width)] = _literal_zero_signature(raw, mention.kind)
            if raw.startswith("+"):
                explicit_plus_offsets.add(value_offset)
        value_offset += width
    return zeroes, frozenset(explicit_plus_offsets)


def _unit_mentions(text: str) -> tuple[_UnitMention, ...]:
    numbers = _semantic_number_mentions(text)
    mentions: list[_UnitMention] = []
    for match in _UNIT.finditer(text):
        group = match.lastgroup
        if group is None:
            raise SafetyError("changed_unit")
        unit = _UNIT_CANONICAL[group]
        # SI metre is lowercase; standalone uppercase M commonly names a variable.
        if group == "m" and match.group(0) == "M":
            continue
        number_bound = any(
            (
                mention.end <= match.start()
                and match.start() - mention.end <= 48
                and _same_clause(text, mention.end, match.start())
                and re.fullmatch(r"[\s,()\[\]{}–—-]*", text[mention.end : match.start()]) is not None
            )
            or (
                mention.start >= match.end()
                and mention.start - match.end() <= 32
                and _same_clause(text, match.end(), mention.start)
                and re.fullmatch(r"[\s,()\[\]{}–—-]*", text[match.end() : mention.start]) is not None
            )
            for mention in numbers
        )
        if not number_bound and unit not in {"eur/m2", "kwh/m2a"}:
            continue
        mentions.append(_UnitMention(match.start(), match.end(), unit))
    return tuple(mentions)


def _unit_anchors(text: str) -> Counter[str]:
    return Counter(mention.unit for mention in _unit_mentions(text))


def _semantic_anchors(text: str) -> Counter[str]:
    return Counter(
        _SEMANTIC_FORMS[token.casefold()] for token in _WORD.findall(text) if token.casefold() in _SEMANTIC_FORMS
    )


def _explicit_filler_ranges(text: str) -> tuple[tuple[int, int], ...]:
    """Return only acoustically clear or pause-delimited filler spans.

    Several prompt examples (notably ``um``, ``also``, ``you know`` and
    ``I mean``) are ordinary content in other positions.  Treating their words
    as a global deletion allowlist would silently permit semantic truncation.
    """

    words = list(_WORD.finditer(text))
    normalized = [_normalize_word(match.group(0)) for match in words]
    ranges = [
        (match.start(), match.end())
        for match, word in zip(words, normalized, strict=True)
        if word in _ACOUSTIC_FILLER_WORDS
    ]
    pause_characters = frozenset(",;:—–-()[]")
    for sequence in _PAUSED_FILLER_SEQUENCES:
        width = len(sequence)
        for index in range(len(words) - width + 1):
            if tuple(normalized[index : index + width]) != sequence:
                continue
            start = words[index].start()
            end = words[index + width - 1].end()
            if any(
                text[words[offset].end() : words[offset + 1].start()].strip()
                for offset in range(index, index + width - 1)
            ):
                continue
            left = text[:start].rstrip()
            right = text[end:].lstrip()
            if (not left or left[-1] in pause_characters) and (
                not right or right[0] in pause_characters or right[0] in ".!?"
            ):
                ranges.append((start, end))
    return tuple(sorted(set(ranges)))


def _self_correction_ranges(text: str) -> tuple[tuple[int, int], ...]:
    signal = re.compile(r"\b(?:nein|ich\s+meine|besser\s+gesagt|korrektur)\b", re.IGNORECASE)
    ranges: list[tuple[int, int]] = []
    for match in signal.finditer(text):
        clause_breaks = [item.end() for item in _CLAUSE_BREAK.finditer(text, 0, match.start())]
        clause_start = clause_breaks[-1] if clause_breaks else 0
        delimiters = list(re.finditer(r"[,;:]|[—–]", text[clause_start : match.start()]))
        if not delimiters:
            continue
        delimiter = delimiters[-1]
        wrong_end = clause_start + delimiter.start()
        delimiter_end = clause_start + delimiter.end()
        if text[delimiter_end : match.start()].strip():
            continue
        trailing = re.match(r"\s*[,;:—–-]\s*", text[match.end() :])
        if trailing is None:
            continue
        end = match.end() + trailing.end()
        correction_end_match = _CLAUSE_BREAK.search(text, end)
        correction_end = correction_end_match.start() if correction_end_match is not None else len(text)
        correction_words = list(_NUMERIC_OR_WORD.finditer(text, end, correction_end))
        wrong_words = list(_NUMERIC_OR_WORD.finditer(text, clause_start, wrong_end))
        if not correction_words or not wrong_words:
            continue
        correction_first = _normalize_word(correction_words[0].group(0))
        same_lead = [item for item in wrong_words if _normalize_word(item.group(0)) == correction_first]
        if same_lead:
            wrong_start = same_lead[-1].start()
        else:
            wrong_last = _normalize_word(wrong_words[-1].group(0))
            if _german_number_value(wrong_last) is None or _german_number_value(correction_first) is None:
                continue
            wrong_start = wrong_words[-1].start()
        ranges.append((wrong_start, end))
    selected: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if selected and start <= selected[-1][1]:
            selected[-1] = (selected[-1][0], max(end, selected[-1][1]))
        else:
            selected.append((start, end))
    return tuple(selected)


def _without_self_corrections(text: str) -> str:
    output = text
    for start, end in reversed(_self_correction_ranges(text)):
        output = f"{output[:start]} {output[end:]}"
    return output


def _without_explicit_fillers(text: str) -> str:
    output = text
    for start, end in reversed(_explicit_filler_ranges(text)):
        output = f"{output[:start]} {output[end:]}"
    return output


def _normalize_word(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().replace("ß", "ss")


def _roman_numeral(value: int) -> str:
    parts: list[str] = []
    remainder = value
    for amount, numeral in ((10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while remainder >= amount:
            parts.append(numeral)
            remainder -= amount
    return "".join(parts)


def _render_spelled_initialisms(text: str) -> str:
    return _SPELLED_INITIALISM.sub(
        lambda match: "".join(match.group("letters").split()),
        text,
    )


def _render_standard_abbreviations(text: str) -> str:
    return _STANDARD_UST_ID.sub("USTIDNR", text)


def _render_spoken_court_roman(text: str) -> str:
    def replacement(match: re.Match[str]) -> str:
        value = _german_number_value(match.group("number"))
        if value is None or not 1 <= value <= 20:
            return match.group(0)
        return f"{match.group('prefix')}{_roman_numeral(value)} {match.group('division')}"

    return _SPOKEN_COURT_ROMAN.sub(replacement, text)


def _without_exact_word_repetitions(text: str) -> str:
    """Remove an immediately repeated, non-numeric word or phrase.

    The repetition must be contiguous apart from whitespace or a spoken-pause
    delimiter.  Sentence boundaries, line breaks, numbers, and units are never
    crossed or deduplicated.
    """

    output = text
    while True:
        words = list(_WORD.finditer(output))
        normalized = [_normalize_word(match.group(0)) for match in words]
        selected: tuple[int, int] | None = None
        for second_start in range(1, len(words)):
            max_width = min(8, second_start, len(words) - second_start)
            for width in range(max_width, 0, -1):
                first_start = second_start - width
                if normalized[first_start:second_start] != normalized[second_start : second_start + width]:
                    continue
                sequence = normalized[first_start:second_start]
                if any(
                    (_german_number_value(word) is not None and word not in {"ein", "eine", "einen", "einem", "einer"})
                    or word in _UNIT_WORDS
                    for word in sequence
                ):
                    continue
                separator = output[words[second_start - 1].end() : words[second_start].start()]
                if re.fullmatch(r"[ \t,;:—–-]*", separator) is None:
                    continue
                selected = (words[second_start].start(), words[second_start + width - 1].end())
                break
            if selected is not None:
                break
        if selected is None:
            return output
        start, end = selected
        output = f"{output[:start]}{output[end:]}"


def _without_bound_contextual_list_connectives(source: str, candidate: str) -> str:
    """Remove ``sowie`` only where the candidate proves a bullet boundary."""

    bullet = re.compile(r"(?m)^[ \t]*-[ \t]+(?P<body>\S[^\r\n]*)[ \t]*$")
    bullet_lines = list(bullet.finditer(candidate))
    boundaries: set[tuple[str, str]] = set()
    for left, right in pairwise(bullet_lines):
        if candidate[left.end() : right.start()].strip():
            continue
        left_words = _WORD.findall(left.group("body"))
        right_words = _WORD.findall(right.group("body"))
        if left_words and right_words:
            boundaries.add((_normalize_word(left_words[-1]), _normalize_word(right_words[0])))
    if not boundaries:
        return source

    words = list(_WORD.finditer(source))
    removals: list[tuple[int, int]] = []
    for index, word in enumerate(words):
        if _normalize_word(word.group(0)) != "sowie" or index == 0 or index + 1 >= len(words):
            continue
        boundary = (
            _normalize_word(words[index - 1].group(0)),
            _normalize_word(words[index + 1].group(0)),
        )
        if boundary not in boundaries:
            continue
        clause_start = source.rfind(":", max(0, word.start() - 1000), word.start())
        if clause_start < 0 or ";" not in source[clause_start : word.start()]:
            continue
        removals.append((word.start(), word.end()))
    output = source
    for start, end in reversed(removals):
        output = f"{output[:start]}{output[end:]}"
    return output


def _spoken_format_command_plan(text: str) -> tuple[str, tuple[_FormatCommandAnchor, ...]]:
    anchors: list[_FormatCommandAnchor] = []

    def surrounding_words(value: str, match: re.Match[str]) -> tuple[str, str, int, int]:
        before = list(_WORD.finditer(value, 0, match.start()))
        after = _WORD.search(value, match.end())
        left = _normalize_word(before[-1].group(0)) if before else ""
        right = _normalize_word(after.group(0)) if after is not None else ""
        return (
            left,
            right,
            sum(_normalize_word(item.group(0)) == left for item in before) - 1 if left else -1,
            sum(_normalize_word(item.group(0)) == right for item in _WORD.finditer(value, 0, after.end())) - 1
            if after is not None
            else -1,
        )

    def line_break_replacement(match: re.Match[str]) -> str:
        left, right, left_occurrence, right_occurrence = surrounding_words(text, match)
        command = " ".join(_normalize_word(match.group("command")).split())
        if (
            not left
            or not right
            or left in _FORMAT_COMMAND_LINE_GUARDS
            or right in _FORMAT_COMMAND_COPULAS
            or (command == "absatz" and _german_number_value(right) is not None)
        ):
            return match.group(0)
        mark = "\n\n" if "absatz" in command else "\n"
        anchors.append(_FormatCommandAnchor("line_break", left, right, mark, left_occurrence, right_occurrence))
        return mark

    rendered = _SPOKEN_LINE_BREAK_COMMAND.sub(line_break_replacement, text)

    def inline_replacement(match: re.Match[str]) -> str:
        left, right, left_occurrence, right_occurrence = surrounding_words(rendered, match)
        if (
            left in _FORMAT_COMMAND_NOUN_GUARDS
            or right in _FORMAT_COMMAND_COPULAS
            or (
                (_german_number_value(left) is not None or _NUMBER.fullmatch(left))
                and (_german_number_value(right) is not None or _NUMBER.fullmatch(right))
            )
        ):
            return match.group(0)
        anchors.append(_FormatCommandAnchor("punctuation", left, right, ",", left_occurrence, right_occurrence))
        return ","

    rendered = _SPOKEN_INLINE_FORMAT_COMMAND.sub(inline_replacement, rendered)
    terminal = _SPOKEN_TERMINAL_FORMAT_COMMAND.search(rendered)
    if terminal is None:
        return rendered, tuple(anchors)
    before = list(_WORD.finditer(rendered, 0, terminal.start()))
    left = _normalize_word(before[-1].group(0)) if before else ""
    if left in _FORMAT_COMMAND_NOUN_GUARDS:
        return rendered, tuple(anchors)
    punctuation = {"punkt": ".", "fragezeichen": "?", "ausrufezeichen": "!"}
    mark = punctuation[_normalize_word(terminal.group("command"))]
    left_occurrence = sum(_normalize_word(item.group(0)) == left for item in before) - 1 if left else -1
    anchors.append(_FormatCommandAnchor("terminal", left, "", mark, left_occurrence, -1))
    return f"{rendered[: terminal.start()]}{mark}", tuple(anchors)


def _render_unambiguous_spoken_format_commands(text: str) -> str:
    return _spoken_format_command_plan(text)[0]


def _semantic_lemma(value: str) -> str:
    normalized = _normalize_word(value)
    return _SEMANTIC_FORMS.get(normalized, normalized)


def _meaningful_word_mentions(text: str, excluded_ranges: tuple[tuple[int, int], ...]) -> list[_LexicalMention]:
    result: list[_LexicalMention] = []
    for match in _WORD.finditer(text):
        if any(match.start() < end and start < match.end() for start, end in excluded_ranges):
            continue
        normalized = _normalize_word(match.group(0))
        if normalized in _SPOKEN_LIST_ITEM_VALUES or match.group(0).isdecimal():
            continue
        result.append(_LexicalMention(match.group(0), normalized, match.start(), match.end()))
    return result


def _context_words(
    text: str,
    start: int,
    end: int,
    excluded_ranges: tuple[tuple[int, int], ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    def has_clause_break(start_offset: int, end_offset: int) -> bool:
        return any(
            not any(
                match.start() < excluded_end and excluded_start < match.end()
                for excluded_start, excluded_end in excluded_ranges
            )
            for match in _CLAUSE_BREAK.finditer(text, start_offset, end_offset)
        )

    words = _meaningful_word_mentions(text, excluded_ranges)
    left = [
        _semantic_lemma(item.normalized)
        for item in words
        if item.end <= start and not has_clause_break(item.end, start) and item.normalized not in _ROLE_CONTEXT_EXEMPT
    ][-2:]
    right = [
        _semantic_lemma(item.normalized)
        for item in words
        if item.start >= end and not has_clause_break(end, item.start) and item.normalized not in _ROLE_CONTEXT_EXEMPT
    ][:1]
    return tuple(left), tuple(right)


def _has_clause_break(text: str, left: int, right: int) -> bool:
    start, end = sorted((left, right))
    excluded_ranges = tuple(match.span() for match in _NUMBER.finditer(text)) + _legal_reference_ranges(text)
    return any(
        not any(
            match.start() < excluded_end and excluded_start < match.end()
            for excluded_start, excluded_end in excluded_ranges
        )
        for match in _CLAUSE_BREAK.finditer(text, start, end)
    )


def _same_clause(text: str, left: int, right: int) -> bool:
    return not _has_clause_break(text, left, right)


def _bound_anchor_kind(mentions: tuple[_NumberMention, ...], unit: str | None) -> str:
    values = tuple(value for mention in mentions for value in mention.values)
    if unit == "time":
        valid = _valid_time_values(values) and all(mention.kind != "invalid" for mention in mentions)
        return "time" if valid else "invalid"
    if unit is not None:
        return "number" if mentions and all(mention.kind == "number" for mention in mentions) else "invalid"
    if len(mentions) == 1:
        return mentions[0].kind
    return "invalid"


def _bound_semantic_anchors(text: str) -> tuple[_BoundSemanticAnchor, ...]:
    numbers = list(_semantic_number_mentions(text))
    units = list(_unit_mentions(text))
    consumed: set[int] = set()
    raw: list[tuple[int, int, tuple[str, ...], str | None, tuple[_NumberMention, ...]]] = []
    for unit in units:
        before = [
            (index, mention)
            for index, mention in enumerate(numbers)
            if index not in consumed
            and mention.end <= unit.start
            and unit.start - mention.end <= 48
            and _same_clause(text, mention.end, unit.start)
        ]
        after = [
            (index, mention)
            for index, mention in enumerate(numbers)
            if index not in consumed
            and mention.start >= unit.end
            and mention.start - unit.end <= 32
            and _same_clause(text, unit.end, mention.start)
        ]
        chosen: list[tuple[int, _NumberMention]] = []
        if before:
            chosen.append(before[-1])
        elif after:
            chosen.append(after[0])
        if unit.unit == "time" and chosen:
            first_index, first = chosen[0]
            if len(first.values) == 1 and after and after[0][0] != first_index:
                chosen.append(after[0])
        for index, _mention in chosen:
            consumed.add(index)
        values = tuple(value for _index, mention in chosen for value in mention.values)
        anchor_start = min([unit.start, *(mention.start for _index, mention in chosen)])
        anchor_end = max([unit.end, *(mention.end for _index, mention in chosen)])
        raw.append((anchor_start, anchor_end, values, unit.unit, tuple(mention for _index, mention in chosen)))
    for index, mention in enumerate(numbers):
        if index not in consumed:
            raw.append((mention.start, mention.end, mention.values, None, (mention,)))
    all_ranges = tuple((start, end) for start, end, _values, _unit, _mentions in raw) + _legal_reference_ranges(text)
    anchors: list[_BoundSemanticAnchor] = []
    for start, end, values, unit, mentions in sorted(raw):
        left, right = _context_words(text, start, end, all_ranges)
        anchors.append(
            _BoundSemanticAnchor(
                start,
                end,
                values,
                unit,
                left,
                right,
                _bound_anchor_kind(mentions, unit),
                mentions,
            )
        )
    return tuple(anchors)


def _repair_role(anchor: _BoundSemanticAnchor) -> tuple[str, str | None, tuple[str, ...], tuple[str, ...]]:
    return (anchor.kind, anchor.unit, anchor.left_context, anchor.right_context)


def _format_date_digits(raw: str, values: tuple[str, ...]) -> str | None:
    match = re.fullmatch(r"([0-9]{1,2})([./-])([0-9]{1,2})\2([0-9]{4})", raw)
    components = _integer_components(values)
    if match is None or components is None or not _valid_date_values(values):
        return None
    widths = (len(match.group(1)), len(match.group(3)), len(match.group(4)))
    rendered: list[str] = []
    for component, width in zip(components, widths, strict=True):
        digits = str(component)
        if len(digits) > width:
            return None
        rendered.append(digits.zfill(width))
    return match.group(2).join(rendered)


def _format_time_digits(raw: str, values: tuple[str, ...], value_offset: int) -> str | None:
    components = _integer_components(values)
    if components is None:
        return None
    colon = re.fullmatch(r"([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?", raw)
    if colon is not None:
        if value_offset != 0 or not _valid_time_values(values):
            return None
        widths = tuple(len(group) for group in colon.groups() if group is not None)
        if len(widths) != len(components):
            return None
        rendered: list[str] = []
        for component, width in zip(components, widths, strict=True):
            digits = str(component)
            if len(digits) > width:
                return None
            rendered.append(digits.zfill(width))
        return ":".join(rendered)
    if len(components) != 1 or re.fullmatch(r"[0-9]{1,2}", raw) is None:
        return None
    if value_offset == 0:
        if not 0 <= components[0] <= 23:
            return None
    elif not 0 <= components[0] <= 59:
        return None
    digits = str(components[0])
    if len(digits) > len(raw):
        return None
    return digits.zfill(len(raw))


def _format_number_digits(raw: str, values: tuple[str, ...], unit: str | None) -> str | None:
    if len(values) != 1:
        return None
    value = values[0]
    plain = re.fullmatch(r"[0-9]+", raw)
    if plain is not None:
        if re.fullmatch(r"[0-9]+", value) is None or (len(raw) > 1 and raw.startswith("0")):
            return None
        if unit is None and len(value) > 6:
            return None
        return str(int(value))
    grouped = re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{3})+", raw)
    if grouped is not None:
        if re.fullmatch(r"[0-9]+", value) is None:
            return None
        rendered = f"{int(value):,}".replace(",", ".")
        return rendered if "." in rendered else None
    decimal = re.fullmatch(r"([0-9]+),([0-9]+)", raw)
    if decimal is None:
        return None
    source_decimal = re.fullmatch(r"([0-9]+)\.([0-9]+)", value)
    if source_decimal is None or (len(decimal.group(1)) > 1 and decimal.group(1).startswith("0")):
        return None
    scale = len(decimal.group(2))
    fraction = source_decimal.group(2)
    if len(fraction) > scale:
        return None
    return f"{int(source_decimal.group(1))},{fraction.ljust(scale, '0')}"


def _format_numeric_mention(
    raw: str,
    values: tuple[str, ...],
    *,
    kind: str,
    unit: str | None,
    value_offset: int,
) -> str | None:
    if kind == "date":
        return _format_date_digits(raw, values)
    if kind == "time":
        return _format_time_digits(raw, values, value_offset)
    if kind == "number":
        return _format_number_digits(raw, values, unit)
    return None


def repair_unambiguous_numeric_anchors(source: str, candidate: str) -> str:
    """Repair only digit-valued candidate anchors whose source role is unique.

    The operation is deliberately all-or-nothing.  Every anchor is matched in
    order by exact kind, unit, and local role before any replacement is made;
    an ambiguous or unformattable mismatch fails through ``SafetyError``.
    """

    source_anchors = _bound_semantic_anchors(source)
    candidate_anchors = _bound_semantic_anchors(candidate)
    if len(source_anchors) != len(candidate_anchors):
        raise SafetyError("changed_number")
    mismatches = [
        index
        for index, (source_anchor, candidate_anchor) in enumerate(zip(source_anchors, candidate_anchors, strict=True))
        if source_anchor.values != candidate_anchor.values
    ]
    if not mismatches:
        return candidate
    # Literal source numbers travel through protected placeholders.  Once the
    # placeholders have been restored their provenance is no longer visible,
    # so mixing such literals with a repair could mask a moved placeholder.
    if any(
        _NUMBER.fullmatch(source[mention.start : mention.end]) is not None
        for anchor in source_anchors
        for mention in anchor.mentions
    ):
        raise SafetyError("changed_number")

    for source_anchor, candidate_anchor in zip(source_anchors, candidate_anchors, strict=True):
        if source_anchor.unit != candidate_anchor.unit:
            raise SafetyError("changed_unit")
        if (
            source_anchor.kind != candidate_anchor.kind
            or source_anchor.kind == "invalid"
            or len(source_anchor.values) != len(candidate_anchor.values)
        ):
            raise SafetyError("changed_number")
        if _repair_role(source_anchor) != _repair_role(candidate_anchor):
            raise SafetyError("changed_number_role")

    source_roles = Counter(_repair_role(anchor) for anchor in source_anchors)
    candidate_roles = Counter(_repair_role(anchor) for anchor in candidate_anchors)
    if any(count != 1 for count in source_roles.values()) or any(count != 1 for count in candidate_roles.values()):
        raise SafetyError("changed_number_role")

    replacements: list[tuple[int, int, str]] = []
    for index in mismatches:
        source_anchor = source_anchors[index]
        candidate_anchor = candidate_anchors[index]
        if not source_anchor.mentions or not all(mention.repairable_source for mention in source_anchor.mentions):
            raise SafetyError("changed_number")
        offset = 0
        for mention in candidate_anchor.mentions:
            raw = candidate[mention.start : mention.end]
            if _NUMBER.fullmatch(raw) is None:
                raise SafetyError("changed_number")
            width = len(mention.values)
            source_values = source_anchor.values[offset : offset + width]
            if len(source_values) != width:
                raise SafetyError("changed_number")
            replacement = _format_numeric_mention(
                raw,
                source_values,
                kind=candidate_anchor.kind,
                unit=candidate_anchor.unit,
                value_offset=offset,
            )
            if replacement is None:
                raise SafetyError("changed_number")
            if replacement != raw:
                replacements.append((mention.start, mention.end, replacement))
            offset += width
        if offset != len(source_anchor.values):
            raise SafetyError("changed_number")

    repaired = candidate
    previous_start = len(candidate) + 1
    for start, end, replacement in sorted(replacements, reverse=True):
        if end > previous_start:
            raise SafetyError("changed_number")
        repaired = f"{repaired[:start]}{replacement}{repaired[end:]}"
        previous_start = start

    repaired_anchors = _bound_semantic_anchors(repaired)
    if len(source_anchors) != len(repaired_anchors):
        raise SafetyError("changed_number")
    for source_anchor, repaired_anchor in zip(source_anchors, repaired_anchors, strict=True):
        if source_anchor.values != repaired_anchor.values:
            raise SafetyError("changed_number")
        if source_anchor.unit != repaired_anchor.unit:
            raise SafetyError("changed_unit")
        if _repair_role(source_anchor) != _repair_role(repaired_anchor):
            raise SafetyError("changed_number_role")
    return repaired


def _tokens_equivalent(left: str, right: str) -> bool:
    left_normalized = _semantic_lemma(left)
    right_normalized = _semantic_lemma(right)
    if left_normalized == right_normalized:
        return True
    if left_normalized in _SEMANTIC_FORMS.values() or right_normalized in _SEMANTIC_FORMS.values():
        return False
    return frozenset({left_normalized, right_normalized}) in _REVIEWED_LEXICAL_EQUIVALENTS


def _contexts_equivalent(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    if len(left) != len(right):
        return False
    return all(_tokens_equivalent(source, candidate) for source, candidate in zip(left, right, strict=True))


def _validate_bound_spoken_format_commands(source: str, candidate: str) -> None:
    _, anchors = _spoken_format_command_plan(source)
    if not anchors:
        return
    words = list(_WORD.finditer(candidate))
    for anchor in anchors:
        if anchor.kind == "terminal":
            if not candidate.rstrip().endswith(anchor.mark):
                raise SafetyError("changed_format_command")
            continue
        left_matches = [word for word in words if _tokens_equivalent(word.group(0), anchor.left)]
        right_matches = [word for word in words if _tokens_equivalent(word.group(0), anchor.right)]
        if (
            anchor.left_occurrence < 0
            or anchor.right_occurrence < 0
            or anchor.left_occurrence >= len(left_matches)
            or anchor.right_occurrence >= len(right_matches)
        ):
            raise SafetyError("changed_format_command")
        left = left_matches[anchor.left_occurrence]
        right = right_matches[anchor.right_occurrence]
        if left.end() > right.start():
            raise SafetyError("changed_format_command")
        between = candidate[left.end() : right.start()]
        if anchor.kind == "line_break":
            matched = "\n" in between
            if anchor.mark == "\n\n":
                matched = re.search(r"\n\s*\n", between) is not None
        else:
            matched = anchor.mark in between
        if not matched:
            raise SafetyError("changed_format_command")


def _validate_positioned_semantic_anchors(source: str, candidate: str) -> None:
    if _semantic_number_anchors(source) != _semantic_number_anchors(candidate):
        raise SafetyError("changed_number")
    source_zeroes, source_pluses = _literal_number_format_bindings(source)
    candidate_zeroes, candidate_pluses = _literal_number_format_bindings(candidate)
    if source_pluses != candidate_pluses:
        raise SafetyError("changed_number")
    for position, source_signature in source_zeroes.items():
        candidate_signature = candidate_zeroes.get(position)
        if candidate_signature is None:
            if any(source_signature):
                raise SafetyError("changed_number")
            continue
        if source_signature != candidate_signature:
            raise SafetyError("changed_number")
    source_units = tuple(mention.unit for mention in _unit_mentions(source))
    candidate_units = tuple(mention.unit for mention in _unit_mentions(candidate))
    if source_units != candidate_units:
        raise SafetyError("changed_unit")


def _named_role_anchors(text: str) -> tuple[tuple[str, str], ...]:
    words = list(_WORD.finditer(text))
    anchors: list[tuple[str, str]] = []
    for index, match in enumerate(words[:-1]):
        role = _normalize_word(match.group(0)).rstrip(".")
        if role not in _NAME_ROLES:
            continue
        name = words[index + 1].group(0)
        if not name[:1].isupper() and role not in _SPOKEN_NAME_ROLES:
            continue
        anchors.append((role, _normalize_word(name)))
    for match in re.finditer(r"(?m)^\s*([A-ZÄÖÜ][\wÄÖÜäöüß.-]{1,63})\s*:", text):
        anchors.append(("speaker", _normalize_word(match.group(1))))
    return tuple(anchors)


def _local_operator_anchors(text: str, pattern: re.Pattern[str]) -> tuple[tuple[str, str, str, bool, bool], ...]:
    words = _lexical_mentions(text)
    anchors: list[tuple[str, str, str, bool, bool]] = []
    for match in pattern.finditer(text):
        before = [item for item in words if item.end <= match.start()][-2:]
        after = [item for item in words if item.start >= match.end()][:2]
        left_mention = before[-1] if before else None
        right_mention = after[0] if after else None
        left = _semantic_lemma(left_mention.normalized) if left_mention is not None else ""
        right = _semantic_lemma(right_mention.normalized) if right_mention is not None else ""
        left_same_clause = left_mention is not None and not _has_clause_break(text, left_mention.end, match.start())
        right_same_clause = right_mention is not None and not _has_clause_break(text, match.end(), right_mention.start)
        anchors.append(
            (
                _semantic_lemma(match.group(0)),
                left,
                right,
                left_same_clause,
                right_same_clause,
            )
        )
    return tuple(anchors)


_SENTENCE_ABBREVIATIONS = frozenset(
    {
        "abs",
        "art",
        "b",
        "bzw",
        "ca",
        "d",
        "dr",
        "etc",
        "ggf",
        "h",
        "lit",
        "nr",
        "prof",
        "s",
        "u",
        "usw",
        "vgl",
        "z",
    }
)


def _sentence_type_marks(text: str) -> tuple[tuple[int, str], ...]:
    excluded_ranges = (
        tuple(match.span() for match in _NUMBER.finditer(text))
        + _legal_reference_ranges(text)
        + tuple(match.span() for match in _CRITICAL_LITERAL.finditer(text))
    )
    result: list[tuple[int, str]] = []
    for match in re.finditer(r"[.!?]", text):
        if any(match.start() < end and start < match.end() for start, end in excluded_ranges):
            continue
        if match.group(0) == ".":
            preceding = list(_WORD.finditer(text, 0, match.start()))
            if preceding and _normalize_word(preceding[-1].group(0)) in _SENTENCE_ABBREVIATIONS:
                continue
        result.append((match.start(), match.group(0)))
    return tuple(result)


def _sentence_type_anchors(
    text: str,
) -> tuple[tuple[str, str, int, int, tuple[str, ...]], ...]:
    words = _lexical_mentions(text)
    grouped: dict[tuple[str, str, int, int], list[str]] = {}
    for position, mark in _sentence_type_marks(text):
        before = [item for item in words if item.end <= position]
        after = [item for item in words if item.start > position]
        left_mention = before[-1] if before else None
        right_mention = after[0] if after else None
        left = left_mention.normalized if left_mention is not None else ""
        right = right_mention.normalized if right_mention is not None else ""
        left_occurrence = sum(item.normalized == left for item in before) - 1 if left else -1
        right_occurrence = (
            sum(
                item.normalized == right
                for item in words
                if right_mention is not None and item.start <= right_mention.start
            )
            - 1
            if right
            else -1
        )
        grouped.setdefault((left, right, left_occurrence, right_occurrence), []).append(mark)
    return tuple((*key, tuple(marks)) for key, marks in grouped.items())


def _validate_explicit_sentence_types(source: str, candidate: str) -> None:
    candidate_words = _lexical_mentions(candidate)
    candidate_marks = _sentence_type_marks(candidate)
    for left, right, left_occurrence, right_occurrence, source_marks in _sentence_type_anchors(source):
        left_matches = [item for item in candidate_words if _tokens_equivalent(item.normalized, left)] if left else []
        right_matches = (
            [item for item in candidate_words if _tokens_equivalent(item.normalized, right)] if right else []
        )
        if left and (left_occurrence < 0 or left_occurrence >= len(left_matches)):
            continue
        if right and (right_occurrence < 0 or right_occurrence >= len(right_matches)):
            continue
        gap_start = left_matches[left_occurrence].end if left else 0
        gap_end = right_matches[right_occurrence].start if right else len(candidate)
        if gap_start > gap_end:
            continue
        observed = tuple(mark for position, mark in candidate_marks if gap_start <= position < gap_end)
        if tuple(mark for mark in observed if mark in "?!") != tuple(mark for mark in source_marks if mark in "?!"):
            raise SafetyError("changed_sentence_type")
        if observed and (len(observed) < len(source_marks) or observed[-len(source_marks) :] != source_marks):
            raise SafetyError("changed_sentence_type")


_SEMANTIC_SYMBOL_LITERALS = frozenset({"#", "@", "&", "*", "/", "\\", "§", "%", "‰", "°", "²", "³", "€", "$", "£"})


def _unbound_semantic_symbols(text: str) -> tuple[str, ...]:
    bound_ranges = tuple(
        [
            *((mention.start, mention.end) for mention in _semantic_number_mentions(text)),
            *((mention.start, mention.end) for mention in _unit_mentions(text)),
            *_legal_reference_ranges(text),
            *(match.span() for match in _CRITICAL_LITERAL.finditer(text)),
        ]
    )
    return tuple(
        character
        for index, character in enumerate(text)
        if (unicodedata.category(character).startswith("S") or character in _SEMANTIC_SYMBOL_LITERALS)
        and not any(start <= index < end for start, end in bound_ranges)
    )


def _validate_unbound_semantic_symbols(source: str, candidate: str) -> None:
    source_symbols = _unbound_semantic_symbols(source)
    candidate_symbols = _unbound_semantic_symbols(candidate)
    if source_symbols == candidate_symbols:
        return
    if Counter(candidate_symbols) - Counter(source_symbols):
        raise SafetyError("content_addition")
    if Counter(source_symbols) - Counter(candidate_symbols):
        raise SafetyError("content_loss")
    raise SafetyError("changed_semantic_symbol")


def _lexical_mentions(text: str) -> list[_LexicalMention]:
    semantic_ranges = tuple(
        (start, end)
        for start, end in [
            *((mention.start, mention.end) for mention in _semantic_number_mentions(text)),
            *((mention.start, mention.end) for mention in _unit_mentions(text)),
            *_legal_reference_ranges(text),
        ]
    )
    return _meaningful_word_mentions(text, semantic_ranges)


def _replace_block_edits(
    source: list[_LexicalMention], candidate: list[_LexicalMention]
) -> tuple[list[int], list[int]]:
    if len(source) * len(candidate) > 4096:
        return list(range(len(source))), list(range(len(candidate)))
    rows = len(source) + 1
    columns = len(candidate) + 1
    costs = [[0] * columns for _ in range(rows)]
    operations = [[""] * columns for _ in range(rows)]
    for index in range(1, rows):
        costs[index][0] = index
        operations[index][0] = "delete"
    for index in range(1, columns):
        costs[0][index] = index
        operations[0][index] = "insert"
    for source_index in range(1, rows):
        for candidate_index in range(1, columns):
            equivalent = _tokens_equivalent(
                source[source_index - 1].normalized,
                candidate[candidate_index - 1].normalized,
            )
            choices = (
                (costs[source_index - 1][candidate_index - 1] + (0 if equivalent else 2), "match"),
                (costs[source_index - 1][candidate_index] + 1, "delete"),
                (costs[source_index][candidate_index - 1] + 1, "insert"),
            )
            costs[source_index][candidate_index], operations[source_index][candidate_index] = min(
                choices, key=lambda item: (item[0], {"match": 0, "delete": 1, "insert": 2}[item[1]])
            )
    deleted: list[int] = []
    inserted: list[int] = []
    source_index = len(source)
    candidate_index = len(candidate)
    while source_index or candidate_index:
        operation = operations[source_index][candidate_index]
        if operation == "match":
            if not _tokens_equivalent(
                source[source_index - 1].normalized,
                candidate[candidate_index - 1].normalized,
            ):
                deleted.append(source_index - 1)
                inserted.append(candidate_index - 1)
            source_index -= 1
            candidate_index -= 1
        elif operation == "delete":
            deleted.append(source_index - 1)
            source_index -= 1
        else:
            inserted.append(candidate_index - 1)
            candidate_index -= 1
    return sorted(deleted), sorted(inserted)


def _validate_lexical_edits(source: str, candidate: str) -> None:
    source_mentions = _lexical_mentions(source)
    candidate_mentions = _lexical_mentions(candidate)
    source_values = [item.normalized for item in source_mentions]
    candidate_values = [item.normalized for item in candidate_mentions]
    matcher = SequenceMatcher(None, source_values, candidate_values, autojunk=False)
    deleted: list[int] = []
    inserted: list[int] = []
    for tag, source_start, source_end, candidate_start, candidate_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        block_deleted, block_inserted = _replace_block_edits(
            source_mentions[source_start:source_end],
            candidate_mentions[candidate_start:candidate_end],
        )
        deleted.extend(source_start + index for index in block_deleted)
        inserted.extend(candidate_start + index for index in block_inserted)

    correction_ranges = _self_correction_ranges(source)
    filler_ranges = _explicit_filler_ranges(source)
    deleted_set = set(deleted)
    for index in deleted:
        mention = source_mentions[index]
        matching_filler_ranges = [
            (start, end) for start, end in filler_ranges if mention.start < end and start < mention.end
        ]
        if matching_filler_ranges and any(
            {
                other_index
                for other_index, other in enumerate(source_mentions)
                if other.start < end and start < other.end
            }
            <= deleted_set
            for start, end in matching_filler_ranges
        ):
            continue
        if any(mention.start < end and start < mention.end for start, end in correction_ranges):
            continue
        previous_same = (
            index > 0
            and _tokens_equivalent(source_mentions[index - 1].normalized, mention.normalized)
            and index - 1 not in deleted_set
        )
        next_same = (
            index + 1 < len(source_mentions)
            and _tokens_equivalent(source_mentions[index + 1].normalized, mention.normalized)
            and index + 1 not in deleted_set
        )
        correction_rebind = any(
            other_index not in deleted_set
            and _tokens_equivalent(other.normalized, mention.normalized)
            and any(
                min(mention.start, other.end) < end and start < max(mention.end, other.start)
                for start, end in correction_ranges
            )
            for other_index, other in enumerate(source_mentions)
            if other_index != index
        )
        if previous_same or next_same or correction_rebind:
            continue
        raise SafetyError("content_loss")

    # The budget is deliberately constant, not proportional to transcript
    # length.  It permits at most two closed-list grammar repairs and no novel
    # content word, however long the dictation is.
    grammar_insertions = 0
    for index in inserted:
        if candidate_mentions[index].normalized not in _GRAMMAR_INSERTION_WORDS:
            raise SafetyError("content_novelty")
        grammar_insertions += 1
    if grammar_insertions > 2:
        raise SafetyError("content_addition")


def _critical_ranges(text: str) -> tuple[tuple[int, int, str], ...]:
    candidates: list[tuple[int, int, str]] = []
    for pattern in (_AMOUNT, _LEGAL_REFERENCE, _NORM, _CRITICAL_LITERAL, _NUMBER):
        candidates.extend((match.start(), match.end(), match.group(0)) for match in pattern.finditer(text))
    selected: list[tuple[int, int, str]] = []
    for item in sorted(candidates, key=lambda value: (value[0], -(value[1] - value[0]))):
        if any(item[0] < other[1] and other[0] < item[1] for other in selected):
            continue
        selected.append(item)
    return tuple(sorted(selected))


def load_policy_markers(path: Path) -> tuple[str, ...]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SafetyError("invalid_protection_policy") from error
    if not isinstance(value, dict):
        raise SafetyError("invalid_protection_policy")
    policy_id = value.get("policy_id")
    if policy_id not in {"lfm2_qad_lexical_v1", "gemma_lexical_v1"}:
        raise SafetyError("invalid_protection_policy")
    if policy_id == "lfm2_qad_lexical_v1":
        if (
            value.get("schema_version") != 2
            or set(value) != {"schema_version", "policy_id", "model_binding", "max_spans", "markers"}
            or value.get("model_binding")
            != {
                "base_repository_id": "LiquidAI/LFM2.5-350M-Base",
                "base_revision": "9960764e30892e01f29a6dc23df2533fcd8bd5ae",
                "tokenizer_json_sha256": "4905ab82b2cfc25e0c88adc8f4eeffe759c57c5626312b30b0aaeaf8ad3379bc",
                "vocabulary_size": 65_536,
                "prompt_contract": "plain_completion_v1",
                "catalog_prompt_sha256": "e0ff2d5297f3d4d5ae7b8af85ea1cf52a24704bfb2e61990eab6de52b42058d8",
                "training_prompt_sha256": "372f879803334a68e310fe2e658c11678600baf0f4ef72834e4acd409f747dd6",
                "output_contract": "plain_text_v1",
                "generation_max_new_tokens": 384,
                "runtime_input": "raw_transcript_no_keep_markers",
            }
        ):
            raise SafetyError("invalid_protection_policy")
    elif value.get("schema_version") != 1:
        raise SafetyError("invalid_protection_policy")
    if value.get("max_spans") != 32 or not isinstance(value.get("markers"), list):
        raise SafetyError("invalid_protection_policy")
    markers = tuple(chr(ord("A") + index) if index < 26 else f"A{chr(ord('A') + index - 26)}" for index in range(32))
    expected = tuple(f"⟦KEEP_{label}⟧" for label in markers)
    observed: list[str] = []
    for index, item in enumerate(value["markers"]):
        if (
            not isinstance(item, dict)
            or (policy_id == "lfm2_qad_lexical_v1" and set(item) != {"marker_index", "marker"})
            or item.get("marker_index") != index
            or not isinstance(item.get("marker"), str)
        ):
            raise SafetyError("invalid_protection_policy")
        observed.append(item["marker"])
    if tuple(observed) != expected:
        raise SafetyError("invalid_protection_policy")
    return expected


def protect_transcript(text: str, markers: tuple[str, ...]) -> ProtectedTranscript:
    if "⟦KEEP_" in text:
        raise SafetyError("reserved_marker_collision")
    ranges = _critical_ranges(text)
    if len(ranges) > len(markers):
        raise SafetyError("too_many_protected_spans")
    pieces: list[str] = []
    values: list[str] = []
    position = 0
    for index, (start, end, value) in enumerate(ranges):
        pieces.append(text[position:start])
        pieces.append(markers[index])
        values.append(value)
        position = end
    pieces.append(text[position:])
    return ProtectedTranscript("".join(pieces), markers[: len(values)], tuple(values))


def restore_protected_values(protected: ProtectedTranscript, generated: str) -> str:
    if tuple(match.group(0) for match in _MARKER.finditer(generated)) != protected.markers:
        raise SafetyError("damaged_placeholder")
    restored = generated
    for marker, value in zip(protected.markers, protected.values, strict=True):
        if restored.count(marker) != 1:
            raise SafetyError("damaged_placeholder")
        restored = restored.replace(marker, value)
    if _MARKER.search(restored) or "⟦KEEP_" in restored:
        raise SafetyError("unknown_placeholder")
    return restored


def _inline_text(value: str) -> str:
    output: list[str] = []
    position = 0
    open_tag: str | None = None
    for match in _TAG.finditer(value):
        output.append(value[position : match.start()])
        closing, tag = match.groups()
        if tag == "BR" and not closing and open_tag is None:
            output.append("\n")
        elif tag in _INLINE_TAGS:
            if closing and open_tag == tag:
                open_tag = None
            elif not closing and open_tag is None:
                open_tag = tag
            else:
                raise SafetyError("invalid_sst")
        else:
            raise SafetyError("invalid_sst")
        position = match.end()
    output.append(value[position:])
    if open_tag is not None:
        raise SafetyError("invalid_sst")
    text = "".join(output)
    if not text:
        raise SafetyError("invalid_sst")
    return text


def render_plain_sst(value: str) -> str:
    if not value.startswith("[DOC]\n") or not value.endswith("\n[/DOC]"):
        raise SafetyError("invalid_sst")
    lines = value.split("\n")
    index = 1
    sections: list[str] = []
    block_count = 0
    while index < len(lines) - 1:
        line = lines[index]
        block_count += 1
        if block_count > 128:
            raise SafetyError("invalid_structure")
        if line in {"[OL]", "[UL]"}:
            closing = "[/OL]" if line == "[OL]" else "[/UL]"
            ordered = line == "[OL]"
            index += 1
            items: list[str] = []
            while index < len(lines) - 1 and lines[index] != closing:
                match = re.fullmatch(r"\[LI([123])\](.*)\[/LI\1\]", lines[index])
                if match is None or len(items) >= 256:
                    raise SafetyError("invalid_sst")
                prefix = "1." if ordered else "-"
                items.append(f"{'  ' * (int(match.group(1)) - 1)}{prefix} {_inline_text(match.group(2))}")
                index += 1
            if index >= len(lines) - 1 or lines[index] != closing or not items:
                raise SafetyError("invalid_sst")
            sections.append("\n".join(items))
            index += 1
            continue
        if line == "[SIGNATURE]":
            index += 1
            signature: list[str] = []
            while index < len(lines) - 1 and lines[index] != "[/SIGNATURE]":
                match = re.fullmatch(r"\[LINE\]([^\[]+)\[/LINE\]", lines[index])
                if match is None or len(signature) >= 32:
                    raise SafetyError("invalid_sst")
                signature.append(match.group(1))
                index += 1
            if index >= len(lines) - 1 or lines[index] != "[/SIGNATURE]" or not signature:
                raise SafetyError("invalid_sst")
            sections.append("\n".join(signature))
            index += 1
            continue
        match = re.match(r"^\[([A-Z0-9_]+)\](.*)$", line)
        if match is None or match.group(1) not in _TEXT_TAGS:
            raise SafetyError("invalid_sst")
        tag, first = match.groups()
        closing = f"[/{tag}]"
        content = [first]
        index += 1
        while not content[-1].endswith(closing):
            if index >= len(lines) - 1:
                raise SafetyError("invalid_sst")
            content.append(lines[index])
            index += 1
        content[-1] = content[-1][: -len(closing)]
        sections.append(_inline_text("\n".join(content)))
    if index != len(lines) - 1 or lines[index] != "[/DOC]" or not sections:
        raise SafetyError("invalid_sst")
    return "\n\n".join(sections)


def render_plain_text(value: str) -> str:
    """Validate and normalize an unwrapped plain_text_v1 completion."""

    text = value.strip()
    if not text:
        raise SafetyError("empty_output")
    text = text.replace("\r\n", "\n")
    if "\r" in text or any(character != "\n" and unicodedata.category(character).startswith("C") for character in text):
        raise SafetyError("control_character_leak")
    if "⟦KEEP_" in text or _MARKER.search(text):
        raise SafetyError("marker_leak")
    if _PLAIN_CONTROL_MARKUP.search(text):
        raise SafetyError("control_markup_leak")
    if (
        _PLAIN_WRAPPER_LABEL.search(text)
        or _PLAIN_BRACKET_LABEL.match(text)
        or _PLAIN_BOILERPLATE.match(text)
        or _PLAIN_OUTER_XML.fullmatch(text)
    ):
        raise SafetyError("unsafe_plain_structure")
    if any(text.startswith(opening) and text.endswith(closing) for opening, closing in _PLAIN_OUTER_QUOTES):
        raise SafetyError("unsafe_plain_structure")
    nonempty_lines = [line for line in text.splitlines() if line.strip()]
    if nonempty_lines and all(line.lstrip().startswith(">") for line in nonempty_lines):
        raise SafetyError("unsafe_plain_structure")
    if text[0] in "[{" and text[-1] in "]}":
        try:
            structured = json.loads(text)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(structured, dict | list):
                raise SafetyError("unsafe_plain_structure")
    if "```" in text or "~~~" in text:
        raise SafetyError("unsafe_plain_structure")
    if text.count("\n") > 4096:
        raise SafetyError("unsafe_plain_structure")
    return text


def validate_content(source: str, candidate: str) -> None:
    checks = (
        (_NORM, "changed_norm"),
        (_CRITICAL_LITERAL, "changed_critical_span"),
    )
    for pattern, code in checks:
        if Counter(pattern.findall(source)) != Counter(pattern.findall(candidate)):
            raise SafetyError(code)
    if _legal_reference_anchors(source) != _legal_reference_anchors(candidate):
        raise SafetyError("changed_legal_reference")
    if _list_item_anchors(source) != _list_item_anchors(candidate):
        raise SafetyError("changed_list_structure")
    if Counter(_semantic_number_anchors(source)) != Counter(_semantic_number_anchors(candidate)):
        raise SafetyError("changed_number")
    if _unit_anchors(source) != _unit_anchors(candidate):
        raise SafetyError("changed_unit")
    source_polarity = Counter(_semantic_lemma(match.group(0)) for match in _POLARITY.finditer(source))
    candidate_polarity = Counter(_semantic_lemma(match.group(0)) for match in _POLARITY.finditer(candidate))
    if source_polarity != candidate_polarity:
        raise SafetyError("changed_polarity")
    source_modality = Counter(match.group(0).casefold() for match in _MODALITY.finditer(source))
    candidate_modality = Counter(match.group(0).casefold() for match in _MODALITY.finditer(candidate))
    if source_modality != candidate_modality:
        raise SafetyError("changed_modality")
    source_length = sum(len(mention.normalized) for mention in _lexical_mentions(source))
    candidate_length = sum(len(mention.normalized) for mention in _lexical_mentions(candidate))
    if source_length >= 24 and candidate_length * 100 < source_length * 40:
        raise SafetyError("content_loss")
    if source_length >= 12 and candidate_length > source_length * 3 + 40:
        raise SafetyError("content_addition")


def validate_plain_text_content(source: str, candidate: str) -> None:
    """Prove locally bound semantics and a closed set of permitted deletions."""

    lexical_source = _without_self_corrections(source)
    lexical_source = _without_explicit_fillers(lexical_source)
    lexical_source = _without_exact_word_repetitions(lexical_source)
    _validate_bound_spoken_format_commands(lexical_source, candidate)
    lexical_source = _without_bound_contextual_list_connectives(lexical_source, candidate)
    lexical_source = _render_unambiguous_spoken_legal_references(lexical_source)
    lexical_source = _render_unambiguous_spoken_format_commands(lexical_source)
    lexical_source = _render_spelled_initialisms(lexical_source)
    lexical_source = _render_standard_abbreviations(lexical_source)
    lexical_source = _render_spoken_court_roman(lexical_source)
    semantic_candidate = _render_spoken_court_roman(
        _render_standard_abbreviations(_render_spelled_initialisms(candidate))
    )
    semantic_source = lexical_source
    _validate_unbound_semantic_symbols(semantic_source, semantic_candidate)
    validate_content(semantic_source, semantic_candidate)
    # Detect gross invention before more specific semantic-anchor diagnostics.
    # The allowance is a constant two closed-list grammar tokens, never a
    # transcript-length-dependent invention budget.
    if len(_lexical_mentions(semantic_candidate)) > len(_lexical_mentions(semantic_source)) + 2:
        raise SafetyError("content_addition")
    _validate_positioned_semantic_anchors(semantic_source, semantic_candidate)
    if _semantic_anchors(semantic_source) != _semantic_anchors(semantic_candidate):
        raise SafetyError("changed_semantic_anchor")
    if _named_role_anchors(semantic_source) != _named_role_anchors(semantic_candidate):
        raise SafetyError("changed_named_anchor")
    if _local_operator_anchors(semantic_source, _POLARITY) != _local_operator_anchors(semantic_candidate, _POLARITY):
        raise SafetyError("changed_polarity_position")
    if _local_operator_anchors(semantic_source, _MODALITY) != _local_operator_anchors(semantic_candidate, _MODALITY):
        raise SafetyError("changed_modality_position")
    _validate_explicit_sentence_types(semantic_source, semantic_candidate)
    _validate_lexical_edits(semantic_source, semantic_candidate)
