import pytest

from scriber_polishing.document_ast import Block, BlockType, Document, ListItem


def test_document_ast_rejects_a_list_block_without_items() -> None:
    with pytest.raises(ValueError, match="require items"):
        Block(type=BlockType.ORDERED_LIST)


def test_document_ast_accepts_a_canonical_list_document() -> None:
    document = Document(blocks=(Block(type=BlockType.UNORDERED_LIST, items=(ListItem(level=1, text="Punkt"),)),))

    assert document.blocks[0].items[0].text == "Punkt"


@pytest.mark.parametrize(
    "items",
    [
        (ListItem(level=2, text="Kein Elterneintrag"),),
        (ListItem(level=1, text="Eltern"), ListItem(level=3, text="Ebene übersprungen")),
    ],
)
def test_document_ast_rejects_invalid_list_nesting(items: tuple[ListItem, ...]) -> None:
    with pytest.raises(ValueError, match="level"):
        Block(type=BlockType.UNORDERED_LIST, items=items)
