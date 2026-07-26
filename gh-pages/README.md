# PostgreSQL Logical Replication Guide (GitHub Pages site)

An interactive, static site that walks a user through setting up PostgreSQL logical
replication for a chosen scenario: pick a **source**, **target**, **initial-sync method**,
and **scope**, and get a tailored runbook with prerequisites, setup commands, monitoring,
cutover, troubleshooting, and flow diagrams.

It is a **manual-setup companion** to `pgreplkit` and does not depend on it.

## Run locally

It is pure static HTML/CSS/JS — no build step. Serve the folder over HTTP (the guide page
uses `fetch()` to load content fragments, which does not work over `file://`):

```bash
cd gh-pages
python3 -m http.server 8099
# open http://localhost:8099/
```

## Deploy on GitHub Pages

1. Push this repo to GitHub.
2. Settings → Pages → Build and deployment → **Deploy from a branch**.
3. Choose the branch and set the folder to **`/gh-pages`** (or move these files to `/docs`
   and select `/docs`).
4. The included **`.nojekyll`** file is required — it stops GitHub's Jekyll from ignoring the
   `content/_shared/` directory (Jekyll drops paths beginning with `_`).

## How it is organised

```
gh-pages/
  index.html              landing page + scenario builder
  guide.html              renders a scenario's runbook
  css/style.css           all styling
  js/
    scenarios.js          REGISTRY: engines, sync methods, validity rules, versions  (the data model)
    app.js                landing page: dropdowns, validation, routing
    guide-renderer.js     builds guide.html from a content fragment + shared includes
    diagrams.js           reusable Mermaid flowchart templates
    clipboard.js          copy-to-clipboard buttons
  content/
    <source>-to-<target>.html   one thin file per scenario (intro + which methods apply)
    bidirectional.html          the active-active (origin = none) scenario
    _shared/
      prerequisites-common.html
      setup-replication-copy.html
      setup-pg-dump.html
      setup-rds-snapshot.html
      setup-aurora-clone.html
      monitoring.html
      cutover.html
      troubleshooting.html
```

The SQL/command content lives **once** in `content/_shared/setup-*.html`; each scenario file
just composes the applicable methods. `guide.html` assembles the page: it reads the selection
from the URL, validates it against `scenarios.js`, fetches the scenario fragment, inlines the
`_shared` includes it references, builds the version/prerequisite table from the registry, adds
a table of contents, renders the diagrams, and wires the copy buttons.

## Extending it

Everything is driven by the registry in `js/scenarios.js` — no engine list is hard-coded in
the pages. To add support for something new:

- **New source or target engine** — add an entry to `sources` / `targets` in `scenarios.js`
  (id, label, supported `pgVersions`, how to enable logical WAL, privileges, any egress note,
  and a `docUrl` for its official documentation used in the References section), then create
  `content/<source>-to-<target>.html` (copy an existing thin scenario file).
- **New initial-sync method** — add an entry to `syncMethods` with an `appliesWhen(ctx)` guard,
  create `content/_shared/setup-<method>.html`, and reference it from the relevant scenario
  files with `<div data-sync="<method>"><div data-include="setup-<method>"></div></div>`.
- **New validity rule** — add a small function to `RULES` in `scenarios.js`; it is applied
  automatically.
- **New diagram shape** — add a key to `DIAGRAMS` in `js/diagrams.js` and reference it with
  `<div data-diagram="<key>"></div>`.

Content-file conventions:
- Mark the introduction section with `data-role="intro"` so the renderer places it above the
  auto-generated version table.
- Wrap method-specific sections in `<div data-sync="<method>">…</div>`; the renderer keeps only
  the block matching the selected sync method.
- Pull in a shared block with `<div data-include="<name>"></div>` (loads
  `content/_shared/<name>.html`).

## A note on accuracy

The platform-independent SQL in the shared setup fragments (publication/subscription creation,
the exported-snapshot pg_dump seed, `origin = none` bi-directional, sequence sync, and the
monitoring queries) was verified against a live PostgreSQL 16 pair. The RDS/Aurora physical-seed
steps (`rds_tools.logical_seed_lsn()`, `aurora_volume_logical_start_lsn()`,
`pg_replication_origin_advance`) mirror procedures verified in the repository's playbooks.
Always rehearse against non-production data before a real migration.
