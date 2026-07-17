# Design QA — HTML summary contents navigation

## Source

- Live reference: https://www.asterlab.ai/research/scaling_autonomous_research_to_thousands_of_agents#parallelizing-simple-program-search
- Full reference screenshot: `C:\Users\Alexander.Immler\tmp\aster-toc-reference.webp`
- Focused reference screenshot: `C:\Users\Alexander.Immler\tmp\aster-toc-focused.webp`
- Reference viewport/state: 1440 × 1000, `Parallelizing simple program search` active.

## Implementation

- Local route: `http://127.0.0.1:5055/transcript/html-summary-qa`
- Full implementation screenshot: `C:\Users\Alexander.Immler\tmp\scriber-summary-qa-desktop-final.webp`
- Focused implementation screenshot: `C:\Users\Alexander.Immler\tmp\scriber-toc-focused.webp`
- Mobile implementation screenshot: `C:\Users\Alexander.Immler\tmp\scriber-summary-qa-mobile.webp`
- Implementation viewport/state: 1440 × 1000, summary expanded, `Kurzueberblick` active, main scroll position 0.

## Full-frame comparison

The Asterlab reference and Scriber implementation were inspected together in one comparison input at the same 1440 × 1000 viewport. The measured structure matches the reference pattern: a 230 px contents rail, 40 px column gap, 720 px reading column, and balanced right gutter. Scriber preserves its existing dark theme, title bar, and accordion container; these are intentional product-context differences rather than source mismatches. No cropped content, broken alignment, accidental horizontal overflow, or inconsistent active marker was found.

## Focused comparison

The two contents rails were inspected together in a second comparison input. Both use an uppercase 0.72 rem eyebrow, a continuous 1 px left rule, a full-height 1 px active marker, 14 px primary indentation, 28 px nested indentation, muted inactive links, and stronger active text. The implementation keeps the same hierarchy and density while using Scriber’s existing typography and color tokens.

## Iteration history

1. Initial implementation used a 990 px two-column shell, an inset pseudo-element for the active marker, and duplicated top spacing inside sections.
2. The layout was corrected to the measured 1260 px `230 / 40 / 720 / 40 / 230` geometry, the active state was changed to the source-style border with `margin-left: -1px`, and section-first-child spacing was removed.
3. Final full-frame and focused comparisons found no remaining priority 0, 1, or 2 visual mismatch.

## Chrome interactions and runtime evidence

- Chrome DevTools MCP 1.6.0, isolated headless Chrome, usage statistics and CrUX disabled.
- Clicking `Fazit` updated the URL hash to `#summary-fazit`, scrolled to the bottom, and set `aria-current` to `Fazit` while the contents rail stayed sticky.
- Clicking `Sicherheitsmodell` updated the hash and active state at the normal mid-document scroll position.
- Eleven headings produced eleven unique app-owned IDs, including the duplicate-title suffix `summary-stabile-anker-2`.
- At 1024 × 900 and 390 × 844 the contents rail was hidden and document/main horizontal overflow remained zero; wide tables stayed internally scrollable.
- An injected script, image, JavaScript URL, model ID/class/style, and event attributes were all absent from the rendered summary; the XSS sentinel remained unset.
- The clean reload produced no Chrome console warnings or errors. Only the Vite connection debug messages and React development info message remained.

Final result: passed
