import assert from "node:assert/strict";
import test from "node:test";
import {
  cycleMatchIndex,
  findTranscriptMatches,
  foldSearchText,
  genericSpeakerNumber,
  parseTranscriptContent,
  speakerColorIndex,
  type TranscriptBlock,
} from "./transcript-content";

function textOf(blocks: TranscriptBlock[]): string[] {
  return blocks.map((block) => block.text);
}

test("canonical diarized lines become speaker turns with their start time", () => {
  const blocks = parseTranscriptContent(
    [
      "[0:00] Speaker 0: Hello and welcome.",
      "[0:04] Speaker 0: Today we talk about tides.",
      "[0:09] Speaker 1: Thanks for having me.",
      "[0:12] Speaker 0: Let us begin.",
    ].join("\n"),
  );
  assert.deepEqual(blocks, [
    {
      startLabel: "0:00",
      startSeconds: 0,
      speaker: "Speaker 0",
      text: "Hello and welcome. Today we talk about tides.",
    },
    { startLabel: "0:09", startSeconds: 9, speaker: "Speaker 1", text: "Thanks for having me." },
    { startLabel: "0:12", startSeconds: 12, speaker: "Speaker 0", text: "Let us begin." },
  ]);
});

test("canonical timestamps beyond one hour and h:mm:ss labels are understood", () => {
  const blocks = parseTranscriptContent("[125:04] Speaker 1: Late remark.\n[1:02:03] Alice: Another one.");
  assert.equal(blocks.length, 2);
  assert.equal(blocks[0].startLabel, "2:05:04");
  assert.equal(blocks[0].startSeconds, 125 * 60 + 4);
  assert.equal(blocks[0].speaker, "Speaker 1");
  assert.equal(blocks[1].startLabel, "1:02:03");
  assert.equal(blocks[1].startSeconds, 3723);
  assert.equal(blocks[1].speaker, "Alice");
  assert.equal(blocks[1].text, "Another one.");
});

test("named speakers are accepted when the whole transcript is diarized", () => {
  const blocks = parseTranscriptContent("[0:01] Alice Meyer: Hi.\n[0:03] Bob: Hallo.\n[0:05] Alice Meyer: Weiter.");
  assert.deepEqual(
    blocks.map((block) => block.speaker),
    ["Alice Meyer", "Bob", "Alice Meyer"],
  );
});

test("timestamp-only lines keep a sentence colon as text", () => {
  const blocks = parseTranscriptContent(
    "[0:12] Here is the thing: tides are slow.\n[0:20] They follow the moon.\n[0:31] The ratio is 3:1 today.",
  );
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].speaker, undefined);
  assert.equal(blocks[0].startLabel, "0:12");
  assert.equal(blocks[0].text, "Here is the thing: tides are slow. They follow the moon. The ratio is 3:1 today.");
});

test("long single-speaker turns are split into readable timed paragraphs without losing words", () => {
  const lines = Array.from(
    { length: 60 },
    (_, index) => `[${index}:00] Speaker 0: Sentence ${index} carries a little more meaning.`,
  );
  const blocks = parseTranscriptContent(lines.join("\n"));
  assert.ok(blocks.length > 1);
  assert.ok(blocks.every((block) => block.speaker === "Speaker 0" && block.startLabel));
  assert.equal(blocks[0].continued, undefined);
  assert.ok(blocks.slice(1).every((block) => block.continued === true));
  const words = blocks.map((block) => block.text).join(" ");
  for (let index = 0; index < 60; index += 1) assert.ok(words.includes(`Sentence ${index} carries`));
});

test("Speaker 0 never selects an undefined color", () => {
  const palette = ["a", "b", "c", "d", "e", "f"];
  for (const speaker of ["Speaker 0", "Speaker 1", "Speaker 7", "Alice", "", undefined, -3, 0, Number.NaN]) {
    const index = speakerColorIndex(speaker, palette.length);
    assert.ok(index >= 0 && index < palette.length, `bad index for ${String(speaker)}`);
    assert.notEqual(palette[index], undefined);
  }
  assert.equal(speakerColorIndex("Speaker 0", 6), 0);
  assert.equal(speakerColorIndex("Speaker 7", 6), 1);
  assert.equal(speakerColorIndex(-1, 6), 5);
  assert.equal(speakerColorIndex("Alice", 6), speakerColorIndex("alice", 6));
  assert.equal(speakerColorIndex("Speaker 1", 0), 0);
  assert.equal(genericSpeakerNumber("Speaker 0"), 0);
  assert.equal(genericSpeakerNumber("Alice"), null);
});

test("legacy [Speaker N]: paragraphs keep their speakers", () => {
  const blocks = parseTranscriptContent("[Speaker 1]: Hallo zusammen.\n\n[Speaker 2]: Guten Tag.\n\nOhne Sprecher");
  assert.deepEqual(blocks, [
    { speaker: "Speaker 1", text: "Hallo zusammen." },
    { speaker: "Speaker 2", text: "Guten Tag." },
    { text: "Ohne Sprecher" },
  ]);
  assert.deepEqual(parseTranscriptContent("[Speaker x]: bleibt Text"), [{ text: "[Speaker x]: bleibt Text" }]);
});

test("plain text keeps explicit line breaks and paragraphs", () => {
  const blocks = parseTranscriptContent("Erste Zeile\nZweite Zeile\n\n\nNeuer Absatz\n   eingerückt");
  assert.deepEqual(textOf(blocks), ["Erste Zeile\nZweite Zeile", "Neuer Absatz\n   eingerückt"]);
  assert.ok(blocks.every((block) => block.speaker === undefined && block.startLabel === undefined));
});

test("CRLF line endings parse like LF", () => {
  assert.deepEqual(
    parseTranscriptContent("[0:01] Speaker 0: Eins.\r\n[0:02] Speaker 1: Zwei."),
    parseTranscriptContent("[0:01] Speaker 0: Eins.\n[0:02] Speaker 1: Zwei."),
  );
  assert.deepEqual(textOf(parseTranscriptContent("A\r\nB\r\n\r\nC")), ["A\nB", "C"]);
});

test("empty input yields no blocks", () => {
  assert.deepEqual(parseTranscriptContent(""), []);
  assert.deepEqual(parseTranscriptContent("  \n\n \r\n"), []);
  assert.deepEqual(parseTranscriptContent(undefined as unknown as string), []);
});

test("untimed lines inside a canonical transcript stay on their own line", () => {
  const blocks = parseTranscriptContent(
    "Intro line\n[0:05] Speaker 0: Timed.\ncontinued note\n\n[0:09] Speaker 0: Next.",
  );
  assert.deepEqual(textOf(blocks), ["Intro line", "Timed.\ncontinued note", "Next."]);
});

test("search folds case, diacritics, and ß", () => {
  assert.equal(foldSearchText("Größe ÜBER Straße"), "grosse uber strasse");
  const blocks: TranscriptBlock[] = [
    { text: "Über die Brücke und über den Fluss." },
    { text: "Die Straße ist breit." },
  ];
  assert.deepEqual(findTranscriptMatches(blocks, "uber"), [
    { blockIndex: 0, start: 0, end: 4 },
    { blockIndex: 0, start: 20, end: 24 },
  ]);
  assert.deepEqual(findTranscriptMatches(blocks, "BRÜCKE"), [{ blockIndex: 0, start: 9, end: 15 }]);
  assert.deepEqual(findTranscriptMatches(blocks, "strasse"), [{ blockIndex: 1, start: 4, end: 10 }]);
  assert.equal(blocks[1].text.slice(4, 10), "Straße");
});

test("search maps decomposed text back to original offsets", () => {
  const decomposed = "Müller sagt hallo";
  const [match] = findTranscriptMatches([{ text: decomposed }], "müller");
  assert.deepEqual(match, { blockIndex: 0, start: 0, end: 7 });
  assert.equal(decomposed.slice(match.start, match.end), "Müller");
});

test("search matches never overlap and an empty query finds nothing", () => {
  assert.deepEqual(findTranscriptMatches([{ text: "aaaa" }], "aa"), [
    { blockIndex: 0, start: 0, end: 2 },
    { blockIndex: 0, start: 2, end: 4 },
  ]);
  assert.deepEqual(findTranscriptMatches([{ text: "abc" }], "   "), []);
  assert.deepEqual(findTranscriptMatches([{ text: "abc" }], "zzz"), []);
});

test("match cycling wraps around in both directions", () => {
  assert.equal(cycleMatchIndex(0, 3, 1), 1);
  assert.equal(cycleMatchIndex(2, 3, 1), 0);
  assert.equal(cycleMatchIndex(0, 3, -1), 2);
  assert.equal(cycleMatchIndex(-1, 3, 1), 0);
  assert.equal(cycleMatchIndex(-1, 3, -1), 2);
  assert.equal(cycleMatchIndex(0, 0, 1), -1);
});

test("mid-sentence timed chunk boundaries are repaired and split at logical discourse transitions", () => {
  const transcript = [
    "[0:09] All right. It's true. The last video you watched on this channel is the last video you will ever see from that space, which is bittersweet, for sure... But this video is the first one ever shot in this space, and that's very exciting. This, frankly, is... pretty insane. This is the biggest project I've ever done. It is this whole building. It is two stories. It's about 30,000 square feet, and it's going to take about the next year or so from the time of... shooting this... to build it all up. Now, here's the thing. I'm not an architect, I'm not a interior designer, I'm not a professional floor planner, any of that stuff... All I know is exactly what I want the dream video studio to",
    "[0:57] look and feel and work like. And so I found the space and... now we're going to do it. Now, it's funny, most of the advice that I've gotten has been basically not to do this... And that's understandable. Like, it's already inherently risky to empty the piggy bank into a construction project. So I get it. But also, like, anytime it's a big investment like you see this in cars, too, people always talk about, like, building it for the person you're going to sell it to later when you're done. Like, think about making it sellable. Think about the next guy. But I've kind of just... thrown all of that away... just for the love of the game.",
    "[1:37] Because I know that the video production space that I want this to be, this doesn't exist in any other form. Like, this is going to be a brand new thing. So, yeah, not really thinking all about who else is going to buy it after me. This is just... going to be the best possible space for what it's designed for. It actually took more than a year to find this space, and I guess there's about a year of work probably left ahead of us for construction... Now what's fun for you... is this being recorded... in secret a year ago from when you're watching it. And the next video you get to watch is the finished studio, which is sick. So this is going to be an empty building tour,",
    "[2:08] and we will walk through every single room together today.",
  ].join("\n");

  const blocks = parseTranscriptContent(transcript);
  assert.ok(blocks.length >= 4, `expected logical paragraphs, got ${blocks.length}`);

  // No sentence is cut in half across block boundaries.
  for (const block of blocks) {
    assert.ok(
      /[.!?…]["'”’»)\]]*$/.test(block.text),
      `block ended mid-sentence: ${JSON.stringify(block.text.slice(-50))}`,
    );
  }
  const combined = blocks.map((block) => block.text).join(" ");
  assert.ok(combined.includes("dream video studio to look and feel and work like."));
  assert.ok(combined.includes("empty building tour, and we will walk through every single room together today."));
  // Natural discourse transitions open their own paragraphs.
  assert.ok(blocks.some((block) => block.text.startsWith("Now, here's the thing.")));
  assert.ok(blocks.some((block) => block.text.startsWith("Now, it's funny,")));
  assert.ok(blocks.some((block) => block.text.startsWith("Now what's fun for you")));
});

test("German abbreviations, dates, and mid-sentence ellipses do not trigger false breaks", () => {
  const transcript = [
    "[0:00] Dr. Müller erklärte am 26.11.2026 z. B. die Regelung bzw. die Ausnahme für ca. 3.5 Mio. Euro. Danach besprachen wir die Planung für das neue Quartal und die offenen Fristen im Team.",
    "[0:18] Erstens müssen wir die Verträge prüfen, denn d. h. alle Fristen laufen bald ab. Zweitens planen wir den nächsten Schritt mit Prof. Weber am 12. Mai im Büro.",
  ].join("\n");

  const blocks = parseTranscriptContent(transcript);
  assert.ok(blocks.length >= 2);
  assert.ok(blocks[0].text.includes("Dr. Müller erklärte am 26.11.2026 z. B."));
  assert.ok(blocks.some((block) => block.text.startsWith("Erstens müssen wir")));
});

test("long unpunctuated STT run-ons split cleanly on clause boundaries without losing words", () => {
  const words = Array.from({ length: 140 }, (_, i) => (i % 12 === 11 ? `wort${i},` : `wort${i}`));
  const blocks = parseTranscriptContent(`[0:00] ${words.join(" ")}`);
  assert.ok(blocks.length >= 2);
  assert.equal(blocks.map((block) => block.text).join(" "), words.join(" "));
});

test("lexical topic shift (TextTiling) breaks a long speaker turn even without transition words", () => {
  const cooking = [
    "[0:00] Speaker 0: The sourdough starter needs warm water and fresh rye flour every morning.",
    "[0:08] Speaker 0: Fermentation builds gluten structure inside the dough before baking in the cast iron pot.",
    "[0:16] Speaker 0: A crispy crust forms when steam traps moisture around the loaf during oven spring.",
  ];
  const astronomy = [
    "[0:26] Speaker 0: Radio telescopes capture faint pulsar signals across distant galactic nebulae.",
    "[0:34] Speaker 0: Orbital mirrors calibrate infrared spectroscopy for exoplanet atmospheres and cosmic dust.",
    "[0:42] Speaker 0: Deep space probes transmit telemetry back to parabolic antennas on Earth.",
  ];
  const blocks = parseTranscriptContent([...cooking, ...astronomy].join("\n"));
  assert.equal(blocks.length, 2);
  assert.equal(blocks[0].continued, undefined);
  assert.equal(blocks[1].continued, true);
  assert.ok(blocks[0].text.includes("sourdough starter"));
  assert.ok(blocks[1].text.startsWith("Radio telescopes capture"));
});
