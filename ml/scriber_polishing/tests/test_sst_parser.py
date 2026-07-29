import pytest

from scriber_polishing.document_ast import BlockType
from scriber_polishing.sst_parser import SSTParseError, parse_sst
from scriber_polishing.sst_renderer import render_sst

SAMPLE_SST = """[DOC]
[SUBJECT]Stellungnahme zur Umsatzsteuerprüfung[/SUBJECT]
[SALUTATION]Sehr geehrte Frau Huber,[/SALUTATION]
[P]Bitte prüfen Sie § 233a AO.[/P]
[H2]Erforderliche Schritte[/H2]
[OL]
[LI1]Prüfung der Zinsläufe[/LI1]
[LI2]Rückmeldung bis zum 15.08.2026[/LI2]
[/OL]
[CLOSING]Mit freundlichen Grüßen[/CLOSING]
[SIGNATURE]
[LINE]Alexander Immler[/LINE]
[LINE]Geschäftsführer[/LINE]
[/SIGNATURE]
[/DOC]"""


def test_valid_sst_round_trips_through_the_document_ast() -> None:
    document = parse_sst(SAMPLE_SST)

    assert [block.type for block in document.blocks] == [
        BlockType.SUBJECT,
        BlockType.SALUTATION,
        BlockType.PARAGRAPH,
        BlockType.HEADING_2,
        BlockType.ORDERED_LIST,
        BlockType.CLOSING,
        BlockType.SIGNATURE,
    ]
    assert document.blocks[4].items[1].level == 2
    assert document.blocks[4].items[1].text == "Rückmeldung bis zum 15.08.2026"
    assert document.blocks[6].lines == ("Alexander Immler", "Geschäftsführer")
    assert render_sst(document) == SAMPLE_SST


def test_parser_preserves_allowed_inline_emphasis_in_a_paragraph() -> None:
    document = parse_sst("[DOC]\n[P]Bitte [B]dringend[/B] und [BU]vertraulich[/BU] prüfen.[/P]\n[/DOC]")

    assert [(part.text, part.style.value) for part in document.blocks[0].inlines] == [
        ("Bitte ", "plain"),
        ("dringend", "bold"),
        (" und ", "plain"),
        ("vertraulich", "bold_underline"),
        (" prüfen.", "plain"),
    ]
    assert render_sst(document) == "[DOC]\n[P]Bitte [B]dringend[/B] und [BU]vertraulich[/BU] prüfen.[/P]\n[/DOC]"


@pytest.mark.parametrize(
    "sst",
    [
        "[DOC]\n[P][B][U]Text[/U][/B][/P]\n[/DOC]",
        "[DOC]\n[P]Text[/U][/P]\n[/DOC]",
        "[DOC]\n[SCRIPT]alert(1)[/SCRIPT]\n[/DOC]",
        "[DOC]\n[OL]\n[LI1]Punkt[/LI2]\n[/OL]\n[/DOC]",
    ],
)
def test_parser_rejects_unknown_or_non_strictly_nested_tags(sst: str) -> None:
    with pytest.raises(SSTParseError):
        parse_sst(sst)
