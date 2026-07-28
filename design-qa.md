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

---

# Design QA — Native recording overlay pill

## Source

- Rest reference:
  `docs/screenshots/overlay-energy-wave-reference-rest-v0.5.50.png`
- Hover reference:
  `docs/screenshots/overlay-energy-wave-reference-hover-v0.5.50.png`
- Reference state: energy-wave visualizer active, full-width wave beginning at
  the far-left pill edge, stop control hidden at rest and visible on hover.

## Implementation

- Native rest screenshot:
  `docs/screenshots/overlay-energy-wave-native-rest-white-v0.5.50.png`
- Native hover screenshot:
  `docs/screenshots/overlay-energy-wave-native-hover-white-v0.5.50.png`
- Native runtime: release-mode Tauri/WebView2 overlay, Windows display scaling
  130%, 203 × 41 CSS px pill, captured on a pure-white desktop surface.
- Rest comparison:
  `docs/screenshots/overlay-energy-wave-comparison-rest-v0.5.50.png`
- Hover comparison:
  `docs/screenshots/overlay-energy-wave-comparison-hover-v0.5.50.png`
- Classic-bars control screenshot:
  `docs/screenshots/overlay-bars-native-rest-white-v0.5.50.png`
- Classic-bars hover screenshot:
  `docs/screenshots/overlay-bars-native-hover-white-v0.5.50.png`

## Combined visual comparison

The source and native screenshots were normalized to the same visible pill
dimensions and inspected side by side on white. The implementation preserves
the reference's capsule silhouette, full-width midnight-blue shading, left-edge
wave origin, fine gold strands, and hover-only stop control. The native
screenshot uses a quiet RMS sample, so the wave is nearly flat; the active
multi-strand amplitude and 60 Hz movement remain covered by the browser
reference, component tests, and renderer timing contract.

The white background makes the native alpha boundary explicit. All four
screenshot corners are pure white. The dark pill contracts continuously from
293 physical pixels at its center to 249 pixels at its top edge, rather than
forming an opaque rectangular canvas layer. The shadow also narrows
continuously below the capsule and fades to white, confirming that it follows
the pill silhouette instead of a box.

## Settings variant check

The committed Settings selector exposes exactly two variants: `Bars` and
`Energy wave`. The native control run applied `bars` through the same persisted
settings endpoint used by the selector, relaunched the release-mode overlay,
and captured both pointer states on white. It retained the classic black
capsule and blue micro-bars, including its always-visible stop control, while
sharing the corrected rounded alpha boundary and pill-shaped shadow. A separate
run applied `energy_wave` and produced the source-matched screenshots above.
The common wrapper therefore remains stable, while the energy-only Canvas fix
does not alter classic bar geometry or animation.

## Iteration history

1. The installed v0.5.49 overlay exposed WebView2's low-latency Canvas surface
   as an opaque black 203 × 41 px rectangle, masking the CSS gradient and
   rounded corners.
2. The renderer moved back to the standard alpha-composited 2D Canvas path and
   now paints and clips its own full-pill background before drawing the
   allocation-bounded strands.
3. A hybrid touch-and-mouse Windows media-query conflict was removed so a fine
   pointer reliably keeps the stop control hidden until pill hover or keyboard
   focus.
4. The drop shadow moved to an isolated, static rounded sibling behind the
   clipped pill. Native white-background captures verified its capsule-shaped
   spread and soft fade without per-frame filters or layout work.

Final result: passed locally in the native release-mode WebView. The published
installer is rechecked with the same white-background capture before release
acceptance.
