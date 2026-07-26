/* ==========================================================================
   guide-renderer.js — Assembles guide.html from a content fragment + shared
   includes, then decorates it (header, TOC, diagrams, copy buttons).

   URL forms:
     guide.html?source=<id>&target=<id>&sync=<id>&scope=<id>
     guide.html?scenario=<specialId>          (e.g. bidirectional)

   Content fragment location:  content/<contentKey>.html
   Shared includes:            content/_shared/<name>.html
     A fragment can pull a shared block with:
        <div data-include="monitoring"></div>
        <div data-include="troubleshooting"></div>
        <div data-include="prerequisites-common"></div>
   ========================================================================== */

(function () {
  "use strict";

  const R = window.REGISTRY;
  const root = document.getElementById("guide-root");

  /* ---------- URL parsing ---------- */
  function parseParams() {
    const p = new URLSearchParams(window.location.search);
    if (p.get("scenario")) {
      return { special: p.get("scenario") };
    }
    return {
      source: p.get("source"),
      target: p.get("target"),
      sync: p.get("sync"),
      scope: p.get("scope") || ""
    };
  }

  function fail(title, msg) {
    root.innerHTML =
      `<div class="guide-section">
         <h2>${title}</h2>
         <p>${msg}</p>
         <a class="btn-secondary" href="index.html">Back to scenario builder</a>
       </div>`;
  }

  /* ---------- Fetch helpers ---------- */
  async function fetchText(url) {
    const res = await fetch(url, { cache: "no-cache" });
    if (!res.ok) throw new Error(`${url} -> ${res.status}`);
    return res.text();
  }

  async function resolveIncludes(container) {
    const nodes = Array.from(container.querySelectorAll("[data-include]"));
    await Promise.all(nodes.map(async (node) => {
      const name = node.getAttribute("data-include");
      try {
        const html = await fetchText(`content/_shared/${name}.html`);
        const tmp = document.createElement("div");
        tmp.innerHTML = html;
        node.replaceWith(...tmp.childNodes);
      } catch (e) {
        node.innerHTML = `<div class="callout warn">Shared section "${name}" could not be loaded.</div>`;
      }
    }));
  }

  /* ---------- Header + meta ---------- */
  function buildHeader(ctx) {
    const wrap = document.createElement("div");
    wrap.className = "guide-header";

    if (ctx.special) {
      const sp = R.specialScenarios.find((s) => s.id === ctx.special);
      const tags = (sp ? sp.tags : []).map((t) => `<span class="tag">${t}</span>`).join("");
      wrap.innerHTML =
        `<h2>${sp ? sp.label : ctx.special}</h2>
         <p>${sp ? sp.description : ""}</p>
         <div class="guide-meta">${tags}</div>`;
      document.title = (sp ? sp.label : "Guide") + " — Replication Guide";
      return wrap;
    }

    const s = R.getSource(ctx.source);
    const t = R.getTarget(ctx.target);
    const m = R.getSyncMethod(ctx.sync);
    const sc = ctx.scope ? R.getScope(ctx.scope) : null;
    const minPg = R.requiredPgVersion(ctx);

    const tags = [
      `<span class="tag">${s.platform}</span>`,
      `<span class="tag">${t.platform}</span>`,
      `<span class="tag">sync: ${m.short}</span>`,
      sc ? `<span class="tag">scope: ${sc.short}</span>` : "",
      `<span class="tag">PostgreSQL ${minPg}+</span>`
    ].join("");

    wrap.innerHTML =
      `<h2>${s.short} &rarr; ${t.short}</h2>
       <p>Initial data sync via <strong>${m.label}</strong>${sc ? `, replicating <strong>${sc.short}</strong>` : ""}.</p>
       <div class="guide-meta">${tags}</div>`;
    document.title = `${s.short} → ${t.short} — Replication Guide`;
    return wrap;
  }

  /* ---------- Version prerequisites box (data-driven from REGISTRY) ---------- */
  function buildVersionBox(ctx) {
    if (ctx.special) return null; // specials embed their own version note
    const s = R.getSource(ctx.source);
    const t = R.getTarget(ctx.target);
    const m = R.getSyncMethod(ctx.sync);
    const sc = ctx.scope ? R.getScope(ctx.scope) : null;
    const minPg = R.requiredPgVersion(ctx);

    const rows = [
      ["Source engine", `${s.label} (supports PG ${s.pgVersions.min}\u2013${s.pgVersions.max})`],
      ["Target engine", `${t.label} (supports PG ${t.pgVersions.min}\u2013${t.pgVersions.max})`]
    ];

    // Minimum PG row — explain WHY when a scope/feature raises it above the core 10.
    if (minPg <= 10) {
      rows.push(["Minimum PostgreSQL",
        `<strong>10</strong> \u2014 the logical replication core (publications/subscriptions) exists since PG 10, so any supported version works.`]);
    } else {
      const sc = ctx.scope ? R.getScope(ctx.scope) : null;
      const why = (sc && sc.id === "schemas")
        ? `only because you chose <strong>Entire schemas</strong>, which uses <code>FOR TABLES IN SCHEMA</code> (added in PG ${minPg}). The logical replication core itself is PG 10+ \u2014 pick <strong>Specific tables</strong> instead to run on PG 10\u2013${minPg - 1}.`
        : `required by a selected feature added in PG ${minPg}. The logical replication core itself is PG 10+.`;
      rows.push(["Minimum PostgreSQL", `<strong>${minPg}</strong> on both sides \u2014 ${why}`]);
    }

    rows.push(
      ["Enable logical WAL on source", s.logicalWal.detail],
      ["Source privileges", s.privileges],
      ["Target (subscriber) privileges", t.subscriberPrivileges]
    );
    if (sc && sc.minVersion > 10) {
      rows.push(["Scope note", `${sc.blurb}`]);
    }
    if (!m.versionIndependent) {
      rows.push(["Same major version required", `The ${m.label} seed is physical, so source and target must be the <strong>same</strong> PostgreSQL major version. For cross-version, use Replication Copy or pg_dump.`]);
    } else {
      rows.push(["Cross-version", `This sync method is version-independent \u2014 it streams across different major versions (e.g. 16 \u2192 17).`]);
    }
    if (s.egressNote) rows.push(["Network (cross-cloud)", s.egressNote]);

    const section = document.createElement("div");
    section.className = "guide-section";
    section.id = "versions";
    section.innerHTML =
      `<h2>Supported versions &amp; prerequisites</h2>
       <p>This guide applies to the versions and settings below. Confirm each before you start.</p>
       <table class="data-table"><tbody>${
         rows.map(([k, v]) => `<tr><th style="width:32%">${k}</th><td>${v}</td></tr>`).join("")
       }</tbody></table>`;
    return section;
  }

  /* ---------- References (official sources) ---------- */
  function buildReferences(ctx) {
    const refs = R.referencesFor(ctx);
    if (!refs || !refs.length) return null;
    const section = document.createElement("div");
    section.className = "guide-section";
    section.id = "references";
    const items = refs.map((d) =>
      `<li><a href="${d.url}" target="_blank" rel="noopener">${d.label}</a></li>`).join("");
    section.innerHTML =
      `<h2>References (official sources)</h2>
       <p>Cross-check anything in this guide against the authoritative documentation:</p>
       <ul>${items}</ul>`;
    return section;
  }

  /* ---------- Table of contents from h2 ---------- */
  function buildTOC(container) {
    const headings = Array.from(container.querySelectorAll(".guide-section > h2"));
    if (headings.length < 2) return null;
    const toc = document.createElement("details");
    toc.className = "toc";
    toc.open = false; // collapsed by default so it stays out of the way
    const items = headings.map((h, i) => {
      if (!h.parentElement.id) h.parentElement.id = "sec-" + i;
      return `<li><a href="#${h.parentElement.id}">${h.textContent}</a></li>`;
    }).join("");
    toc.innerHTML = `<summary><h3>On this page</h3></summary><ol>${items}</ol>`;
    return toc;
  }

  /* ---------- Main ---------- */
  async function render() {
    const ctx = parseParams();

    // Determine content key + validate.
    let contentKey;
    if (ctx.special) {
      const sp = R.specialScenarios.find((s) => s.id === ctx.special);
      if (!sp) return fail("Unknown scenario", `No scenario named "${ctx.special}".`);
      contentKey = sp.contentKey;
    } else {
      if (!ctx.source || !ctx.target || !ctx.sync) {
        return fail("Incomplete selection",
          "Missing source, target, or sync method. Please start from the builder.");
      }
      const res = R.isValidCombination(ctx);
      if (!res.valid) {
        return fail("Not a valid combination",
          (res.reason || "This combination is not supported.") +
          ' <br><br><a class="btn-secondary" href="index.html">Choose again</a>');
      }
      contentKey = R.contentKeyFor(ctx);
    }

    // Fetch the scenario fragment.
    let fragmentHtml;
    try {
      fragmentHtml = await fetchText(`content/${contentKey}.html`);
    } catch (e) {
      return fail("Guide not available yet",
        `The content for <code>${contentKey}</code> hasn't been published yet. ` +
        `<br><br><a class="btn-secondary" href="index.html">Back to builder</a>`);
    }

    // Assemble.
    const assembled = document.createElement("div");
    assembled.innerHTML = fragmentHtml;

    // Filter method-specific blocks BEFORE resolving includes so we only fetch
    // the setup fragment for the chosen sync method.
    //   <div data-sync="replication-copy"> ... </div>
    // A block is kept if it matches ctx.sync (or if no sync is set, e.g. specials).
    if (!ctx.special && ctx.sync) {
      assembled.querySelectorAll("[data-sync]").forEach((node) => {
        const want = node.getAttribute("data-sync").split(/\s+/);
        if (!want.includes(ctx.sync)) node.remove();
      });
    }

    await resolveIncludes(assembled);

    // Compose final DOM: header, version box, TOC, then fragment sections.
    root.innerHTML = "";
    root.appendChild(buildHeader(ctx));
    const vbox = buildVersionBox(ctx);

    // Insert version box as the first section (after any intro the fragment
    // marks with data-role="intro"), otherwise at the top of the body.
    const intro = assembled.querySelector('[data-role="intro"]');
    if (intro) {
      // keep intro first, then version box after it
      root.appendChild(intro);
    }
    if (vbox) root.appendChild(vbox);

    // Append the rest of the fragment.
    Array.from(assembled.childNodes).forEach((n) => root.appendChild(n));

    // References section (official sources) as the final section.
    const refs = buildReferences(ctx);
    if (refs) root.appendChild(refs);

    // TOC (built from the fully assembled sections) inserted right after header.
    const toc = buildTOC(root);
    if (toc) root.querySelector(".guide-header").after(toc);

    // Decorate.
    if (window.DiagramUtil) window.DiagramUtil.renderAll(root);
    if (window.ClipboardUtil) window.ClipboardUtil.enhanceAll(root);
  }

  document.addEventListener("DOMContentLoaded", () => {
    const printLink = document.getElementById("print-link");
    if (printLink) printLink.addEventListener("click", (e) => { e.preventDefault(); window.print(); });
    render().catch((e) => fail("Something went wrong", String(e)));
  });
})();
