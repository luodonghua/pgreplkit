/* ==========================================================================
   app.js — Landing-page controller.

   Responsibilities:
     - Populate the four dropdowns from REGISTRY (no hard-coded engine lists).
     - Grey-out invalid sync methods live as source/target change.
     - Validate the combination and show the reason when invalid.
     - Render the "Featured Scenarios" grid (matrix + special scenarios).
     - Route to guide.html with the selection encoded in the query string.

   Because every list is read from REGISTRY, adding a new engine/method there
   makes it appear here automatically.
   ========================================================================== */

(function () {
  "use strict";

  const R = window.REGISTRY;

  const el = {
    source: document.getElementById("source-select"),
    target: document.getElementById("target-select"),
    sync: document.getElementById("sync-select"),
    scope: document.getElementById("scope-select"),
    msg: document.getElementById("validation-message"),
    methodHint: document.getElementById("method-hint"),
    generate: document.getElementById("generate-btn"),
    featured: document.getElementById("featured-grid")
  };

  /* ---------- Populate dropdowns ---------- */
  function opt(value, label, disabled) {
    const o = document.createElement("option");
    o.value = value;
    o.textContent = label;
    if (disabled) o.disabled = true;
    return o;
  }

  function fillStatic() {
    el.source.appendChild(opt("", "-- Select source --"));
    R.sources.forEach((s) => el.source.appendChild(opt(s.id, s.label)));

    el.target.appendChild(opt("", "-- Select target --"));
    R.targets.forEach((t) => el.target.appendChild(opt(t.id, t.label)));

    el.scope.appendChild(opt("", "-- Select scope --"));
    R.scopes.forEach((sc) => el.scope.appendChild(opt(sc.id, sc.label)));

    refreshSyncOptions();
  }

  /* ---------- Sync-method options depend on source/target ---------- */
  function refreshSyncOptions() {
    const ctx = current();
    const prev = el.sync.value;
    el.sync.innerHTML = "";
    el.sync.appendChild(opt("", "-- Select method --"));

    R.availableSyncMethods(ctx).forEach((m) => {
      const label = m.enabled ? m.label : m.label + "  (not valid for this pair)";
      el.sync.appendChild(opt(m.id, label, !m.enabled));
    });

    // Keep previous selection if still valid.
    if (prev && !el.sync.querySelector(`option[value="${prev}"]`).disabled) {
      el.sync.value = prev;
    } else if (prev) {
      el.sync.value = "";
    }
  }

  function current() {
    return {
      source: el.source.value,
      target: el.target.value,
      sync: el.sync.value,
      scope: el.scope.value
    };
  }

  /* ---------- Validation + messaging ---------- */
  function showMsg(cls, html) {
    el.msg.className = "validation-message " + cls;
    el.msg.innerHTML = html;
    el.msg.classList.remove("hidden");
  }
  function hideMsg() { el.msg.classList.add("hidden"); }

  function showMethodHint() {
    const m = R.getSyncMethod(el.sync.value);
    if (!m) { el.methodHint.classList.add("hidden"); return; }
    el.methodHint.innerHTML =
      `<span class="callout-title">${m.label}</span>${m.blurb}`;
    el.methodHint.classList.remove("hidden");
  }

  function validate() {
    const ctx = current();
    showMethodHint();

    const res = R.isValidCombination(ctx);

    if (res.incomplete) {
      hideMsg();
      el.generate.disabled = true;
      return;
    }
    if (!res.valid) {
      showMsg("error", `<strong>Not a valid combination.</strong> ${res.reason}`);
      el.generate.disabled = true;
      return;
    }

    // Valid so far. Scope is optional for enabling but recommended.
    const s = R.getSource(ctx.source), t = R.getTarget(ctx.target);
    const minPg = R.requiredPgVersion(ctx);
    let extra = "";
    if (ctx.scope) {
      const sc = R.getScope(ctx.scope);
      if (sc && sc.minVersion > 10) {
        extra = ` This scope prefers PostgreSQL ${sc.minVersion}+ (a fallback is shown for older versions).`;
      }
    }
    showMsg("ok",
      `<strong>Valid.</strong> ${s.short} &rarr; ${t.short}, ` +
      `minimum PostgreSQL <strong>${minPg}</strong> on both sides.${extra}`);
    el.generate.disabled = false;
  }

  /* ---------- Routing ---------- */
  function buildUrl(ctx, special) {
    const p = new URLSearchParams();
    if (special) {
      p.set("scenario", special);
    } else {
      p.set("source", ctx.source);
      p.set("target", ctx.target);
      p.set("sync", ctx.sync);
      if (ctx.scope) p.set("scope", ctx.scope);
    }
    return "guide.html?" + p.toString();
  }

  function go() {
    const ctx = current();
    const res = R.isValidCombination(ctx);
    if (!res.valid) return;
    window.location.href = buildUrl(ctx, null);
  }

  /* ---------- Featured scenarios grid ---------- */
  function featuredCard(title, desc, tags, url) {
    const a = document.createElement("a");
    a.className = "scenario-preview-card";
    a.href = url;
    const tagRow = tags.map((t) => `<span class="tag">${t}</span>`).join("");
    a.innerHTML =
      `<h3>${title}</h3><p>${desc}</p><div class="tag-row">${tagRow}</div>`;
    return a;
  }

  function renderFeatured() {
    const items = [];

    // A curated set of common matrix cells.
    const curated = [
      { source: "self-managed", target: "rds-pg", sync: "replication-copy" },
      { source: "rds-pg", target: "rds-pg", sync: "rds-snapshot" },
      { source: "aurora-pg", target: "aurora-pg", sync: "aurora-clone" },
      { source: "cloudsql", target: "aurora-pg", sync: "replication-copy" },
      { source: "alloydb", target: "rds-pg", sync: "pg-dump" },
      { source: "azure-pg", target: "rds-pg", sync: "replication-copy" }
    ];

    curated.forEach((c) => {
      const s = R.getSource(c.source), t = R.getTarget(c.target), m = R.getSyncMethod(c.sync);
      if (!s || !t || !m) return;
      items.push(featuredCard(
        `${s.short} &rarr; ${t.short}`,
        `Initial sync via ${m.label.replace(/\s*\(.*\)/, "")}.`,
        [s.platform, m.short],
        buildUrl(c, null)
      ));
    });

    // Special scenarios (bi-directional, etc.)
    R.specialScenarios.forEach((sp) => {
      items.push(featuredCard(sp.label, sp.description, sp.tags, buildUrl(null, sp.id)));
    });

    items.forEach((node) => el.featured.appendChild(node));
  }

  /* ---------- Wire up ---------- */
  function init() {
    fillStatic();
    renderFeatured();

    el.source.addEventListener("change", () => { refreshSyncOptions(); validate(); });
    el.target.addEventListener("change", () => { refreshSyncOptions(); validate(); });
    el.sync.addEventListener("change", validate);
    el.scope.addEventListener("change", validate);
    el.generate.addEventListener("click", go);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
