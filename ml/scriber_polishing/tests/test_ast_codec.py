import pytest

from scriber_polishing.ast_codec import document_from_dict, document_to_dict
from scriber_polishing.document_ast import Block, BlockType, Document, Inline, InlineStyle, ListItem


def test_document_ast_json_round_trip_is_lossless() -> None:
    document = Document(
        blocks=(
            Block(
                type=BlockType.SUBJECT,
                text="Projekt Atlas",
                inlines=(Inline("Projekt ", InlineStyle.PLAIN), Inline("Atlas", InlineStyle.BOLD_UNDERLINE)),
            ),
            Block(
                type=BlockType.ORDERED_LIST,
                items=(
                    ListItem(level=1, text="Prüfung"),
                    ListItem(
                        level=2,
                        text="bis 14:30 Uhr",
                        inlines=(Inline("bis "), Inline("14:30 Uhr", InlineStyle.BOLD)),
                    ),
                ),
            ),
            Block(type=BlockType.SIGNATURE, lines=("Mara Feld", "Projektleitung")),
        )
    )

    encoded = document_to_dict(document)

    assert encoded["schema_version"] == 1
    assert document_from_dict(encoded) == document


@pytest.mark.parametrize(
    "payload,error",
    [
        ({"schema_version": 2, "blocks": []}, "schema_version"),
        ({"schema_version": 1, "blocks": [], "unknown": True}, "unexpected"),
        (
            {
                "schema_version": 1,
                "blocks": [{"type": "paragraph", "text": "Text", "inlines": [], "items": [], "lines": [], "x": 1}],
            },
            "unexpected",
        ),
        (
            {
                "schema_version": 1,
                "blocks": [{"type": "script", "text": "Text", "inlines": [], "items": [], "lines": []}],
            },
            "block type",
        ),
    ],
)
def test_document_ast_json_decoder_fails_closed(payload: dict, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        document_from_dict(payload)
