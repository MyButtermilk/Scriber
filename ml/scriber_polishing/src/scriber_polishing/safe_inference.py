"""Fail-closed product inference layered above raw evaluation generation."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from .document_ast import BlockType, Document
from .protected_spans import (
    LEGACY_POLICY_ID,
    ProtectedSpan,
    ProtectedText,
    product_span_ranges,
    protect_with_policy,
)
from .sst_parser import SSTParseError, parse_sst
from .sst_renderer import render_sst
from .target_renderer import render_html, render_markdown, render_plain_text


class RawPrediction(Protocol):
    """The narrow raw-generation result consumed by the product safety layer."""

    prediction_sst: str
    valid_sst: bool
    restoration_error: str | None


@dataclass(frozen=True, slots=True)
class InferenceIssue:
    """One bounded, machine-actionable safety finding."""

    stage: str
    code: str
    severity: str


@dataclass(frozen=True, slots=True)
class ProductDiagnostics:
    """Diagnostics deliberately kept separate from the rendered user output."""

    schema_version: int
    source_sha256: str
    source_characters: int
    chunk_count: int
    status: str
    issues: tuple[InferenceIssue, ...]
    active_learning_priority: str

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic, content-free diagnostics payload."""

        return {
            "schema_version": self.schema_version,
            "source_sha256": self.source_sha256,
            "source_characters": self.source_characters,
            "chunk_count": self.chunk_count,
            "status": self.status,
            "issues": [
                {
                    "stage": issue.stage,
                    "code": issue.code,
                    "severity": issue.severity,
                }
                for issue in self.issues
            ],
            "active_learning": {
                "prioritized": self.active_learning_priority != "none",
                "priority": self.active_learning_priority,
                "reason_codes": (
                    [issue.code for issue in self.issues]
                    if self.active_learning_priority != "none"
                    else []
                ),
            },
        }


@dataclass(frozen=True, slots=True)
class ProductInferenceResult:
    """Safe product output and its non-content diagnostics."""

    output: str
    requested_format: str
    actual_format: str
    diagnostics: ProductDiagnostics


@dataclass(frozen=True, slots=True)
class TranscriptChunk:
    """An exact source slice ending only at a conservative document boundary."""

    text: str
    start: int
    end: int
    boundary: str


@dataclass(frozen=True, slots=True)
class WarmupResult:
    """Bounded evidence that the local generation path was exercised."""

    schema_version: int
    success: bool
    valid_sst: bool
    error_code: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "success": self.success,
            "valid_sst": self.valid_sst,
            "error_code": self.error_code,
        }


_AMOUNT = re.compile(
    r"(?<!\w)(?:"
    r"(?:EUR|USD|CHF|€|\$|£)\s*[-+]?\d[\d.]*(?:,\d+)?"
    r"|[-+]?\d[\d.]*(?:,\d+)?\s*(?:EUR|USD|CHF|€|\$|£)"
    r")(?!\w)",
    re.IGNORECASE,
)
_LEGAL_REFERENCE = re.compile(
    r"(?<!\w)(?:"
    r"§§?\s*\d+[a-zA-Z]*(?:\s+(?:Abs\.\s*\d+|Satz\s*\d+|Nr\.\s*\d+|"
    r"Buchst\.\s*[a-z]))*(?:\s+[A-Z][A-Za-z0-9.-]*)?"
    r"|(?:Art\.|Artikel)\s*\d+[a-zA-Z]*(?:\s+[A-Z][A-Za-z0-9.-]*)?"
    r"|ECLI:[A-Z]{2,}:[A-Z0-9]+(?::[A-Z0-9.]+)+"
    r"|Az\.\s*[\w./ -]+?(?=(?:,|;|$))"
    r")",
)
_NORM = re.compile(
    r"(?<!\w)(?:(?:DIN(?:\s+EN)?|ISO|IEC|VDE)\s+[A-Z0-9]+(?:[-:/.][A-Z0-9]+)*)(?!\w)",
    re.IGNORECASE,
)
_CRITICAL_LITERAL = re.compile(
    r"(?:"
    r"https?://[^\s,]+"
    r"|[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
    r"|\b[A-ZÄÖÜ]{2,}[/-][A-Z0-9ÄÖÜ/-]*\d[A-Z0-9ÄÖÜ/-]*\b"
    r"|\b(?:DE\d{20}|[A-Z]{2}\d{2}(?:\s?[A-Z0-9]){10,30})\b"
    r"|\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"
    r")"
)
_NUMBER = re.compile(r"[-+]?\d+(?:[.,:/-]\d+)*")
_POLARITY = re.compile(
    r"\b(?:nicht|nichts|kein(?:e|en|em|er|es)?|nie|niemals|weder|ohne)\b",
    re.IGNORECASE,
)
_MODALITY = re.compile(
    r"\b(?:"
    r"muss|musst|müsst|müssen|musste|mussten|müsste|müssten|"
    r"kann|kannst|könnt|können|konnte|konnten|könnte|könnten|"
    r"darf|darfst|dürft|dürfen|durfte|durften|dürfte|dürften|"
    r"soll|sollst|sollt|sollen|sollte|sollten|"
    r"wird|wirst|werdet|werden|würde|würden"
    r")\b",
    re.IGNORECASE,
)
_PARAGRAPH_BOUNDARY = re.compile(r"\n[ \t]*\n+")
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])(?:[ \t]+|\n(?![ \t]*\n))")
_ABBREVIATION_END = re.compile(
    r"(?:\b(?:bzw|ca|Dr|Prof|Nr|S|Abs|Art|vgl|etc)\.|\bz\.\s*B\.|\bu\.\s*a\.)$",
    re.IGNORECASE,
)
_HEADING_CUE = re.compile(
    r"\b(?:hauptüberschrift|zwischenüberschrift|überschrift(?:\s+(?:eins|zwei|"
    r"der\s+(?:ersten|zweiten)\s+ebene))?)\b",
    re.IGNORECASE,
)
_SST_TOKEN = re.compile(r"\[/?[A-Z][A-Z0-9_]*\]")
_TAG_LIKE = re.compile(
    r"\[/?[A-Za-z][A-Za-z0-9_]*(?:\s+[^\]\r\n]{1,80})?\]"
)
_ALLOWED_SST_TOKENS = frozenset(
    {
        "[DOC]",
        "[/DOC]",
        "[SUBJECT]",
        "[/SUBJECT]",
        "[H1]",
        "[/H1]",
        "[H2]",
        "[/H2]",
        "[SALUTATION]",
        "[/SALUTATION]",
        "[DATE_LINE]",
        "[/DATE_LINE]",
        "[P]",
        "[/P]",
        "[QUOTE]",
        "[/QUOTE]",
        "[OL]",
        "[/OL]",
        "[UL]",
        "[/UL]",
        "[LI1]",
        "[/LI1]",
        "[LI2]",
        "[/LI2]",
        "[LI3]",
        "[/LI3]",
        "[CLOSING]",
        "[/CLOSING]",
        "[SIGNATURE]",
        "[/SIGNATURE]",
        "[LINE]",
        "[/LINE]",
        "[ATTACHMENTS]",
        "[/ATTACHMENTS]",
        "[PS]",
        "[/PS]",
        "[B]",
        "[/B]",
        "[U]",
        "[/U]",
        "[BU]",
        "[/BU]",
        "[BR]",
    }
)
_LINE_END_TOKENS = (
    "[/SUBJECT]",
    "[/H1]",
    "[/H2]",
    "[/SALUTATION]",
    "[/DATE_LINE]",
    "[/P]",
    "[/QUOTE]",
    "[/OL]",
    "[/UL]",
    "[/LI1]",
    "[/LI2]",
    "[/LI3]",
    "[/CLOSING]",
    "[/SIGNATURE]",
    "[/LINE]",
    "[/ATTACHMENTS]",
    "[/PS]",
)


def _ordered_critical_values(value: str) -> tuple[str, ...]:
    return tuple(item[2] for item in _product_span_ranges(value))


def _product_span_ranges(text: str) -> tuple[tuple[int, int, str], ...]:
    return product_span_ranges(text)


def protect_product_spans(
    text: str,
    policy: Mapping[str, object] | None = None,
) -> ProtectedText:
    """Protect all deterministic product-critical literals before generation."""

    if policy is not None and policy.get("policy_id") != LEGACY_POLICY_ID:
        return protect_with_policy(text, policy)
    if "⟦SCRIBER_PROTECTED_" in text:
        raise ValueError("source transcript contains a reserved protected placeholder")
    pieces: list[str] = []
    spans: list[ProtectedSpan] = []
    position = 0
    for start, end, value in _product_span_ranges(text):
        pieces.append(text[position:start])
        placeholder = f"⟦SCRIBER_PROTECTED_{len(spans):04d}⟧"
        pieces.append(placeholder)
        spans.append(ProtectedSpan(placeholder=placeholder, value=value))
        position = end
    pieces.append(text[position:])
    return ProtectedText(text="".join(pieces), spans=tuple(spans))


def _inside_protected_span(
    position: int,
    spans: tuple[tuple[int, int, str], ...],
) -> bool:
    return any(start < position < end for start, end, _value in spans)


def conservative_chunks(
    transcript: str,
    *,
    max_characters: int = 3000,
) -> tuple[TranscriptChunk, ...]:
    """Partition exactly at paragraph/sentence boundaries, never inside a sentence."""

    if not transcript:
        raise ValueError("transcript must be non-empty")
    if max_characters <= 0:
        raise ValueError("max_characters must be positive")
    spans = _product_span_ranges(transcript)
    boundaries: dict[int, str] = {}
    for match in _PARAGRAPH_BOUNDARY.finditer(transcript):
        if not _inside_protected_span(match.end(), spans):
            boundaries[match.end()] = "paragraph"
    for match in _SENTENCE_BOUNDARY.finditer(transcript):
        position = match.end()
        if _inside_protected_span(match.start(), spans):
            continue
        prefix = transcript[: match.start()]
        if _ABBREVIATION_END.search(prefix):
            continue
        following = transcript[position:].lstrip()
        if following and not re.match(r'["„‚(\[]?[A-ZÄÖÜ0-9•*-]', following):
            continue
        boundaries.setdefault(position, "sentence")
    boundaries[len(transcript)] = "document_end"
    ordered = tuple(sorted(boundaries))
    chunks: list[TranscriptChunk] = []
    start = 0
    while start < len(transcript):
        target = start + max_characters
        before = [position for position in ordered if start < position <= target]
        end = (
            before[-1]
            if before
            else next(position for position in ordered if position > start)
        )
        chunks.append(
            TranscriptChunk(
                text=transcript[start:end],
                start=start,
                end=end,
                boundary=boundaries[end],
            )
        )
        start = end
    if "".join(chunk.text for chunk in chunks) != transcript:
        raise RuntimeError("internal chunking invariant failed")
    return tuple(chunks)


def warmup_product_inference(
    predictor: Callable[[str], RawPrediction],
) -> WarmupResult:
    """Exercise model loading and greedy generation with fixed private-free text."""

    try:
        prediction = predictor("Lokaler Warmup.")
    except Exception:
        return WarmupResult(
            schema_version=1,
            success=False,
            valid_sst=False,
            error_code="generation_error",
        )
    valid_sst = prediction.restoration_error is None
    if valid_sst:
        try:
            parse_sst(prediction.prediction_sst)
        except (SSTParseError, ValueError):
            valid_sst = False
    return WarmupResult(
        schema_version=1,
        success=True,
        valid_sst=valid_sst,
        error_code=None,
    )


def _content_issues(source: str, candidate: str) -> tuple[InferenceIssue, ...]:
    issues: list[InferenceIssue] = []
    if Counter(_AMOUNT.findall(source)) != Counter(_AMOUNT.findall(candidate)):
        issues.append(InferenceIssue("content_validation", "changed_amount", "critical"))
    if Counter(_LEGAL_REFERENCE.findall(source)) != Counter(_LEGAL_REFERENCE.findall(candidate)):
        issues.append(InferenceIssue("content_validation", "changed_legal_reference", "critical"))
    if Counter(_NORM.findall(source)) != Counter(_NORM.findall(candidate)):
        issues.append(InferenceIssue("content_validation", "changed_norm", "critical"))
    if Counter(_CRITICAL_LITERAL.findall(source)) != Counter(_CRITICAL_LITERAL.findall(candidate)):
        issues.append(InferenceIssue("content_validation", "changed_critical_span", "critical"))
    source_critical_order = _ordered_critical_values(source)
    candidate_critical_order = _ordered_critical_values(candidate)
    if (
        source_critical_order != candidate_critical_order
        and Counter(source_critical_order) == Counter(candidate_critical_order)
    ):
        issues.append(
            InferenceIssue(
                "content_validation",
                "reordered_critical_spans",
                "critical",
            )
        )
    if Counter(_NUMBER.findall(source)) != Counter(_NUMBER.findall(candidate)):
        issues.append(InferenceIssue("content_validation", "changed_number", "critical"))
    source_polarity = Counter(
        "kein" if match.group(0).casefold().startswith("kein") else match.group(0).casefold()
        for match in _POLARITY.finditer(source)
    )
    candidate_polarity = Counter(
        "kein" if match.group(0).casefold().startswith("kein") else match.group(0).casefold()
        for match in _POLARITY.finditer(candidate)
    )
    if source_polarity != candidate_polarity:
        issues.append(InferenceIssue("content_validation", "changed_polarity", "critical"))
    source_modality = Counter(match.group(0).casefold() for match in _MODALITY.finditer(source))
    candidate_modality = Counter(match.group(0).casefold() for match in _MODALITY.finditer(candidate))
    if source_modality != candidate_modality:
        issues.append(InferenceIssue("content_validation", "changed_modality", "critical"))
    source_content_length = len(re.sub(r"\W", "", source, flags=re.UNICODE))
    candidate_content_length = len(re.sub(r"\W", "", candidate, flags=re.UNICODE))
    if source_content_length >= 24 and candidate_content_length * 100 < source_content_length * 40:
        issues.append(InferenceIssue("content_validation", "content_loss", "critical"))
    if source_content_length >= 12 and candidate_content_length > source_content_length * 3 + 40:
        issues.append(InferenceIssue("content_validation", "content_addition", "critical"))
    return tuple(issues)


def _normalized_words(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold()))


def _ast_issues(source: str, document: Document) -> tuple[InferenceIssue, ...]:
    block_types = tuple(block.type for block in document.blocks)
    singleton_types = {
        BlockType.SUBJECT,
        BlockType.SALUTATION,
        BlockType.DATE_LINE,
        BlockType.CLOSING,
        BlockType.SIGNATURE,
        BlockType.ATTACHMENTS,
        BlockType.POST_SCRIPT,
    }
    counts = Counter(block_types)
    structurally_invalid = (
        len(document.blocks) > 128
        or any(counts[block_type] > 1 for block_type in singleton_types)
        or (BlockType.SUBJECT in block_types and block_types[0] is not BlockType.SUBJECT)
        or (
            BlockType.POST_SCRIPT in block_types
            and block_types[-1] is not BlockType.POST_SCRIPT
        )
        or sum(
            len(block.items)
            for block in document.blocks
            if block.type in {BlockType.ORDERED_LIST, BlockType.UNORDERED_LIST}
        )
        > 256
        or sum(
            len(block.lines)
            for block in document.blocks
            if block.type is BlockType.SIGNATURE
        )
        > 32
    )
    if BlockType.CLOSING in block_types:
        closing_index = block_types.index(BlockType.CLOSING)
        allowed_tail = {
            BlockType.SIGNATURE,
            BlockType.ATTACHMENTS,
            BlockType.POST_SCRIPT,
        }
        structurally_invalid = structurally_invalid or any(
            block_type not in allowed_tail
            for block_type in block_types[closing_index + 1 :]
        )
    if structurally_invalid:
        return (InferenceIssue("ast_validation", "invalid_structure", "critical"),)

    heading_blocks = tuple(
        block
        for block in document.blocks
        if block.type in {BlockType.HEADING_1, BlockType.HEADING_2}
    )
    if not heading_blocks:
        return ()
    source_lines = {
        _normalized_words(line)
        for line in source.splitlines()
        if line.strip() and len(line.strip()) <= 160
    }
    normalized_source = f" {_normalized_words(source)} "
    explicit_heading = _HEADING_CUE.search(source) is not None
    if any(
        not (
            f" {_normalized_words(block.text)} " in normalized_source
            and (
                explicit_heading
                or (
                    _normalized_words(block.text) in source_lines
                    and len(source.splitlines()) > 1
                )
            )
        )
        for block in heading_blocks
    ):
        return (InferenceIssue("ast_validation", "invented_heading", "critical"),)
    return ()


def _diagnostics(
    source: str,
    *,
    status: str,
    issues: tuple[InferenceIssue, ...] = (),
    chunk_count: int = 1,
) -> ProductDiagnostics:
    learning_issues = tuple(
        issue for issue in issues if issue.code != "empty_transcript"
    )
    if any(issue.severity == "critical" for issue in learning_issues):
        priority = "critical"
    elif learning_issues:
        priority = "high"
    else:
        priority = "none"
    return ProductDiagnostics(
        schema_version=1,
        source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        source_characters=len(source),
        chunk_count=chunk_count,
        status=status,
        issues=issues,
        active_learning_priority=priority,
    )


def _visible_non_whitespace(value: str) -> str:
    without_tags = _SST_TOKEN.sub("", value)
    return re.sub(r"\s", "", without_tags)


def repair_sst_syntax(value: str) -> str | None:
    """Repair only missing SST line boundaries while preserving every text atom."""

    if not value or any(
        token not in _ALLOWED_SST_TOKENS for token in _TAG_LIKE.findall(value)
    ):
        return None
    repaired = value.replace("\r\n", "\n").replace("\r", "\n")
    if repaired.startswith("[DOC]") and not repaired.startswith("[DOC]\n"):
        repaired = "[DOC]\n" + repaired[len("[DOC]") :]
    if repaired.endswith("[/DOC]") and not repaired.endswith("\n[/DOC]"):
        repaired = repaired[: -len("[/DOC]")] + "\n[/DOC]"
    ends = "|".join(re.escape(token) for token in _LINE_END_TOKENS)
    containers = "|".join(
        re.escape(token)
        for token in ("[OL]", "[UL]", "[SIGNATURE]")
    )
    repaired = re.sub(rf"((?:{containers}))(?=[^\n])", r"\1\n", repaired)
    repaired = re.sub(rf"((?:{ends}))(?=[^\n])", r"\1\n", repaired)
    if _visible_non_whitespace(repaired) != _visible_non_whitespace(value):
        return None
    try:
        document = parse_sst(repaired)
    except (SSTParseError, ValueError):
        return None
    return render_sst(document)


def conservative_plain_text_cleanup(value: str) -> str | None:
    """Remove only known SST syntax, preserving all non-whitespace text in order."""

    tokens = _TAG_LIKE.findall(value)
    if any(token not in _ALLOWED_SST_TOKENS for token in tokens):
        return None
    output: list[str] = []
    position = 0
    for match in _SST_TOKEN.finditer(value):
        output.append(value[position : match.start()])
        token = match.group(0)
        if token == "[BR]" or token in _LINE_END_TOKENS:
            output.append("\n")
        position = match.end()
    output.append(value[position:])
    lines = [line.strip() for line in "".join(output).splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    if not cleaned or _visible_non_whitespace(cleaned) != _visible_non_whitespace(value):
        return None
    return cleaned


def _render(document: Document, output_format: str) -> str:
    if output_format == "plain":
        return render_plain_text(document)
    if output_format == "sst":
        return render_sst(document)
    if output_format == "markdown":
        return render_markdown(document)
    if output_format == "html":
        return render_html(document)
    raise ValueError(f"unsupported output format: {output_format}")


def run_product_inference(
    transcript: str,
    *,
    predictor: Callable[[str], RawPrediction],
    output_format: str = "plain",
    max_chunk_characters: int | None = 3000,
) -> ProductInferenceResult:
    """Run one safe product inference; content failures return the full source."""

    if not transcript.strip():
        issue = InferenceIssue("input_validation", "empty_transcript", "high")
        return ProductInferenceResult(
            transcript,
            output_format,
            "plain",
            _diagnostics(
                transcript,
                status="original_fallback",
                issues=(issue,),
            ),
        )
    if max_chunk_characters is not None and len(transcript) > max_chunk_characters:
        chunks = conservative_chunks(
            transcript,
            max_characters=max_chunk_characters,
        )
        results = tuple(
            run_product_inference(
                chunk.text,
                predictor=predictor,
                output_format="sst",
                max_chunk_characters=None,
            )
            for chunk in chunks
        )
        failed = tuple(
            result
            for result in results
            if result.diagnostics.status == "original_fallback"
        )
        if failed:
            issues = tuple(
                issue
                for result in failed
                for issue in result.diagnostics.issues
            )
            return ProductInferenceResult(
                transcript,
                output_format,
                "plain",
                _diagnostics(
                    transcript,
                    status="original_fallback",
                    issues=issues,
                    chunk_count=len(chunks),
                ),
            )
        if any(result.actual_format == "plain" for result in results):
            combined_plain = "\n\n".join(result.output.strip() for result in results)
            content_issues = _content_issues(transcript, combined_plain)
            if content_issues:
                return ProductInferenceResult(
                    transcript,
                    output_format,
                    "plain",
                    _diagnostics(
                        transcript,
                        status="original_fallback",
                        issues=content_issues,
                        chunk_count=len(chunks),
                    ),
                )
            cleanup_issues = tuple(
                issue
                for result in results
                for issue in result.diagnostics.issues
            )
            return ProductInferenceResult(
                combined_plain,
                output_format,
                "plain",
                _diagnostics(
                    transcript,
                    status="plain_text_fallback",
                    issues=cleanup_issues,
                    chunk_count=len(chunks),
                ),
            )
        documents = tuple(parse_sst(result.output) for result in results)
        document = Document(
            blocks=tuple(
                block
                for chunk_document in documents
                for block in chunk_document.blocks
            )
        )
        ast_issues = _ast_issues(transcript, document)
        combined_plain = render_plain_text(document)
        content_issues = _content_issues(transcript, combined_plain)
        if ast_issues or content_issues:
            issues = (*ast_issues, *content_issues)
            return ProductInferenceResult(
                transcript,
                output_format,
                "plain",
                _diagnostics(
                    transcript,
                    status="original_fallback",
                    issues=issues,
                    chunk_count=len(chunks),
                ),
            )
        issues = tuple(
            issue
            for result in results
            for issue in result.diagnostics.issues
        )
        status = "repaired_sst" if issues else "accepted"
        return ProductInferenceResult(
            _render(document, output_format),
            output_format,
            output_format,
            _diagnostics(
                transcript,
                status=status,
                issues=issues,
                chunk_count=len(chunks),
            ),
        )

    try:
        prediction = predictor(transcript)
    except Exception:
        issue = InferenceIssue("generation", "generation_error", "high")
        return ProductInferenceResult(
            transcript,
            output_format,
            "plain",
            _diagnostics(
                transcript,
                status="original_fallback",
                issues=(issue,),
            ),
        )
    if prediction.restoration_error is not None:
        issue = InferenceIssue("placeholder_restoration", "damaged_placeholder", "critical")
        return ProductInferenceResult(
            transcript,
            output_format,
            "plain",
            _diagnostics(transcript, status="original_fallback", issues=(issue,)),
        )
    status = "accepted"
    issues: tuple[InferenceIssue, ...] = ()
    safe_sst = prediction.prediction_sst
    try:
        document: Document = parse_sst(safe_sst)
    except (SSTParseError, ValueError):
        repaired = repair_sst_syntax(safe_sst)
        if repaired is None:
            cleaned = conservative_plain_text_cleanup(safe_sst)
            if cleaned is not None:
                content_issues = _content_issues(transcript, cleaned)
                if content_issues:
                    return ProductInferenceResult(
                        transcript,
                        output_format,
                        "plain",
                        _diagnostics(
                            transcript,
                            status="original_fallback",
                            issues=content_issues,
                        ),
                    )
                cleanup_issues = (
                    InferenceIssue("sst_parse", "invalid_sst", "high"),
                    InferenceIssue("plain_text_cleanup", "plain_text_cleanup", "high"),
                )
                return ProductInferenceResult(
                    cleaned,
                    output_format,
                    "plain",
                    _diagnostics(
                        transcript,
                        status="plain_text_fallback",
                        issues=cleanup_issues,
                    ),
                )
            issue = InferenceIssue("sst_parse", "invalid_sst", "high")
            return ProductInferenceResult(
                transcript,
                output_format,
                "plain",
                _diagnostics(transcript, status="original_fallback", issues=(issue,)),
            )
        safe_sst = repaired
        document = parse_sst(safe_sst)
        status = "repaired_sst"
        issues = (InferenceIssue("sst_repair", "sst_syntax_repaired", "high"),)
    plain = render_plain_text(document)
    ast_issues = _ast_issues(transcript, document)
    if ast_issues:
        return ProductInferenceResult(
            transcript,
            output_format,
            "plain",
            _diagnostics(transcript, status="original_fallback", issues=ast_issues),
        )
    content_issues = _content_issues(transcript, plain)
    if content_issues:
        return ProductInferenceResult(
            transcript,
            output_format,
            "plain",
            _diagnostics(transcript, status="original_fallback", issues=content_issues),
        )
    return ProductInferenceResult(
        _render(document, output_format),
        output_format,
        output_format,
        _diagnostics(transcript, status=status, issues=issues),
    )
