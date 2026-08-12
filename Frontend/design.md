# Design — Scriber

A locked design system for Scriber’s main WebView. Every page redesign reads this file before emitting code. Extend or amend this file when the system needs to grow; do not invent a page-local visual language.

## Product context

- Audience: Windows knowledge workers who need fast, dependable transcription.
- Primary jobs: start a recording or import, understand progress, retrieve and work with the result.
- Voice: precise, calm, technical. Prefer short labels and concrete verbs.
- Scope: the main application WebView. The native recording overlay, tray panel, boot shell, icons, and backend are outside this redesign.

## Genre

Modern-minimal. Scriber should read as a quiet desktop instrument, not a marketing site and not a soft-neumorphic toy.

## Macrostructure family

- Marketing pages: not currently in scope. If introduced, use a compact Long Document with no hero enrichment by default.
- App pages: **Workbench**. A compact work header precedes one dominant task surface; histories and secondary controls sit below or beside it according to the task.
- Content pages: **Long Document**. Transcript detail uses a reading column, a persistent utility toolbar, and low-chrome disclosure sections.

## Theme

The canonical values live in [`tokens.css`](./tokens.css). Its runtime selectors are scoped to the main WebView so the native recording overlay and tray retain their established contracts. Cool neutrals keep the existing Scriber identity; cobalt is the only brand accent and stays below five percent of a viewport.

### Light

- `--color-paper` `oklch(97% 0.009 255)`
- `--color-paper-2` `oklch(94% 0.012 255)`
- `--color-paper-3` `oklch(91% 0.014 255)`
- `--color-ink` `oklch(18% 0.02 257)`
- `--color-ink-2` `oklch(30% 0.023 255)`
- `--color-rule` `oklch(84% 0.014 255)`
- `--color-rule-2` `oklch(72% 0.018 255)`
- `--color-muted` `oklch(48% 0.018 255)`
- `--color-accent` `oklch(72% 0.18 257)`
- `--color-accent-ink` `oklch(18% 0.02 257)`
- `--color-focus` `oklch(48% 0.22 257)`

### Dark

- `--color-paper` `oklch(16% 0.018 255)`
- `--color-paper-2` `oklch(19.5% 0.019 255)`
- `--color-paper-3` `oklch(24% 0.02 255)`
- `--color-ink` `oklch(94.5% 0.012 255)`
- `--color-ink-2` `oklch(84% 0.012 255)`
- `--color-rule` `oklch(31% 0.02 255)`
- `--color-rule-2` `oklch(40% 0.02 255)`
- `--color-muted` `oklch(68% 0.014 255)`
- `--color-accent` `oklch(72% 0.15 257)`
- `--color-accent-ink` `oklch(16% 0.018 255)`
- `--color-focus` `oklch(80% 0.16 257)`

Use elevation through surface lightness and a one-pixel rule. `--shadow-whisper` is the sole normal panel shadow; `--shadow-popover` is reserved for floating overlays. Do not restore directional neumorphic shadow pairs.

## Typography

- Display: Switzer through the main-WebView-only `Scriber Switzer` alias, weight 700, roman. Weight 600 is allowed for compact brand and section labels.
- Body: Inter through the main-WebView-only `Scriber Inter` alias, weight 400. Weight 500 is reserved for controls and emphasized metadata.
- Mono: the native UI monospace stack, weight 400, only for console output, timestamps, shortcuts, and machine identifiers.
- Display tracking: `-0.035em`.
- Type scale anchor: `--text-display: clamp(2.15rem, 1.72rem + 1.2vw, 3.05rem)`.
- Page titles are compact (`--text-2xl` to `--text-display`), left-aligned, roman, and never gradient-filled or italicized.
- Body copy uses a maximum measure of 65 characters where reading, not scanning, is the job.
- Numeric status and timing data use tabular figures.

Inter is retained as the body face because it is already bundled, locally served, and approved for this established desktop UI. It is never used as the display or wordmark face.

## Spacing

Use the four-point named scale in `tokens.css`. New authored CSS uses `var(--space-*)`; component markup may use equivalent Tailwind utilities when they map exactly to the same rhythm.

- Page shell: retain the shared 1320 px `app-page-shell` contract and `data-page-shell` hook.
- Work header: tight top rhythm, one bottom rule, no outer card.
- Task surface: one containment layer. Do not put a bordered card inside another bordered card merely to create depth.
- Dense settings may group fields with rules and whitespace; every group does not need its own raised panel.
- Controls have a 44 px touch floor. Adjacent inputs and buttons share height.

## Motion

- Enter: `--ease-out`; exit: `--ease-in`; state toggle: `--ease-in-out`.
- Page and keyboard navigation are immediate.
- Buttons use one small press translation; cards do not universally lift or scale.
- Menus, sheets, and popovers use opacity plus a small transform only. Exits are faster than entrances.
- Preserve the existing theme reveal because it communicates a global theme change.
- Reduced motion collapses spatial motion to opacity at 150 ms or less. Functional progress indicators remain.
- Never use `transition: all`, bounce/overshoot curves, parallax, or decorative infinite motion.

## Microinteractions stance

- Silent success when the result is already visible.
- Copy actions swap their label/check state; they do not raise a toast.
- Failures name the failed action and provide the next step.
- Focus rings appear instantly and remain visible on every interactive element.
- Hover behavior is gated to fine pointers and always has focus/tap parity.
- Tooltip delay: 800–1000 ms on hover, 0 ms on keyboard focus.
- Clickable labels remain on one line; their containing row reflows first.
- Loading uses known-shape skeletons for histories and inline progress for buttons.

## CTA voice

- Primary CTA: compact cobalt fill, dark `--color-accent-ink`, 8 px radius, one-line verb-led label such as “Start recording” or “Choose file”.
- Secondary CTA: paper surface with a one-pixel rule and ink text. No drop shadow unless it floats above content.
- Destructive CTA: danger rule/text plus a written consequence; no color-only signal.
- Icon-only actions require an accessible name and a minimum 44 × 44 px target.

## Navigation

- Desktop: a calm utility rail. The active route is marked by a narrow cobalt line, stronger ink, and a subtle paper step—not a large raised pill.
- Mobile: retain the existing sheet and compact header.
- Keep command search, locale, theme, and console utilities visually secondary to the five primary tasks.
- Navigation changes are immediate. Labels never wrap.

## Page headers

- Use one compact `PageIntro` pattern: optional contextual label in sentence case, title, description, and actions.
- No all-caps eyebrow, decorative leading rule, oversized boxed intro, or sticky shadow stack.
- Context, title, and description stay in one vertical reading column at every viewport; actions may align to the trailing edge when space permits.
- A page’s title and the task surface below it align to the same left edge.

## Per-page allowances

- Live Mic: the recording control remains the single visual focal point; history is subordinate.
- Meetings: active capture and device readiness may use semantic status tokens in addition to cobalt.
- YouTube and File: input/upload surfaces may use a dashed rule when it communicates drop-target affordance.
- Settings: denser section navigation is allowed, but field groups still use the shared surface and control language.
- Debug Console: monospace is allowed only inside output, timestamps, shortcuts, and identifiers.
- Transcript Detail: content pages may use wider reading leading and a sticky utility toolbar; body copy stays visually dominant.
- Not Found: one direct recovery action; no illustration or marketing copy.
- App pages must not use decorative enrichment. Function carries the page.

## What pages MUST share

- Feather identity and Switzer wordmark.
- The same light/dark palette, cobalt accent placement, font pair, radii, rules, and focus treatment.
- The compact work-header rhythm.
- Button height, radius, and verb-led copy.
- One-level panel containment and tonal elevation.
- Responsive behavior at 320, 375, 414, and 768 px with no horizontal scrolling.

## What pages MAY differ on

- Workbench composition: task-first stack, task/history split, or settings navigation/content split.
- Density according to the job.
- Semantic status colors when status is real and paired with text or an icon.
- Sticky utilities where they preserve access during long work.

## Exports

These portable formats mirror `tokens.css`. The application keeps its legacy HSL compatibility variables while the Hallmark layer migrates authored surfaces to these role tokens.

### `tokens.css`

```css
:root {
  --color-paper: oklch(97% 0.009 255);
  --color-paper-2: oklch(94% 0.012 255);
  --color-paper-3: oklch(91% 0.014 255);
  --color-rule: oklch(84% 0.014 255);
  --color-rule-2: oklch(72% 0.018 255);
  --color-muted: oklch(48% 0.018 255);
  --color-neutral: oklch(38% 0.02 255);
  --color-ink-2: oklch(30% 0.023 255);
  --color-ink: oklch(18% 0.02 257);
  --color-accent: oklch(72% 0.18 257);
  --color-accent-ink: oklch(18% 0.02 257);
  --color-focus: oklch(48% 0.22 257);

  --font-display: "Scriber Switzer", "Switzer", ui-sans-serif, system-ui, sans-serif;
  --font-body: "Scriber Inter", "Inter", ui-sans-serif, system-ui, sans-serif;
  --font-outlier: ui-monospace, "Cascadia Code", "SFMono-Regular", monospace;

  --space-3xs: 0.125rem;
  --space-2xs: 0.25rem;
  --space-xs: 0.5rem;
  --space-sm: 0.75rem;
  --space-md: 1rem;
  --space-lg: 1.5rem;
  --space-xl: 2.5rem;
  --space-2xl: 4rem;
  --space-3xl: 6rem;
  --space-4xl: 9rem;

  --text-xs: 0.75rem;
  --text-sm: 0.875rem;
  --text-base: 1rem;
  --text-md: 1.125rem;
  --text-lg: 1.25rem;
  --text-xl: 1.5625rem;
  --text-2xl: 1.9531rem;
  --text-display: clamp(2.15rem, 1.72rem + 1.2vw, 3.05rem);

  --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
  --ease-in: cubic-bezier(0.7, 0, 0.84, 0);
  --ease-in-out: cubic-bezier(0.65, 0, 0.35, 1);
  --dur-micro: 100ms;
  --dur-short: 180ms;
  --dur-medium: 280ms;
  --dur-long: 420ms;

  --radius-card: 0.75rem;
  --radius-pill: 999px;
  --radius-input: 0.5rem;
}
```

### Tailwind v4 `@theme`

```css
@theme {
  --color-paper: oklch(97% 0.009 255);
  --color-paper-2: oklch(94% 0.012 255);
  --color-paper-3: oklch(91% 0.014 255);
  --color-rule: oklch(84% 0.014 255);
  --color-rule-2: oklch(72% 0.018 255);
  --color-muted: oklch(48% 0.018 255);
  --color-neutral: oklch(38% 0.02 255);
  --color-ink-2: oklch(30% 0.023 255);
  --color-ink: oklch(18% 0.02 257);
  --color-accent: oklch(72% 0.18 257);
  --color-accent-ink: oklch(18% 0.02 257);
  --color-focus: oklch(48% 0.22 257);

  --font-display: "Scriber Switzer", "Switzer", ui-sans-serif, system-ui, sans-serif;
  --font-body: "Scriber Inter", "Inter", ui-sans-serif, system-ui, sans-serif;
  --font-outlier: ui-monospace, "Cascadia Code", monospace;

  --spacing-3xs: 0.125rem;
  --spacing-2xs: 0.25rem;
  --spacing-xs: 0.5rem;
  --spacing-sm: 0.75rem;
  --spacing-md: 1rem;
  --spacing-lg: 1.5rem;
  --spacing-xl: 2.5rem;
  --spacing-2xl: 4rem;

  --text-xs: 0.75rem;
  --text-sm: 0.875rem;
  --text-base: 1rem;
  --text-md: 1.125rem;
  --text-lg: 1.25rem;
  --text-xl: 1.5625rem;
  --text-2xl: 1.9531rem;

  --radius-card: 0.75rem;
  --radius-pill: 999px;
  --radius-input: 0.5rem;
  --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
  --ease-in: cubic-bezier(0.7, 0, 0.84, 0);
  --ease-in-out: cubic-bezier(0.65, 0, 0.35, 1);
}
```

### DTCG `tokens.json`

```json
{
  "$schema": "https://design-tokens.github.io/community-group/format/",
  "color": {
    "paper": { "$value": "oklch(97% 0.009 255)", "$type": "color" },
    "paper-2": { "$value": "oklch(94% 0.012 255)", "$type": "color" },
    "paper-3": { "$value": "oklch(91% 0.014 255)", "$type": "color" },
    "rule": { "$value": "oklch(84% 0.014 255)", "$type": "color" },
    "rule-2": { "$value": "oklch(72% 0.018 255)", "$type": "color" },
    "muted": { "$value": "oklch(48% 0.018 255)", "$type": "color" },
    "neutral": { "$value": "oklch(38% 0.02 255)", "$type": "color" },
    "ink-2": { "$value": "oklch(30% 0.023 255)", "$type": "color" },
    "ink": { "$value": "oklch(18% 0.02 257)", "$type": "color" },
    "accent": { "$value": "oklch(72% 0.18 257)", "$type": "color" },
    "accent-ink": { "$value": "oklch(18% 0.02 257)", "$type": "color" },
    "focus": { "$value": "oklch(48% 0.22 257)", "$type": "color" }
  },
  "font": {
    "display": { "$value": "Scriber Switzer, Switzer, ui-sans-serif, system-ui, sans-serif", "$type": "fontFamily" },
    "body": { "$value": "Scriber Inter, Inter, ui-sans-serif, system-ui, sans-serif", "$type": "fontFamily" },
    "outlier": { "$value": "ui-monospace, Cascadia Code, SFMono-Regular, monospace", "$type": "fontFamily" }
  },
  "space": {
    "3xs": { "$value": "0.125rem", "$type": "dimension" },
    "2xs": { "$value": "0.25rem", "$type": "dimension" },
    "xs": { "$value": "0.5rem", "$type": "dimension" },
    "sm": { "$value": "0.75rem", "$type": "dimension" },
    "md": { "$value": "1rem", "$type": "dimension" },
    "lg": { "$value": "1.5rem", "$type": "dimension" },
    "xl": { "$value": "2.5rem", "$type": "dimension" },
    "2xl": { "$value": "4rem", "$type": "dimension" }
  },
  "duration": {
    "micro": { "$value": "100ms", "$type": "duration" },
    "short": { "$value": "180ms", "$type": "duration" },
    "medium": { "$value": "280ms", "$type": "duration" },
    "long": { "$value": "420ms", "$type": "duration" }
  }
}
```

### shadcn/ui CSS variables

```css
:root {
  --background: 97% 0.009 255;
  --foreground: 18% 0.02 257;
  --card: 94% 0.012 255;
  --card-foreground: 18% 0.02 257;
  --popover: 97% 0.009 255;
  --popover-foreground: 18% 0.02 257;
  --primary: 72% 0.18 257;
  --primary-foreground: 18% 0.02 257;
  --secondary: 91% 0.014 255;
  --secondary-foreground: 30% 0.023 255;
  --muted: 91% 0.014 255;
  --muted-foreground: 48% 0.018 255;
  --accent: 92% 0.035 257;
  --accent-foreground: 18% 0.02 257;
  --destructive: 52% 0.18 25;
  --destructive-foreground: 97% 0.009 255;
  --border: 84% 0.014 255;
  --input: 84% 0.014 255;
  --ring: 48% 0.22 257;
  --radius: 0.75rem;
}

.dark {
  --background: 16% 0.018 255;
  --foreground: 94.5% 0.012 255;
  --card: 19.5% 0.019 255;
  --card-foreground: 94.5% 0.012 255;
  --popover: 19.5% 0.019 255;
  --popover-foreground: 94.5% 0.012 255;
  --primary: 72% 0.15 257;
  --primary-foreground: 16% 0.018 255;
  --secondary: 24% 0.02 255;
  --secondary-foreground: 84% 0.012 255;
  --muted: 24% 0.02 255;
  --muted-foreground: 68% 0.014 255;
  --accent: 25% 0.045 257;
  --accent-foreground: 94.5% 0.012 255;
  --destructive: 72% 0.15 25;
  --destructive-foreground: 16% 0.018 255;
  --border: 31% 0.02 255;
  --input: 31% 0.02 255;
  --ring: 80% 0.16 257;
}
```
