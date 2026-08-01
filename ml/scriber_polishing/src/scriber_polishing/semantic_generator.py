"""Deterministic semantic-plan and document generation for local data factory runs."""

from __future__ import annotations

from dataclasses import dataclass

from .document_ast import Block, BlockType, Document, Inline, InlineStyle, ListItem
from .seed_library import SeedRecord, select_seed
from .sst_renderer import render_sst
from .target_renderer import render_html, render_markdown, render_plain_text


@dataclass(frozen=True, slots=True)
class SemanticPlan:
    """Primary fact truth for a generated canonical document."""

    document_id: str
    seed: int
    family: str
    domain: str
    facts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GeneratedExample:
    """A canonical AST and projections derived only from that AST."""

    plan: SemanticPlan
    document: Document
    target_sst: str
    target_plain_text: str
    target_markdown: str
    target_html: str
    formatting_commands: tuple[str, ...]


def generate_semantic_plan(seed: int) -> SemanticPlan:
    """Create a deterministic, synthetic fact plan from a local seed record."""
    record = select_seed(seed)
    facts = (record.subject, record.body, *record.list_items)
    return SemanticPlan(
        document_id=f"local-{record.family.value}-{seed:08d}",
        seed=seed,
        family=record.family.value,
        domain=record.domain,
        facts=facts,
    )


def generate_document(plan: SemanticPlan) -> Document:
    """Build the canonical document AST for a plan, without rendering shortcuts."""
    record = select_seed(plan.seed)
    if record.family.value != plan.family:
        raise ValueError("semantic plan family does not match its deterministic seed")
    return _document_from_record(record)


def generate_example(seed: int) -> GeneratedExample:
    """Generate one complete, reproducible local data-factory vertical slice."""
    plan = generate_semantic_plan(seed)
    document = generate_document(plan)
    record = select_seed(seed)
    return GeneratedExample(
        plan=plan,
        document=document,
        target_sst=render_sst(document),
        target_plain_text=render_plain_text(document),
        target_markdown=render_markdown(document),
        target_html=render_html(document),
        formatting_commands=record.formatting_commands,
    )


def _document_from_record(record: SeedRecord) -> Document:
    subject_inlines = (Inline(record.subject, InlineStyle.BOLD_UNDERLINE),)
    return Document(
        blocks=(
            Block(BlockType.SUBJECT, text=record.subject, inlines=subject_inlines),
            Block(BlockType.SALUTATION, text=record.salutation),
            Block(BlockType.PARAGRAPH, text=record.body),
            Block(
                BlockType.UNORDERED_LIST,
                items=tuple(ListItem(level=1, text=item) for item in record.list_items),
            ),
            Block(BlockType.CLOSING, text=record.closing),
            Block(BlockType.SIGNATURE, lines=record.signature),
        )
    )
