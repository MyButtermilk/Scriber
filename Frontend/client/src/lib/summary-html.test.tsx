import { describe, expect, it } from "vitest";

import { prepareSummaryHtml } from "./summary-html";

describe("prepareSummaryHtml editorial structure", () => {
  it("replaces model-supplied classes with app-owned overview and compact takeaway hooks", () => {
    const prepared = prepareSummaryHtml(`
      <section class="model-card">
        <h2>Project status</h2>
        <p>The delivery plan is ready for review.</p>
        <ul class="forced-grid">
          <li><strong>Scope:</strong> The first release stays focused.</li>
          <li><strong>Timing:</strong> Review starts on Thursday.</li>
          <li><strong>Owner:</strong> The product team coordinates delivery.</li>
          <li><strong>Risk:</strong> One external dependency remains.</li>
        </ul>
      </section>
      <section><h2>Background</h2><p>Supporting context follows.</p></section>
    `);

    const document = new DOMParser().parseFromString(prepared.html, "text/html");
    const overview = document.querySelector(":scope section:first-of-type");
    const takeaways = overview?.querySelector(":scope > ul");

    expect(overview?.classList.contains("summary-overview")).toBe(true);
    expect(overview?.classList.contains("model-card")).toBe(false);
    expect(takeaways?.classList.contains("summary-snapshot")).toBe(true);
    expect(takeaways?.classList.contains("summary-snapshot--takeaways")).toBe(true);
    expect(takeaways?.classList.contains("is-compact")).toBe(true);
    expect(takeaways?.classList.contains("forced-grid")).toBe(false);
  });

  it("keeps verbose or nested takeaways on one reading track", () => {
    const verbose =
      "A detailed takeaway needs a full line because it explains dependencies, trade-offs, and the next review step without reducing the content to a label.";
    const prepared = prepareSummaryHtml(`
      <section>
        <h2>Project status</h2>
        <p>The delivery plan is ready for review.</p>
        <ul>
          <li>${verbose}</li>
          <li>${verbose}</li>
          <li>${verbose}</li>
          <li>${verbose}<ul><li>Nested evidence</li></ul></li>
        </ul>
      </section>
      <section><h2>Background</h2><p>Supporting context follows.</p></section>
    `);

    const document = new DOMParser().parseFromString(prepared.html, "text/html");
    const takeaways = document.querySelector(".summary-snapshot--takeaways");

    expect(takeaways?.classList.contains("is-compact")).toBe(false);
  });

  it("marks genuine fact-value snapshots separately from takeaway lists", () => {
    const prepared = prepareSummaryHtml(`
      <section>
        <h2>Release brief</h2>
        <p>The release has two confirmed facts.</p>
        <dl><dt>Target</dt><dd>September</dd><dt>Owner</dt><dd>Platform team</dd></dl>
      </section>
      <section><h2>Background</h2><p>Supporting context follows.</p></section>
    `);

    const document = new DOMParser().parseFromString(prepared.html, "text/html");

    expect(document.querySelector(".summary-snapshot--facts")?.classList.contains("summary-snapshot")).toBe(true);
    expect(document.querySelector(".summary-snapshot--takeaways")).toBeNull();
  });

  it("styles only the optional snapshot immediately after the standfirst", () => {
    const prepared = prepareSummaryHtml(`
      <section>
        <h2>Release brief</h2>
        <p>The release has a compact opening snapshot.</p>
        <ul><li>First point</li><li>Second point</li></ul>
        <p>The overview continues with explanatory prose.</p>
        <ul><li>A normal supporting list</li><li>Another supporting item</li></ul>
      </section>
      <section><h2>Background</h2><p>Supporting context follows.</p></section>
    `);

    const document = new DOMParser().parseFromString(prepared.html, "text/html");
    const lists = document.querySelectorAll(".summary-overview > ul");

    expect(lists).toHaveLength(2);
    expect(lists[0].classList.contains("summary-snapshot--takeaways")).toBe(true);
    expect(lists[1].classList.length).toBe(0);
  });
});
