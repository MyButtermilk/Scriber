import { Document, Text as InlineRun, ListItem, OrderedList, Page, UnorderedList } from "@formepdf/react";
import type { FontRegistration } from "@formepdf/react";
import type { ReactNode } from "react";

import { Badge } from "@/components/pdf/badge/badge";
import type { BadgeVariant } from "@/components/pdf/badge/badge";
import { PdfCard } from "@/components/pdf/card/card";
import { Divider } from "@/components/pdf/divider/divider";
import { Heading } from "@/components/pdf/heading/heading";
import { KeepTogether } from "@/components/pdf/keep-together/keep-together";
import { KeyValue } from "@/components/pdf/key-value/key-value";
import { PdfList } from "@/components/pdf/list/list";
import { PageFooter } from "@/components/pdf/page-footer/page-footer";
import { PageNumber } from "@/components/pdf/page-number/page-number";
import { Section } from "@/components/pdf/section/section";
import { Table, TableBody, TableCell, TableHeader, TableRow } from "@/components/pdf/table/table";
import { Text } from "@/components/pdf/text/text";
import { PdfcnThemeProvider } from "@/components/pdf/theme-provider";
import type { MeetingExportDocument, TranscriptExportDocument } from "@/lib/api-types";
import { Fixed, Text as PdfText, View } from "@/lib/pdf-primitives";
import type { Style } from "@/lib/pdf-primitives";

import { inlinesToText, parseSummaryForPdf, transcriptParagraphs } from "./summary-blocks";
import type { PdfBlock, PdfInline, PdfListBlock, PdfSummary } from "./summary-blocks";
import { normalizePdfText, singleLinePdfText, supportsItalic } from "./text";
import { scriberPdfTheme } from "./theme";

const theme = scriberPdfTheme;
const { colors } = theme;
const body = theme.typography.body;
const page = theme.spacing.page;

/** Localized words the transcript documents print around the user's content. */
export interface TranscriptPdfLabels {
  locale: string;
  transcript: string;
  summary: string;
  date: string;
  duration: string;
  /** Uses `{page}` and `{total}` placeholders. */
  pageOf: string;
  speaker: (speakerNumber: number) => string;
}

export type TranscriptPdfVariant = "full" | "summary";

const MONO_FONT = "Courier";
const LONG_PARAGRAPH_CHARACTERS = 1_200;
const PARAGRAPH_TARGET_CHARACTERS = 800;

function formatDocumentDate(value: string, locale: string, dateStyle: "long" | "medium" = "long"): string {
  const text = String(value || "").trim();
  if (!text) return "";
  try {
    const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
    if (dateOnly) {
      const date = new Date(Number(dateOnly[1]), Number(dateOnly[2]) - 1, Number(dateOnly[3]));
      return new Intl.DateTimeFormat(locale, { dateStyle }).format(date);
    }
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(text)) {
      const date = new Date(text);
      if (!Number.isNaN(date.getTime())) {
        return new Intl.DateTimeFormat(locale, { dateStyle, timeStyle: "short" }).format(date);
      }
    }
  } catch {
    // An unknown locale keeps the stored value.
  }
  return text;
}

/**
 * Break a transcript paragraph that runs for pages into reading-sized ones.
 * Only whitespace after sentence punctuation becomes a paragraph boundary, so
 * every word stays in order.
 */
export function readableParagraphs(text: string): string[] {
  if (text.length <= LONG_PARAGRAPH_CHARACTERS) return [text];
  const paragraphs: string[] = [];
  let current = "";
  for (const sentence of text.split(/(?<=[.!?…]["”»)]?)\s+/u)) {
    if (current && current.length + sentence.length + 1 > PARAGRAPH_TARGET_CHARACTERS) {
      paragraphs.push(current);
      current = sentence;
    } else {
      current = current ? `${current} ${sentence}` : sentence;
    }
  }
  if (current) paragraphs.push(current);
  return paragraphs;
}

function inlineStyle(inline: PdfInline): Style | null {
  const style: Style = {};
  if (inline.code) {
    style.fontFamily = MONO_FONT;
    style.fontSize = body.fontSize - 0.5;
  }
  if (inline.bold) style.fontWeight = 600;
  if (inline.italic && supportsItalic(inline.text)) style.fontStyle = "italic";
  return Object.keys(style).length ? style : null;
}

function inlineNodes(inlines: PdfInline[]): ReactNode[] {
  return inlines.map((inline, index) => {
    const text = normalizePdfText(inline.text);
    const style = inlineStyle(inline);
    // Runs must be the engine's own inline element: it flattens those into
    // one paragraph, while any other component would become a block.
    return style ? (
      <InlineRun key={index} style={style}>
        {text}
      </InlineRun>
    ) : (
      text
    );
  });
}

const bodyTextStyle: Style = {
  color: colors.foreground,
  fontFamily: body.fontFamily,
  fontSize: body.fontSize,
  lineHeight: body.lineHeight,
  marginBottom: theme.spacing.paragraphGap,
  minOrphanLines: 2,
  minWidowLines: 2,
};

const RichText = ({ inlines, style }: { inlines: PdfInline[]; style?: Style }) => (
  <PdfText style={[bodyTextStyle, style]}>{inlineNodes(inlines)}</PdfText>
);

const Eyebrow = ({ children }: { children: string }) => (
  <Text
    variant="xs"
    weight="semibold"
    color="accent"
    transform="uppercase"
    noMargin
    style={{ fontSize: 8.5, letterSpacing: 0.9, marginBottom: 6 }}
  >
    {children}
  </Text>
);

const SectionHeading = ({ children }: { children: string }) => (
  <View bookmark={children} style={{ marginTop: theme.spacing.sectionGap }}>
    <Heading level={2} noMargin keepWithNext style={{ marginBottom: 4 }}>
      {children}
    </Heading>
    <Divider spacing="none" color="border" style={{ marginBottom: theme.spacing.componentGap }} />
  </View>
);

/**
 * The engine has no keep-with-next rule, so a heading is set in one unbroken
 * group with the block that follows it. Callers only pass a block that is
 * short enough to move to the next page as a whole.
 */
const HeadedGroup = ({ heading, children }: { heading: ReactNode; children?: ReactNode }) => (
  <KeepTogether>
    {heading}
    {children}
  </KeepTogether>
);

const KEEP_PARAGRAPH_CHARACTERS = 1_400;
const KEEP_LIST_ITEMS = 6;
const KEEP_TABLE_ROWS = 8;

function keepsWithHeading(block: PdfBlock | undefined): boolean {
  if (!block) return false;
  switch (block.type) {
    case "paragraph":
      return inlinesToText(block.inlines).length <= KEEP_PARAGRAPH_CHARACTERS;
    case "list":
      return block.items.length <= KEEP_LIST_ITEMS && block.items.every((item) => item.children.length === 0);
    case "table":
      return block.head.length + block.rows.length <= KEEP_TABLE_ROWS;
    case "quote":
    case "definitions":
    case "code":
      return true;
    default:
      return false;
  }
}

const RichList = ({ list, depth = 0 }: { list: PdfListBlock; depth?: number }) => {
  const style: Style = {
    ...bodyTextStyle,
    marginBottom: depth ? 0 : theme.spacing.paragraphGap,
    marginLeft: depth ? 4 : 2,
  };
  const items = list.items.map((item, index) => (
    <ListItem key={index} style={{ marginBottom: 3 }}>
      <InlineRun>{inlineNodes(item.inlines)}</InlineRun>
      {item.children.map((child, childIndex) => (
        <RichList key={childIndex} list={child} depth={depth + 1} />
      ))}
    </ListItem>
  ));
  return list.ordered ? (
    <OrderedList style={style}>{items}</OrderedList>
  ) : (
    <UnorderedList marker={depth ? "circle" : "disc"} style={style}>
      {items}
    </UnorderedList>
  );
};

const SummaryTable = ({ block }: { block: Extract<PdfBlock, { type: "table" }> }) => (
  <View style={{ marginBottom: theme.spacing.componentGap, marginTop: 2 }}>
    <Table variant="line">
      {block.head.length > 0 && (
        <TableHeader>
          {block.head.map((row, rowIndex) => (
            <TableRow key={rowIndex} header>
              {row.map((cell, cellIndex) => (
                <TableCell key={cellIndex}>{normalizePdfText(inlinesToText(cell.inlines))}</TableCell>
              ))}
            </TableRow>
          ))}
        </TableHeader>
      )}
      <TableBody>
        {block.rows.map((row, rowIndex) => (
          <TableRow key={rowIndex}>
            {row.map((cell, cellIndex) => (
              <TableCell key={cellIndex}>
                {cell.header ? (
                  <RichText
                    inlines={cell.inlines.map((inline) => ({ ...inline, bold: true }))}
                    style={{ fontSize: body.fontSize - 1, marginBottom: 0 }}
                  />
                ) : (
                  <RichText inlines={cell.inlines} style={{ fontSize: body.fontSize - 1, marginBottom: 0 }} />
                )}
              </TableCell>
            ))}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  </View>
);

const SummaryBlock = ({ block }: { block: PdfBlock }) => {
  switch (block.type) {
    case "heading": {
      const text = normalizePdfText(inlinesToText(block.inlines));
      if (block.level === 2) return <SectionHeading>{text}</SectionHeading>;
      return (
        <Heading level={block.level} keepWithNext style={{ marginBottom: 4, marginTop: theme.spacing.componentGap }}>
          {text}
        </Heading>
      );
    }
    case "paragraph":
      return <RichText inlines={block.inlines} />;
    case "list":
      return <RichList list={block} />;
    case "quote":
      return (
        <Section variant="callout" spacing="sm" accentColor="accent">
          {block.paragraphs.map((inlines, index) => (
            <RichText
              key={index}
              inlines={inlines}
              style={{ color: colors.mutedForeground, marginBottom: index === block.paragraphs.length - 1 ? 0 : 4 }}
            />
          ))}
        </Section>
      );
    case "code":
      return (
        <PdfCard variant="muted" padding="sm">
          <PdfText style={{ color: colors.foreground, fontFamily: MONO_FONT, fontSize: 9, lineHeight: 1.45 }}>
            {normalizePdfText(block.text)}
          </PdfText>
        </PdfCard>
      );
    case "definitions":
      return (
        <KeyValue
          direction="vertical"
          size="md"
          noWrap
          items={block.items.map((item) => ({
            key: normalizePdfText(inlinesToText(item.term)),
            value: normalizePdfText(inlinesToText(item.description)),
          }))}
        />
      );
    case "table":
      return <SummaryTable block={block} />;
    case "rule":
      return <Divider spacing="md" color="border" />;
  }
};

const SummaryBody = ({ summary }: { summary: PdfSummary }) => {
  const nodes: ReactNode[] = [];
  const { blocks } = summary;
  for (let index = 0; index < blocks.length; index += 1) {
    const block = blocks[index];
    const next = blocks[index + 1];
    if (block.type === "heading" && keepsWithHeading(next)) {
      nodes.push(
        <HeadedGroup key={index} heading={<SummaryBlock block={block} />}>
          <SummaryBlock block={next} />
        </HeadedGroup>,
      );
      index += 1;
    } else {
      nodes.push(<SummaryBlock key={index} block={block} />);
    }
  }
  return <>{nodes}</>;
};

interface MetaEntry {
  key: string;
  value: string;
}

const MetaRow = ({ entries }: { entries: MetaEntry[] }) => {
  const visible = entries.filter((entry) => entry.value);
  if (!visible.length) return null;
  return (
    <View style={{ columnGap: 28, flexDirection: "row", flexWrap: "wrap", marginTop: 14, rowGap: 10 }}>
      {visible.map((entry) => (
        <View key={entry.key} style={{ flexShrink: 0 }}>
          <Text
            variant="xs"
            color="mutedForeground"
            transform="uppercase"
            noMargin
            style={{ fontSize: 7.5, letterSpacing: 0.7, marginBottom: 2 }}
          >
            {entry.key}
          </Text>
          <Text variant="xs" weight="medium" noMargin style={{ fontSize: 10 }}>
            {entry.value}
          </Text>
        </View>
      ))}
    </View>
  );
};

interface TitleBlockProps {
  eyebrow: string;
  title: string;
  standfirst?: PdfInline[];
  meta: MetaEntry[];
}

const TitleBlock = ({ eyebrow, title, standfirst, meta }: TitleBlockProps) => (
  <View style={{ marginBottom: 4 }}>
    <Eyebrow>{eyebrow}</Eyebrow>
    <Heading level={1} noMargin tracking="tight" style={{ lineHeight: 1.18 }}>
      {normalizePdfText(title)}
    </Heading>
    {standfirst && standfirst.length > 0 && (
      <RichText
        inlines={standfirst}
        style={{ color: colors.mutedForeground, fontSize: 12.5, lineHeight: 1.5, marginBottom: 0, marginTop: 10 }}
      />
    )}
    <MetaRow entries={meta} />
    <Divider spacing="none" color="foreground" thickness="thin" style={{ marginTop: 16 }} />
  </View>
);

interface DocumentFrameProps {
  title: string;
  kind: string;
  language: string;
  pageOf: string;
  footerNote: string;
  fonts: FontRegistration[];
  children: ReactNode;
}

const DocumentFrame = ({ title, kind, language, pageOf, footerNote, fonts, children }: DocumentFrameProps) => (
  <Document
    title={singleLinePdfText(title, 200)}
    author="Scriber"
    creator="Scriber"
    subject={kind}
    lang={language}
    fonts={fonts}
    style={{ color: colors.foreground, fontFamily: body.fontFamily, fontSize: body.fontSize }}
  >
    <Page
      size="A4"
      margin={{ bottom: page.marginBottom, left: page.marginLeft, right: page.marginRight, top: page.marginTop }}
    >
      <Fixed position="header">
        <View
          style={{
            alignItems: "center",
            borderBottomColor: colors.border,
            borderBottomWidth: 0.75,
            flexDirection: "row",
            justifyContent: "space-between",
            marginBottom: 22,
            paddingBottom: 7,
          }}
        >
          <Text
            variant="xs"
            weight="semibold"
            transform="uppercase"
            noMargin
            style={{ fontSize: 7.5, letterSpacing: 0.9 }}
          >
            {`Scriber · ${kind}`}
          </Text>
          <Text variant="xs" color="mutedForeground" noMargin style={{ fontSize: 7.5 }}>
            {singleLinePdfText(title, 72)}
          </Text>
        </View>
      </Fixed>
      <PageFooter
        fixed
        variant="simple"
        marginTop={18}
        leftText={
          <Text variant="xs" color="mutedForeground" noMargin style={{ fontSize: 7.5 }}>
            {footerNote}
          </Text>
        }
        rightText={
          <View style={{ width: 96 }}>
            <PageNumber format={pageOf} align="right" size="xs" style={{ fontSize: 7.5 }} />
          </View>
        }
        style={{ borderTopWidth: 0.75, paddingTop: 7 }}
      />
      {children}
    </Page>
  </Document>
);

function transcriptNodes(content: string, labels: TranscriptPdfLabels): ReactNode[] {
  const nodes: ReactNode[] = [];
  transcriptParagraphs(content).forEach((paragraph, index) => {
    const pieces = readableParagraphs(normalizePdfText(paragraph.text));
    pieces.forEach((piece, pieceIndex) => {
      const text = (
        <PdfText
          style={[
            bodyTextStyle,
            pieceIndex === pieces.length - 1 && paragraph.speakerNumber !== null ? { marginBottom: 12 } : null,
          ]}
        >
          {piece}
        </PdfText>
      );
      if (pieceIndex > 0 || paragraph.speakerNumber === null) {
        nodes.push(<View key={`${index}-${pieceIndex}`}>{text}</View>);
        return;
      }
      // A speaker label never stays behind at the bottom of a page.
      nodes.push(
        <HeadedGroup
          key={`${index}-${pieceIndex}`}
          heading={
            <Text
              variant="xs"
              weight="semibold"
              color="accent"
              noMargin
              style={{ fontSize: 8.5, letterSpacing: 0.3, marginBottom: 2 }}
            >
              {labels.speaker(paragraph.speakerNumber)}
            </Text>
          }
        >
          {text}
        </HeadedGroup>,
      );
    });
  });
  return nodes;
}

/** Keep a section heading on the same page as the first block below it. */
function headedSection(heading: string, nodes: ReactNode[]): ReactNode {
  if (!nodes.length) return null;
  return (
    <>
      <HeadedGroup heading={<SectionHeading>{heading}</SectionHeading>}>{nodes[0]}</HeadedGroup>
      {nodes.slice(1)}
    </>
  );
}

export interface TranscriptPdfProps {
  model: TranscriptExportDocument;
  labels: TranscriptPdfLabels;
  variant: TranscriptPdfVariant;
  fonts: FontRegistration[];
}

const TranscriptPdf = ({ model, labels, variant, fonts }: TranscriptPdfProps) => {
  const summary = parseSummaryForPdf(model.summary, model.summaryFormat);
  const hasSummary = summary.blocks.length > 0 || summary.standfirst.length > 0 || Boolean(summary.title);
  const summaryOnly = variant === "summary";
  const kind = summaryOnly ? labels.summary : labels.transcript;
  const date = formatDocumentDate(model.date, labels.locale);
  const meta = [
    { key: labels.date, value: date },
    { key: labels.duration, value: model.duration },
  ];
  // A summary-only document is titled by the summary itself; the recording's
  // title stays in the running header and footer.
  const summaryTitled = summaryOnly && Boolean(summary.title);
  const summaryLead: ReactNode[] = [];
  if (summary.title) {
    summaryLead.push(
      <Heading key="title" level={3} style={{ marginBottom: 4, marginTop: 0 }}>
        {normalizePdfText(summary.title)}
      </Heading>,
    );
  }
  if (summary.standfirst.length > 0) {
    summaryLead.push(
      <RichText
        key="standfirst"
        inlines={summary.standfirst}
        style={{ color: colors.mutedForeground, fontSize: 11.5 }}
      />,
    );
  }

  return (
    <DocumentFrame
      title={model.title}
      kind={kind}
      language={labels.locale}
      pageOf={labels.pageOf}
      footerNote={[singleLinePdfText(model.title, 60), date].filter(Boolean).join(" · ")}
      fonts={fonts}
    >
      <TitleBlock
        eyebrow={kind}
        title={summaryTitled ? summary.title : model.title}
        standfirst={summaryOnly ? summary.standfirst : undefined}
        meta={meta}
      />
      {hasSummary &&
        !summaryOnly &&
        headedSection(labels.summary, [
          <View key="lead">{summaryLead}</View>,
          <SummaryBody key="body" summary={demoteSummaryHeadings(summary)} />,
        ])}
      {summaryOnly && (
        <>
          {summary.blocks[0]?.type !== "heading" && <View style={{ height: 16 }} />}
          <SummaryBody summary={summary} />
        </>
      )}
      {!summaryOnly && headedSection(labels.transcript, transcriptNodes(model.content, labels))}
    </DocumentFrame>
  );
};

/**
 * Inside the full transcript document the summary sits under its own section
 * heading, so its headings move down one level to keep the outline nested.
 */
function demoteSummaryHeadings(summary: PdfSummary): PdfSummary {
  return {
    ...summary,
    blocks: summary.blocks.map((block) =>
      block.type === "heading" ? { ...block, level: Math.min(4, block.level + 1) as 3 | 4 } : block,
    ),
  };
}

const ACTION_STATUS_VARIANTS: Record<string, BadgeVariant> = {
  open: "outline",
  done: "success",
  dismissed: "default",
};

export interface MeetingPdfProps {
  model: MeetingExportDocument;
  fonts: FontRegistration[];
}

const bulletText = (
  item: MeetingExportDocument["sections"][number]["items"][number],
  labels: MeetingExportDocument["labels"],
) =>
  normalizePdfText(
    [item.text, item.owner && `${labels.owner}: ${item.owner}`, item.dueDate && `${labels.due}: ${item.dueDate}`]
      .filter(Boolean)
      .join(" · "),
  );

const ActionItemsTable = ({
  section,
  model,
}: {
  section: MeetingExportDocument["sections"][number];
  model: MeetingExportDocument;
}) => (
  <Table variant="line">
    <TableHeader>
      <TableRow header>
        <TableCell width="46%">{section.heading}</TableCell>
        <TableCell width="20%">{model.labels.owner}</TableCell>
        <TableCell width="17%">{model.labels.due}</TableCell>
        <TableCell width="17%">{model.labels.status}</TableCell>
      </TableRow>
    </TableHeader>
    <TableBody>
      {section.items.map((item, index) => (
        <TableRow key={index}>
          <TableCell width="46%">{normalizePdfText(item.text)}</TableCell>
          <TableCell width="20%">{normalizePdfText(item.owner) || "–"}</TableCell>
          <TableCell width="17%">{formatDocumentDate(item.dueDate, model.language, "medium") || "–"}</TableCell>
          <TableCell width="17%">
            <View style={{ alignItems: "flex-start" }}>
              <Badge variant={ACTION_STATUS_VARIANTS[item.statusKey] ?? "outline"} size="sm" label={item.status} />
            </View>
          </TableCell>
        </TableRow>
      ))}
    </TableBody>
  </Table>
);

const MeetingPdf = ({ model, fonts }: MeetingPdfProps) => {
  const { labels } = model;
  const date = formatDocumentDate(model.date, model.language);
  const meta = [
    { key: labels.date, value: date || labels.unknown },
    { key: labels.duration, value: model.duration },
    { key: labels.transcript, value: `${model.segmentCount} ${labels.timestamped_segments}` },
    { key: labels.timing_quality, value: model.timingQuality },
  ];
  const summaryParagraphs = model.executiveSummary
    .split(/\n{2,}/)
    .map((paragraph) => paragraph.trim())
    .filter(Boolean);

  return (
    <DocumentFrame
      title={model.title}
      kind={labels.meeting}
      language={model.language}
      pageOf={labels.page_of}
      footerNote={[singleLinePdfText(model.title, 60), date].filter(Boolean).join(" · ")}
      fonts={fonts}
    >
      <TitleBlock eyebrow={labels.meeting} title={model.title} meta={meta} />

      {summaryParagraphs.length > 0 &&
        headedSection(labels.executive_summary, [
          <Section key="summary" variant="highlight" spacing="none" accentColor="accent" style={{ padding: 14 }}>
            {summaryParagraphs.map((paragraph, index) => (
              <PdfText
                key={index}
                style={[bodyTextStyle, { marginBottom: index === summaryParagraphs.length - 1 ? 0 : 6 }]}
              >
                {normalizePdfText(paragraph)}
              </PdfText>
            ))}
          </Section>,
        ])}

      {model.sections.map((section) => {
        const heading = <SectionHeading>{section.heading}</SectionHeading>;
        if (section.key === "action_items") {
          const table = <ActionItemsTable section={section} model={model} />;
          return section.items.length <= KEEP_TABLE_ROWS ? (
            <HeadedGroup key={section.key} heading={heading}>
              {table}
            </HeadedGroup>
          ) : (
            <View key={section.key}>
              {heading}
              {table}
            </View>
          );
        }
        const lead = section.items.slice(0, KEEP_LIST_ITEMS);
        const rest = section.items.slice(KEEP_LIST_ITEMS);
        return (
          <View key={section.key}>
            <HeadedGroup heading={heading}>
              <PdfList variant="bullet" gap="sm" items={lead.map((item) => ({ text: bulletText(item, labels) }))} />
            </HeadedGroup>
            {rest.length > 0 && (
              <PdfList variant="bullet" gap="sm" items={rest.map((item) => ({ text: bulletText(item, labels) }))} />
            )}
          </View>
        );
      })}

      {headedSection(
        labels.notes,
        model.notes.map((note, index) => (
          <KeepTogether key={index} style={{ flexDirection: "row", gap: 12, marginBottom: 6 }}>
            <Text variant="xs" color="mutedForeground" noMargin style={{ fontSize: 9, marginTop: 1.5, width: 44 }}>
              {note.at}
            </Text>
            <PdfText style={[bodyTextStyle, { flex: 1, marginBottom: 0 }]}>{normalizePdfText(note.body)}</PdfText>
          </KeepTogether>
        )),
      )}

      {headedSection(
        labels.timestamped_transcript,
        model.segments.map((segment, index) => (
          <View key={index} style={{ flexDirection: "row", gap: 12, marginBottom: 9 }}>
            <View style={{ width: 62 }}>
              <Text variant="xs" color="mutedForeground" noMargin style={{ fontSize: 8.5, marginTop: 1.5 }}>
                {`${segment.start} – ${segment.end}`}
              </Text>
            </View>
            <View style={{ flex: 1 }}>
              <Text variant="xs" weight="semibold" color="accent" noMargin style={{ fontSize: 8.5, marginBottom: 1.5 }}>
                {segment.estimated
                  ? `${normalizePdfText(segment.speaker)} · ${labels.estimated_timing}`
                  : normalizePdfText(segment.speaker)}
              </Text>
              <PdfText style={[bodyTextStyle, { marginBottom: 0 }]}>{normalizePdfText(segment.text)}</PdfText>
            </View>
          </View>
        )),
      )}
    </DocumentFrame>
  );
};

/** The transcript export, or only its summary, as a themed pdfcn document. */
export const transcriptPdfElement = (props: TranscriptPdfProps) => (
  <PdfcnThemeProvider theme={theme}>
    <TranscriptPdf {...props} />
  </PdfcnThemeProvider>
);

/** The Meeting export as a themed pdfcn document. */
export const meetingPdfElement = (props: MeetingPdfProps) => (
  <PdfcnThemeProvider theme={theme}>
    <MeetingPdf {...props} />
  </PdfcnThemeProvider>
);
